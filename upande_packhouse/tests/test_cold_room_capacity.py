"""Cold Room v2 bucket capacity: thresholds, capacity source, and that the
occupied count is the page's one stock definition (core/stock). Read-only."""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from upande_packhouse.api.v2 import cold_room
from upande_packhouse.api.v2 import cold_room_capacity as cc
from upande_packhouse.api.v2.core import stock


class TestLevels(unittest.TestCase):
	def test_thresholds(self):
		cases = [
			(0, "normal"), (79.99, "normal"), (80, "getting"), (89.99, "getting"),
			(90, "almost"), (99.99, "almost"), (100, "full"), (250, "full"),
		]  # fmt: skip
		for pct, key in cases:
			self.assertEqual(cc.level_for(pct)[0], key, pct)
		self.assertEqual(cc.level_for(None)[0], "unknown")

	def test_gauge_numbers(self):
		g = cc.gauge(1406, 1366)
		self.assertEqual((g["free"], g["over_by"], g["level"]), (0, 40, "full"))
		self.assertEqual(g["pct"], 102.9)
		g = cc.gauge(89, 100)
		self.assertEqual((g["free"], g["over_by"], g["level"]), (11, 0, "getting"))

	def test_display_rounding_never_promotes_level(self):
		# 89.96% rounds to 90.0 for display but is still "Getting full"
		g = cc.gauge(8996, 10000)
		self.assertEqual(g["pct"], 90.0)
		self.assertEqual(g["level"], "getting")

	def test_no_capacity_is_unknown_not_zero_percent(self):
		g = cc.gauge(5, 0)
		self.assertIsNone(g["pct"])
		self.assertEqual((g["level"], g["over_by"]), ("unknown", 0))

	def test_capacity_source_order(self):
		cfg = {
			"A": frappe._dict(max_buckets=500, max_shelves=100),
			"B": frappe._dict(max_buckets=0, max_shelves=100),
		}
		self.assertEqual(cc.capacity_for("A", 7, cfg)[0], 500)
		self.assertEqual(cc.capacity_for("B", 7, cfg)[0], 100 * cc.BUCKETS_PER_SHELF)
		self.assertEqual(cc.capacity_for("C", 7, cfg), (7 * cc.BUCKETS_PER_SHELF, "Registered shelves", 7))


class TestAgainstStock(IntegrationTestCase):
	def test_occupied_is_core_stock_definition(self):
		if not frappe.db.count("Shelf Item"):
			self.skipTest("no shelf data on this site")
		rows = stock.bucket_rows(farms=None)
		block = cc.build(rows, None)
		self.assertEqual(block["total"]["occupied"], stock.totals(rows).buckets)
		by_farm = {g.shelf_farm: g.buckets for g in stock.summarize(rows, by=("shelf_farm",))}
		for s in block["stores"]:
			self.assertEqual(s["occupied"], by_farm.get(s["farm_key"] or None, 0), s["farm"])
			self.assertEqual(
				s["free"] + s["over_by"], abs(s["capacity"] - s["occupied"]) if s["over_by"] else s["free"]
			)
		self.assertEqual(
			sum(s["occupied"] for s in block["stores"]) - block["total"]["shared_buckets"],
			block["total"]["occupied"],
		)

	def test_endpoint_follows_farm_filter_and_ignores_dates(self):
		farm = frappe.db.get_value("Shelf", {"farm": ["is", "set"]}, "farm")
		if not farm:
			self.skipTest("no shelf data on this site")
		a = cold_room.get_cold_room(farm=farm)["bucket_capacity"]
		b = cold_room.get_cold_room(farm=farm, from_date="2000-01-01", to_date="2000-01-02")[
			"bucket_capacity"
		]
		self.assertEqual([s["farm"] for s in a["stores"]], [farm])
		self.assertEqual(a["total"], b["total"])  # live position: dates do not change it

	def test_shelf_list_consistent_with_gauge(self):
		farm = frappe.db.get_value("Shelf", {"farm": ["is", "set"]}, "farm")
		if not farm:
			self.skipTest("no shelf data on this site")
		rows = stock.bucket_rows(farms=[farm])
		sl = cc.shelf_list(rows, [farm], limit=10**6)
		self.assertTrue(all(s["farm"] == farm for s in sl["shelves"]))
		self.assertEqual(sl["occupied_shelves"], len(sl["shelves"]))
		self.assertEqual(sl["slots_used"], sum(s["buckets"] for s in sl["shelves"]))
		self.assertEqual(
			sl["over_shelves"], sum(1 for s in sl["shelves"] if s["buckets"] > cc.BUCKETS_PER_SHELF)
		)
		counts = [s["buckets"] for s in sl["shelves"]]
		self.assertEqual(counts, sorted(counts, reverse=True))
		# every distinct bucket in the stock rows sits on at least one listed shelf
		listed = {b["bucket_id"] for s in sl["shelves"] for b in s["bucket_list"]}
		self.assertEqual(listed, {r.bucket_id for r in rows})
		one = cc.shelf_list(rows, [farm], only_farm=farm)
		self.assertEqual(one["occupied_shelves"], sl["occupied_shelves"])
		self.assertEqual(cc.shelf_list(rows, [farm], only_farm="No Such Farm")["occupied_shelves"], 0)

	def test_cold_store_capacity_override(self):
		cfg = {"Karen": frappe._dict(farm="Karen", max_buckets=10, max_shelves=0)}
		with patch.object(cc, "_store_config", return_value=cfg):
			block = cc.build([], ["Karen"])
		k = next(s for s in block["stores"] if s["farm"] == "Karen")
		self.assertEqual((k["capacity"], k["capacity_source"]), (10, "Cold Store Capacity (max buckets)"))

	def test_empty_scope_returns_empty_not_error(self):
		out = cc.get_cold_room_shelves(region="Nowhere-land")
		self.assertTrue(out["success"], out)


