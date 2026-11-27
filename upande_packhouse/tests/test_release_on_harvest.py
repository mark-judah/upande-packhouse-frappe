"""A bucket harvested again lets go of what it was held for: allocation rows, pick rows, anomalies.

Plants a bucket allocated to three order lines before its latest harvest (one a plain allocation, one whose pick
row was part of a transfer, one a transfer with no allocation left) and a fourth allocated after it, then runs the
harvest-time release. Everything is rolled back."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from upande_packhouse import bucket_reuse

BUCKET = "TRHRV1"
PLAIN, MOVING, GONE, FRESH = "trhv-plain", "trhv-moving", "trhv-gone", "trhv-fresh"
OPL = "OPL-TRHV-1"
HARVEST = "STG-TRHV-HARVEST"


def _pick_row(name, soi, idx, **flags):
	cols = [
		"name",
		"owner",
		"modified_by",
		"creation",
		"modified",
		"docstatus",
		"idx",
		"parent",
		"parentfield",
		"parenttype",
		"qty",
		"stock_qty",
		"item_code",
		"bucket",
		"stem_length",
		"sales_order_item",
	]
	vals = [
		name,
		"Administrator",
		"Administrator",
		add_to_date(now_datetime(), days=-1),
		now_datetime(),
		0,
		idx,
		OPL,
		"items",
		"Order Pick List",
		10,
		50,
		"Madam Red",
		BUCKET,
		"42cm",
		soi,
	]
	for k, v in flags.items():
		cols.append(k)
		vals.append(v)
	frappe.db.sql(
		f"INSERT INTO `tabPick List Item` ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", vals
	)


class TestReleaseOnHarvest(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
		self.commit.start()
		frappe.db.sql(
			"""INSERT INTO `tabOrder Pick List` (name, owner, modified_by, creation, modified, docstatus, idx,
			       custom_total_stems) VALUES (%s,'Administrator','Administrator',NOW(),NOW(),1,0,200)""",
			OPL,
		)
		bas = frappe.get_doc(
			{
				"doctype": "Bucket Allocation Status",
				"bucket_id": BUCKET,
				"item_code": "Madam Red",
				"stem_length": "42cm",
				"total_quantity": 400,
				"bucket_allocations": [
					{"sales_order": "SO-P", "sales_order_item": PLAIN, "quantity_allocated": 50},
					{"sales_order": "SO-M", "sales_order_item": MOVING, "quantity_allocated": 50},
					{"sales_order": "SO-F", "sales_order_item": FRESH, "quantity_allocated": 50},
				],
			}
		)
		bas.flags.ignore_links = True
		bas.flags.ignore_validate = True
		bas.insert(ignore_permissions=True)
		self.bas = bas.name
		yesterday = add_to_date(now_datetime(), days=-1)
		# the first two were made before the harvest, the fresh one after it
		frappe.db.sql(
			"UPDATE `tabBucket Allocations` SET creation = %s WHERE parent = %s AND sales_order_item != %s",
			(yesterday, self.bas, FRESH),
		)
		_pick_row("trhv-pick-plain", PLAIN, 1)
		_pick_row("trhv-pick-moving", MOVING, 2, loaded_in_trolley=1)
		_pick_row("trhv-pick-gone", GONE, 3, awaiting_transfer=1)
		_pick_row("trhv-pick-fresh", FRESH, 4)
		# the pick rows of the fresh line are new too
		frappe.db.sql("UPDATE `tabPick List Item` SET creation = NOW() WHERE name = 'trhv-pick-fresh'")
		frappe.db.sql(
			"""INSERT INTO `tabStock Entry` (name, owner, modified_by, creation, modified, docstatus, idx,
			       stock_entry_type, custom_bucket_id) VALUES (%s,'Administrator','Administrator',%s,%s,1,0,
			       'Harvesting',%s)""",
			(HARVEST, add_to_date(now_datetime(), minutes=-30), now_datetime(), BUCKET),
		)

	def tearDown(self):
		frappe.db.rollback()
		self.commit.stop()

	def _release(self):
		return bucket_reuse.release_on_harvest(BUCKET, HARVEST)

	def _lines(self):
		return {
			r[0]
			for r in frappe.db.sql(
				"SELECT sales_order_item FROM `tabBucket Allocations` WHERE parent = %s AND cancelled = 0",
				self.bas,
			)
		}

	def _anomalies(self):
		return frappe.db.sql(
			"""SELECT skipped_step, sales_order, order_pick_list FROM `tabBucket Reuse Anomaly`
			   WHERE bucket_id = %s AND harvest_stock_entry = %s""",
			(BUCKET, HARVEST),
			as_dict=True,
		)

	def test_the_stale_allocations_are_deleted_and_the_fresh_one_is_kept(self):
		out = self._release()
		self.assertEqual(self._lines(), {FRESH})
		self.assertTrue(frappe.db.exists("Bucket Allocation Status", self.bas))
		self.assertEqual(out["allocations"], 1)

	def test_the_pick_rows_of_the_stale_lines_go_and_the_fresh_one_stays(self):
		self._release()
		left = frappe.db.sql_list("SELECT name FROM `tabPick List Item` WHERE parent = %s", OPL)
		self.assertEqual(left, ["trhv-pick-fresh"])

	def test_the_pick_list_is_not_cancelled_or_deleted(self):
		self._release()
		self.assertEqual(frappe.db.get_value("Order Pick List", OPL, "docstatus"), 1)
		self.assertEqual(float(frappe.db.get_value("Order Pick List", OPL, "custom_total_stems") or 0), 50)

	def test_a_plain_allocation_is_issuing_skipped_and_a_transfer_is_transfer_incomplete(self):
		self._release()
		by_step = sorted(a.skipped_step for a in self._anomalies())
		self.assertEqual(by_step, ["Issuing Skipped", "Transfer Incomplete", "Transfer Incomplete"])

	def test_the_incomplete_transfers_are_counted_and_logged_for_the_journey(self):
		out = self._release()
		self.assertEqual(out["transfers"], 2)
		events = frappe.db.sql(
			"SELECT outcome FROM `tabBucket Transfer Event` WHERE bucket = %s AND stage = 'Transfer incomplete'",
			BUCKET,
		)
		self.assertEqual(len(events), 2)

	def test_the_anomaly_names_the_pick_list_the_row_was_on(self):
		self._release()
		self.assertTrue(all(a.order_pick_list == OPL for a in self._anomalies()))

	def test_nothing_stale_means_nothing_released(self):
		frappe.db.sql(
			"UPDATE `tabStock Entry` SET creation = %s WHERE name = %s",
			(add_to_date(now_datetime(), days=-3), HARVEST),
		)
		out = self._release()
		self.assertEqual((out["allocations"], out["transfers"]), (0, 0))
		self.assertEqual(self._lines(), {PLAIN, MOVING, FRESH})

	def test_it_is_safe_to_run_twice(self):
		self._release()
		again = self._release()
		self.assertEqual((again["allocations"], again["transfers"]), (0, 0))
		self.assertEqual(len(self._anomalies()), 3)
