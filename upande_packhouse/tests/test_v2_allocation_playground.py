"""Allocation Playground (phase 1): planning an order line holds buckets and stems, allocation reads and
releases the hold, and the board adds up.

Reads the local site's real data. Everything that writes runs in a savepoint that is rolled back, with
frappe.db.commit / rollback patched to no-ops; row counts are asserted unchanged afterwards. Tests skip when
the data is not there.

Run:  bench --site kaitet.local run-tests --app upande_packhouse --module upande_packhouse.tests.test_v2_allocation_playground
"""

import unittest
from unittest.mock import patch

import frappe

from upande_packhouse import allocation_plan as plans
from upande_packhouse import availability
from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.api.v2.core import received, stock

TABLES = (
	"Allocation Plan",
	"Allocation Confirmation",
	"Bucket Reservation",
	"Confirmed Stems",
	"Bucket Allocations",
	"Sales Order Item",
)


class _RelaxedConfig(dict):
	"""Nothing too old, nothing cooling: the local site's stock is old."""

	def get(self, key, default=None):
		return {"max_allocation_age": 10**6, "cooling_hours": 0}


class _Rollback:
	def __enter__(self):
		self.before = {t: frappe.db.count(t) for t in TABLES}
		self.orig_rollback = frappe.db.rollback
		frappe.db.savepoint("pg_test")
		self._p = [
			patch.object(frappe.db, "commit", lambda *a, **k: None),
			patch.object(frappe.db, "rollback", lambda *a, **k: None),
			patch.object(
				stock, "_config", lambda: {"discard_age": 10**6, "amber_time": 3, "farm_config": _RelaxedConfig()}
			),
			# The local Stock Entry table is huge; with the relaxed age these reads would scan a year of it.
			patch.object(received, "received_unshelved", lambda **k: []),
			patch.object(received, "harvested_day", lambda *a, **k: {}),
			patch.object(received, "unshelved_bucket_ids", lambda ids: set()),
		]
		for p in self._p:
			p.start()
		return self

	def __exit__(self, *exc):
		for p in self._p:
			p.stop()
		self.orig_rollback(save_point="pg_test")
		after = {t: frappe.db.count(t) for t in TABLES}
		assert after == self.before, f"row counts changed after rollback: {self.before} -> {after}"
		return False


def _team(name="ZZ TEST TEAM A"):
	"""A packing team for the test (created inside the rolled-back savepoint when the site has none by that name)."""
	if not frappe.db.exists("Packing Teams", name):
		frappe.get_doc({"doctype": "Packing Teams", "team": name}).insert(ignore_permissions=True)
	return name


def _case():
	"""A farm with a free bucket, and an open order line for the same variety and length."""
	with patch.object(
		stock, "_config", lambda: {"discard_age": 10**6, "amber_time": 3, "farm_config": _RelaxedConfig()}
	):
		for r in stock.bucket_rows():
			if r.allocatable < 20 or not r.stem_length or not r.shelf_farm:
				continue
			lines = frappe.db.sql(
				"""SELECT soi.name AS soi, soi.parent, so.delivery_date
				   FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
				   WHERE so.docstatus = 1 AND so.business_unit = 'Roses' AND soi.item_code = %s
				     AND soi.custom_length = %s AND soi.stock_qty >= 20
				     AND IFNULL(soi.custom_mixed_box, 0) = 0 AND IFNULL(soi.custom_mixed_bunch, 0) = 0
				   ORDER BY so.delivery_date DESC LIMIT 6""",
				(r.variety, r.stem_length),
				as_dict=True,
			)
			if len(lines) >= 2:
				return frappe._dict(bucket=r, lines=lines, farm=r.shelf_farm)
	return None


