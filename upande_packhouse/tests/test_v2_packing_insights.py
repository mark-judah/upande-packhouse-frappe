"""Packing Insights v2: the finish-time arithmetic, the filters, and the read-only endpoints.

The pure-function tests need no data. The *_site tests read the local site read-only
(nothing written or committed).
"""

from datetime import datetime, timedelta

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.v2 import packing_insights as pi


def _team(**kw):
	base = {
		"team": "Team A",
		"cells": {},
		"queue": {"stems": 0.0, "orders": 0},
		"exp_boxes": 0,
		"packed_boxes": 0,
		"pack": {},
		"issue": {},
	}
	base.update(kw)
	return base


class TestFinishTime(UnitTestCase):
	def test_nothing_left_is_done(self):
		r = pi._finish(_team())
		self.assertEqual(r["state"], "done")
		self.assertEqual(r["eta_min"], 0.0)

	def test_work_and_no_packing_today_has_no_estimate(self):
		r = pi._finish(_team(queue={"stems": 500.0, "orders": 2}))
		self.assertEqual(r["state"], "nodata")
		self.assertIsNone(r["eta_min"])

	def test_packing_speed_sets_the_finish(self):
		# 6,000 stems left, 100 stems a minute over 120 minutes -> 60 minutes
		pk = {"stems": 12000.0, "boxes": 30.0, "mins": 120.0, "people": 3}
		r = pi._finish(_team(queue={"stems": 6000.0, "orders": 3}, pack=pk))
		self.assertEqual(r["state"], "ok")
		self.assertAlmostEqual(r["eta_min"], 60.0)
		self.assertAlmostEqual(r["bph"], 15.0)

	def test_slower_of_issuing_and_packing_wins(self):
		pk = {"stems": 12000.0, "boxes": 30.0, "mins": 120.0}
		iss = {"stems": 1200.0, "mins": 120.0}  # 10 a minute
		t = _team(
			queue={"stems": 1000.0, "orders": 1},
			cells={"issue": {"stems": 2000.0, "orders": 4}},
			pack=pk,
			issue=iss,
		)
		r = pi._finish(t)
		# packing: 3,000 / 100 = 30 min; issuing: 2,000 / 10 = 200 min
		self.assertAlmostEqual(r["eta_min"], 200.0)

	def test_work_to_issue_without_issuing_speed_has_no_estimate(self):
		pk = {"stems": 12000.0, "boxes": 30.0, "mins": 120.0}
		r = pi._finish(_team(cells={"issue": {"stems": 100.0, "orders": 1}}, pack=pk))
		self.assertEqual(r["state"], "nodata")

	def test_a_team_that_just_started_is_measured_over_thirty_minutes(self):
		pk = {"stems": 600.0, "boxes": 2.0, "mins": 5.0}
		r = pi._finish(_team(queue={"stems": 600.0, "orders": 1}, pack=pk))
		self.assertAlmostEqual(r["eta_min"], 30.0)  # 600 / (600 / 30)

	def test_blocked_stems_are_reported_not_estimated(self):
		pk = {"stems": 12000.0, "boxes": 30.0, "mins": 120.0}
		t = _team(
			queue={"stems": 100.0, "orders": 1},
			cells={"transfer": {"stems": 900.0, "orders": 2}, "draft": {"stems": 50.0, "orders": 1}},
			pack=pk,
		)
		r = pi._finish(t)
		self.assertEqual(r["blocked"], 950.0)
		self.assertEqual(r["to_pack"], 100.0)

	def test_only_blocked_work_left_is_done_with_blocked_stems(self):
		r = pi._finish(_team(cells={"transfer": {"stems": 900.0, "orders": 2}}))
		self.assertEqual(r["state"], "done")
		self.assertEqual(r["blocked"], 900.0)


