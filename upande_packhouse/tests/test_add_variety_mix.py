"""Add variety on a mixed box or bunch: the new variety takes the line's remainder beside it in the same boxes
(A + B = the line's stems), whole bunches or not, and both are held at once. The mix is never split.

XPOL 54826 (26-11): adding Madam Red for Furiosa split mix 1 into 13 + 8 boxes and mixed bunch ::7 into
20 + 1 (::8), and the new boxes were never filled.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da


def _sub(splittable, needed=400, held=380, own_free=7, free=100):
	return {
		"variety": "Furiosa",
		"splittable": splittable,
		"needed": needed,
		"held": held,
		"own_free": own_free,
		"add_stems": 20,
		"per_box": 20,
		"options": [{"variety": "Madam Red", "adds": 20, "free": free, "mode": "boxes"}],
		"color_options": [],
	}


class UnitTestAddVarietyMix(UnitTestCase):
	def _add(self, sub):
		with (
			patch.object(da, "get_shortage_substitutes", return_value=sub),
			patch.object(da.frappe.db, "get_value", return_value="SO-T"),
			patch.object(
				da,
				"_share_split",
				return_value={"parts": [{"sales_order_item": "NEW", "variety": "Madam Red", "stems": 13}]},
			) as share,
			patch.object(da, "_hold_lines", return_value=20) as hold,
			patch.object(da, "_split_commit"),
			patch.object(da.sa, "substitute_variety") as substitute,
			patch.object(da, "split_substitute") as split,
			patch.object(da, "_split_mix") as mix,
		):
			out = da.add_variety("SOI", "Kapkolia", "Madam Red")
		return out, share, hold, substitute, split, mix

	def test_the_new_variety_takes_the_remainder_in_the_same_boxes(self):
		out, share, hold, substitute, split, mix = self._add(_sub(False))
		# 380 held + 7 free stay Furiosa; Madam Red takes 13, not a whole bunch
		share.assert_called_once_with("SOI", "Madam Red", 13, message=out["summary"])
		hold.assert_called_once_with("Kapkolia", "SO-T", ["SOI", "NEW"])
		self.assertEqual(
			out["summary"],
			"Added Madam Red 13 stems to Furiosa, which required 400: Furiosa fills 387, Madam Red fills the remaining 13.",
		)
		substitute.assert_not_called()
		split.assert_not_called()
		mix.assert_not_called()

	def test_not_enough_of_the_new_variety_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._add(_sub(False, free=10))

	def test_a_line_that_fills_itself_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._add(_sub(False, held=390, own_free=20))

	def test_a_straight_line_still_splits(self):
		with (
			patch.object(da, "get_shortage_substitutes", return_value=_sub(True)),
			patch.object(da, "_share_split") as share,
			patch.object(da, "split_substitute", return_value={}) as split,
		):
			da.add_variety("SOI", "Kapkolia", "Madam Red")
		split.assert_called_once_with("SOI", "Kapkolia", [{"variety": "Madam Red", "stems": 20}])
		share.assert_not_called()

	def test_picks_can_end_off_a_whole_bunch(self):
		item = {
			"sales_order_item": "NEW",
			"sales_order": "SO-T",
			"item_code": "Madam Red",
			"conversion_factor": 5,
			"batches": [
				{"bucket_id": "B1", "available_qty": 9, "stem_length": "42cm", "is_sales_shelf": 1},
				{"bucket_id": "B2", "available_qty": 9, "stem_length": "42cm", "is_sales_shelf": 1},
			],
		}
		with patch.object(da, "_downgrade_reason", return_value=""):
			picks, left = da._fifo_pick(item, 13, {}, whole_bunches=False)
		self.assertEqual([p["qty"] for p in picks], [9, 4])
		self.assertEqual(left, 0)
