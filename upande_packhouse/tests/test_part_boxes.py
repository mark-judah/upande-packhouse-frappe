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
		da._use_group(None, {})

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


class UnitTestAllocateFollowsTheOrderList(UnitTestCase):
	"""Allocate plans only the orders still short, and only those the page's filters keep."""

	def tearDown(self):
		da._use_group(None, {})

	def _rows(self):
		return [
			frappe._dict(name="SO-1", allocation_percentage=0),
			frappe._dict(name="SO-2", allocation_percentage=40),
			frappe._dict(name="SO-3", allocation_percentage=100),
		]

	def _orders(self):
		with patch.object(da.sa, "get_pending_sales_orders", return_value=self._rows()):
			return [r.name for r in da._orders("Kapkolia", "2026-11-19", None)]

	def test_a_fully_allocated_order_is_never_planned(self):
		da._use_group(None, {})
		self.assertEqual(self._orders(), ["SO-1", "SO-2"])

	def test_the_stage_toggles_are_followed(self):
		da._use_group(None, {"stages": [1]})
		self.assertEqual(self._orders(), ["SO-2"])
		da._use_group(None, {"stages": [0, 1, 2]})
		self.assertEqual(self._orders(), ["SO-1", "SO-2"])

	def test_the_list_filters_are_followed(self):
		da._use_group(None, {"priority": "High"})
		self.assertTrue(da._scoped())
		with patch.object(da.frappe.db, "sql_list", return_value=["SO-2"]) as q:
			self.assertEqual(self._orders(), ["SO-2"])
		self.assertIn("custom_priority", q.call_args[0][0])

	def test_filters_ride_along_to_background_jobs(self):
		da._use_group("Spray Roses", {"search": "abc", "box_type": "mixed"})
		kw = da._scope_kw()
		da._use_group(None, {})
		da._use_group(kw["item_group"], kw["order_filters"])
		self.assertEqual(da._order_filters(), {"search": "abc", "box_type": "mixed"})
		self.assertEqual(da._group(), "Spray Roses")
