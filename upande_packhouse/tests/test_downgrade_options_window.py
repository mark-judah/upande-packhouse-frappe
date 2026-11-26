"""Allocate all's downgrade step offers only stems that are really free.

Each order's view of a bucket leaves out what OTHER orders' proposals hold, not its own. Across the whole
window the step kept the first view it read: when that was the view of the order holding the bucket, the
bucket was offered as a downgrade to another order, and Allocate downgrades (reading that order's own view)
found no room and allocated nothing. Auto-allocate for one order reads one view, so it worked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da


def _item(soi, so, length, left, batches):
	return {
		"sales_order_item": soi,
		"sales_order": so,
		"item_code": "Giselle",
		"required_length": length,
		"pending_stock_qty": left,
		"total_allocated_qty": 0,
		"conversion_factor": 20,
		"batches": batches,
	}


def _bucket(bucket, length, avail, status):
	return {
		"bucket_id": bucket,
		"stem_length": length,
		"available_qty": avail,
		"length_status": status,
		"downgrade_approval": "requires_approval" if status == "downgrade" else "",
		"is_sales_shelf": 1,
	}


class UnitTestDowngradeOptionsWindow(UnitTestCase):
	def _options(self, own):
		x = frappe._dict(name="SO-X", custom_order_name="X", customer="C")
		y = frappe._dict(name="SO-Y", custom_order_name="Y", customer="C")
		# X ordered 82cm and its proposal holds all 200 of bucket B82 (its view does not subtract its own hold)
		x_item = _item("X1", "SO-X", "82cm", 0, [_bucket("B82", "82cm", 200, "exact")])
		x_item["total_allocated_qty"] = 0
		# Y ordered 62cm: in its view X's hold is subtracted from B82; F82 is a free 82cm bucket
		y_item = _item(
			"Y1",
			"SO-Y",
			"62cm",
			100,
			[_bucket("B82", "82cm", 0, "downgrade"), _bucket("F82", "82cm", 60, "downgrade")],
		)
		lines = {"SO-X": (x, [x_item]), "SO-Y": (y, [y_item])}
		with patch.object(da, "_own_pending_by_bucket", return_value=own):
			return da._downgrade_options(lines, {}, {})

	def test_a_bucket_another_order_holds_is_not_offered(self):
		out = self._options({("SO-X", "B82", "Giselle", "82cm"): 200})
		self.assertEqual(len(out), 1)
		self.assertEqual([b["bucket_id"] for b in out[0]["buckets"]], ["F82"])
		self.assertEqual(out[0]["stems"], 60)

	def test_held_stems_count_against_the_tightest_view(self):
		# even without the own-proposal read, Y's view (0 free) wins over X's (200)
		out = self._options({})
		self.assertEqual([b["bucket_id"] for b in out[0]["buckets"]], ["F82"])
