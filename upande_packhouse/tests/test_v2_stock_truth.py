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

	def test_spare_on_a_bucket_going_to_another_farm_is_withheld_not_sellable(self):
		out = self.c(allocated=40.0, free=60.0, spare=60.0)
		self.assertEqual({s: n for s, n, _ in out}, {"committed_live": 40.0, "spare_moving": 60.0})
		self.assertEqual(st.STATE_META["spare_moving"][1], "withheld")

	def test_too_old_and_cooling_come_before_spare(self):
		self.assertEqual(self.c(spare=100.0, too_old=True)[0][0], "too_old")
		self.assertEqual(self.c(spare=100.0, cooling=True, hours_on_shelf=3)[0][0], "cooling")

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


class TestLonger(UnitTestCase):
	def test_longer_counts_only_longer_lengths_of_the_variety(self):
		by_len = {"42cm": 100.0, "52cm": 1520.0, "62cm": 1403.0, "--": 7.0}
		self.assertEqual(st._longer_than(by_len, "42cm"), 2923.0)
		self.assertEqual(st._longer_than(by_len, "52cm"), 1403.0)
		self.assertEqual(st._longer_than(by_len, "62cm"), 0.0)
		self.assertEqual(st._longer_than(by_len, "--"), 0.0)


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
					item("ZZTB5", 100, 1),
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

		# ZZTB5: 40 of its 100 stems are allocated, in transit, to an order at ANOTHER farm
		other = frappe.db.get_value("Farm", {"name": ["!=", "Kapkolia"]}, "name")
		if other:
			frappe.db.set_value("Sales Order", so[0], "farm", other, update_modified=False)
			frappe.get_doc(
				{
					"doctype": "Bucket Allocation Status",
					"bucket_id": "ZZTB5",
					"item_code": "Marisa",
					"stem_length": "62cm",
					"shelf_farm": "Kapkolia",
					"farm": "Kapkolia",
					"total_quantity": 100,
					"allocated_quantity": 40,
					"available_quantity": 60,
					"in_transit": 1,
					"bucket_allocations": [
						{
							"sales_order": so[0],
							"sales_order_item": soi,
							"quantity_allocated": 40,
							"cancelled": 0,
						}
					],
				}
			).insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
		cls.has_spare = bool(other)

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

	def test_spare_stems_split_out_of_free_and_the_split_adds_up(self):
		if not self.has_spare:
			self.skipTest("no second farm to own the allocation")
		rows = {
			r.bucket_id: r
			for r in st.stock.bucket_rows(farms=["Kapkolia"], varieties=["Marisa"])
			if r.bucket_id.startswith("ZZT")
		}
		b5 = rows["ZZTB5"]
		self.assertEqual((b5.allocated, b5.spare, b5.allocatable), (40.0, 60.0, 0.0))
		for r in rows.values():
			self.assertAlmostEqual(
				r.free, r.allocatable + r.aged_stems + r.cooling_stems + r.spare + r.reserved, places=3
			)
		spare = [
			r
			for r in st.ledger(variety="Marisa", farm="Kapkolia", state="spare_moving", limit=1000)["rows"]
			if r["bucket"] == "ZZTB5"
		]
		self.assertEqual([r["stems"] for r in spare], [60.0])

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
