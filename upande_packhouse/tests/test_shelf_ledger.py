"""The shelf ledger balances: opening + in - out = closing, partial issues come from pick rows, and lots the
log never closed get a dated, marked exit. Plants lots on a made-up farm; everything is rolled back."""

from datetime import date, datetime

import frappe
from frappe.tests import IntegrationTestCase

from upande_packhouse.api.v2.shelf_ledger import build

FARM = "TSL Farm"
DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 12)


def _lot(name, bucket, qty, shelved, removed=None, reason=None, shelf="TSL-S1", variety="Madam Red"):
	frappe.db.sql(
		"""INSERT INTO `tabShelving Log` (name, owner, modified_by, creation, modified, docstatus, idx, bucket_id,
		       shelf, farm, variety, stem_length, stem_qty, shelved_on, removed_on, reason)
		   VALUES (%s,'Administrator','Administrator',NOW(),NOW(),0,0,%s,%s,%s,%s,'42cm',%s,%s,%s,%s)""",
		(name, bucket, shelf, FARM, variety, qty, shelved, removed, reason),
	)


def _issue(name, bucket, qty, at, opl="OPL-TSL-1"):
	frappe.db.sql(
		"""INSERT INTO `tabPick List Item` (name, owner, modified_by, creation, modified, docstatus, idx, parent,
		       parentfield, parenttype, qty, stock_qty, item_code, bucket, stem_length, issued, issued_at)
		   VALUES (%s,'Administrator','Administrator',NOW(),NOW(),0,1,%s,'items','Order Pick List',%s,%s,
		       'Madam Red',%s,'42cm',1,%s)""",
		(name, opl, qty, qty, bucket, at),
	)


def _totals(r):
	o = sum(x["q"] for x in r["open"])
	i = sum(m["q"] for m in r["moves"] if m["k"] == "i")
	x = sum(m["q"] for m in r["moves"] if m["k"] == "x")
	return o, i, x


class TestShelfLedger(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.sql(
			"""INSERT INTO `tabOrder Pick List` (name, owner, modified_by, creation, modified, docstatus, idx)
			   VALUES ('OPL-TSL-1','Administrator','Administrator',NOW(),NOW(),1,0)"""
		)
		# on the shelf from the day before, issued in two draws today, closed in the log at its full size
		_lot("tsl-1", "TSLB1", 100, "2026-03-09 08:00:00", "2026-03-10 15:00:00", "Issued to Sales Order")
		_issue("tsl-p1", "TSLB1", 30, "2026-03-10 10:00:00")
		_issue("tsl-p2", "TSLB1", 70, "2026-03-10 15:00:00")
		# shelved today, discarded today
		_lot("tsl-2", "TSLB2", 40, "2026-03-10 09:00:00", "2026-03-10 11:00:00", "Discarded", shelf="TSL-S2")
		# never closed; the bucket is harvested again on the 11th
		_lot("tsl-3", "TSLB3", 60, "2026-03-09 09:00:00")
		frappe.db.sql(
			"""INSERT INTO `tabStock Entry` (name, owner, modified_by, creation, modified, docstatus, idx,
			       stock_entry_type, custom_bucket_id) VALUES ('TSL-SE-1','Administrator','Administrator',
			       '2026-03-11 07:00:00','2026-03-11 07:00:00',1,0,'Harvesting','TSLB3')"""
		)

	def tearDown(self):
		frappe.db.rollback()

	def test_balances_and_partial_issues(self):
		r = build(FARM, DAY, NOW, 6)
		o, i, x = _totals(r)
		self.assertEqual(o, 160)  # tsl-1 100 and tsl-3 60
		self.assertEqual(i, 40)
		self.assertEqual(x, 100 + 40)  # two draws of tsl-1, the discard; tsl-3 leaves tomorrow
		issues = [m for m in r["moves"] if m["k"] == "x" and m["m"] == "Issued to an order"]
		self.assertEqual(sorted(m["q"] for m in issues), [30, 70])
		self.assertTrue(all(m["ref"].endswith("OPL-TSL-1") for m in issues))
		self.assertEqual(r["left_out"]["stems"], 0)

	def test_fully_drawn_lot_has_no_second_exit(self):
		r = build(FARM, DAY, NOW, 6)
		exits = [m for m in r["moves"] if m["b"] == "TSLB1" and m["k"] == "x"]
		self.assertEqual(len(exits), 2)

	def test_never_closed_lot_exits_when_bucket_is_harvested_again(self):
		r = build(FARM, date(2026, 3, 11), NOW, 6)
		ex = [m for m in r["moves"] if m["b"] == "TSLB3"]
		self.assertEqual(len(ex), 1)
		self.assertEqual(ex[0]["q"], 60)
		self.assertTrue(ex[0]["inf"])
		self.assertEqual(ex[0]["t"][:5], "07:00")

	def test_each_day_opens_where_the_day_before_closed(self):
		a = _totals(build(FARM, DAY, NOW, 6))
		b = _totals(build(FARM, date(2026, 3, 11), NOW, 6))
		self.assertEqual(a[0] + a[1] - a[2], b[0])

	def test_issue_larger_than_the_lots_is_left_out(self):
		# the bucket has 40 stems on the shelf, an issue of 60 is recorded: 20 have no lot to come from
		_issue("tsl-p3", "TSLB2", 60, "2026-03-10 10:00:00")
		r = build(FARM, DAY, NOW, 6)
		self.assertEqual(r["left_out"]["stems"], 20)
		self.assertEqual(r["left_out"]["issues"], 1)
