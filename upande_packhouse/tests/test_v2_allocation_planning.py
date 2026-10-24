"""Allocation Planning v2: confirmation write path, filters/KPIs, and the one
"available stock" definition shared with Stock Visibility, Avails and Cold Room.

These tests read the local site's real data. Anything that writes (the v1
confirmation endpoint saves a submitted Sales Order) runs inside a savepoint
that is rolled back, with frappe.db.commit / rollback patched to no-ops so
nothing -- not even the endpoint's own commit -- can reach the database. Row
counts are asserted unchanged afterwards. Tests skip when the data isn't there.

Run:  cd sites && ../env/bin/python -m unittest upande_packhouse.tests.test_v2_allocation_planning
(after frappe.init/connect), or via bench run-tests --module.
"""

import unittest
from collections import defaultdict
from unittest.mock import patch

import frappe

from upande_packhouse.api import allocation as v1_allocation
from upande_packhouse.api.v2 import allocation_planning as ap
from upande_packhouse.api.v2 import avails, cold_room, stock_visibility
from upande_packhouse.api.v2.core import stock

FROM, TO = "2026-10-06", "2026-10-14"
DAY = "2026-10-12"


class _RelaxedConfig(dict):
	"""farm_config where nothing is too old and nothing is cooling, so allocatable > 0 on
	a site whose stock is old. Patched into core.stock for ALL pages at once."""

	def get(self, key, default=None):
		return {"max_allocation_age": 10**6, "cooling_hours": 0}


def _relaxed():
	return patch.object(stock, "_config", lambda: {"discard_age": 10**6, "farm_config": _RelaxedConfig()})


def _counts():
	return {
		t: frappe.db.count(t)
		for t in ("Confirmed Stems", "Sales Order Item", "Sales Order", "Bucket Allocations", "Shelf Item")
	}


class _Rollback:
	"""Savepoint + commit/rollback no-ops. Always restores and asserts counts."""

	def __enter__(self):
		self.before = _counts()
		self.orig_rollback = frappe.db.rollback
		frappe.db.savepoint("ap_v2_test")
		self._p = [
			# kaitet.local lacks some link masters (Letter Head "Karen Roses Letterhead", some
			# Packrate / Specification records), so a full save of a submitted order dies on
			# link validation before the confirmation is even looked at. Skip ONLY that check.
			patch("frappe.model.document.Document._validate_links", lambda self: None),
			patch.object(frappe.db, "commit", lambda *a, **k: None),
			patch.object(frappe.db, "rollback", lambda *a, **k: None),
		]
		for p in self._p:
			p.start()
		return self

	def __exit__(self, *exc):
		for p in self._p:
			p.stop()
		self.orig_rollback(save_point="ap_v2_test")
		self.after = _counts()
		assert self.after == self.before, f"row counts changed after rollback: {self.before} -> {self.after}"
		return False


def _confirm(so, line_no, farm, action="confirm", stems=0):
	frappe.local.form_dict = frappe._dict(
		sales_order=so, line_no=str(line_no), processing_location=farm, action=action, stems=stems
	)
	frappe.response.pop("message", None)
	v1_allocation.confimSalesOrderItem()
	return frappe.response.get("message")


def _pick_order(min_qty=40):
	"""A real submitted Roses order in range with no confirmations and a line with room."""
	rows = frappe.db.sql(
		"""SELECT soi.parent, soi.idx, soi.name, soi.custom_ordered_quantity AS cap
		   FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
		   WHERE so.docstatus = 1 AND so.business_unit = 'Roses' AND so.delivery_date BETWEEN %s AND %s
		     AND soi.custom_ordered_quantity >= %s
		     AND NOT EXISTS (SELECT 1 FROM `tabConfirmed Stems` cs WHERE cs.parent = so.name)
		   ORDER BY so.delivery_date DESC, soi.parent, soi.idx LIMIT 40""",
		(FROM, TO, min_qty),
		as_dict=True,
	)
	# The v1 endpoint saves the whole Sales Order, so an order with a broken link
	# (e.g. a missing Letter Head) cannot be confirmed at all. Use the first order
	# whose trial confirmation (rolled back) works.
	for r in rows:
		with _Rollback():
			if (_confirm(r.parent, r.idx, "Kapkolia", "confirm", 1.0) or {}).get("success"):
				return r
	return None


