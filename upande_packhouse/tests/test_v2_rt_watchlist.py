"""Bucket Watchlist + Inactive buckets (api/v2/rt_watchlist.py, rt_inactive.py, core/bucket_activity.py).

Every test runs inside a savepoint that is rolled back, with frappe.db.commit patched out; nothing
persists. Fixtures are inserted with db_insert (no validation) under bucket ids 'TWATCH-*'.
Tests that need the JTEST synthetic buckets or the busy local data skip when it is absent."""

import time
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from upande_packhouse.api.v2 import rt_inactive as INA
from upande_packhouse.api.v2 import rt_watchlist as W
from upande_packhouse.api.v2.core import bucket_activity as ACT

B = "TWATCH-001"


def ins(doctype, **vals):
	"""Insert one row without validation (fixtures only)."""
	d = frappe.new_doc(doctype)
	d.update(vals)
	d.name = vals.get("name") or frappe.generate_hash(length=10)
	d.creation = d.modified = vals.get("creation") or now_datetime()
	d.owner = d.modified_by = "Administrator"
	d.db_insert()
	return d


def ago(**kw):
	return add_to_date(now_datetime(), **{k: -v for k, v in kw.items()})


class Base(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.sql("SET SESSION MAX_JOIN_SIZE=18446744073709551615")
		frappe.db.savepoint("tw")
		self._c = patch.object(frappe.db, "commit", lambda *a, **k: None)
		self._c.start()

	def tearDown(self):
		self._c.stop()
		frappe.db.rollback(save_point="tw")

	def watch(self, bucket=B, since=None, source=W.SRC_MANUAL, **kw):
		since = since or ago(hours=2)
		return (
			W.add_auto(bucket, since, source, **kw)
			if source != W.SRC_MANUAL
			else frappe.get_doc(
				{
					"doctype": "Bucket Watch",
					"bucket_id": bucket,
					"status": "Watching",
					"source": source,
					"watching_since": since,
					"added_by": "Administrator",
					"added_on": since,
				}
			).insert(ignore_permissions=True)
		)

	def appearance(self, bucket=B):
		return W.latest_appearances([frappe.get_doc("Bucket Watch", bucket).as_dict()]).get(bucket)


class TestAutoAdd(Base):
	def test_replacement_missing_adds_issuing_watch(self):
		ins(
			"Bucket Replacement",
			old_bucket=B,
			new_bucket="TWATCH-002",
			status="Open",
			reason="Missing",
			order_name="ORD-1",
			reported_at=ago(minutes=5),
		)
		doc = frappe.get_doc(
			"Bucket Replacement", frappe.db.get_value("Bucket Replacement", {"old_bucket": B}, "name")
		)
		W.on_replacement_insert(doc)
		w = frappe.get_doc("Bucket Watch", B)
		self.assertEqual(w.source, W.SRC_ISSUING)
		self.assertEqual(w.status, "Watching")
		self.assertEqual(w.reference_doctype, "Bucket Replacement")

	def test_replacement_with_trip_or_vehicle_is_a_transfer(self):
		doc = frappe._dict(
			reason="Missing",
			old_bucket=B,
			farm="",
			trip="TRIP-1",
			vehicle="",
			notes="",
			order_name="O",
			order_pick_list="",
			reported_at=ago(minutes=1),
			name="BRPL-X",
		)
		W.on_replacement_insert(doc)
		self.assertEqual(frappe.db.get_value("Bucket Watch", B, "source"), W.SRC_TRANSFER)

	def test_other_reasons_are_not_not_found(self):
		doc = frappe._dict(
			reason="Damaged",
			old_bucket=B,
			farm="",
			trip="",
			vehicle="",
			notes="",
			order_name="O",
			order_pick_list="",
			reported_at=now_datetime(),
			name="BRPL-Y",
		)
		W.on_replacement_insert(doc)
		self.assertFalse(frappe.db.exists("Bucket Watch", B))

	def test_transfer_hook_adds_watch_and_never_raises(self):
		W.safe_add_transfer_not_found(B, "Simotwo", ["OPL-1"], now_datetime(), "n")
		w = frappe.get_doc("Bucket Watch", B)
		self.assertEqual((w.source, w.farm), (W.SRC_TRANSFER, "Simotwo"))
		with patch.object(W, "add_auto", side_effect=RuntimeError("boom")):
			self.assertIsNone(W.safe_add_transfer_not_found("TWATCH-009", "x", [], now_datetime()))
			W.on_replacement_insert(
				frappe._dict(
					reason="Missing",
					old_bucket="TWATCH-009",
					farm="",
					trip="",
					vehicle="",
					notes="",
					order_name="",
					order_pick_list="",
					reported_at=now_datetime(),
					name="Z",
				)
			)
		self.assertFalse(frappe.db.exists("Bucket Watch", "TWATCH-009"))

	def test_idempotent_and_reopens_on_a_newer_event(self):
		t = ago(hours=3)
		self.assertEqual(W.add_auto(B, t, W.SRC_ISSUING, "", "r", "Bucket Replacement", "BR-1"), "added")
		self.assertEqual(W.add_auto(B, t, W.SRC_ISSUING, "", "r", "Bucket Replacement", "BR-1"), "same")
		self.assertEqual(W.add_auto(B, ago(hours=4), W.SRC_ISSUING), "same")  # older event
		self.assertEqual(frappe.db.count("Bucket Watch", {"bucket_id": B}), 1)
		W.close_watch(B, "Confirmed missing", "gone")
		self.assertEqual(W.add_auto(B, ago(hours=4), W.SRC_ISSUING), "same")  # old event, closed since
		self.assertEqual(
			W.add_auto(B, now_datetime(), W.SRC_TRANSFER, "Karen", "again", "Pick List Item", "x"), "reopened"
		)
		w = frappe.get_doc("Bucket Watch", B)
		self.assertEqual((w.status, w.source, w.close_note), ("Watching", W.SRC_TRANSFER, ""))

	def test_jtest_not_found_bucket_backfills_from_the_pick_row(self):
		row = frappe.db.sql(
			"SELECT not_found_at FROM `tabPick List Item` WHERE bucket = 'JTEST-002' AND not_found = 1 LIMIT 1"
		)
		if not row:
			self.skipTest("JTEST-002 synthetic data not present")
		frappe.db.delete("Bucket Watch", {"bucket_id": "JTEST-002"})
		r = W.sync_not_found()
		self.assertGreaterEqual(r["transfers"], 1)
		w = frappe.get_doc("Bucket Watch", "JTEST-002")
		self.assertEqual(w.source, W.SRC_TRANSFER)
		self.assertEqual(str(w.watching_since)[:19], str(row[0][0])[:19])
		n = frappe.db.count("Bucket Watch")
		W.sync_not_found()  # second run: nothing new
		self.assertEqual(frappe.db.count("Bucket Watch"), n)


class TestReappearance(Base):
	"""Each event source, after the watch began, makes the bucket reappear; the excluded ones do not."""

	def setUp(self):
		super().setUp()
		self.t0 = ago(hours=5)
		W.add_auto(B, self.t0, W.SRC_ISSUING)  # auto: 3 min grace
		self.after = ago(hours=1)

	def seen(self):
		return self.appearance()

	def test_nothing_yet(self):
		self.assertIsNone(self.seen())

	def test_before_start_and_inside_grace_do_not_count(self):
		ins("Shelving Log", bucket_id=B, shelf="S1", shelved_on=ago(hours=9))
		ins("Shelving Log", bucket_id=B, shelf="S1", shelved_on=add_to_date(self.t0, seconds=60))
		self.assertIsNone(self.seen())

	def test_shelving_log(self):
		ins("Shelving Log", bucket_id=B, shelf="K13T", shelved_on=self.after, farm="Kapkolia")
		e = self.seen()
		self.assertEqual((e["kind"], e["loc"], e["farm"]), ("shelve", "K13T", "Kapkolia"))

	def test_shelving_log_removal_counts_but_shelf_clear_does_not(self):
		ins(
			"Shelving Log",
			bucket_id=B,
			shelf="S9",
			shelved_on=ago(hours=20),
			removed_on=self.after,
			reason="Shelf Cleared",
		)
		self.assertIsNone(self.seen())
		ins(
			"Shelving Log",
			bucket_id=B,
			shelf="S9",
			shelved_on=ago(hours=20),
			removed_on=self.after,
			reason="Issued to Sales Order",
		)
		self.assertEqual(self.seen()["kind"], "off_shelf")

	def test_stock_entry(self):
		ins(
			"Stock Entry",
			stock_entry_type="Receiving",
			custom_bucket_id=B.lower(),
			docstatus=1,
			posting_date=self.after.date(),
			posting_time=self.after.time(),
			farm="Karen",
		)
		e = self.seen()
		self.assertEqual((e["kind"], e["title"]), ("stock_entry", "Receiving"))

	def test_pick_row_issue_and_loading_but_not_allocation(self):
		opl = frappe.db.get_value("Order Pick List", {"docstatus": ["<", 2]}, "name")
		if not opl:
			self.skipTest("no Order Pick List")
		ins(
			"Pick List Item",
			parent=opl,
			parenttype="Order Pick List",
			parentfield="locations",
			bucket=B,
			shelf="P1",
			farm="Karen",
			creation=self.after,
		)
		self.assertIsNone(self.seen())  # allocation is not an appearance
		frappe.db.sql(
			"UPDATE `tabPick List Item` SET loaded_on_truck_at = %s WHERE bucket = %s", (self.after, B)
		)
		self.assertEqual(self.seen()["title"], "Loaded on truck")
		frappe.db.sql(
			"UPDATE `tabPick List Item` SET issued_at = %s WHERE bucket = %s",
			(add_to_date(self.after, minutes=5), B),
		)
		self.assertEqual(self.seen()["title"], "Issued")

	def test_transfer_event(self):
		ins(
			"Bucket Transfer Event",
			bucket=B,
			stage="On trolley",
			outcome="Done",
			event_time=self.after,
			farm="Torongo",
		)
		self.assertEqual(self.seen()["title"], "On trolley")

	def test_used_as_the_replacement(self):
		ins(
			"Bucket Replacement",
			old_bucket="TWATCH-777",
			new_bucket=B,
			status="Open",
			reason="Missing",
			reported_at=self.after,
			new_shelf="R1",
		)
		self.assertIn("replacement", self.seen()["title"])

	def test_replaced_out_is_not_an_appearance(self):
		ins(
			"Bucket Replacement",
			old_bucket=B,
			new_bucket="TWATCH-778",
			status="Open",
			reason="Missing",
			reported_at=self.after,
		)
		self.assertIsNone(self.seen())

	def test_stem_replacement_and_counts(self):
		if not frappe.db.table_exists("Stem Replacement Log"):
			self.skipTest("Stem Replacement Log is not installed on this site")
		ins("Stem Replacement Log", donor_bucket=B, destination_bucket="TWATCH-5", replaced_at=self.after)
		self.assertEqual(self.seen()["kind"], "replace")
		ins(
			"Bucket Count Bucket",
			parent=frappe.db.get_value("Bucket Count", {}, "name") or "BC-X",
			parenttype="Bucket Count",
			parentfield="buckets",
			bucket_id=B,
			location="Cold room",
			scanned_at=add_to_date(self.after, minutes=30),
		)
		self.assertEqual(self.seen()["title"], "Scanned in a bucket count")

	def test_latest_wins(self):
		ins("Shelving Log", bucket_id=B, shelf="A", shelved_on=ago(hours=3))
		ins("Shelving Log", bucket_id=B, shelf="B", shelved_on=ago(hours=1))
		self.assertEqual(self.seen()["loc"], "B")

	def test_get_is_read_only_and_run_persists(self):
		ins("Shelving Log", bucket_id=B, shelf="K13T", shelved_on=self.after, farm="Kapkolia")
		before = frappe.db.get_value("Bucket Watch", B, ["status", "modified", "last_seen_at"], as_dict=True)
		r = W.get_watchlist(search=B)
		row = next(x for x in r["rows"] if x["bucket_id"] == B)
		self.assertEqual((row["status"], row["live_status"]), ("Watching", "Reappeared"))
		self.assertEqual(
			frappe.db.get_value("Bucket Watch", B, ["status", "modified", "last_seen_at"], as_dict=True),
			before,
		)  # nothing written
		with (
			patch.object(W, "_journey_facts", return_value=(3, "On shelf K13T")),
			patch.object(W, "sync_not_found", return_value={"added": 0}),
		):
			W.run_watchlist(search=B)
		w = frappe.get_doc("Bucket Watch", B)
		self.assertEqual(
			(w.status, w.last_seen_journey, w.last_seen_stage), ("Reappeared", 3, "On shelf K13T")
		)
		self.assertIn("K13T", w.last_seen_event)
		row = next(x for x in W.get_watchlist(search=B)["rows"] if x["bucket_id"] == B)
		self.assertEqual((row["journey"], row["stage"]), (3, "On shelf K13T"))


class TestClosing(Base):
	def test_outcomes(self):
		self.watch()
		self.assertEqual(W.close_watch(B, "Found - resolved", "on K1")["status"], W.RESOLVED)
		self.assertEqual(frappe.db.get_value("Bucket Watch", B, "closed_by"), "Administrator")
		W.reopen_watch(B)
		self.assertEqual(frappe.db.get_value("Bucket Watch", B, "status"), "Watching")
		self.assertEqual(W.close_watch(B, "Confirmed missing")["status"], W.CONFIRMED)
		self.assertFalse(W.close_watch(B, "whatever")["success"])
		self.assertFalse(W.close_watch("NOPE", "Confirmed missing")["success"])

	def test_kpis_separate_found_from_truly_missing(self):
		for i, o in enumerate(("Found - resolved", "Confirmed missing", None, None)):
			b = "TWATCH-1%d" % i
			self.watch(b)
			if o:
				W.close_watch(b, o)
		ins("Shelving Log", bucket_id="TWATCH-13", shelf="Z", shelved_on=ago(minutes=5))
		k = W.get_watchlist(search="TWATCH-1")["kpi"]
		self.assertEqual((k["watching"], k["reappeared"], k["resolved"], k["confirmed"]), (1, 1, 1, 1))
		rows = W.get_watchlist(search="TWATCH-1", status="confirmed")["rows"]
		self.assertEqual([r["bucket_id"] for r in rows], ["TWATCH-11"])

	def test_add_watch_reopens_and_remove(self):
		ins("Shelving Log", bucket_id=B, shelf="Z", shelved_on=ago(days=9))
		self.assertEqual(W.add_watch(B, "n1")["action"], "added")
		self.assertEqual(W.add_watch(B, "n2")["action"], "already watching")
		W.close_watch(B, "Confirmed missing")
		self.assertEqual(W.add_watch(B)["action"], "reopened")
		self.assertEqual(frappe.db.count("Bucket Watch", {"bucket_id": B.lower()}), 1)
		self.assertFalse(W.add_watch("")["success"])
		self.assertTrue(W.remove_watch(B)["success"])
		self.assertFalse(frappe.db.exists("Bucket Watch", B))


class TestSetting(Base):
	def test_default_is_four_and_override(self):
		with patch.object(frappe.db, "get_single_value", return_value=None):
			self.assertEqual(INA.inactive_days(), 4)
		with patch.object(frappe.db, "get_single_value", return_value=0):
			self.assertEqual(INA.inactive_days(), 4)
		with patch.object(frappe.db, "get_single_value", return_value=9):
			self.assertEqual(INA.inactive_days(), 9)
		with patch.object(frappe.db, "get_single_value", side_effect=Exception("no column")):
			self.assertEqual(INA.inactive_days(), 4)

	def test_field_exists_on_production_settings_with_default_4(self):
		if not frappe.db.exists("DocType", "Production Settings"):
			self.skipTest("Production Settings (upande_quality) is not installed on this site")
		f = frappe.get_meta("Production Settings").get_field("inactive_bucket_days")
		self.assertIsNotNone(f)
		self.assertEqual((f.fieldtype, str(f.default)), ("Int", "4"))


class TestInactive(Base):
	def shelve(self, b, shelved_days_ago, stems=50, closed=False, shelf=None):
		on = ago(days=shelved_days_ago)
		shelf = shelf or frappe.db.get_value("Shelf", {}, "name")
		ins(
			"Shelving Log",
			bucket_id=b,
			shelf=shelf,
			farm="Karen",
			variety="V",
			stem_qty=stems,
			shelved_on=on,
			removed_on=on + timedelta(hours=1) if closed else None,
			reason="Issued to Sales Order" if closed else "",
		)
		if not closed:
			ins(
				"Shelf Item",
				parent=shelf,
				parenttype="Shelf",
				parentfield="items",
				bucket_id=b,
				variety="V",
				stem_qty=stems,
				date_added=on,
				farm="Karen",
			)

	def rows(self, days=4):
		n, rows, _ = INA.compute(days=days, search="TWATCH")
		return n, {r["bucket"]: r for r in rows}

	def test_definition(self):
		if not frappe.db.get_value("Shelf", {}, "name"):
			self.skipTest("no shelves")
		self.shelve("TWATCH-A", 10)  # idle 10 d on a shelf: inactive
		self.shelve("TWATCH-B", 1)  # active yesterday
		self.shelve("TWATCH-C", 10, closed=True)  # fully issued, idle in the field: NOT in the process
		self.shelve("TWATCH-D", 10)  # old shelving but a recent transfer event: active
		ins(
			"Bucket Transfer Event",
			bucket="TWATCH-D",
			stage="On trolley",
			outcome="Done",
			event_time=ago(days=1),
		)
		self.shelve("TWATCH-E", 6)  # 6 d idle: inactive at 4, not at 7
		n, r = self.rows(4)
		self.assertEqual(n, 4)
		self.assertEqual(set(r), {"TWATCH-A", "TWATCH-E"})
		self.assertEqual(r["TWATCH-A"]["stage"], "On shelf")
		self.assertAlmostEqual(r["TWATCH-A"]["days_idle"], 10, delta=0.1)
		self.assertEqual(r["TWATCH-A"]["stems"], 50)
		self.assertEqual(set(self.rows(7)[1]), {"TWATCH-A"})
		self.assertEqual(self.rows(30)[1], {})

	def test_open_log_without_shelf_item_and_open_watch_flag(self):
		self.shelve("TWATCH-F", 9)
		frappe.db.sql("DELETE FROM `tabShelf Item` WHERE bucket_id = 'TWATCH-F'")
		self.watch("TWATCH-F")
		r = self.rows(4)[1]["TWATCH-F"]
		self.assertEqual(r["stage"], "Open shelving log")
		self.assertTrue(r["watched"])

	def test_default_days_come_from_settings(self):
		with patch.object(frappe.db, "get_single_value", return_value=11):
			self.assertEqual(INA.compute(search="TWATCH")[0], 11)
		self.assertEqual(INA.compute(days=3, search="TWATCH")[0], 3)

	def test_endpoint_shape_and_cap(self):
		self.shelve("TWATCH-G", 20)
		r = INA.get_inactive_buckets(days=4, search="TWATCH", limit=1)
		self.assertTrue(r["success"])
		self.assertEqual((r["count"], r["kpi"]["buckets"], r["kpi"]["oldest_days"] >= 19.9), (1, 1, True))
		self.assertEqual(r["stages"], {"On shelf": 1})


class TestReconcileAndSpeed(Base):
	def test_inactive_list_reconciles_with_raw_sql(self):
		"""For buckets on a shelf (or an open shelving log, no transfer / allocation stage) the
		inactive set equals 'in the process AND no event newer than the cutoff', worked out here
		with plain SQL against each source table."""
		days = 4
		t0 = time.time()
		n, rows, in_process = INA.compute(days=days)
		took = time.time() - t0
		cutoff = now_datetime() - timedelta(days=days)
		mine = {r["bucket"] for r in rows if r["stage"] in ("On shelf", "Open shelving log")}
		stage_ids = {
			r.b
			for r in frappe.db.sql(
				"""SELECT UPPER(bucket_id) b FROM `tabShelf Item` WHERE IFNULL(bucket_id,'') != '' AND IFNULL(stem_qty,0) > 0
			UNION SELECT UPPER(bucket_id) FROM `tabShelving Log` WHERE removed_on IS NULL AND IFNULL(bucket_id,'') != ''""",
				as_dict=True,
			)
		}
		# buckets whose stage is something else (transit / allocation) are out of this comparison
		other = {r["bucket"] for r in rows if r["stage"] not in ("On shelf", "Open shelving log")}
		ids = tuple(stage_ids)
		if not ids:
			self.skipTest("no bucket is on a shelf on this site")
		P = {"ids": ids, "c": cutoff, "d": cutoff.date() - timedelta(days=1)}
		active = {
			r[0]
			for r in frappe.db.sql(
				"""SELECT UPPER(bucket_id) FROM `tabShelving Log` WHERE bucket_id IN %(ids)s AND (shelved_on >= %(c)s OR (removed_on >= %(c)s AND IFNULL(reason,'') != 'Shelf Cleared'))
			UNION SELECT UPPER(bucket_id) FROM `tabShelf Item` WHERE bucket_id IN %(ids)s AND date_added >= %(c)s
			UNION SELECT UPPER(custom_bucket_id) FROM `tabStock Entry` WHERE custom_bucket_id IN %(ids)s AND docstatus < 2 AND posting_date >= %(d)s
			      AND TIMESTAMP(posting_date, posting_time) >= %(c)s
			UNION SELECT UPPER(p.bucket) FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name = p.parent WHERE p.bucket IN %(ids)s AND o.docstatus < 2 AND p.parenttype = 'Order Pick List'
			      AND GREATEST(IFNULL(p.creation,'2000-01-01'), IFNULL(p.loaded_in_trolley_at,'2000-01-01'), IFNULL(p.loaded_on_truck_at,'2000-01-01'), IFNULL(p.left_farm_at,'2000-01-01'),
			                   IFNULL(p.arrived_at,'2000-01-01'), IFNULL(p.shelved_at,'2000-01-01'), IFNULL(p.issued_at,'2000-01-01')) >= %(c)s
			UNION SELECT UPPER(b.bucket_id) FROM `tabBucket Allocations` a JOIN `tabBucket Allocation Status` b ON b.name = a.parent WHERE b.bucket_id IN %(ids)s AND a.creation >= %(c)s AND IFNULL(a.cancelled,0) = 0
			UNION SELECT UPPER(bucket) FROM `tabBucket Transfer Event` WHERE bucket IN %(ids)s AND event_time >= %(c)s
			UNION SELECT UPPER(old_bucket) FROM `tabBucket Replacement` WHERE old_bucket IN %(ids)s AND (reported_at >= %(c)s OR resolved_at >= %(c)s)
			UNION SELECT UPPER(new_bucket) FROM `tabBucket Replacement` WHERE new_bucket IN %(ids)s AND (reported_at >= %(c)s OR resolved_at >= %(c)s)""",
				P,
			)
		}
		expected = (stage_ids - active) - other
		self.assertEqual(
			mine,
			expected,
			"only in list: %s; only in SQL: %s" % (sorted(mine - expected)[:5], sorted(expected - mine)[:5]),
		)
		self.assertEqual(n, days)
		self.assertLess(took, 8, "inactive list took %.1fs (target 3s locally)" % took)

	def test_kpis_follow_the_rows(self):
		r = INA.get_inactive_buckets(days=4, limit=0)
		self.assertEqual(r["kpi"]["buckets"], len(r["rows"]))
		self.assertAlmostEqual(r["kpi"]["stems"], sum(x["stems"] for x in r["rows"]), delta=1)
		self.assertEqual(sum(r["stages"].values()), r["count"])

	def test_watchlist_with_hundreds_of_watches_is_fast(self):
		ids = [
			r[0]
			for r in frappe.db.sql(
				"SELECT DISTINCT UPPER(bucket_id) FROM `tabShelving Log` WHERE bucket_id IS NOT NULL AND bucket_id != '' ORDER BY shelved_on DESC LIMIT 300"
			)
		]
		if len(ids) < 100:
			self.skipTest("not enough buckets")
		for b in ids:
			if not frappe.db.exists("Bucket Watch", b):
				ins(
					"Bucket Watch",
					name=b,
					bucket_id=b,
					status="Watching",
					source=W.SRC_MANUAL,
					watching_since=ago(days=20),
					added_on=ago(days=20),
					added_by="Administrator",
				)
		t0 = time.time()
		r = W.get_watchlist()
		took = time.time() - t0
		self.assertGreaterEqual(r["kpi"]["total"], len(ids))
		self.assertLess(took, 5, "get_watchlist for %d watches took %.1fs" % (r["kpi"]["total"], took))
