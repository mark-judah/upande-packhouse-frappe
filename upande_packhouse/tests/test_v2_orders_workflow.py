"""Orders & Workflow v2 and the order-side core: stage wording, takt time, business unit.

The pure-function tests need no data. The *_site tests read the local site
read-only (no writes, nothing committed) and skip when the data isn't there.
"""

from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.v2 import orders_workflow as ow
from upande_packhouse.api.v2.core import pipeline


def _line(**kw):
	base = dict(
		ordered_stems=800.0,
		allocated_stems=800.0,
		issued_stems=800.0,
		packed_stems=800.0,
		precooled_stems=800.0,
		staged_stems=800.0,
		loaded_stems=0.0,
		dispatched_stems=0.0,
	)
	base.update(kw)
	return frappe._dict(base)


def _item(**kw):
	base = dict(
		opl="OPL-1",
		docstatus=1,
		ordered_stems=800.0,
		picked_stems=800.0,
		issued_stems=800.0,
		planned_stems=800.0,
		packed_stems=800.0,
		precooled_stems=800.0,
		staged_stems=800.0,
		loaded_stems=0.0,
		dispatched_stems=0.0,
		sales_invoices=[],
	)
	base.update(kw)
	return base


class TestStageWording(UnitTestCase):
	def test_state_threshold(self):
		self.assertEqual(pipeline.stage_state(800, 800), "done")
		self.assertEqual(pipeline.stage_state(799.6, 800), "done")  # half a stem of float noise
		self.assertEqual(pipeline.stage_state(200, 800), "partial")
		self.assertEqual(pipeline.stage_state(0, 800), "none")
		self.assertEqual(pipeline.stage_state(5, 0), "partial")  # nothing expected is never "done"

	def test_word_rule(self):
		self.assertEqual(pipeline.stage_word("loaded", "done"), "Loaded")
		self.assertEqual(pipeline.stage_word("loaded", "partial"), "Loading")
		self.assertEqual(pipeline.stage_word("staged", "partial"), "Staging")
		self.assertEqual(pipeline.stage_word("dispatched", "partial"), "Dispatching")
		self.assertEqual(pipeline.stage_word("packed", "partial"), "Packing")
		self.assertEqual(pipeline.stage_word("loaded", "none"), "Not loaded")

	def test_the_reported_card_is_loading_not_loaded(self):
		# FNP SAUDI ARABIA-02354 · Reflex: staged 800/800, loaded 200/800, dispatched 0/800
		ln = _line(loaded_stems=200.0)
		self.assertEqual(pipeline.line_stage(ln), ("staged", "loaded"))
		self.assertEqual(pipeline.line_stage_label(ln), "Loading")
		self.assertEqual(pipeline.line_stage_label(_line(loaded_stems=800.0)), "Loaded")
		self.assertEqual(
			pipeline.line_stage_label(_line(loaded_stems=800.0, dispatched_stems=100.0)), "Dispatching"
		)

	def test_item_stage_follows_the_same_rule(self):
		self.assertEqual(ow._stage(_item(loaded_stems=200.0)), "loading")
		self.assertEqual(ow.STAGE_LABEL["loading"], "Loading")
		self.assertEqual(ow._stage(_item(loaded_stems=800.0)), "loaded")
		self.assertEqual(ow._stage(_item(staged_stems=300.0, loaded_stems=0.0)), "staging")
		self.assertEqual(ow._stage(_item(staged_stems=800.0)), "staged")
		self.assertEqual(ow._stage(_item(precooled_stems=100.0, staged_stems=0.0)), "precooling")
		self.assertEqual(ow._stage(_item(precooled_stems=800.0, staged_stems=0.0)), "precooled")
		self.assertEqual(
			ow._stage(_item(packed_stems=400.0, precooled_stems=0.0, staged_stems=0.0)), "packing"
		)
		self.assertEqual(ow._stage(_item(precooled_stems=0.0, staged_stems=0.0)), "packed")
		self.assertEqual(
			ow._stage(_item(issued_stems=400.0, packed_stems=0.0, precooled_stems=0.0, staged_stems=0.0)),
			"issuing",
		)
		self.assertEqual(
			ow._stage(_item(issued_stems=800.0, packed_stems=0.0, precooled_stems=0.0, staged_stems=0.0)),
			"to_pack",
		)
		self.assertEqual(
			ow._stage(_item(issued_stems=0.0, packed_stems=0.0, precooled_stems=0.0, staged_stems=0.0)),
			"to_issue",
		)
		self.assertEqual(ow._stage(_item(opl=None)), "notalloc")

	def test_partly_dispatched_is_never_dispatched_or_invoiced(self):
		it = _item(loaded_stems=800.0, dispatched_stems=300.0, sales_invoices=["SINV-1"])
		self.assertEqual(ow._stage(it), "dispatching")
		it["dispatched_stems"] = 800.0
		self.assertEqual(ow._stage(it), "invoiced")
		it["sales_invoices"] = []
		self.assertEqual(ow._stage(it), "dispatched")

	def test_progress_words_match_the_stage(self):
		prog = {p["stage"]: p for p in ow._progress(_item(loaded_stems=200.0))}
		self.assertEqual(prog["loaded"]["word"], "Loading")
		self.assertEqual(prog["staged"]["word"], "Staged")
		self.assertEqual(prog["dispatched"]["word"], "Not dispatched")


