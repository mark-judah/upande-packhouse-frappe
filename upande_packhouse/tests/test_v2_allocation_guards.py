"""Allocation Planning v2, phase 2: heads-ups, try it out, use first, expected stock turning into real buckets.

The rules are tested on a made-up day (FakeModel) so every case is exact. The integration tests read the local
site's real data and run in a savepoint that is rolled back, like test_v2_allocation_playground.

Run:  bench --site kaitet.local run-tests --app upande_packhouse --module upande_packhouse.tests.test_v2_allocation_guards
"""

import unittest
from collections import defaultdict
from unittest.mock import patch

import frappe
from frappe.utils import flt

from upande_packhouse import allocation_plan as plans
from upande_packhouse.api.v2 import allocation_guards as g
from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.tests.test_v2_allocation_playground import _case, _Rollback, _team


def line(name, variety, length, left, customer=None, spec=None, mix=None, allocated=0.0):
	return frappe._dict(
		name=name,
		parent="SO-MIX" if mix else "SO-" + name,
		item_code=variety,
		custom_length=length,
		custom_line=spec,
		ordered_stems=left + allocated,
		allocated_stems=allocated,
		customer=customer or "C-" + name,
		customer_name=customer or "Customer " + name,
		custom_mixed_box=1 if mix else 0,
		custom_mixed_bunch=0,
		custom_mix_group=mix or "",
		custom_bunch_group="",
	)


class FakeModel(g.Model):
	"""A made-up delivery day: lines, shelf supply by (variety, length) and forecast by (variety, length)."""

	def __init__(self, lines, supply=None, forecast=None, specs=None, planned=None, errors=None):
		self.day, self.farm = "2026-10-30", "Kapkolia"
		self.errors = errors or {}
		self.lines = lines
		self.by_soi = {ln.name: ln for ln in lines}
		self.planned = planned or {}
		self.supply = {k: {"free": 0.0, "old": 0.0, "held": 0.0, **v} for k, v in (supply or {}).items()}
		self.specs = defaultdict(set, specs or {})
		self._likely = {k: tuple(v) for k, v in (forecast or {}).items()}
		self.expected_plan = defaultdict(float)
		self.left = {}
		for ln in lines:
			pl = self.planned.get(ln.name, {"real": 0.0, "expected": 0.0})
			self.left[ln.name] = max(0.0, ln.ordered_stems - ln.allocated_stems - pl["real"] - pl["expected"])
			self.expected_plan[(ln.item_code, ln.custom_length or "")] += pl["expected"]

	def likely_sure(self, variety, length):
		return self._likely.get((variety, length or ""), (0.0, 0.0))

	def error_pct(self, variety):
		return self.errors.get(variety)


def codes(result):
	return [(i["code"], i["severity"]) for i in result["heads_up"]]


def ask(model, *items):
	return g.check("2026-10-30", "Kapkolia", [{"sales_order_item": n, "stems": s, **({"length": ln} if ln else {})} for n, s, ln in items], model)


