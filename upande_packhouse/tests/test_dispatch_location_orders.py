from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.mobile import api

# Ravine and Karen each dispatch their own orders: a plan row matching a customer
# (and delivery point) must not claim the other location's order, or that order's
# staged boxes block a dispatch they were never going to be on.
# The database is mocked: these run on a bare site (CI) as well.

DAY = "2026-10-25"
FARMS = {"Ravine": {"Kapkolia", "Chepsito", None, ""}, "Karen": {"Karen"}}


def _so(name, farm, customer="ROSE PARIS", dp="MITCHEL COTTS"):
	return frappe._dict(name=name, customer=customer, custom_delivery_point=dp, farm=farm)


def _row(plan, customer="ROSE PARIS", dp="MITCHEL COTTS", so=None):
	return frappe._dict(parent=plan, customer=customer, delivery_point=dp, sales_order=so)


class UnitTestDispatchLocationOrders(UnitTestCase):
	def _orders(self, location, plans, rows, sos):
		def get_all(doctype, **kw):
			return {"Loading Plan": plans, "Loading Plan Item": rows, "Sales Order": sos}[doctype]

		with (
			patch.object(api.frappe, "get_all", side_effect=get_all),
			patch.object(api, "location_farms", side_effect=lambda loc: FARMS[loc]),
		):
			return api.location_sales_orders(DAY, location)

	def test_plan_row_with_order_claims_only_that_order(self):
		plans = [frappe._dict(name="LP-R", location="Ravine")]
		rows = [_row("LP-R", so="SO-RAVINE")]
		sos = [_so("SO-RAVINE", "Kapkolia"), _so("SO-KAREN", "Karen")]
		self.assertEqual(self._orders("Ravine", plans, rows, sos), {"SO-RAVINE"})
		self.assertEqual(self._orders("Karen", plans, rows, sos), {"SO-KAREN"})

	def test_customer_row_never_claims_other_locations_order(self):
		plans = [frappe._dict(name="LP-R", location="Ravine")]
		rows = [_row("LP-R")]
		sos = [_so("SO-RAVINE", "Kapkolia"), _so("SO-KAREN", "Karen")]
		self.assertEqual(self._orders("Ravine", plans, rows, sos), {"SO-RAVINE"})
		self.assertEqual(self._orders("Karen", plans, rows, sos), {"SO-KAREN"})

	def test_order_named_on_other_plan_goes_there(self):
		plans = [frappe._dict(name="LP-R", location="Ravine")]
		rows = [_row("LP-R", so="SO-KAREN")]
		sos = [_so("SO-KAREN", "Karen")]
		self.assertEqual(self._orders("Ravine", plans, rows, sos), {"SO-KAREN"})
		self.assertEqual(self._orders("Karen", plans, rows, sos), set())

	def test_missing_staged_boxes_skip_other_location(self):
		plans = [frappe._dict(name="LP-R", location="Ravine")]
		rows = [_row("LP-R", so="SO-RAVINE")]
		sos = [_so("SO-RAVINE", "Kapkolia"), _so("SO-KAREN", "Karen")]
		seen = {}

		def get_all(doctype, filters=None, **kw):
			if doctype == "Sales Order" and kw.get("pluck"):
				return [s.name for s in sos]
			if doctype == "Order Pick List":
				seen["sos"] = filters["sales_order"][1]
				return []
			return {"Loading Plan": plans, "Loading Plan Item": rows, "Sales Order": sos}[doctype]

		with (
			patch.object(api.frappe, "get_all", side_effect=get_all),
			patch.object(api, "location_farms", side_effect=lambda loc: FARMS[loc]),
		):
			api.missing_staged_boxes_for_date(DAY, "Ravine")
		self.assertEqual(seen["sos"], ["SO-RAVINE"])