class TestPlanning(unittest.TestCase):
	def setUp(self):
		self.c = _case()
		if not self.c:
			self.skipTest("no free bucket with two open order lines of the same variety and length")

	def _plan(self, line, stems, team=None):
		return pg.confirm_lines(
			str(line.delivery_date), self.c.farm, [{"sales_order_item": line.soi, "stems": stems}], team=team or _team()
		)

	def test_planning_holds_buckets_and_hides_them_from_other_lines(self):
		a, b = self.c.lines[0], self.c.lines[1]
		with _Rollback():
			res = self._plan(a, 10)
			held = res["lines"][0]["held"]
			self.assertGreater(held, 0)
			rows = frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}, fields=["bucket_id", "stems"])
			self.assertEqual(round(sum(r.stems for r in rows), 3), round(held, 3))
			# held for A: hidden from B, visible to A
			hidden = availability.reserved_by_bucket(exclude_soi=b.soi)
			shown = availability.reserved_by_bucket(exclude_soi=a.soi)
			for r in rows:
				self.assertGreaterEqual(hidden.get(r.bucket_id, 0), r.stems)
				self.assertEqual(shown.get(r.bucket_id, 0), 0)
			# the shared stock read: the hold is part of free and is not allocatable
			bucket_rows = {r.bucket_id: r for r in stock.bucket_rows(varieties=[self.c.bucket.variety])}
			for r in rows:
				self.assertGreaterEqual(bucket_rows[r.bucket_id].reserved, r.stems)
				self.assertLessEqual(bucket_rows[r.bucket_id].allocatable, bucket_rows[r.bucket_id].free - r.stems + 1e-6)
			# and mirrored to the v1 Confirmed Stems table the allocator reads
			cs = frappe.db.get_value(
				"Confirmed Stems",
				{"parent": a.parent, "sales_order_item": a.soi, "farm": self.c.farm},
				"stems",
			)
			self.assertEqual(round(cs, 3), round(held, 3))

	def test_confirmed_lines_can_be_seen_and_taken_out(self):
		a = self.c.lines[0]
		day, v, ln = str(a.delivery_date), self.c.bucket.variety, self.c.bucket.stem_length
		with _Rollback():
			self._plan(a, 10)
			pool = pg.get_pool(day, self.c.farm, v, ln)
			mine = [x for x in pool["confirmed_lines"] if x["soi"] == a.soi]
			self.assertEqual(len(mine), 1)
			self.assertGreater(mine[0]["held"] + mine[0]["expected"], 0)
			self.assertEqual(round(mine[0]["held"] + mine[0]["expected"], 3), 10.0)
			if mine[0]["held"]:
				self.assertGreaterEqual(mine[0]["buckets"], 1)
			# the Confirmed view lists the variety even though nothing needs attention there
			confirmed_view = pg.get_board(day, self.c.farm, "confirmed")["pools"]
			self.assertIn((v, ln), {(p["variety"], p["length"]) for p in confirmed_view})
			self.assertTrue(all(p["confirmed_lines"] or p["held"] or p["expected_plan"] for p in confirmed_view))
			# take it out: the stock is free again and the mirror row is gone
			res = pg.unconfirm_line(a.soi)
			self.assertGreaterEqual(res["released"], 1)
			self.assertFalse(frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}))
			self.assertFalse(plans.live_confirmations(soi=a.soi))
			self.assertFalse(frappe.db.exists("Confirmed Stems", {"parent": a.parent, "sales_order_item": a.soi, "farm": self.c.farm}))
			self.assertFalse([x for x in pg.get_pool(day, self.c.farm, v, ln)["confirmed_lines"] if x["soi"] == a.soi])

	def test_expected_stems_are_mirrored_into_confirmed_stems_too(self):
		"""Allocation reads the Sales Order's Confirmed Stems table, so what is confirmed from the expected harvest has to be in it."""
		a = self.c.lines[0]
		day = str(a.delivery_date)
		row = {"parent": a.parent, "sales_order_item": a.soi, "farm": self.c.farm}
		with _Rollback(), patch.object(stock, "bucket_rows", lambda **k: []):
			self._plan(a, 10)  # nothing on the shelf: all expected
			self.assertFalse(frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}))
			self.assertEqual(frappe.db.get_value("Confirmed Stems", row, "stems"), 10.0)
			self._plan(a, 25)
			self.assertEqual(frappe.db.get_value("Confirmed Stems", row, "stems"), 25.0)
			self._plan(a, 0)
			self.assertFalse(frappe.db.exists("Confirmed Stems", row))

	def test_the_mirror_adds_what_is_held_and_what_is_expected(self):
		a = self.c.lines[0]
		row = {"parent": a.parent, "sales_order_item": a.soi, "farm": self.c.farm}
		with _Rollback():
			plan = plans.get_plan(str(a.delivery_date), self.c.farm)
			line = {"sales_order": a.parent, "sales_order_item": a.soi, "customer": None,
			        "ordered_variety": self.c.bucket.variety, "ordered_length": self.c.bucket.stem_length, "mix_key": ""}
			args = dict(plan=plan, line=line, farm=self.c.farm, variety=self.c.bucket.variety, length=self.c.bucket.stem_length)
			_conf, held = plans.reserve_real(stems=10, **args)
			self.assertEqual(held, 10.0)
			plans.plan_virtual(stems=5, **args)
			self.assertEqual(frappe.db.get_value("Confirmed Stems", row, "stems"), 15.0)  # 10 held + 5 expected
			for c in plans.live_confirmations(soi=a.soi):
				if c.source == "Virtual":
					plans.release_confirmation(c.name, "Manual")
			self.assertEqual(frappe.db.get_value("Confirmed Stems", row, "stems"), 10.0)

	def test_oldest_buckets_are_held_first(self):
		a = self.c.lines[0]
		with _Rollback():
			pool = plans.free_for_line(self.c.farm, self.c.bucket.variety, self.c.bucket.stem_length, a.soi)
			ages = [b.age_days for b in pool]
			self.assertEqual(ages, sorted(ages, reverse=True))

	def test_replanning_replaces_and_zero_releases(self):
		a = self.c.lines[0]
		with _Rollback():
			self._plan(a, 10)
			self._plan(a, 5)
			live = frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}, pluck="stems")
			self.assertEqual(round(sum(live), 3), 5.0)
			self._plan(a, 0)
			self.assertFalse(frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}))
			self.assertFalse(
				frappe.db.exists("Confirmed Stems", {"parent": a.parent, "sales_order_item": a.soi, "farm": self.c.farm})
			)
			self.assertFalse(plans.live_confirmations(soi=a.soi))

	def test_allocation_consumes_the_hold_and_a_full_line_releases_the_rest(self):
		a = self.c.lines[0]
		with _Rollback():
			held = self._plan(a, 10)["lines"][0]["held"]
			res = frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}, fields=["bucket_id", "stems"], order_by="creation asc")
			first = res[0]
			take = min(first.stems, 4.0)
			plans.consume(first.bucket_id, a.soi, take)
			left = frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}, pluck="stems")
			self.assertEqual(round(sum(left), 3), round(held - take, 3))
			conf = plans.live_confirmations(soi=a.soi)[0]
			self.assertEqual(round(conf.fulfilled_stems, 3), round(take, 3))
			self.assertEqual(conf.status, "Partly Fulfilled")
			# the allocator marks the line fully allocated: the rest is released
			frappe.db.set_value("Sales Order Item", a.soi, "custom_fully_allocated", 1, update_modified=False)
			plans.after_allocation([{"bucket_id": first.bucket_id, "sales_order_item": a.soi, "qty": 0}])
			self.assertFalse(frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}))

	def test_cancelled_or_changed_line_releases(self):
		a = self.c.lines[0]
		with _Rollback():
			self._plan(a, 10)
			doc = frappe.get_doc("Sales Order", a.parent)
			doc.items = [r for r in doc.items if r.name != a.soi]  # the line is gone
			plans.release_changed_lines(doc)
			self.assertFalse(frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}))
			closed = frappe.db.get_value("Allocation Confirmation", {"sales_order_item": a.soi}, "release_reason")
			self.assertEqual(closed, "Line changed")

	def test_a_held_bucket_that_left_the_shelf_is_broken(self):
		a = self.c.lines[0]
		with _Rollback():
			self._plan(a, 10)
			name = frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}, pluck="name")[0]
			frappe.db.set_value("Bucket Reservation", name, "bucket_id", "NO-SUCH-BUCKET", update_modified=False)
			with patch.object(frappe.db, "set_value", wraps=frappe.db.set_value):
				plans.mark_missing_buckets()
			self.assertEqual(frappe.db.get_value("Bucket Reservation", name, "status"), "Broken")

	def test_delivery_passed_releases(self):
		a = self.c.lines[0]
		with _Rollback():
			self._plan(a, 10)
			plan = plans.get_plan(str(a.delivery_date), self.c.farm)
			frappe.db.set_value("Allocation Confirmation", {"plan": plan.name}, "delivery_date", "2000-01-01")
			plans.release_passed_days()
			self.assertFalse(frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}))

	def test_cannot_plan_more_than_is_left(self):
		a = self.c.lines[0]
		with _Rollback(), self.assertRaises(frappe.ValidationError):
			self._plan(a, 10**9)

	def test_allocator_refuses_a_bucket_held_for_another_line_and_reads_the_hold_as_its_own(self):
		from upande_packhouse.upande_packhouse.page.sales_allocation import sales_allocation as sa

		a, b = self.c.lines[0], self.c.lines[1]
		cfg = sa._get_production_config()
		location = next((loc for loc, farms in cfg["farms_by_location"].items() if self.c.farm in farms), None)
		if not location:
			self.skipTest("the farm has no location in Production Settings")
		with _Rollback():
			held = self._plan(a, 10)["lines"][0]["held"]
			if not held:
				self.skipTest("nothing could be held")
			res = frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}, fields=["bucket_id", "stems", "item_code", "stem_length"])[0]
			bucket_free = next(r for r in stock.bucket_rows(varieties=[res.item_code]) if r.bucket_id == res.bucket_id)
			# take everything that is not held: leave only the held stems, so line B asking for 1 stem must be refused
			spare = bucket_free.free - bucket_free.reserved
			payload = [
				{
					"bucket_id": res.bucket_id,
					"item_code": res.item_code,
					"stem_length": res.stem_length,
					"qty": spare + 1,
					"sales_order_item": b.soi,
				}
			]
			# the box fit would trim the quantity first; this test is about the hold, so let it through
			with patch.object(sa, "_fit_allocations_to_boxes", lambda doc, allocs: (allocs, [])), self.assertRaises(
				frappe.ValidationError
			) as ctx:
				sa._allocate_stock_with_buckets_impl(b.parent, payload, location)
			self.assertIn("held for another order", str(ctx.exception))