class TestKpisAndPressure(IntegrationTestCase):
	"""The five KPI numbers and the shelf-based pressure, checked against raw SQL."""

	def setUp(self):
		if not frappe.db.count("Shelf Item"):
			self.skipTest("no shelf data on this site")
		self.out = cold_room.get_cold_room()
		self.assertTrue(self.out["success"], self.out)

	def test_total_shelves_matches_raw_sql(self):
		raw = frappe.db.sql("SELECT COUNT(*) FROM `tabShelf`")[0][0]
		self.assertEqual(self.out["bucket_capacity"]["total"]["shelves"], raw)
		t = self.out["bucket_capacity"]["total"]
		self.assertEqual(t["shelves_in_use"] + t["shelves_empty"], raw)
		self.assertEqual(t["shelves_full"] + t["shelves_over"] + t["shelves_partial"], t["shelves_in_use"])
		# the drilldown lists every registered shelf, empty ones included
		sl = cc.get_cold_room_shelves(include_empty=1)
		self.assertEqual(len(sl["shelves"]), raw)
		self.assertEqual(sl["registered_shelves"], raw)
		self.assertEqual(sl["empty_shelves"], t["shelves_empty"])

	def test_shelf_states_match_raw_sql(self):
		raw = frappe.db.sql(
			"""SELECT NULLIF(s.farm, ''), x.n, COUNT(*) FROM (
			       SELECT si.parent, COUNT(DISTINCT si.bucket_id) n FROM `tabShelf Item` si
			       WHERE si.stem_qty > 0 AND IFNULL(si.bucket_id, '') != '' AND IFNULL(si.variety, '') != ''
			       GROUP BY si.parent) x JOIN `tabShelf` s ON s.name = x.parent
			   GROUP BY NULLIF(s.farm, ''), x.n"""
		)
		want = {}
		for farm, n, c in raw:
			d = want.setdefault(farm, {"full": 0, "over": 0, "partial": 0})
			d["full" if n == 2 else "over" if n > 2 else "partial"] += c
		for s in self.out["bucket_capacity"]["stores"]:
			w = want.get(s["farm_key"] or None, {"full": 0, "over": 0, "partial": 0})
			self.assertEqual(
				(s["shelves_full"], s["shelves_over"], s["shelves_partial"]),
				(w["full"], w["over"], w["partial"]),
				s["farm"],
			)
			self.assertEqual(s["shelves_empty"], s["shelves"] - sum(w.values()), s["farm"])
			if s["shelves"]:
				self.assertAlmostEqual(
					s["shelves_full_pct"], round((w["full"] + w["over"]) / s["shelves"] * 100, 1), places=1
				)

	def test_pressure_level_is_shelf_capacity_level(self):
		for s in self.out["bucket_capacity"]["stores"]:
			self.assertEqual(
				s["capacity"], s["shelves"] * cc.BUCKETS_PER_SHELF, s["farm"]
			)  # no override on this site
			self.assertEqual(
				s["level"], cc.level_for(s["occupied"] / s["capacity"] * 100 if s["capacity"] else None)[0]
			)
			self.assertEqual(s["incoming_exceeds_free"], s["incoming_buckets"] > s["free"])

	def test_average_age_is_stem_weighted_from_core_stock(self):
		raw = frappe.db.sql(
			"""SELECT SUM(x.stems * x.age) / SUM(x.stems) FROM (
			       SELECT SUM(si.stem_qty) stems,
			              DATEDIFF(CURDATE(), MIN(COALESCE(si.harvest_date, DATE(si.date_added)))) age
			       FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
			       WHERE si.stem_qty > 0 AND IFNULL(si.bucket_id, '') != '' AND IFNULL(si.variety, '') != ''
			       GROUP BY si.bucket_id, si.variety, si.stem_length, s.farm) x"""
		)[0][0]
		rows = stock.bucket_rows(farms=None)
		self.assertEqual(self.out["totals"]["avg_age_days"], stock.totals(rows).avg_age_days)
		# raw SQL groups differently from bucket_rows, so allow rounding/grouping slack
		self.assertAlmostEqual(self.out["totals"]["avg_age_days"], float(raw), delta=1.0)

	def test_received_not_shelved_kpi_equals_buckets_tab_list(self):
		b = cold_room.get_cold_room_buckets()
		ns = self.out["flows"]["not_shelved"]
		self.assertEqual(ns.buckets, b["summary"]["not_shelved"].buckets)
		self.assertEqual(ns.stems, b["summary"]["not_shelved"].stems)
		self.assertEqual(ns.buckets, len({r["bucket_id"] for r in b["received_not_shelved"]}))

	def test_drilldown_rows_add_up_to_every_chart_bar(self):
		"""Chart/KPI drilldowns are the bucket rows (get_cold_room_buckets.age): their
		stems per variety / length / farm / age band equal the bar that was clicked."""
		b = cold_room.get_cold_room_buckets()
		age = b["age"]
		self.assertAlmostEqual(sum(r["stems"] for r in age), self.out["totals"]["stems"], places=3)
		for v in self.out["by_variety"]:
			self.assertAlmostEqual(
				sum(r["stems"] for r in age if r["variety"] == v["variety"]), v["stems"], places=3
			)
		for v in self.out["by_length"]:
			self.assertAlmostEqual(
				sum(r["stems"] for r in age if str(r["stem_length"] or "") == str(v["stem_length"] or "")),
				v["stems"],
				places=3,
			)
		for v in self.out["by_farm"]:
			self.assertAlmostEqual(
				sum(r["stems"] for r in age if r["farm"] == v["farm"]), v["stems"], places=3
			)
		for band in self.out["age_bands"]:
			got = sum(
				r["stems"] for r in age
				if (r["age_days"] or 0) >= band["from_days"] and (band["to_days"] is None or (r["age_days"] or 0) < band["to_days"])
			)  # fmt: skip
			self.assertAlmostEqual(got, band["stems"], places=3)

	def test_buckets_endpoint_only_returns_what_the_page_uses(self):
		b = cold_room.get_cold_room_buckets()
		for k in ("discard_shelved", "requested_not_issued"):
			self.assertNotIn(k, b)
		self.assertIn("received_not_shelved", b)

	def test_removed_blocks_are_gone(self):
		for k in ("received", "requested", "discarded"):
			self.assertNotIn(k, self.out["flows"])

	def test_incoming_vs_free_flag(self):
		blk = cc.build([], None, incoming={"Karen": 5})
		k = next((s for s in blk["stores"] if s["farm"] == "Karen"), None)
		if k:
			self.assertEqual(k["incoming_buckets"], 5)
			self.assertEqual(k["incoming_exceeds_free"], 5 > k["free"])

	def test_dashboard_is_fast(self):
		import time

		t = time.time()
		cold_room.get_cold_room()
		self.assertLess(time.time() - t, 1.5)
