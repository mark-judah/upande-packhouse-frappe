from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse import quality_b as qb

# Quality B orders (order type Shopping Cart): a local sale of Grade B flowers. They get their own delivery point,
# need none of the export fields, are sold in whole bunches of the variety's bunch, and their pick lists are shared out
# among the Quality B teams. The database is mocked.


def _line(item, qty, uom=None, stock_uom="Stems", factor=1, stock_qty=None, rate=None):
	line = frappe._dict(
		item_code=item,
		qty=qty,
		rate=rate,
		price_list_rate=rate,
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
			patch.object(qb, "_bunch_uom", return_value=("Bunch (10)", 10.0)),
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

	def test_lines_are_sold_in_whole_bunches_and_packed_whole(self):
		webstore = _line(
			"Moonwalk", 30, rate=0.15
		)  # made by the webstore: no unit yet, the quantity is stems
		bunched = _line("Fuchsiana", 2, uom="Bunch (10)", factor=10, stock_qty=20)  # changed on the desk
		short = _line("Aqua", 25)  # not a whole bunch: rounded up
		doc = _order([webstore, bunched, short])
		self._validate(doc)
		self.assertEqual(
			(webstore.uom, webstore.qty, webstore.conversion_factor, webstore.stock_qty),
			("Bunch (10)", 3, 10.0, 30),
		)
		self.assertAlmostEqual(webstore.rate, 1.5)  # priced per bunch now
		self.assertEqual((bunched.uom, bunched.qty, bunched.conversion_factor), ("Bunch (10)", 2, 10.0))
		self.assertEqual((short.qty, short.stock_qty), (3, 30))
		self.assertEqual((webstore.custom_packrate, webstore.custom_number_of_boxes), ("30", 1))
		self.assertEqual(bunched.custom_packrate, "20")

	def test_a_line_takes_the_bunch_its_item_is_sold_in(self):
		# SAL-ORD-2026-05401: Yelloween sells in Bunch (10) but its Item lists Bunch (7) first
		rows = [
			SimpleNamespace(uom="Bunch (7)", conversion_factor=7),
			SimpleNamespace(uom="Stems", conversion_factor=1),
		]
		with (
			patch.object(qb.frappe, "get_all", return_value=rows),
			patch.object(qb.frappe.db, "get_value", return_value="Bunch (10)"),
		):
			self.assertEqual(qb._bunch_uom("Yelloween", "Stems"), ("Bunch (10)", 10))
		# no bunch to sell in: the item's own bunch
		with (
			patch.object(qb.frappe, "get_all", return_value=rows),
			patch.object(qb.frappe.db, "get_value", return_value="Stems"),
		):
			self.assertEqual(qb._bunch_uom("Yelloween", "Stems"), ("Bunch (7)", 7.0))

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

		seen = []
		# the column checks read the real tables before every query is captured: cached, they need no query
		for dt in ("Sales Order", "Sales Order Item"):
			sa.frappe.db.get_table_columns(dt)

		def capture(sql, params=None, *args, **kwargs):
			seen.append(sql)
			return []

		with (
			patch.object(
				sa, "_planning_switches", return_value={"sequence": False, "team": False, "unplanned": set()}
			),
			patch.object(sa.frappe.db, "sql", side_effect=capture),
		):
			sa.get_pending_sales_orders(delivery_start="2026-11-29", delivery_end="2026-11-29")
		self.assertTrue(any("IFNULL(so.order_type, '') != 'Shopping Cart'" in q for q in seen))


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