class TestRules(unittest.TestCase):
	def test_longer_length_is_red_when_a_customer_who_accepts_only_that_variety_would_be_short(self):
		a = line("A", "Moonwalk", "42cm", 100)
		b = line("B", "Moonwalk", "52cm", 500)  # no specification: strict
		m = FakeModel([a, b], supply={("Moonwalk", "52cm"): {"free": 300}})
		r = ask(m, ("A", 100, "52cm"))
		self.assertEqual(codes(r), [("longer_length", "red")])
		self.assertIn("Customer B", r["heads_up"][0]["message"])
		self.assertIn("42cm", r["heads_up"][0]["message"])
		self.assertEqual(r["heads_up"][0]["actions"][0]["kind"], "use_ordered_length")

	def test_longer_length_is_amber_when_the_short_customer_accepts_other_varieties(self):
		a = line("A", "Moonwalk", "42cm", 100)
		b = line("B", "Moonwalk", "52cm", 500, spec="S1")
		m = FakeModel([a, b], supply={("Moonwalk", "52cm"): {"free": 300}}, specs={"S1": {"Moonwalk", "Aqua"}})
		r = ask(m, ("A", 100, "52cm"))
		self.assertEqual(codes(r), [("longer_length", "amber")])

	def test_longer_length_is_quiet_when_the_stock_is_getting_old(self):
		a = line("A", "Moonwalk", "42cm", 100)
		b = line("B", "Moonwalk", "52cm", 500)
		m = FakeModel([a, b], supply={("Moonwalk", "52cm"): {"free": 300, "old": 300}})
		r = ask(m, ("A", 100, "52cm"))
		self.assertEqual(r["heads_up"], [])
		self.assertIn("getting old", r["quiet"][0])

	def test_longer_length_is_quiet_when_nobody_else_needs_it(self):
		a = line("A", "Moonwalk", "42cm", 100)
		m = FakeModel([a], supply={("Moonwalk", "52cm"): {"free": 300}})
		r = ask(m, ("A", 100, "52cm"))
		self.assertEqual(r["heads_up"], [])
		self.assertIn("nobody else needs", r["quiet"][0])

	def test_a_shorter_length_than_ordered_is_refused(self):
		a = line("A", "Moonwalk", "52cm", 100)
		with self.assertRaises(frappe.ValidationError):
			ask(FakeModel([a]), ("A", 10, "42cm"))

	def test_another_customer_needs_this_variety(self):
		a = line("A", "Aqua", "52cm", 100, spec="S1")  # accepts Aqua or Pink Athena
		b = line("B", "Aqua", "52cm", 400)  # strict
		m = FakeModel([a, b], supply={("Aqua", "52cm"): {"free": 450}}, specs={"S1": {"Aqua", "Pink Athena"}})
		r = ask(m, ("A", 100, None))
		self.assertEqual(codes(r), [("squeeze", "amber")])
		self.assertIn("Pink Athena", r["heads_up"][0]["message"])
		self.assertEqual(r["heads_up"][0]["actions"][0]["kind"], "open_order")

	def test_a_strict_customer_taking_the_variety_is_not_a_squeeze(self):
		a = line("A", "Aqua", "52cm", 100)
		b = line("B", "Aqua", "52cm", 400)
		m = FakeModel([a, b], supply={("Aqua", "52cm"): {"free": 450}})
		self.assertEqual(ask(m, ("A", 100, None))["heads_up"], [])

	def test_a_mixed_box_that_cannot_be_completed_is_red(self):
		m1 = line("M1", "Aqua", "52cm", 100, mix="1")
		m2 = line("M2", "Athena", "52cm", 200, mix="1")
		m = FakeModel([m1, m2], supply={("Aqua", "52cm"): {"free": 500}})  # nothing of Athena
		r = ask(m, ("M1", 100, None))
		self.assertEqual(codes(r), [("mix_incomplete", "red")])
		self.assertIn("Athena", r["heads_up"][0]["message"])

	def test_a_mixed_box_that_can_be_completed_is_quiet(self):
		m1 = line("M1", "Aqua", "52cm", 100, mix="1")
		m2 = line("M2", "Athena", "52cm", 200, mix="1")
		m = FakeModel([m1, m2], supply={("Aqua", "52cm"): {"free": 500}, ("Athena", "52cm"): {"free": 500}})
		self.assertEqual(ask(m, ("M1", 100, None), ("M2", 200, None))["heads_up"], [])

	def test_more_than_we_have_is_red_and_more_than_we_are_sure_of_is_amber(self):
		a = line("A", "Aqua", "52cm", 500)
		m = FakeModel([a], supply={("Aqua", "52cm"): {"free": 100}}, forecast={("Aqua", "52cm"): (200, 140)})
		red = ask(m, ("A", 400, None))  # cover is 300
		self.assertEqual(codes(red), [("not_enough", "red")])
		self.assertIn("300", red["heads_up"][0]["message"])
		amber = ask(m, ("A", 270, None))  # sure is 240
		self.assertEqual(codes(amber), [("not_enough", "amber")])
		self.assertEqual(ask(m, ("A", 200, None))["heads_up"], [])

	def test_not_enough_says_how_much_and_where_it_comes_from(self):
		a = line("A", "Aqua", "52cm", 500)
		m = FakeModel(
			[a],
			supply={("Aqua", "52cm"): {"free": 100}, ("Aqua", "62cm"): {"free": 1000}},
			forecast={("Aqua", "52cm"): (200, 140)},
		)
		r = ask(m, ("A", 400, None))
		msg = r["heads_up"][0]["message"]
		for part in ("400", "300", "100 on the shelf", "200 still to come from the farms", "The other 100 would be short"):
			self.assertIn(part, msg)
		self.assertEqual(r["heads_up"][0]["actions"], [])  # it says what is wrong; the planner decides

	def test_not_sure_says_what_is_sure(self):
		a = line("A", "Aqua", "52cm", 500)
		m = FakeModel([a], supply={("Aqua", "52cm"): {"free": 100}}, forecast={("Aqua", "52cm"): (200, 140)})
		r = ask(m, ("A", 270, None))
		self.assertIn("sure of 240", r["heads_up"][0]["message"])
		self.assertIn("no finished weeks", r["heads_up"][0]["message"])  # nothing to judge the forecast by here
		self.assertIn("The other 30", r["heads_up"][0]["message"])

	def test_not_sure_says_how_far_off_the_forecast_has_been(self):
		a = line("A", "Aqua", "52cm", 500)
		m = FakeModel([a], supply={("Aqua", "52cm"): {"free": 100}}, forecast={("Aqua", "52cm"): (200, 150)}, errors={"Aqua": 25})
		r = ask(m, ("A", 270, None))
		self.assertIn("off by 25% on average this year", r["heads_up"][0]["message"])

	def test_received_stock_counts_towards_what_we_can_cover(self):
		a = line("A", "Aqua", "52cm", 500)
		m = FakeModel([a], supply={("Aqua", "52cm"): {"free": 100, "received": 300}})
		self.assertEqual(ask(m, ("A", 400, None))["heads_up"], [])  # 100 on the shelf + 300 received
		red = ask(m, ("A", 450, None))["heads_up"][0]["message"]
		self.assertIn("300 received and not shelved yet", red)

	def test_confirming_the_same_or_less_says_nothing(self):
		a = line("A", "Aqua", "52cm", 500)
		m = FakeModel([a], planned={"A": {"real": 100, "expected": 0}}, forecast={("Aqua", "52cm"): (0, 0)})
		self.assertEqual(ask(m, ("A", 100, None))["heads_up"], [])
		self.assertEqual(ask(m, ("A", 50, None))["heads_up"], [])


