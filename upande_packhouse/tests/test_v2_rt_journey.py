"""Bucket Journey v2 (api/v2/rt_journey.py).

Builder tests use pure in-memory fixtures (no database). The *_site tests read the local
site read-only: nothing is written or committed, and they skip when the data is absent."""

import unittest

import frappe

from upande_packhouse.api.v2 import rt_journey as RJ


def se(name, typ, date, time, qty=0, **kw):
	d = dict(name=name, stock_entry_type=typ, posting_date=date, posting_time=time, creation=f"{date} {time}", owner="u@x.com", farm="Karen",
		custom_greenhouse="GH 1 - K", custom_stem_length="52cm", qty=qty, items=[{"item": "Aqua", "qty": qty, "length": "52cm", "s": "A - K", "t": "B - K"}] if qty else [])
	d.update(kw)
	return d


def R(**kw):
	base = dict(id="B1", ses=[], cancelled=[], shelf_log=[], shelf_items=[], log_comments=[], discard_rows=[], pli=[], bas=[], bas_alloc=[],
		replacements=[], anomalies=[], tevents=[], stem_repl=[], pack_reject=[], stock_take=[], bucket_count=[], trace=[], qr={})
	base.update(kw)
	return base


def log(name, shelf, on, off=None, reason="Shelved", qty=50, **kw):
	d = dict(name=name, shelf=shelf, farm="Kap", variety="Aqua", stem_qty=qty, stem_length="52cm", greenhouse="", shelved_on=on, removed_on=off, reason=reason,
		shelved_by="u", harvest_date=None, receiving_date=None, shelf_item=None, harvester=None, graded_by=None, cut_stage=None)
	d.update(kw)
	return d


def pli(opl, name, bucket="B1", qty=10, created="2026-10-02 09:00:00", **kw):
	d = dict(name=name, opl=opl, parent=opl, idx=1, bucket=bucket, stock_qty=qty, creation=created, modified=created, shelf="K-1", issued=0, issued_offline=0,
		not_found=0, opl_docstatus=1, opl_customer="Cust", opl_order_name=opl + "-ORD", item_code="Aqua", custom_box_id=1)
	d.update(kw)
	return d


def two_cycles():
	"""Two cycles the same day (08:00 and 14:00): the date-tie case."""
	return [
		se("H1", "Harvesting", "2026-10-01", "08:00:00", 10, custom_receiving_entry="R1"), se("H1b", "Harvesting", "2026-10-01", "08:00:30", 10, custom_receiving_entry="R1"),
		se("R1", "Receiving", "2026-10-01", "09:00:00", 20, custom_receiving_batch_id="BATCH1"),
		se("H2", "Harvesting", "2026-10-01", "14:00:00", 30, custom_receiving_entry="R2"),
		se("R2", "Receiving", "2026-10-01", "15:00:00", 30, custom_receiving_batch_id="BATCH2"),
	]


def cyc(res):
	return res["cycles"]


def evs(c, ev):
	return [s for s in c["steps"] + c["ledger"] if s["ev"] == ev]


