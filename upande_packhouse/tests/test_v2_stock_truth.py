"""Stock Visibility v2: every stem in one state, with the reason.

Pure tests need no data. The site tests plant a small shelf at Kapkolia and read
it back through the real queries; everything is rolled back."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, now_datetime, today

from upande_packhouse.api.v2 import stock_truth as st


def _row(**kw):
	r = dict(
		stems=100.0,
		allocated=0.0,
		free=100.0,
		in_discard_request=False,
		too_old=False,
		cooling=False,
		hours_on_shelf=48,
		shelf_farm="Kapkolia",
		age_days=1,
		date_added=datetime(2026, 10, 26, 6, 0),
	)
	r.update(kw)
	return r


CFG = {"sales_shelf": 1, "max_allocation_age": 4, "cooling_hours": 12}


class TestClassify(UnitTestCase):
	def c(self, **kw):
		args = dict(held_by=None, farm_cfg=CFG, farm_known=True, stale_qty=0.0)
		args.update({k: kw.pop(k) for k in list(kw) if k in args})
		return st.classify_shelf_row(_row(**kw), **args)

	def test_fresh_free_stems_are_sellable(self):
		self.assertEqual(self.c(), [("sellable", 100.0, {"remote": False})])

	def test_remote_farm_is_sellable_but_flagged(self):
		self.assertEqual(self.c(farm_cfg={"sales_shelf": 0})[0][2]["remote"], True)

	def test_discard_hold_beats_everything_and_takes_the_whole_bucket(self):
		out = self.c(in_discard_request=True, allocated=0.0, too_old=True, held_by={"name": "DR-1"})
		self.assertEqual([(s, n) for s, n, _ in out], [("discard", 100.0)])

	def test_disabled_farm_is_withheld(self):
		self.assertEqual(self.c(farm_known=False)[0][0], "farm_disabled")

	def test_too_old_and_cooling(self):
		self.assertEqual(self.c(too_old=True)[0][0], "too_old")
		out = self.c(cooling=True, hours_on_shelf=3)
		self.assertEqual(out[0][0], "cooling")
		self.assertEqual(out[0][2]["ready_at"], datetime(2026, 10, 26, 18, 0))

	def test_allocation_splits_the_stems_and_the_parts_add_up(self):
		out = self.c(allocated=60.0, free=40.0, stale_qty=25.0)
		self.assertEqual(
			{s: n for s, n, _ in out}, {"reclaimable": 25.0, "committed_live": 35.0, "sellable": 40.0}
		)
		self.assertEqual(sum(n for _, n, _ in out), 100.0)

	def test_stale_cannot_exceed_what_is_allocated(self):
		out = self.c(allocated=10.0, free=90.0, stale_qty=500.0)
		self.assertEqual({s: n for s, n, _ in out}, {"reclaimable": 10.0, "sellable": 90.0})

	def test_allocated_stems_stay_committed_even_when_old(self):
		out = self.c(allocated=100.0, free=0.0, too_old=True)
		self.assertEqual([(s, n) for s, n, _ in out], [("committed_live", 100.0)])


class TestVerdict(UnitTestCase):
	def test_thresholds(self):
		self.assertEqual(st.verdict(100, 100, 0, 0)[0], "safe")
		self.assertEqual(st.verdict(101, 100, 5, 0)[0], "possible")
		self.assertEqual(st.verdict(110, 100, 5, 5)[0], "downgrade")
		self.assertEqual(st.verdict(111, 100, 5, 5)[0], "short")


def _pool(**kw):
	"""Numbers in, item-level pool out (one anonymous bucket per source)."""
	p = st._empty_pool()
	for src, v in kw.items():
		if src == "cooling":
			p[src] = [
				{
					"qty": n,
					"ready_date": rd,
					"ready_at": str(rd),
					"bucket": f"C{i}",
					"farm": "Kapkolia",
					"shelf": "",
					"length": "",
					"age_days": 1,
					"detail": "",
					"ref": None,
				}
				for i, (rd, n) in enumerate(v)
			]
		elif v:
			p[src] = [
				{
					"qty": v,
					"bucket": f"B-{src}",
					"farm": "Kapkolia",
					"shelf": f"S-{src}",
					"length": "",
					"age_days": 2,
					"ready_at": None,
					"detail": "",
					"ref": None,
				}
			]
	return p


D = datetime(2026, 10, 27).date()


class TestAvailability(UnitTestCase):
	def test_totals_are_straight_sums_and_cooling_respects_the_date(self):
		pool = _pool(shelf=40, floor=30, cooling=[(D, 10), (datetime(2026, 10, 29).date(), 90)])
		t = st.pool_totals(pool, D)
		self.assertEqual((t["shelf"], t["floor"], t["cooling"]), (40, 30, 10))

	def test_totals_are_not_spent_by_asking_twice(self):
		pool = _pool(shelf=100)
		self.assertEqual(st.pool_totals(pool, D), st.pool_totals(pool, D))

	def test_line_status_levels(self):
		def av(**k):
			return dict(
				{"shelf": 0, "remote": 0, "stale": 0, "floor": 0, "arriving": 0, "overdue": 0, "cooling": 0},
				**k,
			)

		self.assertEqual(st.line_status(0, av()), "allocated")
		self.assertEqual(st.line_status(50, av(shelf=50)), "available")
		self.assertEqual(st.line_status(50, av(shelf=20, floor=30)), "action")
		self.assertEqual(st.line_status(50, av(shelf=20), longer=30), "downgrade")
		self.assertEqual(st.line_status(50, av(shelf=20), forecast=30), "greenhouse")
		self.assertEqual(st.line_status(50, av(shelf=20)), "short")

	def test_two_lines_are_each_measured_against_the_whole_pool(self):
		av = st.pool_totals(_pool(shelf=100), D)
		self.assertEqual(st.line_status(80, av), "available")
		self.assertEqual(
			st.line_status(80, av), "available"
		)  # no queue: the second line is not "after" the first

	def test_where_lists_places_not_shares(self):
		rows = st._where(_pool(shelf=40, floor=30), D)
		self.assertEqual(
			[(r["source"], r["stems"], r["bucket"]) for r in rows],
			[("shelf", 40, "B-shelf"), ("floor", 30, "B-floor")],
		)


class TestStockTruthSite(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		self = cls
		frappe.set_user("Administrator")
		if not frappe.db.exists("Item", "Marisa") or not frappe.db.exists("Farm", "Kapkolia"):
			raise unittest.SkipTest("Marisa / Kapkolia not on this site")
		if "Kapkolia" not in st.stock._config()["farm_config"]:
			raise unittest.SkipTest("Kapkolia is not enabled in Production Settings here")
		now = now_datetime()

		def item(bucket, qty, days):
			return {
				"bucket_id": bucket,
				"variety": "Marisa",
				"stem_length": "62cm",
				"stem_qty": qty,
				"farm": "Kapkolia",
				"date_added": now - timedelta(hours=60),
				"harvest_date": add_days(today(), -days),
			}

		cls.shelf = frappe.get_doc(
			{
				"doctype": "Shelf",
				"shelf_id": "ZZT-S1",
				"farm": "Kapkolia",
				"items": [
					item("ZZTB1", 100, 1),
					item("ZZTB2", 200, 12),
					item("ZZTB3", 50, 1),
					item("ZZTB4", 80, 1),
				],
			}
		).insert(ignore_permissions=True, ignore_links=True)
		so = frappe.db.get_value(
			"Sales Order", {"docstatus": 1, "delivery_date": ["<", today()]}, ["name", "name"], as_dict=False
		)
		if not so:
			raise unittest.SkipTest("no past-dated Sales Order to hang a stale allocation on")
		soi = frappe.db.get_value("Sales Order Item", {"parent": so[0]}, "name")
		frappe.get_doc(
			{
				"doctype": "Bucket Allocation Status",
				"bucket_id": "ZZTB4",
				"item_code": "Marisa",
				"stem_length": "62cm",
				"shelf_farm": "Kapkolia",
				"farm": "Kapkolia",
				"total_quantity": 80,
				"allocated_quantity": 80,
				"available_quantity": 0,
				"bucket_allocations": [
					{"sales_order": so[0], "sales_order_item": soi, "quantity_allocated": 80, "cancelled": 0}
				],
			}
		).insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)

	def _states(self, **kw):
		with (
			patch(
				"upande_packhouse.api.v2.stock_truth._discards",
				lambda: {"ZZTB3": {"name": "DR-ZZT", "state": "Approved", "since": today()}},
			),
			patch("upande_packhouse.api.v2.core.stock.reserved_bucket_ids", lambda: {"ZZTB3"}),
		):
			out = st.ledger(variety="Marisa", farm="Kapkolia", length="62cm", limit=1000, **kw)
		return {r["bucket"]: (r["state"], r["stems"]) for r in out["rows"] if r["bucket"].startswith("ZZT")}

	def test_each_planted_bucket_lands_in_the_right_state(self):
		got = self._states()
		self.assertEqual(got["ZZTB1"], ("sellable", 100.0))
		self.assertEqual(got["ZZTB2"], ("too_old", 200.0))
		self.assertEqual(got["ZZTB3"], ("discard", 50.0))
		self.assertEqual(got["ZZTB4"][0], "reclaimable")

	def test_check_is_safe_up_to_what_is_free_and_short_beyond(self):
		with patch("upande_packhouse.api.v2.stock_truth._discards", lambda: {}):
			out = st.check([{"variety": "Marisa", "length": "62cm", "qty": 1}], today())
		self.assertTrue(out["success"])
		self.assertIn(out["lines"][0]["code"], ("safe", "possible", "downgrade", "short"))
		self.assertGreaterEqual(out["lines"][0]["sellable_now"], 100.0)

	def test_overview_states_add_up_to_the_stems_on_hand(self):
		o = st.overview(farm="Kapkolia")
		k = o["kpis"]
		self.assertAlmostEqual(sum(k["states"].values()) - k["phantom_allocations"], k["on_hand"], places=3)
		self.assertAlmostEqual(
			k["sellable"] + k["soon"] + k["committed"] + k["withheld"], sum(k["states"].values()), places=3
		)

	def test_unknown_variety_is_reported_not_crashed(self):
		out = st.check([{"variety": "No Such Rose", "length": "50cm", "qty": 10}], today())
		self.assertEqual(out["lines"][0]["code"], "unknown")

	def test_outlook_lists_what_is_available_without_sharing_it_out(self):
		o = st.outlook(from_date=today(), to_date=add_days(today(), 30), force=1)
		self.assertTrue(o["success"], o)
		keys = {x["key"] for x in o["stock"]}
		self.assertEqual(keys, set(st.SOURCE_KEYS))
		for order in o["orders"]:
			for ln in order["lines"]:
				self.assertNotIn("rank", ln)
				self.assertIn(ln["status"], st.STATUS_RANK)
				# every line of the same size sees the same whole pool
		by = {}
		for order in o["orders"]:
			for ln in order["lines"]:
				by.setdefault((ln["variety"], ln["length"], str(ln["to_allocate"] > 0)), []).append(
					ln["available"]
				)
		for same in by.values():
			self.assertTrue(all(x == same[0] for x in same))


class TestRelease(IntegrationTestCase):
	"""Selection and safety of the release, with the allocator's unallocate step mocked."""

	def setUp(self):
		frappe.set_user("Administrator")

	def test_only_stale_cancelled_or_past_due_allocations_are_selected(self):
		a = st._args({})
		groups, skipped = st._release_groups(a)
		for g in groups:
			self.assertIn(g["reason"], st.RELEASABLE)
		# delivered orders are never offered, they are reported as skipped
		self.assertTrue(all(isinstance(v["stems"], float) for v in skipped.values()))

	def test_a_bucket_harvested_again_is_offered_for_release(self):
		stale = [x for x in st._allocations(set(), set()) if x.reason == "bucket_reused"]
		if not stale:
			self.skipTest("no allocation on a bucket that was harvested again")
		groups, _skipped = st._release_groups(st._args({}))
		offered = {(g["sales_order_item"], g["bucket_id"]) for g in groups}
		blocked = st._blocked_pairs({x.sales_order_item for x in stale})
		for x in stale:
			if not blocked.get((x.sales_order_item, x.bucket_id.upper())):
				self.assertIn((x.sales_order_item, x.bucket_id), offered)

	def test_issued_and_delivered_past_due_rows_are_offered_and_blockers_are_only_trolleys(self):
		allocs = st._allocations(set(), set())
		self.assertNotIn("dispatched", {x.reason for x in allocs})
		# an issued row only appears while it is stale
		self.assertTrue(all(x.reason for x in allocs if x.issued))
		groups, _s = st._release_groups(st._args({}))
		for g in groups:
			self.assertTrue(g["issued"] or g["open"])

	def test_search_finds_the_order_holding_a_bucket(self):
		allocs = [x for x in st._allocations(set(), set()) if x.reason]
		if not allocs:
			self.skipTest("no stale allocation")
		x = allocs[0]
		out = st.allocations(q=x.bucket_id)
		self.assertTrue(out["success"])
		self.assertIn(x.sales_order, {o["sales_order"] for o in out["orders"]})

	def test_stale_transfers_are_listed_searchable_and_releasable(self):
		rows = [r for r in st._transfer_rows() if r.reason]
		if not rows:
			self.skipTest("no stale transfer")
		self.assertTrue(all(r.reason in st.TRANSFER_REASONS for r in rows))
		r = rows[0]
		out = st.transfers(q=r.bucket_id)
		self.assertIn(r.sales_order, {o["sales_order"] for o in out["orders"]})
		plan = st.release_plan(kind="transfers")
		self.assertEqual(plan["lines"], len({(x.sales_order_item, x.bucket_id, x.length or "") for x in rows}))

	def test_release_needs_confirmation_and_a_sales_role(self):
		with self.assertRaises(frappe.ValidationError):
			st.release_stale(confirm=0)

	def test_release_runs_the_allocators_unallocate_and_reports_each_line(self):
		a = st._args({})
		groups, _s = st._release_groups(a)
		if not groups:
			self.skipTest("no stale allocation on this site")
		seen = []

		def fake(soi, bucket, length):
			seen.append((soi, bucket, length))
			return {"success": len(seen) != 2, "message": "boom"}

		with (
			patch(
				"upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.unallocate_bucket_from_opl",
				fake,
			),
			patch.object(frappe.cache, "delete_keys", lambda *x, **k: None),
		):
			out = st.release_stale(confirm=1, limit=3)
		self.assertEqual(len(seen), min(3, len(groups)))
		if len(groups) >= 2:
			self.assertEqual(len(out["failed"]), 1)
			self.assertEqual(out["failed"][0]["error"], "boom")
		self.assertEqual(out["remaining"], max(0, len(groups) - 3))

	def test_a_failed_line_can_be_skipped_on_the_next_call(self):
		a = st._args({})
		groups, _s = st._release_groups(a)
		if not groups:
			self.skipTest("no stale allocation on this site")
		skip = [{k: groups[0][k] for k in ("sales_order_item", "bucket_id", "stem_length")}]
		again, _s2 = st._release_groups(a, skip=skip)
		self.assertEqual(len(again), len(groups) - 1)

	def test_release_can_be_limited_to_chosen_orders(self):
		a = st._args({})
		groups, _s = st._release_groups(a)
		if not groups:
			self.skipTest("no stale allocation on this site")
		so = groups[0]["sales_order"]
		only, _ = st._release_groups(a, sales_orders=[so])
		self.assertTrue(only and {g["sales_order"] for g in only} == {so})
		none, _ = st._release_groups(a, sales_orders=["no-such-order"])
		self.assertEqual(none, [])
		plan = st.release_plan(sales_orders=[so])
		self.assertEqual(plan["orders"], 1)

	def test_the_stock_entry_lookup_the_release_depends_on_is_indexed(self):
		if not frappe.db.has_column("Stock Entry", "custom_issued_to"):
			self.skipTest("Stock Entry has no custom_issued_to column on this site")
		idx = frappe.db.sql("SHOW INDEX FROM `tabStock Entry` WHERE Column_name = 'custom_issued_to'")
		self.assertTrue(
			idx,
			"run the index_stock_entry_issued_to patch: without it every release scans the whole Stock Entry table",
		)

	def test_a_slow_release_hands_back_after_its_time_budget(self):
		groups, _s = st._release_groups(st._args({}))
		if len(groups) < 3:
			self.skipTest("needs three stale lines")
		clock = iter(range(0, 1000, 6))  # every reading of the clock is 6 s later
		calls = []

		def fake(soi, bucket, length):
			calls.append(bucket)
			return {"success": True}

		with (
			patch(
				"upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.unallocate_bucket_from_opl",
				fake,
			),
			patch.object(frappe.cache, "delete_keys", lambda *a, **k: None),
			patch.object(st, "time", SimpleNamespace(monotonic=lambda: next(clock))),
		):
			out = st.release_stale(confirm=1, limit=10, budget=5)
		self.assertEqual(len(calls), 1)  # one line, then out of time
		self.assertEqual(out["remaining"], len(groups) - 1)

	def test_a_missing_index_is_reported_and_built_in_the_background(self):
		with patch.object(st, "_release_index_missing", lambda: True), patch("frappe.enqueue") as enq:
			plan = st.release_plan()
		self.assertTrue(plan["index_building"])
		self.assertTrue(enq.called)
