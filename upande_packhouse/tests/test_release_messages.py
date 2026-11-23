from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as day

# A release says what happened to every order it was given: an order that does not go out says why (no team,
# no complete box, buckets gone, the order cancelled), an order that goes out short says what was left out, and
# a failure always has a message. The database is mocked.


def _order(so, teams=("Team B",)):
	return {
		"sales_order": so,
		"order_name": so,
		"allocations": [{"bucket_id": "B1", "qty": 100}],
		"teams": dict.fromkeys(teams, 1),
		"boxes": 1,
		"stems": 100,
	}


class UnitTestReleaseMessages(UnitTestCase):
	def _release(self, orders, docstatus=1, teams=None, release=True, impl=None, gone=None):
		def attach(rows):
			for r in rows:
				r.boxes_full, r.boxes_required = 2, 5

		with ExitStack() as s:
			p = s.enter_context
			p(patch.object(day, "_proposal", return_value={"orders": orders}))
			p(patch.object(day, "_cancelled", return_value=False))
			p(patch.object(day, "_publish"))
			p(patch.object(day, "_drop_gone_buckets", side_effect=gone or (lambda o, loc: [])))
			p(patch.object(day, "_check_location"))
			p(
				patch.object(
					day, "_fill_teams", side_effect=lambda o, loc: o["teams"] if teams is None else teams
				)
			)
			p(patch.object(day, "_split_release", return_value=([], [], {})))
			p(
				patch.object(
					day, "_gate_release", return_value=([{"qty": 100}] if release else [], [], {"x": 1})
				)
			)
			p(patch.object(day, "_release_parts", side_effect=lambda r: [r]))
			p(patch.object(day, "_summary", return_value={}))
			p(patch.object(day, "_shortages", return_value=[]))
			p(patch.object(day, "_keep_shortages"))
			p(patch.object(day, "_pending", return_value=[]))
			p(patch.object(day.sa, "_v2_rules", return_value={"enforce_plan": 0}))
			p(patch.object(day.sa, "_attach_boxes", side_effect=attach))
			p(patch.object(day.sa, "close_pending"))
			p(patch.object(day.sa, "_allocate_stock_with_buckets_impl", side_effect=impl or (lambda *a: {})))
			p(patch.object(day.frappe.db, "get_value", return_value=docstatus))
			p(patch.object(day.frappe.db, "commit"))
			p(patch.object(day.frappe.db, "savepoint"))
			p(patch.object(day.frappe.db, "rollback"))
			log = p(patch.object(day.frappe, "log_error"))
			out = day._run_confirm("Kapkolia", "2026-11-24", "2026-11-24", None, None, "u@x")
		return out, log

	def test_a_cancelled_order_says_so_instead_of_vanishing(self):
		out, _ = self._release([_order("SO-1")], docstatus=2)
		self.assertIn("no longer submitted", out["held_back"]["SO-1"])
		self.assertEqual(out["saved"], 0)

	def test_an_order_with_no_team_says_so(self):
		out, _ = self._release([_order("SO-1")], teams={})
		self.assertEqual(out["waiting"], ["SO-1"])
		self.assertIn("No packing team", out["held_back"]["SO-1"])

	def test_an_order_with_no_complete_box_gives_its_count(self):
		out, _ = self._release([_order("SO-1")], release=False)
		self.assertIn("2 of 5 boxes", out["held_back"]["SO-1"])

	def test_buckets_gone_from_the_shelf_are_named(self):
		o = _order("SO-1")

		def gone(order, location):
			order["allocations"] = []
			return ["LL2ZG7"]

		out, _ = self._release([o], gone=gone)
		self.assertIn("LL2ZG7", out["held_back"]["SO-1"])

	def test_what_a_release_left_out_is_reported(self):
		note = "Athena: skipped bucket X1 (90 stems), the line's boxes are full."
		out, _ = self._release([_order("SO-1")], impl=lambda *a: {"fit_notes": [note]})
		self.assertEqual(out["saved"], 1)
		self.assertEqual(out["notes"], {"SO-1": [note]})

	def test_a_failure_with_no_message_still_says_what_failed_and_is_logged(self):
		def boom(*a):
			raise KeyError()

		out, log = self._release([_order("SO-1")], impl=boom)
		self.assertIn("KeyError", out["errors"]["SO-1"])
		log.assert_called()

	def test_the_live_message_carries_the_reasons(self):
		self.assertIn("held_back", day.LIGHT)
		self.assertIn("notes", day.LIGHT)

	def test_why_strips_html(self):
		self.assertEqual(day._why(frappe.ValidationError("<b>Overpacked</b>")), "Overpacked")

	def _reason(self, split, allow=1, pct=80):
		ps = frappe._dict(allow_partial_allocation=allow, partial_allocation_release_percent=pct)
		with patch.object(day.frappe, "get_cached_doc", return_value=ps):
			return day._hold_reason(split, 2, 10)

	def test_held_by_the_release_percentage_says_so_not_no_complete_box(self):
		why = self._reason(([{"qty": 40}], [], {("mix", "1"): 5}))
		self.assertIn("7 of 10 boxes would be complete (70%)", why)
		self.assertIn("release percentage is 80", why)

	def test_held_because_partial_allocation_is_off(self):
		why = self._reason(([{"qty": 40}], [], {("mix", "1"): 5}), allow=0)
		self.assertIn("partial allocation is off", why)

	def test_no_complete_box_at_all(self):
		self.assertIn("No complete box yet (2 of 10", self._reason(([], [], {})))