class TestWithRealData(unittest.TestCase):
	def setUp(self):
		self.c = _case()
		if not self.c:
			self.skipTest("no free bucket with two open order lines of the same variety and length")

	def test_going_ahead_with_a_heads_up_needs_no_reason(self):
		a = self.c.lines[0]
		day = str(a.delivery_date)
		with _Rollback(), patch.object(pg, "expected_arrivals", lambda *x, **k: (0.0, 0.0)), patch.object(
			pg, "_stock_by_pool", lambda *x, **k: {}
		):
			lines = [{"sales_order_item": a.soi, "stems": 10}]
			self.assertTrue(g.check(day, self.c.farm, lines)["heads_up"])  # there is something to say
			pg.confirm_lines(day, self.c.farm, lines, team=_team())  # and the planner may go ahead without a reason
			self.assertTrue(plans.live_confirmations(soi=a.soi))

	def test_expected_stems_become_real_when_the_stock_reaches_the_shelf(self):
		a = self.c.lines[0]
		day = str(a.delivery_date)
		with _Rollback():
			plan = plans.get_plan(day, self.c.farm)
			info = {
				"sales_order": a.parent,
				"sales_order_item": a.soi,
				"customer": None,
				"ordered_variety": self.c.bucket.variety,
				"ordered_length": self.c.bucket.stem_length,
				"mix_key": "",
			}
			plans.plan_virtual(
				plan=plan, line=info, farm=self.c.farm, variety=self.c.bucket.variety, length=self.c.bucket.stem_length, stems=10
			)
			free = sum(b.free for b in plans.free_for_line(self.c.farm, self.c.bucket.variety, self.c.bucket.stem_length, a.soi))
			moved = plans.convert_virtual(day, self.c.farm)
			self.assertAlmostEqual(moved, min(10.0, free), places=3)
			held = sum(frappe.get_all("Bucket Reservation", filters={"sales_order_item": a.soi, "status": "Active"}, pluck="stems"))
			self.assertAlmostEqual(held, min(10.0, free), places=3)
			rest = 10.0 - min(10.0, free)
			virtual = [c for c in plans.live_confirmations(soi=a.soi) if c.source == "Virtual"]
			self.assertAlmostEqual(sum(flt(c.stems) for c in virtual), rest, places=3)
			if rest <= 0:
				self.assertEqual(
					frappe.db.get_value("Allocation Confirmation", {"sales_order_item": a.soi, "source": "Virtual"}, "release_reason"),
					"Now on the shelf",
				)

	def test_use_first_matches_old_stock_to_lines(self):
		a = self.c.lines[0]
		day = str(a.delivery_date)
		with _Rollback():
			pool = pg._stock_by_pool(self.c.farm, {self.c.bucket.variety})
			key = (self.c.bucket.variety, self.c.bucket.stem_length)
			fake = {**pool, key: {**pool.get(key, {}), "free": 500.0, "old": 500.0, "held": 0.0, "oldest": 5, "buckets": 1}}
			with patch.object(pg, "_stock_by_pool", lambda *x, **k: fake):
				r = g.get_use_first(day, self.c.farm)
			item = next((i for i in r["items"] if (i["variety"], i["length"]) == key), None)
			self.assertIsNotNone(item)
			self.assertLessEqual(sum(m["take"] for m in item["matches"]), 500.0 + 1e-6)
			for m in item["matches"]:
				self.assertIn(m["kind"], ("exact", "longer"))
				self.assertGreater(m["apply"]["stems"], 0)