class TestCycles(unittest.TestCase):
	def test_scans_of_one_receiving_are_one_journey_and_next_harvest_starts_another(self):
		res = RJ.build_bucket(R(ses=two_cycles()))
		self.assertEqual(len(cyc(res)), 2)
		self.assertEqual(cyc(res)[0]["stems"], 20)
		self.assertEqual(cyc(res)[1]["stems"], 30)
		self.assertEqual(len(evs(cyc(res)[0], "harvest")), 1)  # 2 scans, one step

	def test_same_day_tie_places_by_full_timestamp_not_date(self):
		r = R(ses=two_cycles(), shelf_log=[log("L1", "K-1", "2026-10-01 10:00:00", "2026-10-01 11:00:00", "Issued to Sales Order"), log("L2", "K-2", "2026-10-01 16:00:00")],
			pli=[pli("OPL1", "P1", created="2026-10-01 09:30:00"), pli("OPL2", "P2", created="2026-10-01 16:30:00")])
		res = RJ.build_bucket(R(**r))
		c1, c2 = cyc(res)
		self.assertEqual([s["where"] for s in evs(c1, "shelve")], ["K-1 · Kap"])
		self.assertEqual([s["where"] for s in evs(c2, "shelve")], ["K-2 · Kap"])
		self.assertEqual([s["opl"] for s in c1["steps"] if s["ev"] == "allocate"], ["OPL1"])
		self.assertEqual([s["opl"] for s in c2["steps"] if s["ev"] == "allocate"], ["OPL2"])

	def test_no_harvest_makes_a_journey_instead_of_dropping(self):
		res = RJ.build_bucket(R(shelf_log=[log("L1", "K-1", "2026-10-01 10:00:00")]))
		self.assertEqual(len(cyc(res)), 1)
		self.assertEqual(cyc(res)[0]["kind"], "no_harvest")
		self.assertTrue(any("No harvest" in f["text"] for f in cyc(res)[0]["flags"]))

	def test_event_without_time_is_listed_unplaced(self):
		res = RJ.build_bucket(R(ses=two_cycles(), shelf_log=[log("L1", "K-1", None)]))
		self.assertEqual(len(res["unplaced"]), 1)
		self.assertIn("no timestamp", res["unplaced"][0]["reason"])


class TestQuality(unittest.TestCase):
	def test_quarantine_release_reject_with_quantities_placed_by_batch(self):
		ses = two_cycles() + [
			se("Q1", "Receiving Quarantined", "2026-10-01", "09:10:00", 20, custom_receiving_batch_id="BATCH1", custom_bucket_id="b1"),
			# released AFTER cycle 2 started: only the batch can place it
			se("A1", "Quarantine Accept", "2026-10-01", "16:00:00", 15, custom_receiving_batch_id="BATCH1"),
			se("X1", "Quarantine Rejects", "2026-10-01", "16:00:01", 5, custom_receiving_batch_id="BATCH1"),
		]
		c1, c2 = cyc(RJ.build_bucket(R(ses=ses)))
		self.assertEqual((c1["quarantined_stems"], c1["accepted_stems"], c1["rejected_stems"]), (20, 15, 5))
		self.assertEqual(len(evs(c2, "accept")), 0)
		self.assertIn("5 stems rejected", evs(c1, "reject")[0]["line"])

	def test_open_quarantine_is_current_stage(self):
		ses = two_cycles()[:3] + [se("Q1", "Receiving Quarantined", "2026-10-01", "09:10:00", 20, custom_receiving_batch_id="BATCH1")]
		self.assertEqual(cyc(RJ.build_bucket(R(ses=ses)))[0]["stage"]["key"], "quarantine")

	def test_coldroom_reject_and_ledger_legs_are_kept(self):
		ses = two_cycles() + [se("C1", "Coldroom Rejects", "2026-10-01", "10:00:00", 3), se("M1", "Move To Graded Sold", "2026-10-01", "10:30:00", 5)]
		c1 = cyc(RJ.build_bucket(R(ses=ses)))[0]
		self.assertEqual(len(evs(c1, "reject")), 1)
		self.assertEqual(len(c1["ledger"]), 1)