class TestTakt(UnitTestCase):
	T0 = datetime(2026, 10, 20, 9, 49)
	NOW = datetime(2026, 10, 20, 16, 35)

	def _staged(self, last, staged=4, boxes=4):
		return {"O": {"boxes": boxes, "staged_boxes": staged, "last_staged": last}}

	def test_not_issued_has_no_takt(self):
		t = ow._takt("O", 4, {}, {}, self.NOW)
		self.assertEqual((t["state"], t["reason"]), ("none", "Not issued yet"))

	def test_no_issue_scan_but_packed(self):
		t = ow._takt("O", 4, {}, self._staged(datetime(2026, 10, 20, 15, 0)), self.NOW, packed_any=True)
		self.assertEqual((t["state"], t["secs"], t["reason"]), ("none", None, "No issue scan recorded"))

	def test_normal_done_is_first_issue_to_last_box_staged(self):
		t = ow._takt("O", 4, {"O": self.T0}, self._staged(datetime(2026, 10, 21, 14, 1)), self.NOW)
		self.assertEqual((t["state"], t["secs"]), ("done", (28 * 60 + 12) * 60))  # 1d 4h 12m

	def test_same_minute_keeps_the_seconds(self):
		# the real KPK-FLOW-TEST row: issued 16:36:47, staged 16:37:37 -> 50 s, not "0m"
		first = datetime(2026, 9, 11, 16, 36, 47)
		t = ow._takt("O", 2, {"O": first}, self._staged(datetime(2026, 9, 11, 16, 37, 37), 2, 2), self.NOW)
		self.assertEqual((t["state"], t["secs"], t["mins"]), ("done", 50, 0))

	def test_staged_before_issue_is_invalid(self):
		t = ow._takt("O", 4, {"O": self.T0}, self._staged(datetime(2026, 10, 20, 9, 0)), self.NOW)
		self.assertEqual(
			(t["state"], t["secs"], t["reason"]), ("invalid", None, "Staging recorded before issue")
		)

	def test_running_until_every_box_is_staged(self):
		t = ow._takt("O", 4, {"O": self.T0}, self._staged(datetime(2026, 10, 20, 15, 0), 3), self.NOW)
		self.assertEqual((t["state"], t["secs"]), ("running", (6 * 60 + 46) * 60))

	def test_all_labelled_boxes_staged_but_stems_short_stays_running(self):
		st = self._staged(datetime(2026, 10, 20, 15, 30), 3, 3)
		self.assertEqual(
			ow._takt("O", 3, {"O": self.T0}, st, self.NOW, stems_staged=False)["state"], "running"
		)
		self.assertEqual(ow._takt("O", 3, {"O": self.T0}, st, self.NOW, stems_staged=True)["state"], "done")

	def test_planned_boxes_not_yet_labelled_keep_it_running(self):
		st = self._staged(datetime(2026, 10, 20, 15, 30), 2, 2)
		self.assertEqual(ow._takt("O", 4, {"O": self.T0}, st, self.NOW)["state"], "running")


