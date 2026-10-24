from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.doctype.loading_plan import loading_plan as lp

# A second truck's Loading Plan takes only the boxes the location's other trucks
# don't carry, up to its Box Capacity. The database is mocked: these run on a bare
# site (CI) as well.

DAY = "2026-10-25"


def _e(opl, boxes):
	return {"opl": opl, "number_of_boxes": boxes, "customer": "C", "delivery_point": "DP"}


def _ls(box, vehicle):
	return frappe._dict(box_label_link=box, box_label=box, vehicle=vehicle)


class UnitTestTruckShare(UnitTestCase):
	def test_boxes_on_other_trucks_are_left_out(self):
		out = lp._truck_share([_e("A", 8), _e("B", 3)], {"A": 5, "B": 3}, 0)
		self.assertEqual([(e["opl"], e["number_of_boxes"]) for e in out], [("A", 3)])

	def test_capacity_cuts_the_last_row(self):
		out = lp._truck_share([_e("A", 8), _e("B", 6), _e("C", 4)], {}, 10)
		self.assertEqual([(e["opl"], e["number_of_boxes"]) for e in out], [("A", 8), ("B", 2)])

	def test_claim_over_several_rows_of_one_opl(self):
		out = lp._truck_share([_e("A", 2), _e("A", 4)], {"A": 3}, 0)
		self.assertEqual([e["number_of_boxes"] for e in out], [3])

	def test_uncounted_opl_stays_only_when_no_other_truck_has_it(self):
		out = lp._truck_share([_e("A", 0), _e("B", 0)], {"A": 1}, 0)
		self.assertEqual([e["opl"] for e in out], ["B"])


class UnitTestOtherTrucksBoxes(UnitTestCase):
	def _claim(self, plans, ls_items, labels, plan_rows=None, capacity=None, departures=None, plan=None):
		capacity = capacity or {}

		def get_all(doctype, **kw):
			if doctype == "Loading Plan":
				return plans
			if doctype == "Loading Sheet Item":
				return ls_items
			if doctype == "Box Label":
				return [frappe._dict(name=k, order_pick_list=v) for k, v in labels.items()]
			if doctype == "Loading Plan Item":
				return (plan_rows or {}).get(kw["filters"]["parent"], [])
			raise AssertionError(doctype)

		with (
			patch.object(lp.frappe, "get_all", side_effect=get_all),
			patch.object(lp, "_box_capacity", side_effect=lambda v: capacity.get(v, 0)),
			patch("upande_packhouse.mobile.api.location_departures", return_value=departures or {}),
		):
			return lp.other_trucks_boxes(DAY, "Ravine", "KCS 937S", plan)

	def test_boxes_loaded_on_the_first_truck_are_claimed(self):
		plans = [frappe._dict(name="LP-1", vehicle="KCF 700Z")]
		items = [_ls("B1", "KCF 700Z"), _ls("B2", "KCF 700Z"), _ls("B3", "KCS 937S")]
		labels = {"B1": "A", "B2": "A", "B3": "A"}
		self.assertEqual(self._claim(plans, items, labels), {"A": 2})

	def test_load_without_a_truck_is_the_other_trucks(self):
		plans = [frappe._dict(name="LP-1", vehicle="KCF 700Z")]
		self.assertEqual(self._claim(plans, [_ls("B1", None)], {"B1": "A"}), {"A": 1})

	def test_load_without_a_truck_is_mine_when_no_other_plan(self):
		self.assertEqual(self._claim([], [_ls("B1", None)], {"B1": "A"}), {})

	def test_capped_truck_still_loading_holds_its_plan(self):
		plans = [frappe._dict(name="LP-1", vehicle="KCF 700Z")]
		rows = {"LP-1": [frappe._dict(order_pick_list="A", number_of_boxes=5)]}
		claim = self._claim(plans, [_ls("B1", "KCF 700Z")], {"B1": "A"}, rows, capacity={"KCF 700Z": 300})
		self.assertEqual(claim, {"A": 5})

	def test_departed_truck_holds_only_what_it_loaded(self):
		plans = [frappe._dict(name="LP-1", vehicle="KCF 700Z")]
		rows = {"LP-1": [frappe._dict(order_pick_list="A", number_of_boxes=5)]}
		claim = self._claim(
			plans,
			[_ls("B1", "KCF 700Z")],
			{"B1": "A"},
			rows,
			capacity={"KCF 700Z": 300},
			departures={"Ravine / KCF 700Z": {"seal": "1"}},
		)
		self.assertEqual(claim, {"A": 1})

	def test_own_plan_is_never_another_trucks(self):
		plans = [frappe._dict(name="LP-2", vehicle="KCS 937S")]
		self.assertEqual(self._claim(plans, [_ls("B1", "KCS 937S")], {"B1": "A"}, plan="LP-2"), {})
