"""Label Print Manager: duplicate-proof reservation, idempotent retries,
resumable rendering and reprints.

Runs against the local site and rolls everything back: commits and rollbacks
inside the code under test are neutralised so no label data is left behind."""

import json
from unittest.mock import patch

import frappe
import pymupdf
from frappe.tests import IntegrationTestCase

from upande_packhouse.api.v2 import label_manager as lm
from upande_packhouse.api.v2.label_print import batch as lp_batch
from upande_packhouse.api.v2.label_print import codes as lp_codes
from upande_packhouse.api.v2.label_print import render
from upande_packhouse.api.v2.label_print import types as lp_types


def _bunch_rows(qty=5):
	return [{"farm": "Kapkolia", "variety": "Marisa", "bunch_size": "Bunch (10)", "stem_length": "62cm", "qty": qty}]


class TestLabelManager(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._p = [
			patch.object(frappe.db, "commit", lambda *a, **k: None),
			patch.object(frappe.db, "rollback", lambda *a, **k: None),
			patch("frappe.enqueue", lambda *a, **k: None),
		]
		for p in cls._p:
			p.start()

	@classmethod
	def tearDownClass(cls):
		for p in cls._p:
			p.stop()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		for dt, name in (("Farm", "Kapkolia"), ("Item", "Marisa"), ("UOM", "Bunch (10)"), ("Stem Length", "62cm")):
			if not frappe.db.exists(dt, name):
				self.skipTest(f"{dt} {name} not on this site")
		if not frappe.db.get_value("Farm", "Kapkolia", "farm_code"):
			self.skipTest("Kapkolia has no farm code")

	def _create(self, label_type="bunch", spec=None, key=None, design=None, layout=None):
		return lp_batch.create_batch(label_type, spec or {"rows": _bunch_rows()}, design, layout or {}, None, key or frappe.generate_hash(length=12))

	# --- renderer

	def test_every_type_renders_a_readable_pdf(self):
		for t, meta in lp_types.TYPES.items():
			d = render.normalize_design(None, t)
			pdf = render.render_pdf(d, {}, [meta["sample"]], t)
			doc = pymupdf.open(stream=pdf, filetype="pdf")
			self.assertEqual(doc.page_count, 1, t)
			if t != "bucket":
				self.assertTrue(any(w in doc[0].get_text() for w in meta["sample"].values()), t)

	def test_payloads_match_what_the_scanners_parse(self):
		self.assertEqual(lp_types.payload("bunch", "BUNCH-7"), '{"bunch_id":"BUNCH-7"}')
		self.assertEqual(lp_types.payload("bucket", "A3F09C"), '{"A3F09C":"bucket"}')
		self.assertEqual(lp_types.payload("shelf", "KPK-D21M"), '{"shelf":"KPK-D21M"}')
		self.assertEqual(lp_types.payload("trolley", "KPK-TROLLEY-1"), '{"KPK-TROLLEY-1":"trolley"}')
		self.assertEqual(lp_types.payload("staging", "COLD ROOM 2"), '{"location":"COLD ROOM 2"}')

	def test_design_is_clamped_and_unknown_fields_reported(self):
		d = render.normalize_design({"margin": 999, "lines": [{"text": "{nope}{code}", "size": 9999}]}, "bucket")
		self.assertEqual(d["margin"], 30)
		self.assertEqual(d["lines"][0]["size"], 200)
		self.assertEqual(d["_unknown_fields"], ["nope"])

	def test_text_size_is_honoured_and_clamped_with_a_warning(self):
		sample = [lp_types.TYPES["bunch"]["sample"]]
		d = render.normalize_design({"base_pt": 12}, "bunch")
		geo = render.solve(d, sample, "bunch")
		self.assertEqual({round(x, 1) for x in geo["sizes"]}, {12.0})
		d = render.normalize_design({"base_pt": 200}, "bunch")
		geo = render.solve(d, sample, "bunch")
		self.assertLess(max(geo["sizes"]), 200)
		self.assertTrue(any("reduced" in w for w in geo["warnings"]))

	def test_sheet_mode_packs_labels_per_page(self):
		d = render.normalize_design(None, "shelf")
		layout = lp_batch.normalize_layout({"mode": "sheet", "sheet": {"paper": "A4"}})
		per = render.labels_per_page(d, layout)
		self.assertGreater(per, 1)
		pdf = render.render_pdf(d, layout, [dict(lp_types.TYPES["shelf"]["sample"], code=f"KPK-A{i}T") for i in range(per + 1)], "shelf")
		self.assertEqual(pymupdf.open(stream=pdf, filetype="pdf").page_count, 2)

	# --- reservation

	def test_bunch_batches_never_overlap(self):
		a = self._create()
		b = self._create()
		ca = set(frappe.get_all("Label Code", {"batch": a["name"]}, pluck="code"))
		cb = set(frappe.get_all("Label Code", {"batch": b["name"]}, pluck="code"))
		self.assertEqual((len(ca), len(cb)), (5, 5))
		self.assertFalse(ca & cb)
		self.assertTrue(all(frappe.db.exists("Bunch QR Code", c) for c in ca | cb))

	def test_retry_with_same_key_makes_nothing_new(self):
		a = self._create(key="same-key-123456")
		before = frappe.db.count("Label Code")
		counter = frappe.db.get_single_value("QR Sequence", "bunch_counter")
		b = self._create(key="same-key-123456")
		self.assertEqual(a["name"], b["name"])
		self.assertTrue(b["duplicate"])
		self.assertEqual(frappe.db.count("Label Code"), before)
		self.assertEqual(frappe.db.get_single_value("QR Sequence", "bunch_counter"), counter)

	def test_counter_behind_existing_bunches_skips_taken_numbers(self):
		start = lp_codes._sequence_start("bunch_counter")
		# plant bunches ahead of the counter, as a drifted live counter would face
		taken = [start + 2, start + 3]
		std = lp_codes._std("Administrator", frappe.utils.now())
		frappe.db.bulk_insert(
			"Bunch QR Code",
			fields=["name", "id", *std],
			values=[[f"BUNCH-{n}", f"BUNCH-{n}", *std.values()] for n in taken],
		)
		b = self._create(spec={"rows": _bunch_rows(4)})
		codes = frappe.get_all("Label Code", {"batch": b["name"]}, pluck="code", order_by="seq")
		self.assertEqual(len(set(codes)), 4)
		self.assertFalse({f"BUNCH-{n}" for n in taken} & set(codes))

	def test_failed_request_leaves_no_trace(self):
		before_counter = frappe.db.get_single_value("QR Sequence", "bunch_counter")
		bad = [{"farm": "Kapkolia", "variety": "No Such Variety", "bunch_size": "Bunch (10)", "stem_length": "62cm", "qty": 3}]
		with self.assertRaises(frappe.ValidationError):
			self._create(spec={"rows": bad})
		self.assertEqual(frappe.db.get_single_value("QR Sequence", "bunch_counter"), before_counter)

	def test_bucket_codes_are_unique_and_clear_of_other_code_spaces(self):
		b = self._create("bucket", {"qty": 500})
		codes = frappe.get_all("Label Code", {"batch": b["name"]}, pluck="code")
		self.assertEqual(len(set(codes)), 500)
		self.assertTrue(all(len(c) == 6 for c in codes))
		self.assertTrue(all(frappe.db.exists("Bucket QR Code", c) for c in codes[:20]))

	def test_fixed_codes_are_not_duplicated_when_asked_twice(self):
		spec = {"rows": [{"prefix": "ZZT", "row": "A", "from": 1, "to": 3}], "levels": ["T", "M"]}
		a = self._create("shelf", spec)
		before = frappe.db.count("Label Code")
		b = self._create("shelf", spec)
		self.assertEqual(a["total"], 6)
		self.assertEqual(b["total"], 6)
		self.assertEqual(frappe.db.count("Label Code"), before)
		self.assertEqual(frappe.db.count("Shelf QR Code", {"shelf_id": ["like", "ZZT-%"]}), 6)

	def test_second_batch_of_existing_fixed_codes_renders(self):
		spec = {"rows": [{"prefix": "ZZT", "from": 1, "to": 3}]}
		self._create("trolley", spec)
		b = self._create("trolley", spec)
		lp_batch.run_batch(b["name"])
		done = lp_batch.summary(b["name"])
		self.assertEqual((done["status"], done["rendered"]), ("Done", 3))

	def test_preview_with_no_rows_does_not_crash(self):
		out = lm.preview("bunch", None, {}, {"rows": []})
		self.assertTrue(out["image"].startswith("data:image/png"))

	def test_staging_and_trolley(self):
		s = self._create("staging", {"names": ["Cold Room 9", "cold room 9", "Dock 2"]})
		self.assertEqual(s["total"], 2)
		t = self._create("trolley", {"rows": [{"prefix": "ZZT", "from": 1, "to": 3}]})
		self.assertEqual(t["total"], 3)
		self.assertEqual(t["first_code"], "ZZT-TROLLEY-1")

	# --- rendering, resume

	def test_render_resumes_from_the_failed_chunk(self):
		b = self._create(spec={"rows": _bunch_rows(7)})
		calls = {"n": 0}
		real = render.render_pdf

		def flaky(*a, **k):
			calls["n"] += 1
			if calls["n"] == 3:
				raise RuntimeError("worker died")
			return real(*a, **k)

		with patch.object(lp_batch, "CHUNK_LABELS", 2), patch.object(render, "render_pdf", flaky):
			frappe.db.set_value("Label Batch", b["name"], "chunk_size", 2)
			lp_batch.run_batch(b["name"])
			mid = frappe.db.get_value("Label Batch", b["name"], ["status", "chunks_done", "rendered"], as_dict=True)
			self.assertEqual((mid.status, mid.chunks_done, mid.rendered), ("Failed", 2, 4))
			lp_batch.resume(b["name"])
			lp_batch.run_batch(b["name"])
		done = lp_batch.summary(b["name"])
		self.assertEqual(done["status"], "Done")
		self.assertEqual(done["rendered"], 7)
		self.assertEqual(calls["n"], 5)  # 2 ok + 1 failed + 2 resumed: finished chunks were not redone
		self.assertEqual(sum(frappe.db.get_all("Label Code", {"batch": b["name"]}, "print_count", pluck="print_count")), 7)
		path = frappe.get_site_path(done["parts"][0]["url"].lstrip("/"))
		self.assertEqual(pymupdf.open(path).page_count, 7)

	def test_finished_batch_is_not_rendered_twice(self):
		b = self._create(spec={"rows": _bunch_rows(2)})
		lp_batch.run_batch(b["name"])
		lp_batch.run_batch(b["name"])
		self.assertEqual(frappe.get_all("Label Code", {"batch": b["name"]}, pluck="print_count"), [1, 1])

	# --- reprint

	def test_reprint_one_label_reserves_nothing(self):
		b = self._create(spec={"rows": _bunch_rows(3)})
		lp_batch.run_batch(b["name"])
		code = frappe.get_all("Label Code", {"batch": b["name"]}, pluck="code", order_by="seq")[1]
		counter = frappe.db.get_single_value("QR Sequence", "bunch_counter")
		count = frappe.db.count("Label Code")
		out = lp_batch.reprint("bunch", [code], None, {}, None, "reprint-key-123456", "Damaged")
		self.assertEqual(frappe.db.get_single_value("QR Sequence", "bunch_counter"), counter)
		self.assertEqual(frappe.db.count("Label Code"), count)
		self.assertEqual(frappe.db.get_value("Label Code", f"bunch:{code}", "print_count"), 2)
		doc = pymupdf.open(stream=__import__("base64").b64decode(out["pdf"]), filetype="pdf")
		self.assertEqual(doc.page_count, 1)
		again = lp_batch.reprint("bunch", [code], None, {}, None, "reprint-key-123456", "Damaged")
		self.assertTrue(again["batch"]["duplicate"])
		self.assertEqual(frappe.db.get_value("Label Code", f"bunch:{code}", "print_count"), 2)

	def test_reprint_adopts_a_legacy_bunch(self):
		legacy = frappe.db.get_value("Bunch QR Code", {"name": ["like", "BUNCH-%"], "item_code": ["is", "set"]}, "name")
		if not legacy:
			self.skipTest("no legacy bunch")
		out = lp_batch.reprint("bunch", [legacy], None, {}, None, frappe.generate_hash(length=12), "")
		self.assertTrue(out["pdf"])
		self.assertTrue(frappe.db.exists("Label Code", f"bunch:{legacy}"))

	def test_reprint_unknown_code_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			lp_batch.reprint("bunch", ["BUNCH-0"], None, {}, None, frappe.generate_hash(length=12))

	# --- lookup

	def test_lookup_reads_scanned_payloads_and_ranges(self):
		b = self._create(spec={"rows": _bunch_rows(3)})
		codes = frappe.get_all("Label Code", {"batch": b["name"]}, pluck="code", order_by="seq")
		res = lm.lookup("bunch", json.dumps({"bunch_id": codes[0]}) + f" {codes[1]}..{codes[2].split('-')[1]}")
		self.assertEqual({i["code"] for i in res["items"]}, set(codes))
		self.assertEqual(res["missing"], [])

	# --- templates

	def test_the_six_standard_templates_exist(self):
		rows = lm._template_rows() if lm._ensure_templates() is None else []
		self.assertEqual({r["label_type"] for r in rows if r["is_default"]}, set(lp_types.TYPES))

	def test_template_save_default_and_delete_round_trip(self):
		design = render.normalize_design({"base_pt": 14}, "bunch")
		rows = lm.save_template("bunch", "ZZ test bunch", design, 0)
		mine = next(r for r in rows if r["name"] == "ZZ test bunch")
		self.assertEqual((mine["label_type"], mine["design"]["base_pt"], mine["is_default"]), ("bunch", 14, 0))
		rows = lm.save_template("bunch", "ZZ test bunch", design, 1)
		self.assertEqual([r["name"] for r in rows if r["label_type"] == "bunch" and r["is_default"]], ["ZZ test bunch"])
		with self.assertRaises(frappe.ValidationError):
			lm.delete_template("ZZ test bunch")
		std = next(r for r in rows if r["label_type"] == "bunch" and r["name"] != "ZZ test bunch")
		lm.save_template("bunch", std["name"], std["design"], 1)
		rows = lm.delete_template("ZZ test bunch")
		self.assertFalse(any(r["name"] == "ZZ test bunch" for r in rows))