class TestTaktFromRows(UnitTestCase):
	"""The real first-issue query against local rows (read-only)."""

	def test_first_issue_query_prefers_issued_at(self):
		if not frappe.db.sql("SELECT 1 FROM `tabPick List Item` WHERE issued = 1 LIMIT 1"):
			self.skipTest("no issued rows")
		name = frappe.db.sql("SELECT parent FROM `tabPick List Item` WHERE issued = 1 LIMIT 1")[0][0]
		first = ow._first_issue([name])[name]
		raw = frappe.db.sql(
			"SELECT MIN(COALESCE(issued_at, modified)) FROM `tabPick List Item` WHERE parent=%s AND (issued=1 OR issued_offline=1)",
			name,
		)[0][0]
		self.assertEqual(first, raw)


class TestBusinessUnitSql(UnitTestCase):
	def test_roses_condition_keeps_blank_unit_rose_lines(self):
		params = {}
		with patch.object(
			pipeline,
			"rose_groups",
			side_effect=lambda k: ["Spray Roses - A"] if k == "spray" else ["Standard Roses - B"],
		):
			sql = pipeline.business_unit_sql("Roses", params)
		self.assertIn("so.business_unit = %(ph2_bu)s", sql)
		self.assertIn("IFNULL(so.business_unit, '') = ''", sql)
		self.assertEqual(params["ph2_bu"], "Roses")
		self.assertEqual(params["ph2_bu_groups"], ("Spray Roses - A", "Standard Roses - B"))

	def test_other_units_are_exact(self):
		params = {}
		sql = pipeline.business_unit_sql("Endebess Coffee", params)
		self.assertNotIn("IFNULL", sql)