class TestDiscardAndAnomaly(unittest.TestCase):
	def test_discard_se_listed_not_discarded_and_stage(self):
		d = dict(name="D1", parent="DR1", discarded=0, farm="Karen", shelf="K-1", age_days=5, variety="Aqua", stem_qty=20, stem_length="52cm", harvest_date="2026-10-01 08:00:00",
			creation="2026-10-04 00:00:00", workflow_state="Approved", requested_date="2026-10-04", approval_date="2026-10-04", docstatus=1)
		res = RJ.build_bucket(R(ses=two_cycles(), discard_rows=[d]))
		c1, c2 = cyc(res)
		self.assertEqual(len(evs(c1, "discard_listed")) + len(evs(c2, "discard_listed")), 1)
		listed = (evs(c1, "discard_listed") or evs(c2, "discard_listed"))[0]
		self.assertIn("never discarded", listed["flags"][0])
		ses = two_cycles() + [se("DS", "Discard", "2026-10-05", "10:00:00", 30, creation="2026-10-05 10:00:00")]
		self.assertEqual(cyc(RJ.build_bucket(R(ses=ses)))[1]["stage"]["key"], "discarded")

	def test_reuse_anomaly_lands_on_the_cycle_its_harvest_started(self):
		a = dict(name="BRA1", skipped_step="Issuing Skipped", detected_on="2026-10-01 14:00:01", farm="Karen", greenhouse="", variety="Aqua", stems=20, previous_shelf="K-1",
			discard_request=None, harvest_stock_entry="H2")
		c1, c2 = cyc(RJ.build_bucket(R(ses=two_cycles(), anomalies=[a])))
		self.assertTrue([s for s in c2["steps"] if s.get("anomaly") and not s.get("leftover")])
		self.assertTrue([s for s in c1["steps"] if s.get("leftover")])
		self.assertTrue(any("anomaly" in f["text"].lower() for f in c2["flags"]))

	def test_skipped_transfer_placed_by_time(self):
		a = dict(name="BRA2", skipped_step="Skipped Transfer", detected_on="2026-10-01 09:30:00", farm="Kap", greenhouse="", variety="Aqua", stems=20, previous_shelf="REM-1",
			discard_request=None, harvest_stock_entry=None)
		c1, c2 = cyc(RJ.build_bucket(R(ses=two_cycles(), anomalies=[a])))
		self.assertEqual(len(evs(c1, "anomaly")), 1)
		self.assertEqual(len(evs(c2, "anomaly")), 0)


class TestAllocation(unittest.TestCase):
	def test_every_pick_row_shows_with_totals_and_cancelled_opl(self):
		rows = [pli("OPL1", "P%d" % i, qty=10, idx=i, custom_box_id=i) for i in range(1, 11)] + [pli("OPL2", "Q1", qty=30, issued=1), pli("OPL3", "Z1", qty=7, opl_docstatus=2)]
		c = cyc(RJ.build_bucket(R(ses=two_cycles()[:3], pli=rows)))[0]
		al = [s for s in c["steps"] if s["ev"] == "allocate" and "opl_all" in s]
		self.assertEqual(sum(s["rows"] for s in al), 12)
		self.assertEqual(sum(s["stems"] for s in al), 137)
		o1 = next(s for s in al if s["opl_all"] == "OPL1")
		self.assertEqual((o1["rows"], o1["stems"]), (10, 100))
		self.assertEqual(len(o1["table"]["rows"]), 10)
		self.assertTrue(next(s for s in al if s["opl_all"] == "OPL3")["cancelled"])
		self.assertEqual(c["allocated_stems"], 130)  # the cancelled pick list is not counted
		self.assertEqual(c["issued_stems"], 30)
		self.assertEqual(c["stage"]["key"], "partly_issued")

	def test_replaced_row_follows_the_replacement_time(self):
		rep = dict(name="BR1", old_bucket="OLD", new_bucket="B1", status="Open", reason="Missing", order_pick_list="OPL1", order_name="ORD", reported_at="2026-10-01 16:10:00", reported_by="u",
			farm="Karen", notes=None, expected_shelf=None, new_shelf=None, trip=None, vehicle=None, stems=10, resolved_at=None, resolution=None)
		# the pick row keeps the creation time of the order made for the OLD bucket (before this bucket's 2nd cycle)
		c1, c2 = cyc(RJ.build_bucket(R(ses=two_cycles(), pli=[pli("OPL1", "P1", created="2026-10-01 09:30:00")], replacements=[rep])))
		self.assertEqual(len([s for s in c2["steps"] if s["ev"] == "allocate" and s.get("opl")]), 1)
		self.assertIn("Used as the replacement for OLD while issuing", evs(c2, "replace")[0]["title"])

	def test_replacement_direction_for_the_old_bucket(self):
		rep = dict(name="BR1", old_bucket="B1", new_bucket="NEW", status="Open", reason="Missing", order_pick_list="OPL1", order_name="ORD", reported_at="2026-10-01 10:10:00", reported_by="u",
			farm="Karen", trip="T1", vehicle="V1", notes=None, expected_shelf=None, new_shelf=None, stems=10, resolved_at=None, resolution=None)
		c1 = cyc(RJ.build_bucket(R(ses=two_cycles(), replacements=[rep])))[0]
		self.assertEqual(evs(c1, "replace")[0]["title"], "Replaced by NEW during a transfer")