class TestHelpers(UnitTestCase):
	def test_median(self):
		self.assertIsNone(pi._median([]))
		self.assertEqual(pi._median([3, 1, 2]), 2)
		self.assertEqual(pi._median([1, 2, 3, 4]), 2.5)

	def test_minutes(self):
		a = datetime(2026, 10, 25, 9, 0)
		self.assertEqual(pi._minutes(a, a + timedelta(minutes=90)), 90.0)
		self.assertEqual(pi._minutes(None, a), 0.0)

	def test_default_range_is_tomorrow_and_a_swapped_range_is_fixed(self):
		a = pi._args(None, None, None, None, None, "all", None)
		tomorrow = str(frappe.utils.add_days(frappe.utils.nowdate(), 1))
		self.assertEqual((a["from_date"], a["to_date"]), (tomorrow, tomorrow))
		b = pi._args("2026-10-26", "2026-10-25", None, None, None, "all", None)
		self.assertEqual((b["from_date"], b["to_date"]), ("2026-10-25", "2026-10-26"))

	def test_scope_applies_every_filter(self):
		a = pi._args("2026-10-25", "2026-10-26", "Ravine", "Torongo", "Team A", "spray", None)
		where, params = pi._scope(a)
		self.assertIn("so.delivery_date BETWEEN", where)
		self.assertIn("IN %(ph_farms)s", where)
		self.assertIn("o.team = %(ph_team)s", where)
		self.assertEqual(params["ph_team"], "Team A")
		self.assertEqual(params["ph_farms"], ("Torongo",))

	def test_rose_type_is_read_from_the_order_lines_not_the_pick_list(self):
		# Order Pick List.item_group is empty on live data, so filtering on it empties the page
		a = pi._args("2026-10-25", "2026-10-26", None, None, None, "spray", None)
		where, _ = pi._scope(a)
		self.assertNotIn("o.item_group", where)
		self.assertTrue("sx.item_group IN" in where or "1=0" in where)

	def test_scope_without_dates_leaves_the_range_out(self):
		a = pi._args("2026-10-25", "2026-10-26", None, None, None, "all", None)
		where, params = pi._scope(a, dates=False)
		self.assertNotIn("delivery_date", where)
		self.assertNotIn("ph_from", params)

	def test_no_team_filter_matches_pick_lists_without_a_team(self):
		a = pi._args("2026-10-25", "2026-10-26", None, None, "No team", "all", None)
		where, params = pi._scope(a)
		self.assertIn("IFNULL(o.team, '') = ''", where)
		self.assertNotIn("ph_team", params)

	def test_region_and_farm_that_disagree_match_nothing(self):
		a = pi._args("2026-10-25", "2026-10-26", "Karen", "Torongo", None, "all", None)
		_, params = pi._scope(a)
		self.assertEqual(params["ph_farms"], ("",))

	def test_top_queue_keeps_the_largest_per_team(self):
		rows = [{"team": "A", "opl": f"O{i}", "due": "2026-10-25", "stems": float(i)} for i in range(1, 9)]
		rows.append({"team": "B", "opl": "OB", "due": "2026-10-25", "stems": 1.0})
		top = pi._top_queue(rows)
		self.assertEqual([r["opl"] for r in top if r["team"] == "A"], ["O8", "O7", "O6", "O5", "O4"])
		self.assertEqual(len([r for r in top if r["team"] == "B"]), 1)


class TestSite(UnitTestCase):
	"""Read-only against the local site."""

	def test_insights_shape(self):
		r = pi.get_packing_insights()
		self.assertTrue(r["success"])
		for k in (
			"teams",
			"kpis",
			"drill",
			"qdrill",
			"remote",
			"unalloc",
			"issuers",
			"line",
			"waits",
			"as_of",
		):
			self.assertIn(k, r)
		for t in r["teams"]:
			self.assertIn(t["state"], ("ok", "done", "nodata"))
		k = r["kpis"]
		self.assertEqual(k["expected_boxes"], sum(t["exp_boxes"] for t in r["teams"]))
		self.assertEqual(k["packed_boxes"], sum(t["packed_boxes"] for t in r["teams"]))

	def test_every_kpi_follows_the_team_filter(self):
		allr = pi.get_packing_insights()
		if not allr["teams"]:
			self.skipTest("no live work on this site")
		name = allr["teams"][0]["team"]
		one = pi.get_packing_insights(team=name)
		self.assertEqual([t["team"] for t in one["teams"]], [name])
		self.assertEqual(one["kpis"]["expected_boxes"], allr["teams"][0]["exp_boxes"])

	def test_a_farm_nobody_uses_empties_the_page(self):
		r = pi.get_packing_insights(farm="No Such Farm")
		self.assertEqual(r["teams"], [])
		self.assertEqual(r["kpis"]["held_stems"], 0)

	def test_history_shape(self):
		r = pi.get_speed_history()
		self.assertTrue(r["success"])
		self.assertIn("days", r)
		self.assertIn("due_boxes", r)
