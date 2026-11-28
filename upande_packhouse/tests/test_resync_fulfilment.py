from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse import allocation_plan as ap

# A line's confirmations are "Fulfilled" by its allocations. An allocation cancelled (unallocated, released) gives
# its stems back, so a confirmation never holds the line as planned with nothing allocated behind it.


def _conf(name, source, stems, done, status, creation):
	return frappe._dict(
		name=name, source=source, stems=stems, fulfilled_stems=done, status=status, creation=creation
	)


class UnitTestResyncFulfilment(UnitTestCase):
	def _run(self, allocated, confs):
		set_ = {}
		with (
			patch.object(ap.frappe.db, "sql", return_value=[[allocated]]),
			patch.object(ap.frappe, "get_all", return_value=confs),
			patch.object(ap.frappe.db, "set_value", side_effect=lambda dt, n, v: set_.update({n: v})),
		):
			ap.resync_fulfilment("SOI-1")
		return set_

	def test_nothing_allocated_gives_every_stem_back(self):
		out = self._run(
			0,
			[
				_conf("AL-1", "Real", 600, 600, "Fulfilled", "2026-11-28 11:00"),
				_conf("AL-2", "Virtual", 1560, 1560, "Fulfilled", "2026-11-28 11:01"),
			],
		)
		self.assertEqual(out["AL-1"], {"fulfilled_stems": 0.0, "status": "Reserved"})
		self.assertEqual(out["AL-2"], {"fulfilled_stems": 0.0, "status": "Planned"})

	def test_what_is_still_allocated_stays_fulfilled_stock_first(self):
		out = self._run(
			700,
			[
				_conf("AL-2", "Virtual", 1560, 1560, "Fulfilled", "2026-11-28 11:01"),
				_conf("AL-1", "Real", 600, 600, "Fulfilled", "2026-11-28 11:00"),
			],
		)
		self.assertNotIn("AL-1", out)
		self.assertEqual(out["AL-2"], {"fulfilled_stems": 100.0, "status": "Partly Fulfilled"})

	def test_matching_allocations_change_nothing(self):
		self.assertEqual(self._run(600, [_conf("AL-1", "Real", 600, 600, "Fulfilled", "x")]), {})
