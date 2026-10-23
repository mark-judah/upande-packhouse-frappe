"""Standing orders calendar endpoint (api/v2/sales_order.get_standing_orders).

The local site has no Auto Repeat records, so the endpoint tests build
synthetic ones inside the test transaction and roll everything back; the
real Sales Orders are only tagged (auto_repeat) and untagged by the rollback.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, getdate, nowdate

from upande_packhouse.api.v2 import sales_order as so_api


def _ar_doc(**kw):
	"""Unsaved Auto Repeat -- the schedule maths needs no database."""
	d = {"doctype": "Auto Repeat", "reference_doctype": "Sales Order", "reference_document": "X"}
	d.update(kw)
	return frappe.get_doc(d)


class TestOccurrenceDates(IntegrationTestCase):
	def test_daily(self):
		ar = _ar_doc(frequency="Daily", start_date="2026-03-01")
		got = so_api.occurrence_dates(ar, "2026-03-01", "2026-03-05")
		self.assertEqual([str(d) for d in got], ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"])

	def test_weekly_and_fortnightly(self):
		ar = _ar_doc(frequency="Weekly", start_date="2026-03-02")
		self.assertEqual([str(d) for d in so_api.occurrence_dates(ar, "2026-03-01", "2026-03-31")],
			["2026-03-09", "2026-03-16", "2026-03-23", "2026-03-30"])
		ar = _ar_doc(frequency="Fortnightly", start_date="2026-03-02")
		self.assertEqual([str(d) for d in so_api.occurrence_dates(ar, "2026-03-01", "2026-03-31")],
			["2026-03-16", "2026-03-30"])

	def test_weekly_on_days(self):
		ar = _ar_doc(frequency="Weekly", start_date="2026-03-02",  # a Monday
			repeat_on_days=[{"day": "Monday"}, {"day": "Thursday"}])
		self.assertEqual([str(d) for d in so_api.occurrence_dates(ar, "2026-03-01", "2026-03-16")],
			["2026-03-05", "2026-03-09", "2026-03-12", "2026-03-16"])

	def test_monthly_quarterly_halfyearly_yearly(self):
		for freq, step in (("Monthly", 1), ("Quarterly", 3), ("Half-yearly", 6), ("Yearly", 12)):
			ar = _ar_doc(frequency=freq, start_date="2026-01-15")
			got = so_api.occurrence_dates(ar, "2026-01-01", "2028-12-31")
			want = [str(getdate(add_months("2026-01-15", step * i))) for i in range(1, 40)
				if getdate(add_months("2026-01-15", step * i)) <= getdate("2028-12-31")]
			self.assertEqual([str(d) for d in got], want, freq)

	def test_monthly_last_day_and_end_date(self):
		ar = _ar_doc(frequency="Monthly", start_date="2026-01-31", repeat_on_last_day=1, end_date="2026-04-15")
		self.assertEqual([str(d) for d in so_api.occurrence_dates(ar, "2026-01-01", "2026-12-31")],
			["2026-02-28", "2026-03-31"])  # nothing after end_date

	def test_window_lower_bound(self):
		ar = _ar_doc(frequency="Daily", start_date="2026-03-01")
		self.assertEqual(str(so_api.occurrence_dates(ar, "2026-03-04", "2026-03-04")[0]), "2026-03-04")


class TestStandingOrdersEndpoint(IntegrationTestCase):
	def setUp(self):
		self._commit = frappe.db.commit
		frappe.db.commit = lambda *a, **k: None
		frappe.set_user("Administrator")
		self.before = frappe.db.count("Auto Repeat"), frappe.db.count("Sales Order")
		rows = frappe.db.sql(
			"""SELECT name, customer, delivery_date FROM `tabSales Order`
			   WHERE business_unit = 'Roses' AND docstatus = 1 AND IFNULL(auto_repeat, '') = ''
			   ORDER BY modified DESC LIMIT 6""", as_dict=True)
		if len(rows) < 3:
			self.skipTest("needs 3 submitted Roses sales orders")
		self.src, self.gen1, self.gen2 = rows[0], rows[1], rows[2]
		frappe.db.savepoint("so_standing")

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.commit = self._commit
		self.assertEqual(self.before, (frappe.db.count("Auto Repeat"), frappe.db.count("Sales Order")))

	def _ar(self, **kw):
		today = getdate(nowdate())
		d = {"doctype": "Auto Repeat", "reference_doctype": "Sales Order", "reference_document": self.src.name,
			"frequency": "Weekly", "start_date": add_days(today, -30)}
		d.update(kw)
		return frappe.get_doc(d).insert(ignore_permissions=True)

	def _tag(self, ar, so, date):
		frappe.db.set_value("Sales Order", so, {"auto_repeat": ar.name, "delivery_date": date}, update_modified=False)

	def _call(self, **kw):
		today = getdate(nowdate())
		kw.setdefault("from_date", str(add_days(today, -35)))
		kw.setdefault("to_date", str(add_days(today, 35)))
		return so_api.get_standing_orders(**kw)

	def test_generated_and_future(self):
		today = getdate(nowdate())
		ar = self._ar()
		self._tag(ar, self.gen1.name, add_days(today, -7))
		self._tag(ar, self.gen2.name, add_days(today, -14))
		r = self._call(customer=self.src.customer)
		self.assertTrue(r["success"], r)
		gen = [e for e in r["events"] if e["kind"] == "generated"]
		sched = [e for e in r["events"] if e["kind"] == "scheduled"]
		self.assertEqual({e["order"] for e in gen}, {self.gen1.name, self.gen2.name})
		self.assertTrue(sched)
		self.assertTrue(all(getdate(e["date"]) >= today for e in sched))
		self.assertTrue(all((getdate(e["date"]) - getdate(ar.start_date)).days % 7 == 0 for e in sched))
		self.assertEqual(r["kpis"]["active"], 1)
		self.assertEqual(r["kpis"]["generated"], 2)
		self.assertEqual(r["kpis"]["due_week"], sum(1 for e in sched if getdate(e["date"]) <= add_days(today, 6)))
		# boxes / stems on the generated rows are the core figures of that order
		self.assertEqual(sum(e["stems"] for e in gen), sum(so_api._order_figures([self.gen1.name, self.gen2.name])[n][1] for n in (self.gen1.name, self.gen2.name)))

	def test_kpi_range_independent_of_grid(self):
		today = getdate(nowdate())
		ar = self._ar()
		self._tag(ar, self.gen1.name, add_days(today, -7))
		r = self._call(kpi_from=str(add_days(today, -3)), kpi_to=str(today))
		self.assertEqual(r["kpis"]["generated"], 0)
		self.assertTrue(any(e["kind"] == "generated" for e in r["events"]))

	def test_disabled_has_no_future_and_filters(self):
		ar = self._ar(disabled=1)
		r = self._call()
		self.assertEqual([e for e in r["events"] if e["kind"] == "scheduled"], [])
		self.assertEqual((r["kpis"]["active"], r["kpis"]["disabled"]), (0, 1))
		self.assertEqual(len(self._call(state="active")["standing"]), 0)
		self.assertEqual(len(self._call(state="disabled")["standing"]), 1)
		self.assertEqual(self._call(customer="no such customer")["standing"], [])

	def test_end_date_stops_schedule(self):
		today = getdate(nowdate())
		self._ar(end_date=add_days(today, 10))
		sched = [e for e in self._call()["events"] if e["kind"] == "scheduled"]
		self.assertTrue(sched)
		self.assertTrue(all(getdate(e["date"]) <= add_days(today, 10) for e in sched))

	def test_region_filter(self):
		self._ar()
		from upande_packhouse.api.v2.core import region
		mine = frappe.db.sql(
			"SELECT COALESCE(NULLIF(farm,''), NULLIF(custom_farm,'')) FROM `tabSales Order` WHERE name=%s", self.src.name)[0][0]
		reg = region.region_of(mine)
		if not reg:
			self.skipTest("source order farm has no region")
		other = "Karen" if reg == "Ravine" else "Ravine"
		self.assertEqual(len(self._call(region=reg)["standing"]), 1)
		self.assertEqual(len(self._call(region=other)["standing"]), 0)