class TestBoard(unittest.TestCase):
	def test_board_adds_up(self):
		day = frappe.db.sql(
			"""SELECT delivery_date FROM `tabSales Order` WHERE docstatus = 1 AND business_unit = 'Roses'
			   GROUP BY delivery_date ORDER BY COUNT(*) DESC LIMIT 1"""
		)
		if not day:
			self.skipTest("no orders")
		with _Rollback():
			farm = pg._default_farm()
			b = pg.get_board(str(day[0][0]), farm, "all")
			pools = b["pools"]
			self.assertEqual(b["kpis"]["lines"], sum(p["lines"] for p in pools))
			self.assertAlmostEqual(b["kpis"]["stems_left"], sum(p["still_to_plan"] for p in pools))
			for p in pools:
				self.assertGreaterEqual(p["still_to_plan"], 0)
				self.assertGreaterEqual(p["short"], 0)
				self.assertLessEqual(p["short"], p["still_to_plan"] + 1e-6)
				self.assertIn(p["status"], ("short", "watch", "ok"))
			attention = pg.get_board(str(day[0][0]), farm, "attention")["pools"]
			self.assertTrue(all(p["status"] != "ok" for p in attention))

	def test_expected_arrivals_shape(self):
		row = frappe.db.sql(
			"""SELECT f.variety, w.iso_year, w.week_number FROM `tabProduction Forecast Week` w
			   JOIN `tabProduction Forecast` f ON f.name = w.parent
			   WHERE w.grade = 'all' AND w.revised_forecast_stems > 0 LIMIT 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("no forecast")
		from datetime import date, timedelta

		day = date.fromisocalendar(row[0].iso_year, row[0].week_number, 3) + timedelta(
			days=int(plans.setting("allocation_harvest_lead_days"))
		)
		likely, sure = pg.expected_arrivals(row[0].variety, "52cm", day)
		self.assertGreaterEqual(likely, 0)
		self.assertLessEqual(sure, likely + 1e-9)
		self.assertEqual(pg.expected_arrivals("NO SUCH VARIETY", "52cm", day), (0.0, 0.0))

	def test_the_settings_exist_and_are_read(self):
		meta = frappe.get_meta("Production Settings")
		for f in plans.SETTING_DEFAULTS:
			self.assertTrue(meta.has_field(f), f)
		self.assertIsNotNone(plans.setting("allocation_virtual_haircut_pct"))


class TestWhatIsNotOnAShelfYet(unittest.TestCase):
	def test_what_is_still_to_come_counts_the_days_harvest_and_only_the_rest_of_the_forecast(self):
		s = {**pg.SUPPLY, "harvested": 400.0, "on_the_way": 100.0}
		with patch.object(pg, "expected_arrivals", lambda *a, **k: (1000.0, 700.0)):
			likely, sure = pg.expected_for("Aqua", "52cm", "2026-10-30", "Kapkolia", s)
		self.assertAlmostEqual(likely, 100 + 600)  # on its way + (1,000 forecast - 400 already harvested)
		self.assertAlmostEqual(sure, 100 + 600 * 0.7)  # the harvest in hand is certain, the forecast carries its error

	def test_the_forecast_error_is_the_years_average_and_sure_follows_it(self):
		row = frappe.db.sql(
			"""SELECT f.variety FROM `tabProduction Forecast Week` w JOIN `tabProduction Forecast` f ON f.name = w.parent
			   WHERE w.grade = 'all' AND w.iso_year = YEAR(CURDATE()) AND w.actual_stems > 1000 AND w.revised_forecast_stems > 0 LIMIT 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("no forecast with actuals")
		err, weeks = pg.forecast_error(row[0].variety)
		if err is None:
			self.skipTest("no finished week with an actual")
		self.assertTrue(0 <= err <= 1)
		self.assertGreater(weeks, 0)
		self.assertEqual(pg.forecast_error("NO SUCH VARIETY"), (None, 0))

	def test_with_nothing_to_judge_by_none_of_the_forecast_counts_as_sure(self):
		with patch.object(pg, "forecast_error", lambda *a, **k: (None, 0)), patch.object(
			frappe.db, "sql", wraps=frappe.db.sql
		):
			d = pg.expected_detail("NO SUCH VARIETY", "52cm", "2026-10-30", "Kapkolia")
		self.assertEqual((d["likely"], d["sure"]), (0.0, 0.0))

	def test_a_received_bucket_can_be_held_for_a_line_and_is_not_offered_to_others(self):
		c = _case()
		if not c:
			self.skipTest("no case")
		a, b = c.lines[0], c.lines[1]
		spare = frappe.db.sql(
			"""SELECT name FROM `tabBucket QR Code` q WHERE NOT EXISTS
			   (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = q.name) LIMIT 1"""
		)
		if not spare:
			self.skipTest("no bucket that is off the shelf")
		bucket = spare[0][0]
		fake = frappe._dict(
			bucket_id=bucket, farm=c.farm, variety=c.bucket.variety, stem_length=c.bucket.stem_length, stems=50.0,
			cold_room="Kapkolia Receiving Cold Store - KR", age_days=1, received_on="2026-10-28", harvest_date="2026-10-28",
		)
		with _Rollback(), patch.object(stock, "bucket_rows", lambda **k: []), patch.object(
			received, "received_unshelved", lambda **k: [fake]
		), patch.object(received, "unshelved_bucket_ids", lambda ids: {bucket}):
			pool = plans.free_for_line(c.farm, c.bucket.variety, c.bucket.stem_length, a.soi)
			self.assertEqual([(x.bucket_id, x.free, x.source, x.cold_room) for x in pool], [(bucket, 50.0, "received", fake.cold_room)])
			plan = plans.get_plan(str(a.delivery_date), c.farm)
			line = {"sales_order": a.parent, "sales_order_item": a.soi, "customer": None,
			        "ordered_variety": c.bucket.variety, "ordered_length": c.bucket.stem_length, "mix_key": ""}
			conf, held = plans.reserve_real(plan=plan, line=line, farm=c.farm, variety=c.bucket.variety, length=c.bucket.stem_length, stems=20)
			self.assertEqual(held, 20.0)
			self.assertEqual(availability.reserved_by_bucket(exclude_soi=b.soi)[bucket], 20.0)
			other = plans.free_for_line(c.farm, c.bucket.variety, c.bucket.stem_length, b.soi)
			self.assertEqual(other[0].free, 30.0)  # 50 received, 20 held for the other line
			own = plans.free_for_line(c.farm, c.bucket.variety, c.bucket.stem_length, a.soi)
			self.assertEqual(own[0].free, 50.0)  # what is held for A is A's to use
			plans.mark_missing_buckets()  # waiting for a shelf is not missing
			self.assertEqual(frappe.db.get_value("Bucket Reservation", {"bucket_id": bucket, "status": "Active"}, "stems"), 20.0)


class TestTeam(unittest.TestCase):
	def setUp(self):
		self.c = _case()
		if not self.c:
			self.skipTest("no case")

	def test_every_confirmation_carries_the_team_and_allocation_can_read_it(self):
		a = self.c.lines[0]
		day = str(a.delivery_date)
		with _Rollback():
			team = _team()
			pg.confirm_lines(day, self.c.farm, [{"sales_order_item": a.soi, "stems": 10}], team=team)
			confs = plans.live_confirmations(soi=a.soi)
			self.assertTrue(confs)
			self.assertEqual({c.team for c in confs}, {team})  # the shelf part and the expected part alike
			self.assertEqual(plans.team_for_line(a.soi), team)
			self.assertEqual(plans.teams_for_lines([a.soi, "NOT-A-LINE"]), {a.soi: team})  # the map allocation takes
			pool = pg.get_pool(day, self.c.farm, self.c.bucket.variety, self.c.bucket.stem_length)
			self.assertEqual({x["team"] for x in pool["confirmed_lines"] if x["soi"] == a.soi}, {team})
			self.assertIn(team, pg.get_board(day, self.c.farm, "all")["teams"])

	def test_each_order_line_has_its_own_team(self):
		a, b = self.c.lines[0], self.c.lines[1]
		day = str(a.delivery_date)
		with _Rollback():
			ta, tb = _team("ZZ TEST TEAM A"), _team("ZZ TEST TEAM B")
			# two lines of the same variety in one request, each with its own team
			pg.confirm_lines(
				day, self.c.farm,
				[{"sales_order_item": a.soi, "stems": 10, "team": ta}, {"sales_order_item": b.soi, "stems": 10, "team": tb}],
			)
			self.assertEqual(plans.team_for_line(a.soi), ta)
			self.assertEqual(plans.team_for_line(b.soi), tb)
			self.assertEqual(plans.teams_for_lines([a.soi, b.soi]), {a.soi: ta, b.soi: tb})
			# changing one line's team leaves the other alone
			pg.confirm_lines(day, self.c.farm, [{"sales_order_item": a.soi, "stems": 10, "team": tb}])
			self.assertEqual(plans.team_for_line(a.soi), tb)
			self.assertEqual(plans.team_for_line(b.soi), tb)
			pg.confirm_lines(day, self.c.farm, [{"sales_order_item": b.soi, "stems": 10, "team": ta}])
			self.assertEqual((plans.team_for_line(a.soi), plans.team_for_line(b.soi)), (tb, ta))
			rows = {x["soi"]: x["team"] for x in pg.get_pool(day, self.c.farm, self.c.bucket.variety, self.c.bucket.stem_length)["lines"]}
			self.assertEqual((rows[a.soi], rows[b.soi]), (tb, ta))

	def test_a_team_is_required_and_must_exist(self):
		a = self.c.lines[0]
		day = str(a.delivery_date)
		with _Rollback():
			lines = [{"sales_order_item": a.soi, "stems": 10}]
			with self.assertRaises(frappe.ValidationError) as ctx:
				pg.confirm_lines(day, self.c.farm, lines)
			self.assertIn("team", str(ctx.exception).lower())
			with self.assertRaises(frappe.ValidationError):
				pg.confirm_lines(day, self.c.farm, lines, team="NO SUCH TEAM")
			# once a line has a team, changing its stems needs no new choice; taking it out needs none at all
			pg.confirm_lines(day, self.c.farm, lines, team=_team())
			pg.confirm_lines(day, self.c.farm, [{"sales_order_item": a.soi, "stems": 5}])
			self.assertEqual(plans.team_for_line(a.soi), _team())
			pg.confirm_lines(day, self.c.farm, [{"sales_order_item": a.soi, "stems": 0}])

	def test_a_mixed_box_has_one_team_for_every_variety(self):
		row = frappe.db.sql(
			"""SELECT soi.parent, soi.custom_mix_group AS grp, so.delivery_date
			   FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
			   WHERE so.docstatus = 1 AND so.business_unit = 'Roses' AND soi.custom_mixed_box = 1 AND IFNULL(soi.custom_mix_group, '') != ''
			   GROUP BY soi.parent, soi.custom_mix_group HAVING COUNT(*) >= 2 ORDER BY so.delivery_date DESC LIMIT 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("no mixed box with two lines")
		members = frappe.get_all(
			"Sales Order Item",
			filters={"parent": row[0].parent, "custom_mix_group": row[0].grp, "custom_mixed_box": 1},
			fields=["name", "stock_qty"],
			limit_page_length=3,
		)
		day, farm = str(row[0].delivery_date), self.c.farm
		first, second = members[0], members[1]
		with _Rollback():
			a, b = _team("ZZ TEST TEAM A"), _team("ZZ TEST TEAM B")
			pg.confirm_lines(day, farm, [{"sales_order_item": first.name, "stems": 5}], team=a)
			# the second variety is not confirmed yet, but the box already has its team
			self.assertEqual(plans.group_team(pg._mix_key(pg._line(second.name))), a)
			pg.confirm_lines(day, farm, [{"sales_order_item": second.name, "stems": 5}])  # no team given: the box's
			self.assertEqual(plans.team_for_line(second.name), a)
			# choosing another team for one variety moves the whole box
			pg.confirm_lines(day, farm, [{"sales_order_item": second.name, "stems": 5}], team=b)
			self.assertEqual(plans.team_for_line(first.name), b)
			self.assertEqual(plans.team_for_line(second.name), b)


class TestSwapAndSplit(unittest.TestCase):
	def setUp(self):
		self.c = _case()
		if not self.c:
			self.skipTest("no case")
		self.a, self.b = self.c.lines[0], self.c.lines[1]
		self.day = str(self.a.delivery_date)

	def _held(self, soi):
		return sum(frappe.get_all("Bucket Reservation", filters={"sales_order_item": soi, "status": "Active"}, pluck="stems"))

	def _total(self, soi):
		return sum(c.stems - c.fulfilled_stems for c in plans.live_confirmations(soi=soi))

	def test_split_gives_part_of_the_confirmed_stems_to_another_customer(self):
		with _Rollback():
			ta, tb = _team("ZZ TEST TEAM A"), _team("ZZ TEST TEAM B")
			pg.confirm_lines(self.day, self.c.farm, [{"sales_order_item": self.a.soi, "stems": 10, "team": ta}])
			held_before = self._held(self.a.soi)
			r = pg.move_stems(self.day, self.c.farm, self.a.soi, self.b.soi, 4, team=tb)
			self.assertEqual((r["moved"], r["kept"], r["received"]), (4.0, 6.0, 4.0))
			self.assertEqual((self._total(self.a.soi), self._total(self.b.soi)), (6.0, 4.0))
			# what was held on the shelf is shared out, not lost or doubled
			self.assertAlmostEqual(self._held(self.a.soi) + self._held(self.b.soi), held_before, places=3)
			self.assertEqual((plans.team_for_line(self.a.soi), plans.team_for_line(self.b.soi)), (ta, tb))
			mirror = {x.sales_order_item: x.stems for x in frappe.get_all("Confirmed Stems", filters={"parent": ("in", [self.a.parent, self.b.parent])}, fields=["sales_order_item", "stems"])}
			self.assertEqual((mirror[self.a.soi], mirror[self.b.soi]), (6.0, 4.0))

	def test_swap_gives_all_of_it_and_the_giver_is_unconfirmed(self):
		with _Rollback():
			ta, tb = _team("ZZ TEST TEAM A"), _team("ZZ TEST TEAM B")
			pg.confirm_lines(self.day, self.c.farm, [{"sales_order_item": self.a.soi, "stems": 10, "team": ta}])
			r = pg.move_stems(self.day, self.c.farm, self.a.soi, self.b.soi, 10, team=tb)
			self.assertEqual(r["kept"], 0.0)
			self.assertFalse(plans.live_confirmations(soi=self.a.soi))
			self.assertEqual(self._total(self.b.soi), 10.0)
			self.assertFalse(frappe.db.exists("Confirmed Stems", {"parent": self.a.parent, "sales_order_item": self.a.soi}))

	def test_one_customers_stems_can_be_split_between_several_customers_at_once(self):
		if len(self.c.lines) < 3:
			self.skipTest("needs three lines of the same variety and length")
		c = self.c.lines[2]
		with _Rollback():
			ta, tb = _team("ZZ TEST TEAM A"), _team("ZZ TEST TEAM B")
			pg.confirm_lines(self.day, self.c.farm, [{"sales_order_item": self.a.soi, "stems": 10, "team": ta}])
			r = pg.split_stems(self.day, self.c.farm, self.a.soi, [
				{"to_soi": self.b.soi, "stems": 4, "team": tb}, {"to_soi": c.soi, "stems": 3, "team": ta}])
			self.assertEqual((r["moved"], r["kept"]), (7.0, 3.0))
			self.assertEqual([self._total(x) for x in (self.a.soi, self.b.soi, c.soi)], [3.0, 4.0, 3.0])
			self.assertEqual([plans.team_for_line(x) for x in (self.b.soi, c.soi)], [tb, ta])
			for bad, part in (
				([{"to_soi": self.b.soi, "stems": 1, "team": ta}, {"to_soi": self.b.soi, "stems": 1, "team": ta}], "only be chosen once"),
				([{"to_soi": self.b.soi, "stems": 2, "team": ta}, {"to_soi": c.soi, "stems": 2, "team": ta}], "only 3"),
				([], "how many"),
			):
				with self.assertRaises(frappe.ValidationError) as ctx:
					pg.split_stems(self.day, self.c.farm, self.a.soi, bad)
				self.assertIn(part, str(ctx.exception).lower())

	def test_what_cannot_be_moved_is_refused_in_plain_words(self):
		with _Rollback():
			ta = _team("ZZ TEST TEAM A")
			pg.confirm_lines(self.day, self.c.farm, [{"sales_order_item": self.a.soi, "stems": 10, "team": ta}])
			for args, part in (
				((self.a.soi, self.a.soi, 1), "another customer"),
				((self.a.soi, self.b.soi, 0), "how many"),
				((self.a.soi, self.b.soi, 11), "only 10"),
				((self.b.soi, self.a.soi, 1), "nothing confirmed"),
			):
				with self.assertRaises(frappe.ValidationError) as ctx:
					pg.move_stems(self.day, self.c.farm, *args, team=ta)
				self.assertIn(part, str(ctx.exception).lower())
			with self.assertRaises(frappe.ValidationError) as ctx:  # a team is needed for the receiver
				pg.move_stems(self.day, self.c.farm, self.a.soi, self.b.soi, 3)
			self.assertIn("team", str(ctx.exception).lower())
			self.assertEqual(self._total(self.a.soi), 10.0)  # nothing changed

	def test_a_mixed_box_can_receive_stems_but_not_give_them(self):
		mixed = frappe.db.sql(
			"""SELECT soi.name, soi.parent, soi.item_code, soi.custom_length, so.delivery_date
			   FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
			   WHERE so.docstatus = 1 AND so.business_unit = 'Roses' AND soi.custom_mixed_box = 1
			     AND IFNULL(soi.custom_mix_group, '') != '' AND soi.item_code = %s AND soi.custom_length = %s
			     AND so.delivery_date = %s AND soi.name != %s LIMIT 1""",
			(self.c.bucket.variety, self.c.bucket.stem_length, self.a.delivery_date, self.a.soi),
			as_dict=True,
		)
		if not mixed:
			self.skipTest("no mixed line of that variety and length on the day")
		m = mixed[0]
		with _Rollback():
			ta, tb = _team("ZZ TEST TEAM A"), _team("ZZ TEST TEAM B")
			pg.confirm_lines(self.day, self.c.farm, [{"sales_order_item": self.a.soi, "stems": 10, "team": ta}])
			pg.move_stems(self.day, self.c.farm, self.a.soi, m.name, 4, team=tb)  # a mixed box receives
			self.assertEqual(self._total(m.name), 4.0)
			self.assertEqual(plans.team_for_line(m.name), tb)
			with self.assertRaises(frappe.ValidationError) as ctx:  # but cannot give them away
				pg.move_stems(self.day, self.c.farm, m.name, self.a.soi, 1, team=ta)
			self.assertIn("packed together", str(ctx.exception).lower())

	def test_the_confirmed_tab_says_which_lines_are_mixed(self):
		with _Rollback():
			pg.confirm_lines(self.day, self.c.farm, [{"sales_order_item": self.a.soi, "stems": 10, "team": _team()}])
			rows = pg.get_pool(self.day, self.c.farm, self.c.bucket.variety, self.c.bucket.stem_length)["confirmed_lines"]
			self.assertEqual([r["mixed"] for r in rows if r["soi"] == self.a.soi], [False])