class TestOrdersWorkflowOnSite(UnitTestCase):
	"""Read-only checks on the local site's real data."""

	def _skip_without_data(self):
		if not frappe.db.exists("DocType", "Sales Order") or not frappe.db.sql(
			"SELECT 1 FROM `tabSales Order` WHERE business_unit = 'Roses' AND docstatus = 1 LIMIT 1"
		):
			self.skipTest("no Roses sales orders on this site")

	def test_only_roses_and_whole_stems(self):
		self._skip_without_data()
		lines = pipeline.fetch_lines(delivery_from="2026-09-01", delivery_to="2026-10-31")
		units = {
			r[0] or ""
			for r in frappe.db.sql(
				"SELECT business_unit FROM `tabSales Order` WHERE name IN %s",
				(tuple({ln.parent for ln in lines}) or ("",),),
			)
		}
		self.assertTrue(units <= {"Roses", ""}, units)
		self.assertFalse([ln.name for ln in lines if ln.ordered_stems != int(ln.ordered_stems)])
		# every Roses order is still there
		roses = frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
			   WHERE so.docstatus = 1 AND so.business_unit = 'Roses' AND IFNULL(soi.item_code, '') != ''
			     AND so.delivery_date BETWEEN '2026-09-01' AND '2026-10-31'"""
		)[0][0]
		self.assertGreaterEqual(len(lines), roses)

	def test_kpis_equal_sum_of_items_and_stage_wording(self):
		self._skip_without_data()
		r = ow.get_orders_workflow(from_date="2026-09-15", to_date="2026-09-20")
		items = [i for s in r["sections"] for i in s["rows"]]
		self.assertTrue(items)
		k = r["kpis"]
		for m in (
			"ordered_stems",
			"to_deliver_stems",
			"picked_stems",
			"issued_stems",
			"planned_stems",
			"packed_stems",
			"dispatched_stems",
		):
			self.assertEqual(k[m], sum(i[m] for i in items), m)
		self.assertLessEqual(k["to_deliver_stems"], k["ordered_stems"])
		for i in items:
			self.assertEqual(i["stage_label"], ow.STAGE_LABEL[i["stage"]])
			if i["stage"] == "loaded":
				self.assertGreaterEqual(i["loaded_stems"], i["ordered_stems"] - 0.5)
			if i["stage"] == "loading":
				self.assertLess(i["loaded_stems"], i["ordered_stems"] - 0.5)
			if i["takt"]["state"] == "done":
				self.assertIsNotNone(i["takt"]["mins"])


RANGES = (
	("2026-09-15", "2026-09-15"),
	("2026-09-16", "2026-09-16"),
	("2026-09-20", "2026-09-20"),
	("2026-10-03", "2026-10-03"),
	("2026-09-14", "2026-09-24"),
)

_LINES = """FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
	WHERE so.docstatus = 1 AND IFNULL(soi.item_code, '') != ''
	AND (so.business_unit = 'Roses' OR (IFNULL(so.business_unit, '') = '' AND soi.item_group LIKE '%%Roses%%'))
	AND so.delivery_date BETWEEN %(a)s AND %(b)s"""


def raw_numbers(a, b):
	"""The same figures straight from the source doctypes, independent of core:
	every row is reached from a Sales Order Item whose ORDER delivery date is in range."""
	p = {"a": a, "b": b}
	sql = frappe.db.sql
	ordered, lines = sql(f"SELECT IFNULL(SUM(soi.stock_qty), 0), COUNT(*) {_LINES}", p)[0]
	orders = sql(f"SELECT COUNT(DISTINCT so.name) {_LINES}", p)[0][0]
	soi = f"soi.name IN (SELECT soi.name {_LINES})"
	picked, issued = sql(
		f"""SELECT IFNULL(SUM(pli.stock_qty), 0),
		           IFNULL(SUM(CASE WHEN pli.issued = 1 OR pli.issued_offline = 1 THEN pli.stock_qty ELSE 0 END), 0)
		    FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name = pli.parent AND o.docstatus < 2
		    JOIN `tabSales Order Item` soi ON soi.name = pli.sales_order_item
		    WHERE {soi}""",
		p,
	)[0]
	planned = sql(
		f"""SELECT IFNULL(SUM(pg.stems), 0) FROM `tabPacking Guide` pg
		    JOIN `tabOrder Pick List` o ON o.name = pg.parent AND o.docstatus < 2
		    JOIN `tabSales Order Item` soi ON soi.name = pg.sales_order_item
		    WHERE pg.parenttype = 'Order Pick List' AND {soi}""",
		p,
	)[0][0]
	dispatched = sql(
		f"""SELECT IFNULL(SUM(dni.stock_qty), 0) FROM `tabDelivery Note Item` dni
		    JOIN `tabDelivery Note` dn ON dn.name = dni.parent AND dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0
		    JOIN `tabSales Order Item` soi ON soi.name = dni.so_detail
		    WHERE {soi}""",
		p,
	)[0][0]
	return {
		"lines": lines,
		"orders": orders,
		"ordered_stems": float(ordered),
		"picked_stems": float(picked),
		"issued_stems": float(issued),
		"planned_stems": float(planned),
		"dispatched_stems": float(dispatched),
	}


class TestOrdersWorkflowMatchesSourceDocs(UnitTestCase):
	"""KPIs must equal a count made directly on the source doctypes, with the
	date range applied to the Sales Order DELIVERY date only."""

	def test_kpis_match_raw_counts_for_delivery_date(self):
		if not frappe.db.sql("SELECT 1 FROM `tabSales Order` WHERE business_unit = 'Roses' LIMIT 1"):
			self.skipTest("no Roses data")
		for a, b in RANGES:
			raw = raw_numbers(a, b)
			k = ow.get_orders_workflow(from_date=a, to_date=b)["kpis"]
			rows = {
				i["soi"]
				for s in ow.get_orders_workflow(from_date=a, to_date=b)["sections"]
				for i in s["rows"]
			}
			self.assertEqual(len(rows), raw["lines"], (a, b, "lines"))
			for key in (
				"orders",
				"ordered_stems",
				"picked_stems",
				"issued_stems",
				"planned_stems",
				"dispatched_stems",
			):
				self.assertEqual(k[key], raw[key], (a, b, key))

	def test_every_row_is_in_range_by_delivery_date_not_pack_date(self):
		a, b = "2026-09-15", "2026-09-16"
		r = ow.get_orders_workflow(from_date=a, to_date=b)
		for s in r["sections"]:
			for i in s["rows"]:
				self.assertTrue(a <= i["delivery_date"] <= b, i["name"])
				self.assertEqual(
					str(frappe.db.get_value("Sales Order", i["sales_order"], "delivery_date")),
					i["delivery_date"],
				)

	def test_range_is_not_moved_by_the_pick_list_date(self):
		# Pick lists whose own date_created differs from the order's delivery date must not
		# move a line in or out of the range.
		row = frappe.db.sql(
			"""SELECT o.name, so.delivery_date, o.date_created FROM `tabOrder Pick List` o
			   JOIN `tabSales Order` so ON so.name = o.sales_order
			   WHERE o.docstatus < 2 AND so.business_unit = 'Roses' AND so.docstatus = 1
			     AND o.date_created != so.delivery_date LIMIT 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("no such pick list")
		row = row[0]
		d = str(row.delivery_date)
		inside = {
			i["opl"] for s in ow.get_orders_workflow(from_date=d, to_date=d)["sections"] for i in s["rows"]
		}
		self.assertIn(row.name, inside)
		c = str(row.date_created)
		self.assertNotIn(
			row.name,
			{
				i["opl"]
				for s in ow.get_orders_workflow(from_date=c, to_date=c)["sections"]
				for i in s["rows"]
				if i["delivery_date"] != c
			},
		)


