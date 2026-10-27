"""Box labels in the Label Print Manager: window, filters, printed flag, preview.

Runs against the local site and rolls back."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from upande_packhouse.api.v2 import box_labels as bl


class TestBoxLabels(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.wide = ("2000-01-01", "2100-01-01")

	def _first_order(self):
		o = bl.orders(*self.wide)
		if not o["orders"]:
			self.skipTest("no box labels on this site")
		return o, o["orders"][0]

	def test_default_window_is_today_and_tomorrow(self):
		o = bl.orders()
		self.assertEqual((o["from_date"], o["to_date"]), (today(), str(add_days(today(), 1))))

	def test_reversed_dates_are_swapped(self):
		o = bl.orders(from_date=add_days(today(), 3), to_date=today())
		self.assertEqual(o["from_date"], today())

	def test_totals_add_up_and_filters_narrow(self):
		o, first = self._first_order()
		self.assertEqual(o["totals"]["boxes"], sum(r["boxes"] for r in o["orders"]))
		by_customer = bl.orders(*self.wide, customer=first["customer"])
		self.assertTrue(
			by_customer["orders"] and all(r["customer"] == first["customer"] for r in by_customer["orders"])
		)
		by_order = bl.orders(*self.wide, q=first["sales_order"])
		self.assertIn(first["sales_order"], [r["sales_order"] for r in by_order["orders"]])
		self.assertEqual(bl.orders(*self.wide, q="no-such-order-zzz")["orders"], [])

	def test_printed_flag_round_trip_and_filters(self):
		o, first = self._first_order()
		boxes = bl.boxes(first["sales_order"])["boxes"]
		self.assertEqual(len(boxes), first["boxes"])
		bl.set_printed(names=[boxes[0]["name"]], printed=1)
		again = next(r for r in bl.orders(*self.wide)["orders"] if r["sales_order"] == first["sales_order"])
		self.assertEqual(again["printed"], first["printed"] + (0 if boxes[0]["printed"] else 1))
		if first["boxes"] == 1:
			self.assertNotIn(
				first["sales_order"],
				[r["sales_order"] for r in bl.orders(*self.wide, printed="todo")["orders"]],
			)
		bl.set_printed(names=[boxes[0]["name"]], printed=0)
		self.assertIsNone(frappe.db.get_value("Box Label", boxes[0]["name"], "printed_at"))

	def test_whole_order_can_be_marked(self):
		o, first = self._first_order()
		bl.set_printed(sales_order=first["sales_order"], printed=1)
		row = next(r for r in bl.orders(*self.wide)["orders"] if r["sales_order"] == first["sales_order"])
		self.assertEqual(row["printed"], row["boxes"])
		self.assertEqual(
			[r["sales_order"] for r in bl.orders(*self.wide, printed="done")["orders"]].count(
				first["sales_order"]
			),
			1,
		)

	def test_preview_uses_the_print_format_and_returns_pdf_and_pages(self):
		o, first = self._first_order()
		if not bl.formats():
			self.skipTest("no Box Label print format")
		out = bl.preview(sales_order=first["sales_order"])
		self.assertEqual(out["count"], first["boxes"])
		self.assertTrue(out["pdf_base64"] and out["pages"][0].startswith("data:image/png"))

	def test_preview_refuses_a_format_that_is_not_for_box_labels(self):
		o, first = self._first_order()
		with self.assertRaises(frappe.ValidationError):
			bl.preview(sales_order=first["sales_order"], print_format="Standard")
