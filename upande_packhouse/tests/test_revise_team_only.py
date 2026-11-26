"""Allocation Planning: a line's team can be revised even when its confirmation is full and covered.

A fully confirmed line, or one with nothing left to confirm, was never listed by Revise and a team change on it
was dropped (only lines with stems still to confirm could change team). A team change whose stems stay now
moves the line's confirmations to the new team without confirming its stems again.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.v2 import allocation_revise as rev


class UnitTestReviseTeamOnly(UnitTestCase):
	def _revise(self, rows, ordered=300, allocated=0, was=300, team_was="Team A"):
		line = frappe._dict(ordered_stems=ordered, allocated_stems=allocated)
		with (
			patch.object(rev.frappe, "only_for"),
			patch.object(rev.pg, "_changed"),
			patch.object(rev.pg, "_day", return_value="2026-11-27"),
			patch.object(
				rev.pg,
				"_planned_week",
				return_value={r["sales_order_item"]: {"real": was, "expected": 0} for r in rows},
			),
			patch.object(rev.pg, "_line", return_value=line),
			patch.object(rev.plans, "team_for_line", return_value=team_was),
			patch.object(rev, "_move_line_team") as move,
			patch.object(rev.pg, "confirm_lines") as confirm,
			patch.object(rev.pg, "_plan_name", return_value=None),
		):
			out = rev.revise_confirmations("2026-11-27", "Kapkolia", lines=rows)
		return out, move, confirm

	def test_a_full_line_moves_team_without_confirming_again(self):
		out, move, confirm = self._revise([{"sales_order_item": "L1", "stems": 300, "team": "Team B"}])
		move.assert_called_once_with("L1", "Team B", "Team A")
		confirm.assert_not_called()
		self.assertEqual((out["lines"], out["teams"]), (1, 1))

	def test_a_covered_line_with_nothing_left_moves_team(self):
		# fully allocated: nothing left to confirm, the suggestion is 0
		out, move, confirm = self._revise(
			[{"sales_order_item": "L1", "stems": 0, "team": "Team B", "team_only": 1}], allocated=300, was=0
		)
		move.assert_called_once()
		confirm.assert_not_called()

	def test_new_stems_and_team_are_confirmed_again(self):
		out, move, confirm = self._revise([{"sales_order_item": "L1", "stems": 200, "team": "Team B"}])
		move.assert_not_called()
		confirm.assert_called_once()
		self.assertEqual(out["teams"], 0)

	def test_same_team_changes_nothing(self):
		out, move, confirm = self._revise([{"sales_order_item": "L1", "stems": 300, "team": "Team A"}])
		move.assert_not_called()
		confirm.assert_not_called()
		self.assertEqual(out["lines"], 0)
