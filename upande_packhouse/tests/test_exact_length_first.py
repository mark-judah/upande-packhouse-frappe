"""Allocate gives every order its own length before any downgrade, and downgrades longest length first.

A longer stem downgraded onto a shorter line used to be taken in allocation order, so an order later in the
sequence that ordered that longer length was left short of its exact length.
"""

import copy
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da


def _line(so, soi, length, stems):
	return {
		"sales_order_item": soi,
		"name": soi,
		"parent": so,
		"item_code": "Furiosa",
		"required_length": length,
		"custom_length": length,
		"custom_number_of_boxes": 1,
		"custom_mixed_box": 0,
		"custom_mixed_bunch": 0,
		"custom_packrate": stems,
		"pending_stock_qty": stems,
		"original_stock_qty": stems,
		"total_allocated_qty": 0,
		"conversion_factor": 20,
		"batches": [],
	}


def _bucket(bucket, length, stems, status):
	return {
		"bucket_id": bucket,
		"available_qty": stems,
		"stem_length": length,
		"length_status": status,
		"downgrade_approval": "amber_expired" if status == "downgrade" else "",
		"is_sales_shelf": 1,
		"warehouse": "W",
	}


class UnitTestExactLengthFirst(UnitTestCase):
	def _plan(self, orders, items):
		rules = {"partial_allocation": True, "enforce_plan": False}
		with (
			patch.object(da, "_rules", return_value=rules),
			patch.object(da.sa, "prefetch_orders"),
			patch.object(
				da.sa,
				"get_sales_order_items_with_buckets",
				side_effect=lambda so, *a, **k: copy.deepcopy(items[so]),
			),
			patch.object(da.sa, "_ac_rows", return_value={}),
			patch.object(da.sa, "_planned_teams", return_value={}),
			patch.object(da.sa, "bypass_cut_stage_default", return_value=1),
			patch.object(da.sa, "end_planning_memo"),
		):
			out, _e, _l, _t = da._plan_day("Kapkolia", "2026-11-27", None, None, orders=orders)
		got = {}
		for o in out:
			for a in o["allocations"]:
				got[a["sales_order_item"]] = got.get(a["sales_order_item"], 0) + a["qty"]
		return got

	def test_a_later_order_of_that_length_gets_it_before_a_downgrade(self):
		first = _line("SO-1", "L42", "42cm", 100)
		second = _line("SO-2", "L52", "52cm", 100)
		first["batches"] = [_bucket("B52", "52cm", 100, "downgrade")]
		second["batches"] = [_bucket("B52", "52cm", 100, "exact")]
		orders = [frappe._dict(name="SO-1", customer="C"), frappe._dict(name="SO-2", customer="C")]
		got = self._plan(orders, {"SO-1": [first], "SO-2": [second]})
		self.assertEqual(got, {"L52": 100})

	def test_what_is_left_still_goes_to_the_shorter_line(self):
		first = _line("SO-1", "L42", "42cm", 100)
		second = _line("SO-2", "L52", "52cm", 100)
		first["batches"] = [_bucket("B52", "52cm", 200, "downgrade")]
		second["batches"] = [_bucket("B52", "52cm", 200, "exact")]
		orders = [frappe._dict(name="SO-1", customer="C"), frappe._dict(name="SO-2", customer="C")]
		got = self._plan(orders, {"SO-1": [first], "SO-2": [second]})
		self.assertEqual(got, {"L52": 100, "L42": 100})

	def test_downgrades_go_to_the_longest_length_ordered_first(self):
		short = _line("SO-1", "L42", "42cm", 100)
		longer = _line("SO-2", "L52", "52cm", 100)
		short["batches"] = [_bucket("B62", "62cm", 100, "downgrade")]
		longer["batches"] = [_bucket("B62", "62cm", 100, "downgrade")]
		orders = [frappe._dict(name="SO-1", customer="C"), frappe._dict(name="SO-2", customer="C")]
		got = self._plan(orders, {"SO-1": [short], "SO-2": [longer]})
		self.assertEqual(got, {"L52": 100})
