from types import SimpleNamespace

from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da
from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa


def _bucket(bucket_id, free, age):
	return {
		"bucket_id": bucket_id,
		"available_qty": free,
		"stem_length": "52cm",
		"length_status": "exact",
		"is_sales_shelf": 1,
		"age_days": age,
	}


def _item(buckets, bunch=18):
	return {
		"sales_order": "SO-1",
		"sales_order_item": "L1",
		"item_code": "Snowstorm",
		"conversion_factor": bunch,
		"batches": buckets,
	}


class UnitTestFifoWholeBunches(UnitTestCase):
	"""A bunch takes its stems from as many buckets as it needs: the oldest buckets are emptied first and only
	the line's total is whole bunches. Before, every 200-stem bucket gave 198 (11 bunches of 18) and kept 2."""

	def test_the_oldest_buckets_are_emptied_first(self):
		item = _item([_bucket("OLD", 200, 5), _bucket("MID", 200, 4), _bucket("NEW", 200, 3)])
		picks, left = da._fifo_pick(item, 540, {})
		self.assertEqual(
			[(p["bucket_id"], p["qty"]) for p in picks], [("OLD", 200), ("MID", 200), ("NEW", 140)]
		)
		self.assertEqual(left, 0)

	def test_only_the_total_is_rounded_off_the_youngest_bucket(self):
		item = _item([_bucket("OLD", 200, 5), _bucket("NEW", 200, 3)])
		picks, left = da._fifo_pick(item, 400, {})
		# 400 is not whole bunches of 18: 396 goes, the 4 come off the youngest bucket
		self.assertEqual([(p["bucket_id"], p["qty"]) for p in picks], [("OLD", 200), ("NEW", 196)])
		self.assertEqual(left, 4)

	def test_a_leftover_of_less_than_a_bunch_is_taken_first(self):
		item = _item([_bucket("REM", 2, 6), _bucket("NEW", 200, 3)])
		picks, _left = da._fifo_pick(item, 36, {})
		self.assertEqual([(p["bucket_id"], p["qty"]) for p in picks], [("REM", 2), ("NEW", 34)])

	def test_taken_counts_only_what_was_kept(self):
		taken = {}
		da._fifo_pick(_item([_bucket("OLD", 200, 5), _bucket("NEW", 200, 3)]), 400, taken)
		self.assertEqual(taken[("NEW", "Snowstorm", "52cm")], 196)

	def test_free_is_whole_bunches_over_all_buckets(self):
		item = _item([_bucket("A", 10, 5), _bucket("B", 10, 4)])
		self.assertEqual(da._free(item, {}), 18)

	def test_trim_keeps_lines_apart(self):
		rows = [
			{"sales_order_item": "L1", "bucket_id": "A", "qty": 20, "conversion_factor": 10},
			{"sales_order_item": "L2", "bucket_id": "B", "qty": 15, "conversion_factor": 10},
			{"sales_order_item": "L1", "bucket_id": "C", "qty": 5, "conversion_factor": 10},
		]
		kept, off = da._trim_to_bunches(rows)
		self.assertEqual([(r["bucket_id"], r["qty"]) for r in kept], [("A", 20), ("B", 10)])
		self.assertEqual(sorted((r["bucket_id"], cut) for r, cut in off), [("B", 5), ("C", 5)])

	def test_the_check_reads_the_line_total(self):
		doc = SimpleNamespace(items=[SimpleNamespace(name="L1", conversion_factor=18, item_code="Snowstorm")])
		sa._check_whole_bunches(
			doc, [{"sales_order_item": "L1", "qty": 2}, {"sales_order_item": "L1", "qty": 196}]
		)
		with self.assertRaises(Exception):
			sa._check_whole_bunches(doc, [{"sales_order_item": "L1", "qty": 200}])