class TestExpectedByFarm(unittest.TestCase):
	def test_the_forecast_is_shown_by_farm_and_adds_up(self):
		row = frappe.db.sql(
			"""SELECT f.variety, w.iso_year, w.week_number FROM `tabProduction Forecast Week` w
			   JOIN `tabProduction Forecast` f ON f.name = w.parent
			   WHERE w.grade = 'all' AND w.revised_forecast_stems > 1000 AND w.iso_year = 2026 LIMIT 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("no forecast")
		from datetime import date, timedelta

		day = date.fromisocalendar(row[0].iso_year, row[0].week_number, 3) + timedelta(
			days=int(plans.setting("allocation_harvest_lead_days"))
		)
		farms = {r.farm for r in frappe.get_all("Shelf Locations", filters={"parenttype": "Production Settings"}, fields=["farm"])}
		for length in ("42cm", "52cm", "62cm"):
			d = pg.expected_detail(row[0].variety, length, day, "Kapkolia")
			self.assertAlmostEqual(sum(d["by_farm"].values()), d["likely"], places=6)
			self.assertLessEqual(d["sure"], d["likely"] + 1e-9)
			# farms, never greenhouses
			self.assertTrue(set(d["by_farm"]) <= farms | {"Unknown"}, d["by_farm"])
