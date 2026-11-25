"""At issue, a bucket's leftover smaller than one bunch of the line being issued comes off the shelf with it.

It used to be kept whenever any open order packed the variety in a smaller bunch (a mix's 3 or 4 stems): issued
Fuchsiana buckets kept 4 stems each on the shelf because one mix packs it in threes.
"""

from unittest.mock import patch

from frappe.tests import UnitTestCase

from upande_packhouse.mobile import api


class UnitTestIssueLeftoverBunch(UnitTestCase):
	def test_the_issuing_lines_bunch_is_the_threshold(self):
		with patch.object(api.frappe.db, "get_value", return_value=10) as gv:
			self.assertEqual(api._issue_bunch("SOI-1"), 10)
		gv.assert_called_once_with("Sales Order Item", "SOI-1", "conversion_factor")

	def test_no_bunch_size_counts_single_stems(self):
		with patch.object(api.frappe.db, "get_value", return_value=None):
			self.assertEqual(api._issue_bunch("SOI-1"), 1)