class TestConfirmWritesSalesOrderChildTable(unittest.TestCase):
	def setUp(self):
		self.row = _pick_order()
		if not self.row:
			self.skipTest("no suitable submitted Roses order on this site")

	def _entries(self, soi):
		return frappe.db.sql(
			"""SELECT farm, stems, parent, parenttype, parentfield FROM `tabConfirmed Stems`
			   WHERE sales_order_item = %s ORDER BY idx""",
			soi,
			as_dict=True,
		)

	def _line(self, so, soi):
		r = ap.get_allocation_planning(from_date=FROM, to_date=TO, sales_order=so)
		return next(ln for ln in r["lines"] if ln["soi"] == soi), r

	def test_confirm_update_partial_two_farms_unconfirm(self):
		so, idx, soi, cap = self.row.parent, self.row.idx, self.row.name, float(self.row.cap)
		soi_before = frappe.db.get_value(
			"Sales Order Item",
			soi,
			["stock_qty", "qty", "custom_ordered_quantity", "item_code"],
			as_dict=True,
		)
		with _Rollback():
			self.assertEqual(self._entries(soi), [])
			# 1. partial confirm for farm A -> a Confirmed Stems child row on the Sales Order
			part = float(int(cap // 4)) or 1.0
			m = _confirm(so, idx, "Kapkolia", "confirm", part)
			self.assertTrue(m["success"], m)
			e = self._entries(soi)
			self.assertEqual(len(e), 1)
			self.assertEqual((e[0].farm, e[0].stems), ("Kapkolia", part))
			self.assertEqual(
				(e[0].parent, e[0].parenttype, e[0].parentfield),
				(so, "Sales Order", "custom_confirmed_stems_table"),
			)
			self.assertEqual(frappe.db.get_value("Sales Order", so, "docstatus"), 1)  # still submitted
			# the page's own read endpoint now shows it
			ln, res = self._line(so, soi)
			self.assertEqual(ln["entries"], [{"farm": "Kapkolia", "stems": part}])
			self.assertEqual((ln["confirmed_stems"], ln["state"]), (part, "partial"))
			self.assertEqual(res["kpis"]["confirmed_stems"], part)
			self.assertEqual(ln["breakdown_totals"]["confirmed"], part)
			self.assertEqual(ln["breakdown_totals"]["unconfirmed"], ln["stems_ordered"] - part)
			kap = next(b for b in ln["breakdown"] if b["farm"] == "Kapkolia")
			self.assertEqual(kap["confirmed"], part)
			# 2. update the same farm -> the SAME child row changes, none added
			m = _confirm(so, idx, "Kapkolia", "confirm", part * 2)
			self.assertTrue(m["success"])
			e = self._entries(soi)
			self.assertEqual([(x.farm, x.stems) for x in e], [("Kapkolia", part * 2)])
			# 3. second farm -> second child row; total = sum
			m = _confirm(so, idx, "Torongo", "confirm", part)
			self.assertEqual(
				sorted((x.farm, x.stems) for x in self._entries(soi)),
				[("Kapkolia", part * 2), ("Torongo", part)],
			)
			ln, _ = self._line(so, soi)
			self.assertEqual(ln["confirmed_stems"], part * 3)
			self.assertEqual(sum(b["confirmed"] for b in ln["breakdown"]), part * 3)
			# 4. over-confirm (total beyond the stored ordered quantity) is refused and changes nothing
			m = _confirm(so, idx, "Torongo", "confirm", cap)
			self.assertFalse(m["success"])
			self.assertIn("Cannot confirm", m["error"])
			self.assertEqual(
				sorted((x.farm, x.stems) for x in self._entries(soi)),
				[("Kapkolia", part * 2), ("Torongo", part)],
			)
			# 5. full confirm by one farm up to the cap -> state "full"
			m = _confirm(so, idx, "Torongo", "confirm", cap - part * 2)
			self.assertTrue(m["success"])
			ln, _ = self._line(so, soi)
			self.assertEqual((ln["confirmed_stems"], ln["state"]), (cap, "full"))
			# 6. unconfirm removes only that farm's row
			m = _confirm(so, idx, "Torongo", "unconfirm")
			self.assertTrue(m["success"])
			self.assertEqual([(x.farm, x.stems) for x in self._entries(soi)], [("Kapkolia", part * 2)])
			# 7. confirming 0 stems also removes the row
			m = _confirm(so, idx, "Kapkolia", "confirm", 0)
			self.assertTrue(m["success"])
			self.assertEqual(self._entries(soi), [])
			ln, _ = self._line(so, soi)
			self.assertEqual((ln["confirmed_stems"], ln["state"]), (0.0, "open"))
			# the Sales Order Item itself was never a write target
			soi_now = frappe.db.get_value(
				"Sales Order Item",
				soi,
				["stock_qty", "qty", "custom_ordered_quantity", "item_code"],
				as_dict=True,
			)
			self.assertEqual(soi_now, soi_before)

	def test_missing_farm_and_bad_line_are_refused(self):
		so = self.row.parent
		with _Rollback():
			self.assertFalse(_confirm(so, self.row.idx, "", "confirm", 5)["success"])
			self.assertFalse(_confirm(so, 9999, "Kapkolia", "confirm", 5)["success"])
			self.assertEqual(self._entries(self.row.name), [])


class TestAllocationPlanningNumbers(unittest.TestCase):
	def setUp(self):
		self.r = ap.get_allocation_planning(from_date=DAY, to_date=DAY)
		if not self.r["lines"]:
			self.skipTest("no order lines on the test day")

	def test_kpis_match_rows_and_status_partition(self):
		k, lines = self.r["kpis"], self.r["lines"]
		self.assertEqual((k["lines"], self.r["total_lines"], len(lines)), (len(lines),) * 3)
		self.assertEqual(k["open_lines"] + k["partial_lines"] + k["full_lines"], k["lines"])
		for status, expect in (
			("open", k["open_lines"]),
			("partial", k["partial_lines"]),
			("full", k["full_lines"]),
			("confirmed", k["confirmed_lines"]),
			("remaining", k["remaining_lines"]),
		):
			r = ap.get_allocation_planning(from_date=DAY, to_date=DAY, status=status)
			self.assertEqual(r["total_lines"], expect, status)
			self.assertEqual(r["kpis"]["lines"], expect, status)
		conf = ap.get_allocation_planning(from_date=DAY, to_date=DAY, status="confirmed")
		self.assertEqual(conf["kpis"]["confirmed_stems"], k["confirmed_stems"])
		self.assertEqual(sum(ln["confirmed_stems"] for ln in conf["lines"]), k["confirmed_stems"])

	def test_every_filter_keeps_lines_equal_to_kpi(self):
		first = self.r["lines"][0]
		for kw in (
			{"customer": first["customer"]},
			{"sales_order": first["sales_order"]},
			{"variety": first["item_code"]},
			{"length": first["length"]},
			{"variety": first["item_code"], "length": first["length"], "customer": first["customer"]},
			{"q": first["item_code"][:4].lower()},
			{"region": "Ravine"},
			{"region": "Karen"},
			{"farm": "Kapkolia"},
			{"cover": "short"},
		):
			r = ap.get_allocation_planning(from_date=DAY, to_date=DAY, **kw)
			self.assertEqual(r["kpis"]["lines"], r["total_lines"], kw)
			self.assertEqual(len(r["lines"]), r["total_lines"], kw)
		ravine = ap.get_allocation_planning(from_date=DAY, to_date=DAY, region="Ravine")["total_lines"]
		karen = ap.get_allocation_planning(from_date=DAY, to_date=DAY, region="Karen")["total_lines"]
		self.assertLessEqual(ravine + karen, self.r["total_lines"])

	def test_per_farm_breakdown_reconciles_to_the_line(self):
		for ln in self.r["lines"]:
			t = ln["breakdown_totals"]
			self.assertAlmostEqual(sum(b["confirmed"] for b in ln["breakdown"]), ln["confirmed_stems"], 6)
			self.assertAlmostEqual(t["confirmed"], ln["confirmed_stems"], 6)
			self.assertAlmostEqual(t["ordered"] - t["confirmed"], t["unconfirmed"], 6) if t["confirmed"] <= t[
				"ordered"
			] else None
			self.assertAlmostEqual(sum(b["available"] for b in ln["breakdown"]), t["available"], 6)
			for b in ln["breakdown"]:  # held + allocated + free == on shelf, available <= free
				self.assertLessEqual(b["available"], b["on_shelf"] - b["held"] - b["allocated"] + 1e-6)
				self.assertAlmostEqual(b["shortfall"], max(0.0, b["confirmed"] - b["available"]), 6)

	def test_available_kpi_is_once_per_variety_length_farm(self):
		"""KPI available == sum over distinct (variety, length, farm) cells, never per line (OR-A4)."""
		with _relaxed():
			r = ap.get_allocation_planning(from_date=DAY, to_date=DAY)
			pairs = {(ln["item_code"], ln["length"]) for ln in r["lines"]}
			cells, _rows = stock.available_cells(pairs=pairs, varieties=sorted({p[0] for p in pairs}))
			self.assertAlmostEqual(
				r["kpis"]["available_stems"], sum(g.allocatable for g in cells.values()), 6
			)
			self.assertAlmostEqual(sum(c["available"] for c in r["cover"]), r["kpis"]["available_stems"], 6)
			self.assertAlmostEqual(sum(c["ordered"] for c in r["cover"]), r["kpis"]["ordered_stems"], 6)
			self.assertAlmostEqual(sum(c["uncovered"] for c in r["cover"]), r["kpis"]["uncovered_stems"], 6)
			self.assertGreater(r["kpis"]["available_stems"], 0)

	def test_cover_short_filter_returns_exactly_the_short_lines(self):
		with _relaxed():
			r = ap.get_allocation_planning(from_date=FROM, to_date=TO)
			short_pairs = {(c["variety"], c["length"]) for c in r["cover"] if c["uncovered"] > 0}
			covered_pairs = {(c["variety"], c["length"]) for c in r["cover"] if c["uncovered"] <= 0}
			if not short_pairs or not covered_pairs:
				self.skipTest("needs both short and covered varieties")
			expect = sum(c["lines"] for c in r["cover"] if (c["variety"], c["length"]) in short_pairs)
			s = ap.get_allocation_planning(from_date=FROM, to_date=TO, cover="short")
			self.assertEqual(s["total_lines"], expect)
			self.assertEqual(s["kpis"]["short_lines"], expect)
			self.assertLess(s["total_lines"], r["total_lines"])
			self.assertAlmostEqual(s["kpis"]["uncovered_stems"], r["kpis"]["uncovered_stems"], 6)
			self.assertTrue(all((ln["item_code"], ln["length"]) in short_pairs for ln in s["lines"]))


class TestAvailableStockIsOneNumber(unittest.TestCase):
	"""Allocation Planning == Stock Visibility == Avails == Cold Room for a (variety, length, farm)."""

	def _combos(self, n=3):
		r = ap.get_allocation_planning(from_date=FROM, to_date=TO)
		pairs = {(ln["item_code"], ln["length"]) for ln in r["lines"]}
		cells, _ = stock.available_cells(pairs=pairs, varieties=sorted({p[0] for p in pairs}))
		ranked = sorted(cells.items(), key=lambda kv: (-kv[1].allocatable, -kv[1].stems))
		return [k for k, g in ranked if g.stems > 0][:n]

	def _check(self, variety, length, farm):
		a = ap.get_allocation_planning(from_date=FROM, to_date=TO, variety=variety, length=length)
		self.assertTrue(a["lines"], (variety, length))
		line = a["lines"][0]
		ap_cell = next(b for b in line["breakdown"] if b["farm"] == farm)
		sv = stock_visibility.get_stock_visibility(
			from_date=FROM, to_date=TO, farm=farm, length=length, q=variety
		)
		sv_row = next(r for r in sv["rows"] if r["variety"] == variety)
		av = avails.get_avails(from_date=FROM, to_date=TO, farm=farm, length=length, variety=variety)
		av_cell = next(
			c for r in av["rows"] if r["variety"] == variety for c in r["cells"] if c["farm"] == farm
		)
		cr = cold_room.get_cold_room(farm=farm)
		cr_cell = next(
			c
			for c in cr["shelf_rows"]
			if (c["variety"], c["stem_length"] or "", c["farm"]) == (variety, length, farm)
		)
		core = stock.available_cells(farms=[farm], varieties=[variety], lengths=[length])[0][
			(variety, length, farm)
		]
		msg = (variety, length, farm)
		# on shelf = held + allocated + free, identical on every page
		for name, v in (
			("ap", ap_cell["on_shelf"]),
			("sv", sv_row["stems"]),
			("avails", av_cell["stems"]),
			("cold_room", cr_cell["shelved"]),
		):
			self.assertAlmostEqual(v, core.stems, 6, f"on shelf {name} {msg}")
		for name, v in (
			("ap", ap_cell["held"]),
			("sv", sv_row["held"]),
			("avails", av_cell["held"]),
			("cr", cr_cell["held"]),
		):
			self.assertAlmostEqual(v, core.held, 6, f"held {name} {msg}")
		for name, v in (
			("ap", ap_cell["allocated"]),
			("sv", sv_row["allocated"]),
			("avails", av_cell["allocated"]),
			("cr", cr_cell["allocated"]),
		):
			self.assertAlmostEqual(v, core.allocated, 6, f"allocated {name} {msg}")
		# available = allocatable: Allocation Planning, Stock Visibility, Avails
		for name, v in (
			("ap", ap_cell["available"]),
			("sv", sv_row["allocatable"]),
			("avails", av_cell["allocatable"]),
		):
			self.assertAlmostEqual(v, core.allocatable, 6, f"available {name} {msg}")
		# Cold Room shows `free` (before the too-old / cooling cut): free >= available, same free elsewhere
		self.assertAlmostEqual(cr_cell["free"], core.free, 6)
		self.assertAlmostEqual(sv_row["free"], core.free, 6)
		self.assertGreaterEqual(cr_cell["free"] + 1e-6, ap_cell["available"])
		return core

	def test_three_combinations_real_config(self):
		combos = self._combos()
		if len(combos) < 3:
			self.skipTest("fewer than 3 (variety, length, farm) cells with stock for the test lines")
		for variety, length, farm in combos:
			self._check(variety, length, farm)

	def test_three_combinations_with_allocatable_stock(self):
		with _relaxed():
			combos = self._combos()
			if len(combos) < 3:
				self.skipTest("fewer than 3 cells")
			got = [self._check(*c) for c in combos]
			self.assertTrue(
				any(g.allocatable > 0 for g in got), "relaxed config should expose allocatable stems"
			)

	def test_core_variety_availability_matches_allocatable(self):
		with _relaxed():
			cells, _ = stock.available_cells()
			variety = next((k[0] for k, g in cells.items() if g.allocatable > 0), None)
			if not variety:
				self.skipTest("no allocatable stock on this site")
			by_farm = stock.variety_availability([variety])[variety]
			expect = defaultdict(float)
			for (v, _ln, farm), g in cells.items():
				if v == variety and g.allocatable > 0:
					expect[farm] += g.allocatable
			self.assertEqual(
				{k: round(v, 6) for k, v in by_farm.items()}, {k: round(v, 6) for k, v in expect.items()}
			)


if __name__ == "__main__":
	unittest.main()
