"""Filter out reharvested: the bucket cycle index and what Stock Visibility does with it.

No test writes to the site: the index and every source query are planted."""

from datetime import date, datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from upande_packhouse.api.v2 import stock_truth as st
from upande_packhouse.api.v2.core import bucket_cycle as bc

T0 = datetime(2026, 11, 1, 8, 0, 0)


def at(hours):
	return T0 + timedelta(hours=hours)


IDX = {
	"AAA111": {"Harvesting": at(10), "Receiving": at(12)},
	"BBB222": {"Harvesting": at(1)},
	"CCC333": {"Receiving": at(5), "Discard": at(30)},
}


class TestIndexLookups(UnitTestCase):
	def test_ids_are_matched_upper_and_trimmed(self):
		self.assertEqual(bc.key(" aaa111 "), "AAA111")
		self.assertTrue(bc.harvested_after(IDX, "aaa111", at(5)))

	def test_harvested_again_needs_a_later_harvest(self):
		self.assertTrue(bc.harvested_after(IDX, "AAA111", at(5)))
		self.assertFalse(bc.harvested_after(IDX, "AAA111", at(10)), "the same entry is not 'again'")
		self.assertFalse(bc.harvested_after(IDX, "AAA111", at(11)))
		self.assertFalse(bc.harvested_after(IDX, "CCC333", at(0)), "a receipt is not a harvest")
		self.assertFalse(bc.harvested_after(IDX, "NOPE", at(0)))
		self.assertFalse(bc.harvested_after(IDX, "AAA111", None))

	def test_later_entry_looks_at_the_types_asked_for(self):
		self.assertTrue(bc.later_entry(IDX, "AAA111", ("Receiving", "Harvesting"), at(11)))
		self.assertFalse(bc.later_entry(IDX, "AAA111", ("Harvesting",), at(11)))
		self.assertTrue(bc.later_entry(IDX, "CCC333", bc.TYPES, at(6)), "the discard counts")
		self.assertFalse(bc.later_entry(IDX, "CCC333", ("Receiving", "Late Receipt", "Harvesting"), at(6)))

	def test_merge_keeps_the_latest_per_bucket_and_type(self):
		data = {}
		bc._merge(data, [("X", "Harvesting", at(3)), ("X", "Harvesting", at(2)), ("X", "Receiving", at(4))])
		self.assertEqual(data, {"X": {"Harvesting": at(3), "Receiving": at(4)}})
		bc._merge(data, [("X", "Harvesting", at(9))])
		self.assertEqual(data["X"]["Harvesting"], at(9))


class TestHarvestDate(UnitTestCase):
	def test_the_date_follows_the_latest_harvest(self):
		data = {}
		bc._merge(data, [("X", "Harvesting", at(3), date(2026, 11, 2))])
		bc._merge(data, [("X", "Harvesting", at(2), date(2026, 10, 30))])  # an older entry never wins
		self.assertEqual(bc.harvest_date(data, "x"), date(2026, 11, 2))
		bc._merge(data, [("X", "Harvesting", at(8), date(2026, 11, 4))])
		self.assertEqual(bc.harvest_date(data, "X"), date(2026, 11, 4))
		self.assertIsNone(bc.harvest_date(data, "NOPE"))

	def test_a_receipt_does_not_set_a_harvest_date(self):
		data = {}
		bc._merge(data, [("X", "Receiving", at(3), date(2026, 11, 2))])
		self.assertIsNone(bc.harvest_date(data, "X"))


class TestPostedSince(UnitTestCase):
	def test_an_onward_move_on_or_after_the_day_counts(self):
		data = {}
		bc._merge(
			data, [("X", "Discard", at(5), date(2026, 11, 3)), ("X", "Packing", at(1), date(2026, 10, 30))]
		)
		self.assertTrue(bc.posted_since(data, "x", ("Discard",), date(2026, 11, 3)), "the same day counts")
		self.assertTrue(bc.posted_since(data, "X", ("Discard",), date(2026, 11, 1)))
		self.assertFalse(bc.posted_since(data, "X", ("Discard",), date(2026, 11, 4)))
		self.assertFalse(bc.posted_since(data, "X", ("Packing",), date(2026, 11, 1)), "that move was earlier")
		self.assertFalse(bc.posted_since(data, "NOPE", ("Discard",), date(2026, 11, 1)))

	def test_every_onward_type_is_in_the_index(self):
		from upande_packhouse.api.v2 import cold_room as cr

		self.assertTrue(set(cr.ONWARD_TYPES) <= set(bc.TYPES))
		self.assertTrue({"Harvesting", *cr.RECEIPT_TYPES} <= set(bc.TYPES))


