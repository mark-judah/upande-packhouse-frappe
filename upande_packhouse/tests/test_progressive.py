from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse import progressive as pr

# Progressive Issuing: a draft pick list waiting for buckets is issued and packed by its complete boxes. The
# database is mocked.


def _row(box, bucket, **flags):
	return frappe._dict(name=f"{bucket}-{box}", bucket=bucket, custom_box_id=box, issued=0, **flags)


def _settings(on=1, start=2):
	return SimpleNamespace(
		get=lambda k: {"progressive_issuing": on, "progressive_issuing_from_boxes": start}.get(k)
	)


class UnitTestProgressiveIssuing(UnitTestCase):
	def _read(self, rows, on=1, start=2, docstatus=0, short=False):
		with (
			patch.object(pr.frappe, "get_cached_doc", return_value=_settings(on, start)),
			patch.object(pr.frappe.db, "get_value", return_value=docstatus),
			patch.object(pr, "_short", return_value=short),
		):
			return pr.readiness("OPL-TEST", rows)

	def test_a_box_is_complete_only_when_every_row_is_shelved(self):
		rows = [
			_row(1, "A"),
			_row(1, "B", awaiting_transfer=1),
			_row(2, "C"),
			_row(3, "D", in_transit=1),
			_row(4, "E", loaded_in_trolley=1, shelved=0),
			_row(5, "F", not_found=1),
		]
		st = self._read(rows)
		self.assertEqual(
			st["boxes"],
			{1: "awaiting transfer", 2: "", 3: "on a truck", 4: "loaded, not shelved", 5: "not found"},
		)
		self.assertEqual((st["total"], st["ready"]), (5, 1))

	def test_issuing_opens_at_the_threshold_or_with_every_box_of_a_small_order(self):
		rows = [_row(1, "A"), _row(2, "B"), _row(3, "C", awaiting_transfer=1)]
		self.assertTrue(self._read(rows, start=2)["open"])
		self.assertFalse(self._read(rows, start=3)["open"])
		small = [_row(1, "A"), _row(2, "B")]
		self.assertTrue(self._read(small, start=10)["open"])  # fewer boxes than the setting: all of them

	def test_closed_when_the_setting_is_off_or_the_pick_list_is_submitted(self):
		rows = [_row(1, "A"), _row(2, "B")]
		self.assertFalse(self._read(rows, on=0)["open"])
		self.assertFalse(self._read(rows, docstatus=1)["open"])
		self.assertFalse(self._read([_row(1, "A", awaiting_transfer=1)])["open"])

	def test_closed_while_its_lines_are_short_of_stems(self):
		# every row shelved, but the lines are not fully allocated: a box can still be missing stems
		st = self._read([_row(1, "A")], short=True)
		self.assertTrue(st["short"])
		self.assertFalse(st["open"])
