"""A release deletes the line's allocation and pick rows and never touches the pick list itself.

Plants a bucket allocated to two lines, with each line's pick row on a submitted pick list, then releases one
line and then the other. Everything is rolled back."""

import frappe
from frappe.tests import IntegrationTestCase

from upande_packhouse.stale_release import drop_allocation_and_pick_rows

BUCKET = "TRELS1"
LINE_A, LINE_B = "trel-line-a", "trel-line-b"
OPL = "OPL-TREL-1"


def _pick_row(name, soi, issued=0):
	frappe.db.sql(
		"""INSERT INTO `tabPick List Item` (name, owner, modified_by, creation, modified, docstatus, idx, parent,
		       parentfield, parenttype, qty, stock_qty, item_code, bucket, stem_length, sales_order_item, issued)
		   VALUES (%s,'Administrator','Administrator',NOW(),NOW(),0,%s,%s,'items','Order Pick List',10,50,
		       'Madam Red',%s,'42cm',%s,%s)""",
		(name, 1 if soi == LINE_A else 2, OPL, BUCKET, soi, issued),
	)


class TestReleaseDropsRows(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.sql(
			"""INSERT INTO `tabOrder Pick List` (name, owner, modified_by, creation, modified, docstatus, idx,
			       custom_total_stems) VALUES (%s,'Administrator','Administrator',NOW(),NOW(),1,0,100)""",
			OPL,
		)
		bas = frappe.get_doc(
			{
				"doctype": "Bucket Allocation Status",
				"bucket_id": BUCKET,
				"item_code": "Madam Red",
				"stem_length": "42cm",
				"total_quantity": 200,
				"bucket_allocations": [
					{"sales_order": "SO-A", "sales_order_item": LINE_A, "quantity_allocated": 50},
					{"sales_order": "SO-B", "sales_order_item": LINE_B, "quantity_allocated": 50},
				],
			}
		)
		bas.flags.ignore_links = True
		bas.flags.ignore_validate = True
		bas.insert(ignore_permissions=True)
		self.bas = bas.name
		_pick_row("trel-pick-a", LINE_A)
		_pick_row("trel-pick-b", LINE_B)

	def tearDown(self):
		frappe.db.rollback()

	def _rows(self):
		return frappe.db.sql(
			"SELECT sales_order_item FROM `tabBucket Allocations` WHERE parent = %s AND cancelled = 0",
			self.bas,
		)

	def test_the_line_loses_its_allocation_and_pick_row_and_the_other_line_keeps_its_own(self):
		res = drop_allocation_and_pick_rows(LINE_A, BUCKET)
		self.assertEqual((res["success"], res["allocations"], res["pick_rows"]), (True, 1, 1))
		self.assertEqual([r[0] for r in self._rows()], [LINE_B])
		self.assertTrue(frappe.db.exists("Bucket Allocation Status", self.bas))
		self.assertEqual(
			frappe.db.sql_list("SELECT name FROM `tabPick List Item` WHERE parent = %s", OPL), ["trel-pick-b"]
		)
		self.assertEqual(frappe.db.get_value("Bucket Allocation Status", self.bas, "allocated_quantity"), 50)

	def test_the_allocation_status_goes_with_its_last_allocation(self):
		drop_allocation_and_pick_rows(LINE_A, BUCKET)
		drop_allocation_and_pick_rows(LINE_B, BUCKET)
		self.assertFalse(frappe.db.exists("Bucket Allocation Status", self.bas))

	def test_the_pick_list_is_never_cancelled_or_deleted_even_when_it_is_left_empty(self):
		drop_allocation_and_pick_rows(LINE_A, BUCKET)
		drop_allocation_and_pick_rows(LINE_B, BUCKET)
		self.assertEqual(frappe.db.get_value("Order Pick List", OPL, "docstatus"), 1)
		self.assertEqual(frappe.db.count("Pick List Item", {"parent": OPL}), 0)
		self.assertEqual(float(frappe.db.get_value("Order Pick List", OPL, "custom_total_stems") or 0), 0)

	def test_a_bucket_already_issued_for_the_line_is_refused_and_left_whole(self):
		frappe.db.sql("UPDATE `tabPick List Item` SET issued = 1 WHERE name = 'trel-pick-a'")
		res = drop_allocation_and_pick_rows(LINE_A, BUCKET)
		self.assertFalse(res["success"])
		self.assertEqual(len(self._rows()), 2)
		self.assertTrue(frappe.db.exists("Pick List Item", "trel-pick-a"))

	def test_a_pick_row_with_no_allocation_behind_it_is_deleted_too(self):
		frappe.db.sql(
			"DELETE FROM `tabBucket Allocations` WHERE parent = %s AND sales_order_item = %s",
			(self.bas, LINE_A),
		)
		res = drop_allocation_and_pick_rows(LINE_A, BUCKET)
		self.assertEqual((res["success"], res["allocations"], res["pick_rows"]), (True, 0, 1))
		self.assertFalse(frappe.db.exists("Pick List Item", "trel-pick-a"))
