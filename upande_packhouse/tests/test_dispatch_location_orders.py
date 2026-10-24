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
