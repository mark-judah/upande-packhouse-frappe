"""Standing orders: orders that repeat through Auto Repeat and APPEAR in advance
of their delivery (standing_orders.py, overrides/auto_repeat.py,
api/v2/standing_orders.py). Schedule maths needs no database, so those tests
build unsaved Auto Repeat documents; the rest run inside the test transaction.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from upande_packhouse import standing_orders as so
from upande_packhouse.api.v2 import standing_orders as api


def _ar(**kw):
	d = {"doctype": "Auto Repeat", "reference_doctype": "Sales Order", "reference_document": "X"}
	d.update(kw)
	return frappe.get_doc(d)


def _days(ar, a, b):
	return [str(d) for d in so.appearance_dates(ar, a, b)]


class TestSchedule(IntegrationTestCase):
	def test_first_order_appears_on_the_start_date(self):
		ar = _ar(frequency="Weekly", start_date="2026-03-02")
		self.assertEqual(
			_days(ar, "2026-03-01", "2026-03-31"),
			["2026-03-02", "2026-03-09", "2026-03-16", "2026-03-23", "2026-03-30"],
		)

	def test_daily_and_fortnightly(self):
		self.assertEqual(
			_days(_ar(frequency="Daily", start_date="2026-03-01"), "2026-03-01", "2026-03-03"),
			["2026-03-01", "2026-03-02", "2026-03-03"],
		)
		self.assertEqual(
			_days(_ar(frequency="Fortnightly", start_date="2026-03-02"), "2026-03-01", "2026-03-31"),
			["2026-03-02", "2026-03-16", "2026-03-30"],
		)

	def test_weekly_on_days_moves_a_start_date_that_is_not_one(self):
		# 2026-03-03 is a Tuesday; the repeat days are Monday and Thursday
		ar = _ar(
			frequency="Weekly",
			start_date="2026-03-03",
			repeat_on_days=[{"day": "Monday"}, {"day": "Thursday"}],
		)
		self.assertEqual(
			_days(ar, "2026-03-01", "2026-03-16"), ["2026-03-05", "2026-03-09", "2026-03-12", "2026-03-16"]
		)

	def test_monthly_on_a_day(self):
		ar = _ar(frequency="Monthly", start_date="2026-03-01", repeat_on_day=15)
		self.assertEqual(_days(ar, "2026-03-01", "2026-05-31"), ["2026-03-15", "2026-04-15", "2026-05-15"])

	def test_end_date_stops_it(self):
		ar = _ar(frequency="Daily", start_date="2026-03-01", end_date="2026-03-03")
		self.assertEqual(_days(ar, "2026-03-01", "2026-03-31"), ["2026-03-01", "2026-03-02", "2026-03-03"])

	def test_delivery_is_days_after_the_appearance(self):
		ar = _ar(frequency="Weekly", start_date="2026-11-01", custom_delivery_days_after=3)
		s = so.schedule(ar, count=2, window_from="2026-11-01")
		self.assertEqual(
			[(str(x["appears"]), str(x["delivers"])) for x in s],
			[("2026-11-01", "2026-11-04"), ("2026-11-08", "2026-11-11")],
		)

	def test_no_days_after_means_delivery_on_the_appearance(self):
		ar = _ar(frequency="Daily", start_date="2026-11-01")
		self.assertEqual(str(so.delivery_for(ar, "2026-11-01")), "2026-11-01")


class TestRepeatedOrder(IntegrationTestCase):
	def test_copy_is_stamped_and_cleaned(self):
		ar = _ar(frequency="Weekly", start_date="2026-11-01", custom_delivery_days_after=3)
		ar.next_schedule_date = "2026-11-08"
		doc = frappe.new_doc("Sales Order")
		doc.delivery_date = "2026-11-08"  # what stock Auto Repeat would have stamped
		doc.append(
			"items",
			{
				"item_code": "x",
				"custom_opl": None,
				"custom_fully_allocated": 1,
				"delivery_date": "2026-11-08",
			},
		)
		doc.append("custom_confirmed_stems_table", {"farm": "F", "stems": 10})
		doc.custom_truck_details = "KAA 123"
		doc.po_no = "588211"
		so.apply_standing_dates(ar, doc)
		self.assertEqual(str(doc.transaction_date), "2026-11-08")
		self.assertEqual(str(doc.delivery_date), "2026-11-11")
		self.assertEqual(str(doc.items[0].delivery_date), "2026-11-11")
		self.assertEqual(doc.items[0].custom_fully_allocated, 0)
		self.assertEqual(len(doc.custom_confirmed_stems_table), 0)
		self.assertFalse(doc.custom_truck_details)
		self.assertFalse(doc.po_no)
		self.assertEqual(doc.custom_week, str(getdate("2026-11-11").isocalendar()[1]))

	def test_other_doctypes_are_untouched(self):
		self.assertFalse(so.is_standing(_ar(reference_doctype="Sales Invoice")))

	def test_days_after_is_bounded(self):
		with self.assertRaises(frappe.ValidationError):
			so.validate_standing(_ar(custom_delivery_days_after=99))


class TestApi(IntegrationTestCase):
	def test_preview_schedule(self):
		r = api.preview_schedule(
			frequency="Weekly", start_date="2026-11-02", days_after=3, count=2, weekdays="[]"
		)
		self.assertTrue(r["success"], r)
		self.assertEqual(
			[(str(x["appears"]), str(x["delivers"])) for x in r["schedule"]],
			[("2026-11-02", "2026-11-05"), ("2026-11-09", "2026-11-12")],
		)

	def test_preview_schedule_rejects_a_bad_frequency(self):
		self.assertFalse(api.preview_schedule(frequency="Sometimes", start_date="2026-11-02")["success"])

	def test_list_runs_and_reports_kpis(self):
		r = api.get_standing_orders()
		self.assertTrue(r["success"], r)
		for k in ("active", "paused", "total", "appear_week", "deliver_week_boxes"):
			self.assertIn(k, r["kpis"])

	def test_email_template_renders_from_the_order(self):
		"""The Notification tab's message, rendered with doc only, reads the figures through standing_summary(doc)."""
		src = frappe.get_all(
			"Sales Order", filters={"docstatus": ["<", 2], "business_unit": "Roses"}, pluck="name", limit=1
		)
		if not src:
			self.skipTest("no Roses Sales Order on this site")
		order = frappe.get_doc("Sales Order", src[0])
		ar = frappe.get_doc(
			{
				"doctype": "Auto Repeat",
				"reference_doctype": "Sales Order",
				"reference_document": order.name,
				"frequency": "Weekly",
				"start_date": nowdate(),
				"custom_delivery_days_after": 3,
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)
		order.db_set("auto_repeat", ar.name)
		order.auto_repeat = ar.name
		tpl = frappe.get_doc("Email Template", so.DEFAULT_TEMPLATE)
		subject = frappe.render_template(tpl.subject, {"doc": order})
		html = frappe.render_template(tpl.response_html, {"doc": order})
		self.assertIn("delivery", subject)
		for needle in ("Open the order", order.name, "Boxes", "Delivery"):
			self.assertIn(needle, html)
		self.assertNotIn("{{", html)
