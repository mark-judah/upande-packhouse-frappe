"""A box whose lines share a variety: Allocate splits that variety's stock across the lines, box by box.

Taken line by line, the first line emptied the shared buckets and the next line of the same variety got
nothing, so not one box was complete and the whole order was left out (XPOL 54826: Furiosa in its mixed box
at 40 a box and in a mixed bunch at 20 a box, 101 usable stems -- one box, not none).
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da

SPEC = "XPOL TEST SPEC"


def _line(soi, variety, per_box, boxes, mixed_bunch=False, group="1"):
	return {
		"sales_order_item": soi,
		"name": soi,
		"parent": "SO-T",
		"item_code": variety,
		"custom_length": "42cm",
		"custom_number_of_boxes": boxes,
		"custom_mixed_box": 0 if mixed_bunch else 1,
		"custom_mix_group": "" if mixed_bunch else group,
		"custom_mixed_bunch": 1 if mixed_bunch else 0,
		"custom_bunch_group": f"{SPEC}::7" if mixed_bunch else "",
		"custom_mix_name": SPEC,
		"custom_packrate_mixed_box": per_box,
		"pending_stock_qty": per_box * boxes,
		"original_stock_qty": per_box * boxes,
		"total_allocated_qty": 0,
		"conversion_factor": 20,
		"uom": "Bunch",
		"stock_uom": "Stem",
		"batches": [],
	}


def _bucket(bucket, stems):
	return {
		"bucket_id": bucket,
		"available_qty": stems,
		"stem_length": "42cm",
		"length_status": "exact",
		"is_sales_shelf": 1,
		"warehouse": "W",
	}


class UnitTestBoxSharedVariety(UnitTestCase):
	def _plan(self, items):
		order = frappe._dict(name="SO-T", customer="C", delivery_date="2026-11-27")
		rules = {"partial_allocation": True, "enforce_plan": False}
		with (
			patch.object(da.sa, "prefetch_orders"),
			patch.object(da.sa, "get_sales_order_items_with_buckets", return_value=items),
			patch.object(da.sa, "_ac_rows", return_value={}),
			patch.object(da.sa, "_planned_teams", return_value={}),
			patch.object(da.sa, "bypass_cut_stage_default", return_value=1),
		):
			out, _errors, _lines, _taken = da._plan_orders(
				"Kapkolia", None, None, (), [order], False, {}, {}, (), rules
			)
		return out

	def test_shared_variety_is_split_box_by_box(self):
		furiosa_box = _line("F1", "Furiosa", 40, 21)
		furiosa_bunch = _line("F2", "Furiosa", 20, 21, mixed_bunch=True)
		aqua = _line("A1", "Aqua", 100, 21)
		furiosa_box["batches"] = furiosa_bunch["batches"] = [_bucket("FEPAL0", 101)]
		aqua["batches"] = [_bucket("AQ1", 5000)]
		out = self._plan([furiosa_box, furiosa_bunch, aqua])
		self.assertEqual(len(out), 1)
		got = {}
		for a in out[0]["allocations"]:
			got[a["sales_order_item"]] = got.get(a["sales_order_item"], 0) + a["qty"]
		# 101 Furiosa stems make one box (40 + 20), whole bunches of 20 each
		self.assertEqual(out[0]["boxes"], 1)
		self.assertEqual(got, {"F1": 40, "F2": 20, "A1": 100})

	def test_a_box_with_no_stock_for_one_line_takes_nothing(self):
		furiosa_box = _line("F1", "Furiosa", 40, 21)
		aqua = _line("A1", "Aqua", 100, 21)
		aqua["batches"] = [_bucket("AQ1", 5000)]
		self.assertEqual(self._plan([furiosa_box, aqua]), [])

	def test_a_straight_line_keeps_everything_the_shelves_have(self):
		straight = _line("S1", "Aqua", 100, 3)
		straight.update(custom_mixed_box=0, custom_mix_group="", custom_mix_name="", custom_packrate=100)
		straight["batches"] = [_bucket("AQ1", 250)]
		out = self._plan([straight])
		self.assertEqual(out[0]["allocations"][0]["qty"], 240)
