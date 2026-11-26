from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as ts


def _trip(name="BRT-1", buckets=()):
	return SimpleNamespace(
		name=name,
		get=lambda k: (
			[SimpleNamespace(order_pick_list=o, bucket=b) for o, b in buckets]
			if k == "trip_buckets"
			else None
		),
	)


class UnitTestRecordLoadsRetry(UnitTestCase):
	"""A trip saved by someone else between reading it and recording a load used to lose the load
	(TimestampMismatchError, logged and swallowed): the trip stayed Draft while its buckets rode the truck."""

	def test_a_collision_reads_the_trip_again_and_records_the_loads(self):
		calls = []

		def record(doc, truck, entries):
			calls.append((doc, [e["bucket"] for e in entries]))
			if len(calls) == 1:
				raise frappe.TimestampMismatchError("modified after you opened it")

		fresh = _trip()
		entries = [{"opl": "OPL-1", "bucket": "77C9A7"}, {"opl": "OPL-1", "bucket": "445H9Y"}]
		with patch.object(ts, "_record_loads", record), patch.object(frappe, "get_doc", return_value=fresh):
			out = ts._record_loads_retrying(_trip(), "KTCB 443K", entries)
		self.assertIs(out, fresh)
		self.assertEqual(calls[1], (fresh, ["77C9A7", "445H9Y"]))

	def test_what_the_other_writer_already_recorded_is_not_counted_twice(self):
		calls = []

		def record(doc, truck, entries):
			calls.append([e["bucket"] for e in entries])
			if len(calls) == 1:
				raise frappe.TimestampMismatchError("x")

		fresh = _trip(buckets=[("OPL-1", "77c9a7")])
		entries = [{"opl": "OPL-1", "bucket": "77C9A7"}, {"opl": "OPL-1", "bucket": "445H9Y"}]
		with patch.object(ts, "_record_loads", record), patch.object(frappe, "get_doc", return_value=fresh):
			ts._record_loads_retrying(_trip(), "KTCB 443K", entries)
		self.assertEqual(calls[1], ["445H9Y"])

	def test_it_gives_up_after_the_last_attempt(self):
		def record(doc, truck, entries):
			raise frappe.TimestampMismatchError("x")

		with (
			patch.object(ts, "_record_loads", record),
			patch.object(frappe, "get_doc", return_value=_trip()),
			self.assertRaises(frappe.TimestampMismatchError),
		):
			ts._record_loads_retrying(_trip(), "KTCB 443K", [{"opl": "OPL-1", "bucket": "A"}])

	def test_a_new_trip_is_not_read_again(self):
		def record(doc, truck, entries):
			raise frappe.TimestampMismatchError("x")

		with patch.object(ts, "_record_loads", record), self.assertRaises(frappe.TimestampMismatchError):
			ts._record_loads_retrying(_trip(name=None), "KTCB 443K", [{"opl": "OPL-1", "bucket": "A"}])
