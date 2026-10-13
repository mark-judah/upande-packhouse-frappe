from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as ts

# A dispatched trip with a bucket that never got shelved must not keep its truck busy
# for good: it ends once the truck has started a later run, the straggler logged. And
# releasing a Draft (packhouse app 1.1.8) requests the truck on the way instead of
# refusing. The database is mocked: these run on a bare site (CI) as well.


def _trip(status="Dispatched", vehicle="KDA 001A"):
	doc = MagicMock()
	doc.name = "BRT-OLD"
	doc.status = status
	doc.vehicle = vehicle
	doc.run = 1
	doc.trip_date = frappe.utils.today()
	doc.dispatched_at = frappe.utils.now()
	rows = [
		frappe._dict(order_pick_list="OPL-1", bucket="B1", farm="Ravine", shelved=1, off_truck=0),
		frappe._dict(order_pick_list="OPL-1", bucket="B2", farm="Ravine", shelved=0, off_truck=0),
	]
	doc.get.side_effect = lambda key, default=None: rows if key == "trip_buckets" else default
	return doc


class UnitTestTripEndNewerRun(UnitTestCase):
	def test_trip_ends_when_truck_started_a_later_run(self):
		doc = _trip()
		with (
			patch.object(ts, "_newer_run_started", return_value="BRT-NEW"),
			patch.object(ts, "_receive_trip") as receive,
			patch.object(ts, "log_transfer_event") as log,
		):
			self.assertTrue(ts._end_trip_for_newer_run(doc))
		receive.assert_called_once_with("BRT-OLD")
		# The straggler stays traceable: named in the comment and in its transfer log.
		self.assertIn("B2", doc.add_comment.call_args.args[1])
		self.assertNotIn("B1", doc.add_comment.call_args.args[1])
		self.assertEqual([c.args[0] for c in log.call_args_list], ["B2"])

	def test_trip_stays_open_without_a_later_run(self):
		doc = _trip()
		with (
			patch.object(ts, "_newer_run_started", return_value=None),
			patch.object(ts, "_receive_trip") as receive,
		):
			self.assertFalse(ts._end_trip_for_newer_run(doc))
		receive.assert_not_called()

	def test_only_a_dispatched_trip_is_ended(self):
		doc = _trip(status="Scheduled")
		with patch.object(ts, "_newer_run_started") as newer:
			self.assertFalse(ts._end_trip_for_newer_run(doc))
		newer.assert_not_called()

	def test_newer_run_looks_at_later_runs_of_the_same_truck(self):
		doc = _trip()
		with patch.object(ts.frappe.db, "sql", return_value=[("BRT-NEW",)]) as sql:
			self.assertEqual(ts._newer_run_started(doc), "BRT-NEW")
		query, params = sql.call_args.args
		self.assertIn("IFNULL(t.run, 0) > %(run)s", query)
		self.assertEqual((params["v"], params["n"], params["run"]), ("KDA 001A", "BRT-OLD", 1))

	def test_sweep_ends_a_trip_whose_truck_went_out_again(self):
		doc = _trip()
		with (
			patch.object(ts.frappe.db, "sql", return_value=[frappe._dict(name="BRT-OLD")]),
			patch.object(ts.frappe.db, "commit"),
			patch.object(ts.frappe, "get_doc", return_value=doc),
			patch.object(ts, "ensure_arrived", return_value=False),
			patch.object(ts, "_end_trip_if_shelved", return_value=False),
			patch.object(ts, "_end_trip_for_newer_run", return_value=True),
			patch.object(ts, "carry_over_to_next_run") as carry,
		):
			self.assertEqual(ts.end_shelved_trips(), ["BRT-OLD"])
		carry.assert_called_once_with("BRT-OLD")


class UnitTestReleaseDraftTrip(UnitTestCase):
	def _release(self, doc, on_road=None):
		with (
			patch.object(ts, "_trip_capacity", return_value=20),
			patch.object(ts, "_vehicle_on_road", return_value=on_road),
			patch.object(ts.frappe.db, "get_value", return_value=None),
		):
			return ts.release_trip(doc)

	def _draft(self, **kw):
		doc = frappe._dict(
			name="BRT-1",
			status="Draft",
			vehicle="KDA 001A",
			trip_date=frappe.utils.today(),
			total_buckets=5,
			orders=[],
			rejected_by="someone@example.com",
			reject_reason="No driver",
		)
		doc.update(kw)
		doc.save = MagicMock()
		doc.add_comment = MagicMock()
		return doc

	def test_release_draft_requests_on_the_way(self):
		doc = self._draft()
		self.assertIsNone(self._release(doc))
		self.assertEqual(doc.status, "Scheduled")
		self.assertEqual(doc.requested_by, frappe.session.user)
		self.assertEqual(doc.requested_at, doc.released_at)
		self.assertFalse(doc.reject_reason)
		doc.save.assert_called_once()

	def test_release_draft_of_an_earlier_day_is_refused(self):
		doc = self._draft(trip_date=frappe.utils.add_days(frappe.utils.today(), -1))
		self.assertIn("earlier day", self._release(doc))
		self.assertEqual(doc.status, "Draft")

	def test_release_without_truck_is_refused(self):
		doc = self._draft(vehicle=None)
		self.assertEqual(self._release(doc), "Trip has no truck.")

	def test_release_while_truck_out_is_refused(self):
		doc = self._draft()
		self.assertIn("still out", self._release(doc, on_road="BRT-0"))
		self.assertEqual(doc.status, "Draft")
