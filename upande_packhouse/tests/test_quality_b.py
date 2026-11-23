from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse import quality_b as qb

# Quality B orders (order type Shopping Cart): a local sale of Grade B flowers. They get their own delivery point,
# need none of the export fields, are sold by the stem, and their pick lists are shared out among the Quality B
# teams. The database is mocked.


def _line(item, qty, uom=None, stock_uom="Stems", factor=1, stock_qty=None):
	line = frappe._dict(
		item_code=item,
		qty=qty,
		uom=uom,
		stock_uom=stock_uom,
		conversion_factor=factor,
		stock_qty=stock_qty,
		custom_mixed_box=0,
		custom_packrate=None,
		custom_number_of_boxes=None,
	)
	return line


def _order(lines, order_type="Shopping Cart", **kw):
	doc = MagicMock()
	data = {
		"order_type": order_type,
		"customer": "Webstore Test Buyer",
		"customer_name": "Webstore Test Buyer",
		"business_unit": None,
		"custom_business_unit": None,
		"custom_delivery_point": None,
		"custom_order_name": None,
		**kw,
	}
	doc.get = lambda k, d=None: data.get(k, d)
	doc.set = lambda k, v: data.__setitem__(k, v)
	doc.items = lines
	doc.customer = data["customer"]
	doc.customer_name = data["customer_name"]
	doc.meta.has_field = lambda f: True
	doc.data = data

	def setattr_(name, value):
		data[name] = value

	type(doc).custom_delivery_point = property(
		lambda s: data["custom_delivery_point"], lambda s, v: setattr_("custom_delivery_point", v)
	)
	type(doc).custom_order_name = property(
		lambda s: data["custom_order_name"], lambda s, v: setattr_("custom_order_name", v)
	)
	return doc


class UnitTestQualityBOrders(UnitTestCase):
	def _validate(self, doc):
		with (
			patch.object(qb.frappe.db, "exists", return_value=True),
			patch.object(qb.frappe.db, "get_value", return_value=None),
		):
			qb.before_validate(doc)

	def test_a_quality_b_order_gets_its_delivery_point_name_and_business_unit(self):
		doc = _order([_line("Moonwalk", 30)])
		self._validate(doc)
		self.assertEqual(doc.data["custom_delivery_point"], qb.DELIVERY_POINT)
		self.assertEqual(doc.data["custom_order_name"], "QB Webstore Test Buyer")
		self.assertEqual(doc.data["business_unit"], "Roses")

	def test_an_export_order_is_left_alone(self):
		doc = _order([_line("Moonwalk", 30, uom="Bunch (10)", factor=10)], order_type="Sales")
		self._validate(doc)
		self.assertIsNone(doc.data["custom_delivery_point"])
		self.assertEqual(doc.items[0].uom, "Bunch (10)")

	def test_lines_are_sold_by_the_stem_and_packed_whole(self):
		webstore = _line("Moonwalk", 30)  # made by the webstore: no unit yet, the quantity is stems
		bunched = _line("Fuchsiana", 2, uom="Bunch (10)", factor=10, stock_qty=20)  # changed on the desk
		doc = _order([webstore, bunched])
		self._validate(doc)
		self.assertEqual((webstore.uom, webstore.qty, webstore.conversion_factor), ("Stems", 30, 1))
		self.assertEqual((bunched.uom, bunched.qty, bunched.conversion_factor), ("Stems", 20, 1))
		self.assertEqual((webstore.custom_packrate, webstore.custom_number_of_boxes), ("30", 1))
		self.assertEqual(bunched.custom_packrate, "20")

	def test_the_team_with_the_fewest_quality_b_stems_gets_the_order(self):
		ps = SimpleNamespace(
			get=lambda k, d=None: [SimpleNamespace(team="Team A"), SimpleNamespace(team="Team B")]
		)
		with (
			patch.object(qb.frappe, "get_cached_doc", return_value=ps),
			patch(
				"upande_packhouse.api.v2.allocation_teams.team_sales_farms",
				return_value={"Team A": "Kapkolia", "Team B": "Kapkolia", "Bravo": "Karen"},
			),
			patch.object(qb.frappe.db, "sql", return_value=[("Team A", 2000.0), ("Team B", 65.0)]),
		):
			self.assertEqual(qb.pick_team("Kapkolia", "2026-11-29"), "Team B")
			self.assertIsNone(qb.pick_team("Chepsito", "2026-11-29"))

	def test_shopping_cart_orders_are_never_on_sales_allocation(self):
		from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa

		seen = {}

		def capture(sql, params=None, as_dict=False):
			seen["sql"] = sql
			return []

		with (
			patch.object(
				sa, "_planning_switches", return_value={"sequence": False, "team": False, "unplanned": set()}
			),
			patch.object(sa.frappe.db, "sql", side_effect=capture),
		):
			sa.get_pending_sales_orders(delivery_start="2026-11-29", delivery_end="2026-11-29")
		self.assertIn("IFNULL(so.order_type, '') != 'Shopping Cart'", seen["sql"])


class UnitTestQualityBIssuing(UnitTestCase):
	"""A scanned bucket not on the pick list: only a Quality B pick list with the setting on may take it."""

	def _take(self, quality_b, setting):
		opl = MagicMock()
		opl.get = lambda k, d=None: {"quality_b": quality_b}.get(k, d)
		ps = SimpleNamespace(get=lambda k, d=None: {"quality_b_issue_any_bucket": setting}.get(k, d))
		with (
			patch.object(qb.frappe.db, "exists", return_value=True),
			patch.object(qb.frappe, "get_doc", return_value=opl),
			patch.object(qb.frappe, "get_cached_doc", return_value=ps),
		):
			qb.take_scanned_bucket("OPL-1", "B9")

	def test_an_export_pick_list_refuses_a_bucket_not_on_it(self):
		with self.assertRaises(frappe.ValidationError) as e:
			self._take(quality_b=0, setting=1)
		self.assertIn("isn't allocated to OPL-1", str(e.exception))

	def test_quality_b_refuses_it_while_the_setting_is_off(self):
		with self.assertRaises(frappe.ValidationError):
			self._take(quality_b=1, setting=0)