class TestShelf(unittest.TestCase):
	def test_reasons_are_read_the_right_way(self):
		rows = [
			log("A", "K-1", "2026-10-01 10:00:00", "2026-10-01 10:30:00", RJ.S2S),
			log("B", "K-2", "2026-10-01 10:30:10", "2026-10-01 11:00:00", "Replaced"),
			log("C", "K-3", "2026-10-01 11:10:00", "2026-10-01 12:00:00", "Offline Issuing"),
			log("D", "K-4", "2026-10-01 12:10:00", "2026-10-01 12:10:00", "Shelf Cleared"),
		]
		c1 = cyc(RJ.build_bucket(R(ses=two_cycles(), shelf_log=rows, log_comments=[dict(reference_name="C", content="<p>Damaged</p>", creation="x", owner="u")])))[0]
		titles = [s["title"] for s in c1["steps"]]
		self.assertIn("Moved to another shelf", titles)  # not "Transferred out"/"In transit"
		self.assertIn("Used as a replacement for another bucket", titles)
		self.assertIn("Issued offline", titles)
		self.assertTrue(any("Damaged" in s["line"] for s in evs(c1, "issue")))
		self.assertEqual(sum(1 for s in c1["steps"] if s["ev"] == "shelve"), 2)  # the moved-to row and the phantom cleared row have no shelving step of their own
		self.assertTrue(any("no shelving record" in s["line"] for s in evs(c1, "clear")))

	def test_open_log_without_shelf_item_is_flagged_and_closed_by_the_next_harvest(self):
		rows = [log("A", "K-1", "2026-10-01 10:00:00", shelf_item="GONE")]
		c1, c2 = cyc(RJ.build_bucket(R(ses=two_cycles(), shelf_log=rows)))
		self.assertTrue(any("never closed" in f["text"] for f in c1["flags"]))
		self.assertNotEqual(c1["stage"]["key"], "on_shelf")

	def test_live_shelf_item_is_the_current_stage(self):
		item = dict(name="SI1", parent="K-9", farm="Kap", variety="Aqua", stem_qty=20, stem_length="52cm", greenhouse="", date_added="2026-10-01 16:00:00", harvest_date=None, receiving_date=None)
		c2 = cyc(RJ.build_bucket(R(ses=two_cycles(), shelf_items=[item])))[1]
		self.assertEqual(c2["stage"]["label"], "On shelf K-9")


class TestTransfer(unittest.TestCase):
	def test_trace_current_stage(self):
		tr = dict(opl="OPL1", order_name="ORD", from_farm="Karen", to_farm="Kap", state="in_transit", events=[
			dict(stage="Awaiting transfer", datetime="2026-10-01 10:00:00", user="u", detail="x"), dict(stage="Loaded on truck", datetime="2026-10-01 11:00:00", user="u", detail="V1", trip="T1"),
			dict(stage="In transit", datetime="2026-10-01 12:00:00", user="u", detail="left")])
		c1 = cyc(RJ.build_bucket(R(ses=two_cycles(), pli=[pli("OPL1", "P1", created="2026-10-01 09:30:00", awaiting_transfer=1)], trace=[tr])))[0]
		self.assertEqual([s["ev"] for s in c1["steps"] if s["ev"].startswith("transfer")], ["transfer_req", "transfer_transit", "transfer_transit"])

	def test_not_found_is_a_step(self):
		c1 = cyc(RJ.build_bucket(R(ses=two_cycles(), pli=[pli("OPL1", "P1", created="2026-10-01 09:30:00", not_found=1, not_found_at="2026-10-01 10:00:00")])))[0]
		self.assertEqual(len(evs(c1, "not_found")), 1)


