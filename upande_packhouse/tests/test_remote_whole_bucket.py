from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da

# Remote transfers request whole buckets: a bucket on a remote farm goes to one order, the stems that order
# does not take arrive at the hub free. A bucket on the sales farm's own shelf can still be shared.


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
	def test_a_remote_bucket_goes_to_one_order_only(self):
		taken = {}
		first, _ = da._fifo_pick(_item("SO-1", "L1", [_bucket("RB", 100, 0)]), 20, taken)
		second, left = da._fifo_pick(_item("SO-2", "L2", [_bucket("RB", 100, 0)]), 20, taken)
		self.assertEqual([p["qty"] for p in first], [20])
		self.assertEqual(second, [])
		self.assertEqual(left, 20)

	def test_the_same_order_can_take_more_of_its_remote_bucket(self):
		taken = {}
		da._fifo_pick(_item("SO-1", "L1", [_bucket("RB", 100, 0)]), 20, taken)
		more, _ = da._fifo_pick(_item("SO-1", "L3", [_bucket("RB", 100, 0)]), 30, taken)
		self.assertEqual([p["qty"] for p in more], [30])

	def test_a_bucket_on_the_sales_farm_is_still_shared(self):
		taken = {}
		da._fifo_pick(_item("SO-1", "L1", [_bucket("HB", 100, 1)]), 20, taken)
		second, _ = da._fifo_pick(_item("SO-2", "L2", [_bucket("HB", 100, 1)]), 20, taken)
		self.assertEqual([p["qty"] for p in second], [20])
