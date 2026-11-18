from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da

# Remote transfers request whole buckets: a bucket on a remote farm is allocated whole to one order line, never
# part of it. A bucket on the sales farm's own shelf can still be shared.


def _item(sales_order, soi, batches):
	return {
		"sales_order": sales_order,
		"sales_order_item": soi,
		"item_code": "Rose",
		"conversion_factor": 10,
		"batches": batches,
	}


def _bucket(bucket_id, stems, sales_shelf):
	return {
		"bucket_id": bucket_id,
		"stem_length": "52cm",
		"available_qty": stems,
		"is_sales_shelf": sales_shelf,
		"length_status": "exact",
	}


class UnitTestRemoteWholeBucket(UnitTestCase):
	def test_a_remote_bucket_goes_whole_to_one_order_only(self):
		taken = {}
		first, _ = da._fifo_pick(_item("SO-1", "L1", [_bucket("RB", 100, 0)]), 100, taken)
		second, left = da._fifo_pick(_item("SO-2", "L2", [_bucket("RB", 100, 0)]), 100, taken)
		self.assertEqual([p["qty"] for p in first], [100])
		self.assertEqual(second, [])
		self.assertEqual(left, 100)

	def test_a_remote_bucket_bigger_than_the_need_is_left(self):
		taken = {}
		picks, left = da._fifo_pick(
			_item("SO-1", "L1", [_bucket("BIG", 100, 0), _bucket("SMALL", 20, 0)]), 30, taken
		)
		self.assertEqual([(p["bucket_id"], p["qty"]) for p in picks], [("SMALL", 20)])
		self.assertEqual(left, 10)

	def test_a_bucket_on_the_sales_farm_is_still_shared(self):
		taken = {}
		da._fifo_pick(_item("SO-1", "L1", [_bucket("HB", 100, 1)]), 20, taken)
		second, _ = da._fifo_pick(_item("SO-2", "L2", [_bucket("HB", 100, 1)]), 20, taken)
		self.assertEqual([p["qty"] for p in second], [20])
