from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation import sales_allocation as sa1
from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa

# A pick list with a bucket marked not found (no replacement): a mixed box waits, a single straight box goes
# on, more than one straight box goes on once this pick list's own buckets still here fill one whole box.
# Nothing left to pack always waits. The database is mocked.


def _opl(rows, **kw):
	return frappe._dict(name="OPL-1", sales_order="SO-1", table_ytkc=[frappe._dict(r) for r in rows], **kw)


def _row(bucket, stems, line="L1", **kw):
	return {"bucket": bucket, "sales_order_item": line, "stock_qty": stems, **kw}


def _line(name, boxes, packrate, stems, mixed=0):
	return frappe._dict(
		name=name,
		stock_qty=stems,
		custom_number_of_boxes=boxes,
		custom_packrate=packrate,
		custom_mixed_box=mixed,
		custom_mixed_bunch=0,
	)


class UnitTestNotFoundSubmit(UnitTestCase):
	def _check(self, opl, lines):
		with patch.object(sa.frappe, "get_all", return_value=lines):
			return sa.not_found_blockers(opl)

	def test_nothing_not_found_never_blocks(self):
		opl = _opl([_row("B1", 200)])
		self.assertEqual(self._check(opl, [_line("L1", 1, 200, 200)]), [])

	def test_a_single_straight_box_goes_on_without_the_bucket(self):
		opl = _opl([_row("B1", 100, not_found=1), _row("B2", 100, issued=1)])
		self.assertEqual(self._check(opl, [_line("L1", 1, 200, 200)]), [])

	def test_nothing_left_to_pack_waits(self):
		opl = _opl([_row("B1", 200, not_found=1)])
		out = self._check(opl, [_line("L1", 1, 200, 200)])
		self.assertIn("nothing left", out[0])

	def test_a_mixed_box_waits_for_a_replacement(self):
		opl = _opl([_row("B1", 50, not_found=1), _row("B2", 50)], mix_group="1")
		out = self._check(opl, [_line("L1", 1, 100, 100, mixed=1)])
		self.assertIn("mixed box", out[0])

	def test_more_boxes_go_on_once_one_box_is_whole(self):
		# 3 boxes of 200: 500 of this pick list's stems still here, two whole boxes
		opl = _opl([_row("B1", 100, not_found=1), _row("B2", 300), _row("B3", 200)])
		self.assertEqual(self._check(opl, [_line("L1", 3, 200, 600)]), [])

	def test_more_boxes_wait_while_no_box_is_whole(self):
		opl = _opl([_row("B1", 150, not_found=1), _row("B2", 150)])
		out = self._check(opl, [_line("L1", 2, 200, 400)])
		self.assertIn("no whole box", out[0])

	def test_another_teams_pick_list_of_the_line_does_not_count(self):
		# the line is split 300/300 between two teams' pick lists; this one's only bucket is not found
		opl = _opl([_row("B1", 300, not_found=1)])
		out = self._check(opl, [_line("L1", 2, 300, 600)])
		self.assertTrue(out)

	def test_the_original_check_waits_for_a_mixed_box_too(self):
		opl = _opl([_row("B1", 50, not_found=1), _row("B2", 50)], mix_group="1", short_accepted=0)
		with patch.object(sa.frappe, "get_all", return_value=[_line("L1", 1, 100, 100, mixed=1)]):
			out = sa1.opl_submit_blockers(opl)
		self.assertTrue(any("mixed box" in b for b in out))