class UnitTestDispatchPerTruck(UnitTestCase):
	"""Two Ravine trucks: each dispatches what was loaded on it, and only its own
	orders' unloaded boxes hold it back."""

	PLANS: ClassVar = [
		frappe._dict(name="LP-A", location="Ravine", vehicle="KAA 1A"),
		frappe._dict(name="LP-B", location="Ravine", vehicle="KBB 2B"),
	]
	ROWS: ClassVar = {
		"LP-A": [_row("LP-A", customer="C1", dp="AIRFLO", so="SO-1")],
		"LP-B": [_row("LP-B", customer="C2", dp="DHL", so="SO-2")],
	}
	SOS: ClassVar = [
		_so("SO-1", "Kapkolia", customer="C1", dp="AIRFLO"),
		_so("SO-2", "Kapkolia", customer="C2", dp="DHL"),
		_so("SO-3", "Kapkolia", customer="C3", dp="JAAV"),
	]
	# SO-3 is on no plan; its box went on truck B.
	LOADED: ClassVar = [
		frappe._dict(name="r1", box_label_link="BOX-1", box_label="BOX-1", vehicle="KAA 1A"),
		frappe._dict(name="r2", box_label_link="BOX-2", box_label="BOX-2", vehicle="KBB 2B"),
		frappe._dict(name="r3", box_label_link="BOX-3", box_label="BOX-3", vehicle="KBB 2B"),
	]
	BOXES: ClassVar = [
		frappe._dict(name="BOX-1", customer_purchase_order="SO-1"),
		frappe._dict(name="BOX-2", customer_purchase_order="SO-2"),
		frappe._dict(name="BOX-3", customer_purchase_order="SO-3"),
	]

	def _run(self, fn, plans=None, loaded=None):
		plans = self.PLANS if plans is None else plans
		loaded = self.LOADED if loaded is None else loaded
		seen = {}

		def get_all(doctype, filters=None, **kw):
			filters = filters or {}
			if doctype == "Loading Plan":
				return [p for p in plans if filters.get("location") in (None, p.location)]
			if doctype == "Loading Plan Item":
				parent = filters.get("parent")
				if isinstance(parent, list):
					return [r for p in parent[1] for r in self.ROWS.get(p, [])]
				return self.ROWS.get(parent, [])
			if doctype == "Sales Order":
				if kw.get("pluck"):
					return [s.name for s in self.SOS]
				if "name" in filters:
					return [s for s in self.SOS if s.name in filters["name"][1]]
				return self.SOS
			if doctype == "Loading Sheet Item":
				return loaded
			if doctype == "Box Label":
				return self.BOXES
			if doctype == "Order Pick List":
				seen["sos"] = sorted(filters["sales_order"][1])
				return []
			raise AssertionError(doctype)

		with (
			patch.object(api.frappe, "get_all", side_effect=get_all),
			patch.object(api.frappe.db, "exists", return_value=True),
			patch.object(api.frappe.db, "get_value", return_value=None),
			patch.object(api, "location_farms", side_effect=lambda loc: FARMS[loc]),
		):
			return fn(), seen

	def test_each_truck_carries_its_plan_and_what_was_loaded_on_it(self):
		a, _ = self._run(lambda: api.truck_sales_orders(DAY, "Ravine", "KAA 1A"))
		b, _ = self._run(lambda: api.truck_sales_orders(DAY, "Ravine", "KBB 2B"))
		self.assertEqual(a, {"SO-1"})
		self.assertEqual(b, {"SO-2", "SO-3"})

	def test_other_trucks_boxes_never_block(self):
		_, seen = self._run(lambda: api.missing_staged_boxes_for_date(DAY, "Ravine", "KAA 1A"))
		self.assertEqual(seen["sos"], ["SO-1"])

	def test_trucks_listed_with_what_each_loaded(self):
		trucks, _ = self._run(lambda: api.dispatch_trucks(DAY, "Ravine"))
		self.assertEqual(
			[(t["vehicle"], t["loading_plan"], t["boxes_loaded"]) for t in trucks],
			[("KAA 1A", "LP-A", 1), ("KBB 2B", "LP-B", 2)],
		)

	def test_only_truck_carries_every_order(self):
		plans = [self.PLANS[0]]
		loaded = [frappe._dict(name="r1", box_label_link="BOX-1", box_label="BOX-1", vehicle=None)]
		orders, _ = self._run(lambda: api.truck_sales_orders(DAY, "Ravine", "KAA 1A"), plans, loaded)
		self.assertEqual(orders, {"SO-1", "SO-2", "SO-3"})
		trucks, _ = self._run(lambda: api.dispatch_trucks(DAY, "Ravine"), plans, loaded)
		self.assertEqual([(t["vehicle"], t["boxes_loaded"]) for t in trucks], [("KAA 1A", 1)])


def _soi(name, item, length, qty, delivered=0):
	return frappe._dict(
		name=name,
		item_code=item,
		custom_length=length,
		qty=qty,
		delivered_qty=delivered,
		conversion_factor=10,
	)


def _loaded(stems, boxes, length):
	return {"stems": stems, "boxes": boxes, "length": length, "source_farm": "Kapkolia"}


class UnitTestDeliveryNoteOrderLines(UnitTestCase):
	"""Delivery Note rows land on the order line of their variety AND length, never
	all on the variety's first line (over limit on submit)."""

	def _split(self, items, var):
		so_cache = {"SO-1": SimpleNamespace(items=items)}
		return [
			(soi.name if soi else None, d["stems"], d["boxes"])
			for _s, _v, d, soi in api.split_over_order_lines(var, so_cache)
		]

	def test_each_length_goes_to_its_own_line(self):
		items = [
			_soi("w42", "Wham", "42cm", 30),
			_soi("w52", "Wham", "52cm", 30),
			_soi("w62", "Wham", "62cm", 30),
		]
		var = {
			("SO-1", "Wham", "42cm"): _loaded(300, 3, "42cm"),
			("SO-1", "Wham", "52cm"): _loaded(300, 3, "52cm"),
			("SO-1", "Wham", "62cm"): _loaded(300, 3, "62cm"),
		}
		self.assertEqual(self._split(items, var), [("w42", 300, 3), ("w52", 300, 3), ("w62", 300, 3)])

	def test_same_length_lines_fill_in_turn(self):
		items = [_soi("f1", "Fireworks", "62cm", 10), _soi("f2", "Fireworks", "62cm", 25)]
		var = {("SO-1", "Fireworks", "62cm"): _loaded(350, 7, "62cm")}
		self.assertEqual(self._split(items, var), [("f1", 100, 2), ("f2", 250, 5)])

	def test_already_delivered_is_skipped(self):
		items = [_soi("a1", "Ariya", "52cm", 10, delivered=10), _soi("a2", "Ariya", "52cm", 10)]
		var = {("SO-1", "Ariya", "52cm"): _loaded(100, 1, "52cm")}
		self.assertEqual(self._split(items, var), [("a2", 100, 1)])