class TestCompletion(UnitTestCase):
	def test_completion_is_staged_over_ordered(self):
		self.assertEqual(pipeline.completion_pct(0, 800), 0.0)
		self.assertEqual(pipeline.completion_pct(200, 800), 25.0)
		self.assertEqual(pipeline.completion_pct(800, 800), 100.0)
		self.assertEqual(pipeline.completion_pct(900, 800), 100.0)
		self.assertIsNone(pipeline.completion_pct(10, 0))

	def test_complete_only_when_all_staged(self):
		r = ow.get_orders_workflow(from_date="2026-09-01", to_date="2026-09-30")
		seen = 0
		for s in r["sections"]:
			for i in s["rows"]:
				c = i["completion_pct"]
				if c is None:
					continue
				seen += 1
				if c >= 100:
					self.assertGreaterEqual(i["staged_stems"], i["ordered_stems"] - 0.5)
				else:
					self.assertLess(i["staged_stems"], i["ordered_stems"])
		self.assertTrue(seen)

	def test_fully_staged_order_reads_100_not_0(self):
		r = ow.get_orders_workflow(from_date="2026-09-11", to_date="2026-09-11")
		rows = {i["opl"]: i for s in r["sections"] for i in s["rows"] if i["opl"]}
		for opl in ("OPL-2026-00024", "OPL-2026-00017"):
			if opl in rows and rows[opl]["primary"]:
				self.assertEqual(rows[opl]["completion_pct"], 100.0)