class TestHelpers(unittest.TestCase):
	def test_ts_pads_unpadded_time(self):
		self.assertEqual(RJ._ts("2026-10-07 15:33:8"), "2026-10-07 15:33:08")
		self.assertEqual(RJ._ts("2026-10-07"), "2026-10-07 00:00:00")
		self.assertEqual(RJ._ts("2026-10-07 09:07:00.964374"), "2026-10-07 09:07:00")

	def test_flow_covers_every_event_key_used(self):
		keys = {f[0] for f in RJ.FLOW}
		for k in ("harvest", "grade", "receive", "quarantine", "release", "reject", "accept", "shelve", "shelf_move", "transfer_req", "transfer_transit", "transfer_recv", "allocate",
			"issue", "replace", "not_found", "pack", "discard_listed", "discard", "clear", "anomaly", "count"):
			self.assertIn(k, keys)
		self.assertEqual({f[0] for f in RJ.FLOW if f[2] == "no"} >= {"qc", "dispatch"}, True)


class TestReconciliationSite(unittest.TestCase):
	"""Read-only: every row keyed to a real bucket appears in exactly one journey, or in `unplaced`."""

	BUCKETS = ["SIM-BUCKET-0012", "RDHWGJ", "L2XS68", "450960", "DISCARD-TEST-001", "DISCARD-TEST-002"]

	@classmethod
	def setUpClass(cls):
		if not frappe.db:
			frappe.init(site="kaitet.local", sites_path=".")
			frappe.connect()

	def check(self, bucket):
		r = RJ.read_bucket(bucket)
		if not (r["ses"] or r["shelf_log"] or r["pli"]):
			self.skipTest("bucket %s not in this site" % bucket)
		res = RJ.build_bucket(r)
		allev = [e for c in res["cycles"] for e in c["steps"] + c["ledger"]] + res["unplaced"]
		docs = {(d["doctype"], d["name"]) for e in allev for d in e["docs"]}
		for s in r["ses"]:
			self.assertIn(("Stock Entry", s["name"]), docs, (bucket, s["stock_entry_type"]))
		for x in r["shelf_log"]:
			self.assertIn(("Shelving Log", x["name"]), docs)
		rows = sum(e.get("rows", 0) for e in allev if e["ev"] == "allocate" and "opl_all" in e)
		stems = sum(e.get("stems", 0) for e in allev if e["ev"] == "allocate" and "opl_all" in e)
		self.assertEqual(rows, len(r["pli"]))
		self.assertAlmostEqual(stems, sum(float(p.get("stock_qty") or 0) for p in r["pli"]), places=3)
		self.assertEqual(sum(1 for e in allev if e["ev"] == "discard_listed"), len(r["discard_rows"]))
		return res

	def test_named_buckets(self):
		for b in self.BUCKETS:
			with self.subTest(bucket=b):
				self.check(b)

	def test_sim_bucket_0012_cycles_do_not_swap_rows(self):
		c = cyc(self.check("SIM-BUCKET-0012"))
		self.assertEqual(len(c), 2)
		self.assertEqual(len(evs(c[0], "shelve")), 1)  # the first shelving only
		self.assertTrue(evs(c[1], "anomaly"))  # the reuse anomaly sits on the journey its harvest started
		self.assertTrue(evs(c[1], "issue"))
