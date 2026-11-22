"""A confirmation saved twice (double click) or above the order never makes allocation ask for more than was ordered.

05209 (2026-11-21): four Confirm clicks wrote four plans per line, and the allocation page aimed at 4x the order.

Run:  bench --site kaitet.local run-tests --app upande_packhouse --module upande_packhouse.tests.test_confirmation_duplicates
"""

import unittest
from unittest.mock import patch

import frappe

from upande_packhouse import allocation_plan
from upande_packhouse.box_groups import box_keys
from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa


def _line(name, stock_qty, boxes, packrate, mixed_box=0, mixed_bunch=0, ordered_quantity=0):
	return frappe._dict(
		name=name,
		qty=stock_qty,
		stock_qty=stock_qty,
		conversion_factor=1,
		custom_ordered_quantity=ordered_quantity,
		custom_number_of_boxes=boxes,
		custom_packrate=packrate,
		custom_packrate_mixed_box=packrate,
		custom_mixed_box=mixed_box,
		custom_mixed_bunch=mixed_bunch,
	)


def _planned(plans, lines):
	"""planned_stems over made-up plans {line: stems} and order lines."""
	with (
		patch.object(sa.frappe.db, "sql", return_value=list(plans.items())),
		patch.object(sa.frappe, "get_all", return_value=lines),
	):
		return sa.planned_stems(list(plans))


class TestPlannedStemsCap(unittest.TestCase):
	def test_plan_saved_four_times_aims_at_the_order(self):
		# 05209 line 1: 420 ordered, four plans of 420
		out = _planned({"L1": 1680.0}, [_line("L1", 420, 21, 20, mixed_bunch=1)])
		self.assertEqual(out["L1"], 420)

	def test_plan_above_a_reduced_order_aims_at_the_line_stems(self):
		# 05243: confirmed 280 when the order said 280, the order is now 240 (custom_ordered_quantity stale)
		out = _planned({"L1": 280.0}, [_line("L1", 240, 12, 20, ordered_quantity=280)])
		self.assertEqual(out["L1"], 240)

	def test_plan_short_of_a_box_still_rounds_up_to_the_box(self):
		out = _planned({"L1": 48.0}, [_line("L1", 600, 2, 300)])
		self.assertEqual(out["L1"], 300)

	def test_plan_within_the_order_is_kept(self):
		out = _planned({"L1": 900.0}, [_line("L1", 1200, 4, 300)])
		self.assertEqual(out["L1"], 900)


class TestConfirmedStemsMirror(unittest.TestCase):
	def test_mirror_keeps_one_row_per_line_and_farm(self):
		deleted, updated = [], []
		with (
			patch.object(allocation_plan.frappe, "get_all", return_value=["CS1", "CS2", "CS3"]),
			patch.object(
				allocation_plan.frappe.db, "delete", side_effect=lambda dt, f: deleted.append(f["name"])
			),
			patch.object(
				allocation_plan.frappe.db, "set_value", side_effect=lambda *a, **k: updated.append(a[1])
			),
		):
			allocation_plan._sync_confirmed_stems("SO-1", "L1", "Kapkolia", 420)
		self.assertEqual(deleted, ["CS2", "CS3"])
		self.assertEqual(updated, ["CS1"])


class TestMixBoxKeys(unittest.TestCase):
	def test_mixed_bunches_and_mixed_box_of_a_spec_are_one_box(self):
		# 05209: bunch groups ::6 and ::7 and mix group 1 of COOP-00152-12 42CM, 21 boxes each
		def ln(name, **kw):
			base = dict(
				sales_order_item=name,
				custom_mix_name="COOP-00152-12  42CM",
				required_length="42cm",
				custom_number_of_boxes=21,
				custom_mixed_box=0,
				custom_mixed_bunch=0,
				custom_mix_group="",
				custom_bunch_group="",
			)
			base.update(kw)
			return base

		lines = [
			ln("B6", custom_mixed_bunch=1, custom_bunch_group="COOP-00152-12  42CM::6"),
			ln("B7", custom_mixed_bunch=1, custom_bunch_group="COOP-00152-12  42CM::7"),
			ln("MB", custom_mixed_box=1, custom_mix_group="1"),
		]
		keys = box_keys(lines)
		self.assertEqual(keys["B6"], keys["MB"])
		self.assertEqual(keys["B7"], keys["MB"])