class TestIndexKeepsFresh(IntegrationTestCase):
	def setUp(self):
		self.store = {}
		self.reads = []

		def read(where, params):
			self.reads.append((where[0], params))
			return [("NEW111", "Harvesting", at(40))]

		self.patches = [
			patch.object(frappe.cache, "get_value", lambda k, *a, **kw: self.store.get(k)),
			patch.object(frappe.cache, "set_value", lambda k, v, *a, **kw: self.store.__setitem__(k, v)),
			patch.object(bc, "_read", read),
		]
		for p in self.patches:
			p.start()

	def tearDown(self):
		for p in self.patches:
			p.stop()

	def test_first_use_builds_from_the_posting_date_index_once(self):
		with patch.object(bc, "now_datetime", lambda: at(40)):
			bc.index()
			bc.index()
		self.assertEqual(len(self.reads), 1)
		self.assertIn("stock_entry_type_docstatus_posting_date_index", self.reads[0][0])

	def test_later_use_only_reads_what_is_new(self):
		with patch.object(bc, "now_datetime", lambda: at(40)):
			bc.index()
		with patch.object(bc, "now_datetime", lambda: at(40) + timedelta(seconds=bc.REFRESH_SECONDS + 1)):
			data = bc.index()
		self.assertEqual(len(self.reads), 2)
		self.assertIn("creation", self.reads[1][0])
		self.assertEqual(
			self.reads[1][1]["since"],
			at(40) - timedelta(minutes=bc.OVERLAP_MINUTES),
			"re-reads a short overlap",
		)
		self.assertIn("NEW111", data)

	def test_inside_the_refresh_window_nothing_is_read(self):
		with patch.object(bc, "now_datetime", lambda: at(40)):
			bc.index()
		with patch.object(bc, "now_datetime", lambda: at(40) + timedelta(seconds=5)):
			bc.index()
		self.assertEqual(len(self.reads), 1)

	def test_an_old_index_is_served_while_a_worker_rebuilds_it(self):
		with patch.object(bc, "now_datetime", lambda: at(40)):
			bc.index()
		later = at(40) + timedelta(seconds=bc.REBUILD_SECONDS + 5)
		with (
			patch.object(bc, "now_datetime", lambda: later),
			patch.object(frappe, "enqueue") as enq,
		):
			data = bc.index()
		self.assertIn("NEW111", data)
		self.assertEqual(enq.call_count, 1)
		self.assertTrue(enq.call_args.kwargs["deduplicate"])


def _alloc(bucket, reused, qty, reason="order_cancelled"):
	return frappe._dict(
		bucket_id=bucket,
		variety="Marisa",
		length="62cm",
		farm="Kapkolia",
		key=(bucket, "Marisa", "62cm"),
		reason=reason,
		issued=0,
		reused=reused,
		qty=qty,
		creation=at(0),
		sales_order="SO-1",
		sales_order_item="SOI-1",
		sales_order_status="",
		customer_name="C",
		customer="C",
		delivery_date=None,
		in_transit=0,
		held=False,
		age_days=3,
	)


def _quarantine(bucket, stems, last_in):
	return frappe._dict(
		bucket_id=bucket,
		variety="Marisa",
		item_name="Marisa",
		item_group="Roses",
		length="62cm",
		stems=stems,
		since=at(0),
		last_in=last_in,
		farm="Kapkolia",
		batch="B-1",
	)


class TestFilterOutReharvested(IntegrationTestCase):
	"""collect() with planted sources: what each setting shows, and what it reports as held back."""

	def _collect(self, **kw):
		idx = {"QQ1": {"Harvesting": at(20)}, "QQ2": {"Harvesting": at(2)}, "RR1": {"Harvesting": at(20)}}
		quarantine = [_quarantine("QQ1", 400.0, at(5)), _quarantine("QQ2", 100.0, at(5))]
		allocs = [_alloc("RR1", True, 300.0), _alloc("RR2", False, 50.0)]
		with (
			patch.object(st.stock, "bucket_rows", lambda **k: []),
			patch.object(st, "_discards", lambda: {}),
			patch.object(st, "_allocations", lambda *a: allocs),
			patch.object(st, "_coldroom_rows", lambda a: []),
			patch.object(st, "_harvest_rows", lambda a: []),
			patch.object(st, "_transit_rows", lambda: []),
			patch.object(st, "_quarantine_rows", lambda: quarantine),
			patch.object(st.bucket_cycle, "index", lambda: idx),
		):
			return st.collect(st._args(dict(**kw)))

	def _by_state(self, c):
		out = {}
		for it in c["items"]:
			out.setdefault(it["state"], 0.0)
			out[it["state"]] += it["stems"]
		return out

	def test_on_by_default(self):
		self.assertTrue(st._args({}).hide_reharvested)
		for off in (0, "0", "false", "off", "No"):
			self.assertFalse(st._args({"hide_reharvested": off}).hide_reharvested, off)
		for on in (1, "1", "true", ""):
			self.assertTrue(st._args({"hide_reharvested": on}).hide_reharvested, on)

	def test_the_request_value_is_read_when_the_endpoint_passes_none(self):
		with patch.object(frappe.local, "form_dict", frappe._dict(hide_reharvested="0")):
			self.assertFalse(st._args({}).hide_reharvested)

	def test_on_drops_buckets_that_were_harvested_again(self):
		c = self._collect()
		got = self._by_state(c)
		self.assertEqual(got["quarantine"], 100.0, "QQ1 was refilled after its last quarantine; QQ2 was not")
		self.assertEqual(got.get("reclaimable", 0.0), 50.0, "only the allocation nobody refilled")
		self.assertEqual(c["hidden"]["quarantine"], {"stems": 400.0, "buckets": 1})
		self.assertEqual(c["hidden"]["reclaimable"], {"stems": 300.0, "buckets": 1})

	def test_off_shows_the_books_unchanged(self):
		c = self._collect(hide_reharvested=0)
		got = self._by_state(c)
		self.assertEqual(got["quarantine"], 500.0)
		self.assertEqual(got["reclaimable"], 350.0)
		self.assertEqual(c["hidden"]["quarantine"]["stems"], 0.0)
		self.assertEqual(c["hidden"]["reclaimable"]["stems"], 0.0)

	def test_the_allocation_list_for_releasing_is_never_trimmed(self):
		self.assertEqual(len(self._collect()["allocs"]), 2, "releasing works on the full ledger")


