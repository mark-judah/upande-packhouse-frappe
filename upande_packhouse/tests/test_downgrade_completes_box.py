"""A downgrade approved after Allocate all completes its mixed box.

Allocate all leaves out a mixed box it cannot fill whole (B - TEPLO 54826-05666: Giselle 62cm had no exact
stems, only longer ones needing a reason). Approving Giselle's downgrade added Giselle alone; nothing went
back for the box's other lines, so the box stayed 0/1. apply_downgrades now plans the orders it touched again
on top of what they hold.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da


def _line(soi, variety, per_box, held=0, stock=0):
	return {
		"sales_order_item": soi,
		"name": soi,
		"parent": "SO-T",
		"item_code": variety,
		"custom_length": "62cm",
		"required_length": "62cm",
		"custom_number_of_boxes": 1,
		"custom_mixed_box": 1,
		"custom_mix_group": "1",
		"custom_mixed_bunch": 0,
		"custom_bunch_group": "",
		"custom_mix_name": "TEPLO",
		"custom_packrate_mixed_box": per_box,
		"pending_stock_qty": per_box,
		"original_stock_qty": per_box,
		"total_allocated_qty": held,
		"conversion_factor": 10,
		"uom": "Bunch",
		"stock_uom": "Stem",
		"batches": [
			{
				"bucket_id": "B-" + soi,
				"available_qty": stock,
				"stem_length": "62cm",
				"length_status": "exact",
				"is_sales_shelf": 1,
				"warehouse": "W",
			}
		]
		if stock
		else [],
	}


class UnitTestDowngradeCompletesBox(UnitTestCase):
	def _plan(self, items, prior):
		order = frappe._dict(name="SO-T", customer="C", delivery_date="2026-11-27")
		rules = {"partial_allocation": True, "enforce_plan": False}
		with (
			patch.object(da.sa, "prefetch_orders"),
			patch.object(da.sa, "get_sales_order_items_with_buckets", return_value=items),
			patch.object(da.sa, "_ac_rows", return_value={}),
			patch.object(da.sa, "_planned_teams", return_value={}),
			patch.object(da.sa, "bypass_cut_stage_default", return_value=1),
		):
			out, _e, _l, _t = da._plan_orders(
				"Kapkolia", None, None, (), [order], False, prior, {}, (), rules
			)
		return out

	def _box(self):
		return [
			_line("DB", "Dima Bombastic", 50, stock=50),
			_line("SO", "Sofie", 70, stock=160),
			_line("GI", "Giselle", 50, stock=0),
			_line("DI", "Dinara", 30, stock=430),
		]

	def test_without_the_downgrade_the_box_is_left_out(self):
		self.assertEqual(self._plan(self._box(), {}), [])

	def test_with_the_downgrade_held_the_other_lines_fill_the_box(self):
		out = self._plan(self._box(), {"GI": 50})
		self.assertEqual(len(out), 1)
		got = {}
		for a in out[0]["allocations"]:
			got[a["sales_order_item"]] = got.get(a["sales_order_item"], 0) + a["qty"]
		self.assertEqual(got, {"DB": 50, "SO": 70, "DI": 30})
		self.assertEqual(out[0]["boxes"], 1)

	def test_apply_downgrades_plans_the_touched_orders_again(self):
		line = frappe._dict(
			parent="SO-T", item_code="Giselle", uom="Bunch", stock_uom="Stem", conversion_factor=10
		)
		view = [
			{
				"item_code": "Giselle",
				"batches": [
					{"bucket_id": "L82", "stem_length": "72cm", "available_qty": 70, "is_sales_shelf": 1}
				],
			}
		]
		with (
			patch.object(da.frappe, "get_all", return_value=["Amber"]),
			patch.object(da.frappe.db, "get_value", return_value=line),
			patch.object(da.sa, "get_sales_order_items_with_buckets", return_value=view),
			patch.object(da, "_left_on_buckets", return_value={("L82", "Giselle", "72cm"): 70}),
			patch.object(da.sa, "_required_stems_for_so_item", return_value=50),
			patch.object(da, "_held_stems", return_value=0),
			patch.object(da, "_add_pending") as add,
			patch.object(da, "_allocate_more", return_value=(150, 1)) as more,
			patch.object(da.frappe.db, "commit"),
			patch.object(da, "_proposal", return_value=[]),
			patch.object(da, "_summary", return_value={}),
		):
			out = da.apply_downgrades(
				"Kapkolia",
				"2026-11-27",
				picks=[
					{
						"sales_order_item": "GI",
						"bucket_id": "L82",
						"length": "72cm",
						"qty": 50,
						"reason": "Amber",
					}
				],
			)
		add.assert_called_once()
		more.assert_called_once()
		self.assertEqual(more.call_args.args[3], {"SO-T"})
		self.assertEqual(out["downgraded"], 50)
		self.assertEqual(out["completed"], 150)
		self.assertEqual(out["stems"], 200)
