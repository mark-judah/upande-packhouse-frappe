"""Mixes go on pick lists in complete boxes, and each pick list's Packing Guide lists only its own boxes."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse import packing_guide
from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa


def _line(name, item, boxes=21, rate=80, group="1"):
	return frappe._dict(
		name=name,
		item_code=item,
		parent="SO-TEST",
		custom_mixed_box=1,
		custom_mix_group=group,
		custom_mixed_bunch=0,
		custom_bunch_group="",
		custom_number_of_boxes=boxes,
		custom_packrate_mixed_box=rate,
	)


class UnitTestWholeMixBoxes(UnitTestCase):
	def setUp(self):
		self.lines = [_line("L1", "Aqua"), _line("L2", "Athena"), _line("L3", "Moonwalk")]
		self.so = SimpleNamespace(items=self.lines)

	def _run(self, asked, placed=None):
		allocations = [
			{"sales_order_item": soi, "qty": q, "bucket_id": "B" + soi} for soi, q in asked.items()
		]
		with patch.object(sa, "_placed_stems", side_effect=lambda soi: (placed or {}).get(soi, 0.0)):
			out, notes = sa._whole_mix_boxes(self.so, allocations)
		return {a["sales_order_item"]: a["qty"] for a in out}, notes

	def test_every_variety_is_floored_to_the_scarcest(self):
		out, notes = self._run({"L1": 400, "L2": 250, "L3": 800})
		# Athena fills 3 boxes of 80: every variety gets 3 boxes
		self.assertEqual(out, {"L1": 240, "L2": 240, "L3": 240})
		self.assertTrue(notes)

	def test_continues_from_what_is_already_on_pick_lists(self):
		out, _notes = self._run({"L1": 160, "L2": 160, "L3": 80}, placed={"L1": 800, "L2": 800, "L3": 800})
		# 10 boxes placed; Moonwalk brings 1 more box
		self.assertEqual(out, {"L1": 80, "L2": 80, "L3": 80})

	def test_no_complete_box_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._run({"L1": 160})

	def test_straight_lines_are_untouched(self):
		straight = frappe._dict(
			name="S1", item_code="Wham", custom_mixed_box=0, custom_mixed_bunch=0, custom_number_of_boxes=3
		)
		so = SimpleNamespace(items=[straight])
		with patch.object(sa, "_placed_stems", return_value=0.0):
			out, notes = sa._whole_mix_boxes(so, [{"sales_order_item": "S1", "qty": 130, "bucket_id": "B"}])
		self.assertEqual(out[0]["qty"], 130)
		self.assertEqual(notes, [])


class UnitTestCarriedBoxes(UnitTestCase):
	def test_a_later_pick_list_lists_only_its_own_boxes(self):
		items = [_line("L1", "Aqua"), _line("L2", "Athena")]
		opl = frappe._dict(
			table_ytkc=[
				frappe._dict(sales_order_item="L1", custom_box_id=60),
				frappe._dict(sales_order_item="L1", custom_box_id=61),
				frappe._dict(sales_order_item="L2", custom_box_id=60),
				frappe._dict(sales_order_item="L2", custom_box_id=61),
			]
		)
		# the mix starts after 42 boxes of the order's other lines: order box 60 is the mix's box 18
		with patch(
			"upande_packhouse.server_scripts.create_mixed_box_picklist.line_box_offset", return_value=42
		):
			self.assertEqual(packing_guide._carried_boxes(opl, items, 21), {18, 19})

	def test_no_rows_yet_lists_every_box(self):
		self.assertIsNone(
			packing_guide._carried_boxes(frappe._dict(table_ytkc=[]), [_line("L1", "Aqua")], 21)
		)
