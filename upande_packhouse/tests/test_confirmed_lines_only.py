from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa

# Sales Allocation lists only what Allocation Planning confirmed: a straight line on its own, a mix as one.


def _line(soi, **k):
	return frappe._dict(sales_order_item=soi, parent="SO-1", **k)


def _conf(status, reason=""):
	return frappe._dict(status=status, release_reason=reason, team="T")


class UnitTestConfirmedLinesOnly(UnitTestCase):
	def _run(self, items, confs, allocated=()):
		with (
			patch.object(sa, "_ac_rows", side_effect=lambda sois: {s: confs.get(s, []) for s in sois}),
			patch.object(sa.frappe.db, "sql_list", return_value=list(allocated)),
		):
			return [i.sales_order_item for i in sa._confirmed_lines_only(items)]

	def test_an_unconfirmed_straight_line_is_not_listed(self):
		items = [_line("A"), _line("B")]
		out = self._run(items, {"A": [_conf("Planned")], "B": [_conf("Released", "Manual")]})
		self.assertEqual(out, ["A"])

	def test_a_line_already_allocated_stays_listed(self):
		self.assertEqual(self._run([_line("B")], {}, allocated=["B"]), ["B"])

	def test_released_once_allocated_counts_as_confirmed(self):
		self.assertEqual(self._run([_line("A")], {"A": [_conf("Released", "Allocated")]}), ["A"])

	def test_a_mix_is_listed_whole_when_any_line_is_confirmed(self):
		mix = dict(custom_mixed_bunch=1, custom_bunch_group="MIX 1::1", custom_number_of_boxes=1)
		items = [_line("M1", **mix), _line("M2", **mix), _line("S")]
		self.assertEqual(self._run(items, {"M1": [_conf("Reserved")]}), ["M1", "M2"])