class TestPickListTeam(unittest.TestCase):
	"""A pick list is packed by the team its lines are released or planned for (05208, 05254, 05262: 2026-11-22)."""

	def _team(self, rows, planned, teams=None):
		with (
			patch.object(sa.frappe.db, "sql", return_value=rows),
			patch.object(sa.allocation_plan, "planned_teams", return_value=planned),
		):
			return sa.opl_team_of("OPL-1", teams)

	def test_mix_goes_to_the_team_of_most_of_the_box(self):
		# 05208 mix 02721: Madam Red has a stray Team A plan, the box's other three lines are Jamafa
		rows = [("MR", 1575.0), ("SN", 840.0), ("WH", 1050.0), ("YE", 1050.0)]
		planned = {"MR": "Team A", "SN": "Jamafa", "WH": "Jamafa", "YE": "Jamafa"}
		self.assertEqual(self._team(rows, planned), "Jamafa")

	def test_release_team_wins_over_the_plan(self):
		self.assertEqual(self._team([("L1", 200.0)], {"L1": "Eldama A"}, {"L1": "Team B"}), "Team B")

	def test_planned_team_when_release_names_none(self):
		# 05254 Giselle: planned Eldama A, stamped Team B by the order's first line
		self.assertEqual(self._team([("GI", 1400.0)], {"GI": "Eldama A"}, {}), "Eldama A")

	def test_no_team_known(self):
		self.assertIsNone(self._team([("L1", 100.0)], {}, {}))


class TestAllocateAllPlansOnTopOfPicks(unittest.TestCase):
	"""Allocate all with an order open on the page (its picks held first as a proposal) still allocates that
	order: planned on top of the picks and added to them, as Auto-allocate order does (2026-11-22)."""

	def test_picked_order_is_planned_and_added_to(self):
		from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da

		picked = frappe._dict(name="SO-PICKED")
		fresh = frappe._dict(name="SO-FRESH")
		planned = [
			{"sales_order": "SO-PICKED", "allocations": [{"qty": 60}], "teams": {}, "boxes": 3, "stems": 60},
			{"sales_order": "SO-FRESH", "allocations": [{"qty": 20}], "teams": {}, "boxes": 1, "stems": 20},
		]
		calls = {}

		def get_all(doctype, filters=None, pluck=None, **kw):
			if doctype == da.PENDING and filters.get("source") == "Allocate all":
				return []  # no earlier Allocate all proposal
			if doctype == da.PENDING:
				return ["SO-PICKED"]  # the open order's picks, held first
			return ["L1"] if (filters or {}).get("parent") == "SO-PICKED" else []

		def plan_day(*args, **kw):
			calls["skip"], calls["prior"] = args[5], dict(kw.get("prior") or {})
			return planned, {}, {}, {}

		with (
			patch.object(da.frappe, "get_all", side_effect=get_all),
			patch.object(da, "_orders", return_value=[picked, fresh]),
			patch.object(da, "_pending_stems", return_value=60.0),
			patch.object(da, "_plan_day", side_effect=plan_day),
			patch.object(da, "_add_checked", side_effect=lambda *a, **k: calls.setdefault("added", a[1])),
			patch.object(da, "_save_pending", side_effect=lambda *a, **k: calls.setdefault("saved", a[1])),
			patch.object(da, "_publish"),
			patch.object(da, "_proposal", return_value=None),
			patch.object(da, "_summary", return_value={}),
			patch.object(da, "_shortages", return_value=[]),
			patch.object(da, "_pending", return_value=[]),
			patch.object(da, "_keep_shortages"),
			patch.object(da.frappe, "enqueue"),
			patch.object(da.frappe.db, "commit"),
		):
			da._run_day("Kapkolia", "2026-11-23", "2026-11-23", ["Kapkolia"], "x@y")
		self.assertEqual(calls["skip"], ())
		self.assertEqual(calls["prior"], {"L1": 60.0})
		self.assertEqual(calls["added"], "SO-PICKED")
		self.assertEqual(calls["saved"], "SO-FRESH")
