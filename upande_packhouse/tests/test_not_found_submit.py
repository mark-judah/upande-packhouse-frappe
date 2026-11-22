from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa

# A pick list with a bucket marked not found (no replacement): a mixed box waits, a single
# straight box goes on, more than one straight box goes on once one whole box is still here.
# The database is mocked.


def _opl(rows, **kw):
	return frappe._dict(name="OPL-1", table_ytkc=[frappe._dict(r) for r in rows], **kw)


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
	def _check(self, opl, lines, allocated, gone):
		with (
			patch.object(sa.frappe, "get_all", return_value=lines),
			patch.object(sa.frappe.db, "sql", return_value=list(gone.items())),
			patch.object(sa, "_cumulative_allocated_stems", side_effect=lambda s: allocated[s]),
		):
			return sa.not_found_blockers(opl)

	def test_nothing_not_found_never_blocks(self):
		opl = _opl([{"bucket": "B1", "sales_order_item": "L1", "not_found": 0}])
		self.assertEqual(self._check(opl, [_line("L1", 1, 200, 200)], {"L1": 200}, {}), [])

	def test_a_single_straight_box_goes_on_without_the_bucket(self):
		opl = _opl(
			[
				{"bucket": "B1", "sales_order_item": "L1", "not_found": 1},
				{"bucket": "B2", "sales_order_item": "L1", "issued": 1},
			]
		)
		self.assertEqual(self._check(opl, [_line("L1", 1, 200, 200)], {"L1": 200}, {"L1": 100}), [])

	def test_a_mixed_box_waits_for_a_replacement(self):
		opl = _opl([{"bucket": "B1", "sales_order_item": "L1", "not_found": 1}], mix_group="1")
		out = self._check(opl, [_line("L1", 1, 100, 100, mixed=1)], {"L1": 100}, {"L1": 50})
		self.assertIn("mixed box", out[0])

	def test_more_boxes_go_on_once_one_box_is_whole(self):
		opl = _opl([{"bucket": "B1", "sales_order_item": "L1", "not_found": 1}])
		# 3 boxes of 200: 600 allocated, 100 not found -> 500 here, two whole boxes
		self.assertEqual(self._check(opl, [_line("L1", 3, 200, 600)], {"L1": 600}, {"L1": 100}), [])

	def test_more_boxes_wait_while_no_box_is_whole(self):
		opl = _opl([{"bucket": "B1", "sales_order_item": "L1", "not_found": 1}])
		# 2 boxes of 200 but only 300 allocated, 150 not found -> 150 here, no whole box
		out = self._check(opl, [_line("L1", 2, 200, 400)], {"L1": 300}, {"L1": 150})
		self.assertIn("no whole box", out[0])