class TestRowsUseTheIndex(IntegrationTestCase):
	"""The coldroom / harvested rows keep their 'latest cycle only' rule through the index, not a subquery."""

	def test_the_slow_subquery_is_gone_from_the_source_queries(self):
		import inspect

		for fn in (st._coldroom_rows, st._harvest_rows, st._allocations, st._transfer_rows):
			src = inspect.getsource(fn)
			self.assertNotIn("n.creation > se.creation", src, fn.__name__)
			self.assertNotIn("se.creation > ba.creation", src, fn.__name__)
			self.assertNotIn("se.creation > pli.creation", src, fn.__name__)


class TestColdRoomFloor(IntegrationTestCase):
	"""The Cold Room's floor: received, not shelved, not harvested again; harvest date and days on the floor."""

	def _floor(self, rows, idx):
		from upande_packhouse.api.v2 import cold_room as cr

		real = frappe.db.sql

		def sql(query, *a, **k):  # only the floor query is planted; Frappe's own lookups still run
			return rows if "tabStock Entry" in str(query) else real(query, *a, **k)

		with (
			patch.object(frappe.db, "sql", sql),
			patch.object(cr.bucket_cycle, "index", lambda: idx),
		):
			return cr._not_shelved(frappe._dict(farms=None, from_date=None, to_date=None))

	def _row(self, bucket, received_at, received_on):
		return frappe._dict(
			bucket_id=bucket,
			farm="Kapkolia",
			variety="Marisa",
			stem_length="62cm",
			received_on=received_on,
			received_at=received_at,
			stock_entry="SE-" + bucket,
			stems=100,
		)

	def test_a_bucket_harvested_again_after_its_receipt_is_not_on_the_floor(self):
		rows = [
			self._row("AAA111", at(5), date(2026, 11, 1)),
			self._row("BBB222", at(5), date(2026, 11, 1)),
			self._row("CCC333", at(5), date(2026, 11, 1)),
		]
		idx = {
			"AAA111": {"Harvesting": at(1), "Receiving": at(5), "harvest_date": date(2026, 11, 1)},
			"BBB222": {"Harvesting": at(9), "Receiving": at(5), "harvest_date": date(2026, 11, 2)},
			"CCC333": {"Harvesting": at(1), "Receiving": at(5), "Late Receipt": at(9)},
		}
		out = self._floor(rows, idx)
		self.assertEqual(
			[r.bucket_id for r in out], ["AAA111"], "BBB222 was refilled, CCC333 was received again"
		)
		self.assertEqual(out[0].harvest_date, date(2026, 11, 1))
		self.assertEqual(out[0].stems, 100.0)
		self.assertEqual(out[0].days_on_floor, out[0].age_days)

	def test_a_bucket_discarded_or_issued_on_the_receipt_day_is_not_on_the_floor(self):
		rows = [self._row("AAA111", at(5), date(2026, 11, 1)), self._row("BBB222", at(5), date(2026, 11, 1))]
		idx = {
			"AAA111": {"Harvesting": at(1), "posted:Discard": date(2026, 11, 1)},
			"BBB222": {
				"Harvesting": at(1),
				"posted:Packing": date(2026, 10, 30),
			},  # earlier cycle: not this one
		}
		self.assertEqual([r.bucket_id for r in self._floor(rows, idx)], ["BBB222"])

	def test_a_harvest_date_after_the_receipt_is_not_shown(self):
		rows = [self._row("AAA111", at(5), date(2026, 11, 1))]
		idx = {"AAA111": {"Harvesting": at(1), "harvest_date": date(2026, 11, 3)}}
		self.assertIsNone(self._floor(rows, idx)[0].harvest_date)

	def test_the_window_is_the_one_stock_visibility_uses(self):
		from upande_packhouse.api.v2 import cold_room as cr

		self.assertEqual(st.COLDROOM_DAYS, cr.FLOW_DAYS)
