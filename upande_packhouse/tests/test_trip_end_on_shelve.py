from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as ts

# Shelving a bucket at the hub must not end (Received) a trip while other buckets it
# carried are still on the truck, and must never dispatch a trip that hasn't left.
# The database is mocked: these run on a bare site (CI) as well.


def _trip(status="Dispatched", buckets=("B1", "B2")):
	doc = MagicMock()
	doc.name = "BRT-TEST"
	doc.status = status
	doc.trip_date = frappe.utils.today()
	doc.received_at = None
	rows = [frappe._dict(order_pick_list="OPL-1", bucket=b, shelved=0, off_truck=0) for b in buckets]
	doc.get.side_effect = lambda key, default=None: rows if key == "trip_buckets" else default
	return doc, rows


def _pick_rows(shelved=(), in_transit=()):
	"""What the Pick List Item query returns for the trip's buckets."""
	return [
		frappe._dict(parent="OPL-1", bucket=b, shelved=1 if b in shelved else 0, in_transit=1)
		for b in set(shelved) | set(in_transit)
	]


class UnitTestTripEndOnShelve(UnitTestCase):
	def _end(self, doc, shelved=(), in_transit=(), on_hub_shelf=()):
		with (
			patch.object(ts, "transfer_hub", return_value="Kapkolia"),
			patch.object(ts.frappe.db, "sql", return_value=_pick_rows(shelved, in_transit)),
			patch.object(ts.frappe.db, "sql_list", return_value=list(on_hub_shelf)),
		):
			return ts._end_trip_if_shelved(doc)

	def test_first_bucket_shelved_does_not_end_the_trip(self):
		doc, rows = _trip()
		self.assertFalse(self._end(doc, shelved=["B1"], in_transit=["B2"]))
		self.assertEqual(doc.status, "Dispatched")
		self.assertEqual([r.shelved for r in rows], [1, 0])

	def test_every_bucket_shelved_ends_the_trip(self):
		doc, _rows = _trip()
		self.assertTrue(self._end(doc, shelved=["B1", "B2"]))
		self.assertEqual(doc.status, "Received")

	def test_bucket_that_left_the_truck_is_not_waited_on(self):
		# B2 is neither shelved nor in transit any more (unloaded / swapped).
		doc, rows = _trip()
		self.assertTrue(self._end(doc, shelved=["B1"]))
		self.assertEqual(rows[1].off_truck, 1)
		self.assertEqual(doc.status, "Received")

	def test_bucket_on_a_hub_shelf_counts_as_shelved(self):
		doc, _rows = _trip()
		self.assertTrue(self._end(doc, shelved=["B1"], in_transit=["B2"], on_hub_shelf=["B2"]))

	def test_shelving_never_dispatches_a_trip_still_loading(self):
		# The arrival query only picks dispatched trips: a Draft/Requested/Scheduled
		# trip holding the bucket is left untouched.
		with (
			patch.object(ts.frappe.db, "sql", return_value=[]) as sql,
			patch.object(ts.frappe, "get_doc") as get_doc,
		):
			ts.auto_arrive_for_bucket("B1")
		self.assertIn("t.status = 'Dispatched'", sql.call_args.args[0])
		get_doc.assert_not_called()

	def test_requested_buckets_shelved_never_ends_a_trip_that_did_not_leave(self):
		# Everything a Draft trip asked for is shelved already: it is not ended here
		# (only by every carried bucket shelved, or End Trip on Bucket Logistics).
		doc, _rows = _trip(status="Draft")
		with (
			patch.object(ts.frappe.db, "sql_list", return_value=["BRT-TEST"]),
			patch.object(ts.frappe, "get_doc", return_value=doc),
			patch.object(ts, "_requested_buckets_shelved", return_value=True),
			patch.object(ts, "_receive_trip") as receive,
		):
			self.assertEqual(ts._end_trips_with_requests_shelved("B1"), [])
		receive.assert_not_called()

	def test_sweep_only_ends_dispatched_trips_with_every_bucket_shelved(self):
		doc, _rows = _trip()
		with (
			patch.object(ts.frappe.db, "sql", return_value=[frappe._dict(name="BRT-TEST")]) as sql,
			patch.object(ts.frappe.db, "commit"),
			patch.object(ts.frappe, "get_doc", return_value=doc),
			patch.object(ts, "ensure_arrived", return_value=False),
			patch.object(ts, "_end_trip_if_shelved", return_value=False),
			patch.object(ts, "_receive_trip") as receive,
		):
			self.assertEqual(ts.end_shelved_trips(), [])
		self.assertIn("t.status = 'Dispatched'", sql.call_args.args[0])
		receive.assert_not_called()
