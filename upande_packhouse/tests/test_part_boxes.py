from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da


class UnitTestPartBoxes(UnitTestCase):
	def test_a_part_box_never_goes_out(self):
		keep = [{"sales_order_item": "b", "qty": 40}]
		out, rest, _boxes = da._gate_release([], keep, {}, 0, 4)
		self.assertEqual(out, [])
		self.assertEqual(rest, keep)

	def test_complete_boxes_go_out_and_the_part_box_waits(self):
		release = [{"sales_order_item": "a", "qty": 600}]
		keep = [{"sales_order_item": "a", "qty": 200}]
		with patch.object(da, "_release_ok", return_value=True):
			out, rest, boxes = da._gate_release(release, keep, {("line", "a"): 1}, 0, 4)
		self.assertEqual(out, release)
		self.assertEqual(rest, keep)
		self.assertEqual(boxes, {("line", "a"): 1})


class UnitTestReleaseParts(UnitTestCase):
	def test_straight_lines_go_out_apart_from_the_mix(self):
		release = [
			{"sales_order_item": "mix1", "qty": 40},
			{"sales_order_item": "str1", "qty": 300},
			{"sales_order_item": "mix2", "qty": 20},
		]
		flags = [
			{"name": "mix1", "custom_mixed_box": 0, "custom_mixed_bunch": 1},
			{"name": "mix2", "custom_mixed_box": 1, "custom_mixed_bunch": 0},
			{"name": "str1", "custom_mixed_box": 0, "custom_mixed_bunch": 0},
		]
		with patch.object(da.frappe, "get_all", return_value=[frappe._dict(f) for f in flags]):
			parts = da._release_parts(release)
		self.assertEqual(parts[0], [release[1]])
		self.assertEqual(parts[1], [release[0], release[2]])

	def test_only_straight(self):
		with patch.object(
			da.frappe,
			"get_all",
			return_value=[frappe._dict(name="s", custom_mixed_box=0, custom_mixed_bunch=0)],
		):
			row = {"sales_order_item": "s", "qty": 1}
			self.assertEqual(da._release_parts([row]), [[row]])


class UnitTestItemGroupFilter(UnitTestCase):
	def tearDown(self):
		da._use_group(None)

	def test_orders_follow_the_item_group(self):
		rows = [frappe._dict(name="SO-1"), frappe._dict(name="SO-2")]
		da._use_group("Spray Roses")
		with (
			patch.object(da.sa, "get_pending_sales_orders", return_value=rows),
			patch.object(da, "_orders_in_group", return_value={"SO-2"}),
		):
			self.assertEqual([r.name for r in da._orders("Kapkolia", "2026-11-19", None)], ["SO-2"])

	def test_no_group_keeps_every_order(self):
		rows = [frappe._dict(name="SO-1"), frappe._dict(name="SO-2")]
		da._use_group("")
		with patch.object(da.sa, "get_pending_sales_orders", return_value=rows):
			self.assertEqual(len(da._orders("Kapkolia", "2026-11-19", None)), 2)
