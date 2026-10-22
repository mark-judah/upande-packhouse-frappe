from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as ts

# A dispatched trip whose truck has started a later run ends once nothing it carried is
# still on the truck; a bucket still in transit keeps it open (the farm loads the next
# run while the truck is still unloading at the hub). And releasing a Draft (packhouse
# app 1.1.8) requests the truck on the way instead of refusing. The database is mocked:
# these run on a bare site (CI) as well.


def _trip(status="Dispatched", vehicle="KDA 001A", straggler=True):
	doc = MagicMock()
	doc.name = "BRT-OLD"
	doc.status = status
	doc.vehicle = vehicle
	doc.run = 1
	doc.trip_date = frappe.utils.today()
	doc.dispatched_at = frappe.utils.now()
	rows = [
		frappe._dict(order_pick_list="OPL-1", bucket="B1", farm="Ravine", shelved=1, off_truck=0),
		frappe._dict(
			order_pick_list="OPL-1", bucket="B2", farm="Ravine", shelved=0, off_truck=0 if straggler else 1
		),
	]
	doc.get.side_effect = lambda key, default=None: rows if key == "trip_buckets" else default
	return doc


class UnitTestTripEndNewerRun(UnitTestCase):
	def test_trip_ends_when_truck_started_a_later_run(self):
		doc = _trip(straggler=False)
		with (
			patch.object(ts, "_newer_run_started", return_value="BRT-NEW"),
			patch.object(ts, "_receive_trip") as receive,
		):
			self.assertTrue(ts._end_trip_for_newer_run(doc))
		receive.assert_called_once_with("BRT-OLD")
		self.assertIn("BRT-NEW", doc.add_comment.call_args.args[1])

	def test_bucket_still_on_the_truck_keeps_the_trip_open(self):
		# B2 is still in transit: loading the truck's next run does not end this one.
		doc = _trip()
		with (
			patch.object(ts, "_carry_over_unshelved", return_value=False),
			patch.object(ts, "_newer_run_started", return_value="BRT-NEW"),
			patch.object(ts, "_receive_trip") as receive,
		):
			self.assertFalse(ts._end_trip_for_newer_run(doc))
		receive.assert_not_called()
		doc.add_comment.assert_not_called()

	def test_carry_setting_moves_the_stragglers_onto_the_later_trip(self):
		# Production Settings > Carry Unshelved Buckets to the Truck's Next Trip: B2 rides
		# the later trip and this one ends.
		doc = _trip()
		with (
			patch.object(ts, "_carry_over_unshelved", return_value=True),
			patch.object(ts, "_newer_run_started", return_value="BRT-NEW"),
			patch.object(ts, "_receive_trip") as receive,
			patch.object(ts, "_carry_unshelved") as carry,
		):
			self.assertTrue(ts._end_trip_for_newer_run(doc))
		receive.assert_called_once_with("BRT-OLD")
		carry.assert_called_once()
		self.assertEqual([b.bucket for b in carry.call_args.args[2]], ["B2"])

	def test_trip_stays_open_without_a_later_run(self):
		doc = _trip(straggler=False)
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


def _part_shelved_trip():
	"""100 buckets carried for two orders, 68 shelved, 32 still in transit -- while 40 more
	buckets of the same orders were shelved at the hub on other trips or without one."""
	doc = MagicMock()
	doc.name, doc.status, doc.vehicle, doc.run = "BRT-OLD", "Dispatched", "KDA 001A", 1
	doc.trip_date, doc.dispatched_at, doc.arrived_at = frappe.utils.today(), frappe.utils.now(), None
	rows = [
		frappe._dict(
			order_pick_list="OPL-{0}".format(i % 2 + 1),
			bucket="T{0:03d}".format(i),
			farm="Ravine",
			shelved=1 if i < 68 else 0,
			off_truck=0,
		)
		for i in range(100)
	]
	doc.orders = [frappe._dict(order_pick_list=opl, farm="Ravine", buckets=50) for opl in ("OPL-1", "OPL-2")]
	doc.get.side_effect = lambda key, default=None: rows if key == "trip_buckets" else default
	elsewhere = ["E{0:03d}".format(i) for i in range(40)]
	pick = [
		frappe._dict(parent=r.order_pick_list, bucket=r.bucket, shelved=r.shelved, in_transit=1 - r.shelved)
		for r in rows
	]
	pick += [
		frappe._dict(parent="OPL-{0}".format(i % 2 + 1), bucket=b, shelved=1, in_transit=0)
		for i, b in enumerate(elsewhere)
	]

	def sql(query, values=None, *args, **kwargs):
		if "t.status = 'Dispatched' AND t.trip_date" in query:
			return [frappe._dict(name="BRT-OLD")]
		if "GROUP BY parent, UPPER(bucket)" in query:  # _end_trip_if_shelved: its own buckets
			return [p for p in pick if p.bucket in values["buckets"]]
		if "pli.parent IN %(opls)s" in query:  # _transfer_buckets: every bucket of the orders
			return [
				frappe._dict(
					name=p.bucket,
					opl=p.parent,
					bucket=p.bucket,
					farm="Ravine",
					shelved=p.shelved,
					in_transit=p.in_transit,
				)
				for p in pick
			]
		if "`tabBucket Request Trip Order`" in query:  # what trips requested of the orders
			return [
				frappe._dict(opl=o.order_pick_list, farm="Ravine", buckets=50, trip="BRT-OLD")
				for o in doc.orders
			]
		return []

	return doc, sql


class UnitTestPartShelvedTripStaysOpen(UnitTestCase):
	def test_68_of_100_shelved_is_not_ended_by_other_buckets_of_its_orders(self):
		doc, sql = _part_shelved_trip()
		with (
			patch.object(ts, "transfer_hub", return_value="Kapkolia"),
			patch.object(ts.frappe.db, "sql", side_effect=sql),
			patch.object(ts.frappe.db, "sql_list", return_value=["BRT-OLD"]),
			patch.object(ts.frappe.db, "commit"),
			patch.object(ts.frappe, "get_doc", return_value=doc),
			patch.object(ts, "shared_buckets", return_value={}),
			patch.object(ts, "_settle_held_trip", return_value=False),
			patch.object(ts, "_settle_held_trips", return_value=[]),
			patch.object(ts, "_arrive_trips_carrying", return_value=[]),
			patch.object(ts, "_vehicle_on_road", return_value="BRT-OLD"),
			patch.object(ts, "ensure_arrived", return_value=False),
			patch.object(ts, "_dispatch_waiting_trips", return_value=[]),
			patch.object(ts, "_newer_run_started", return_value="BRT-NEW"),
			patch.object(ts, "_receive_trip") as receive,
			patch.object(ts, "carry_over_to_next_run") as carry,
		):
			# The orders' shelved buckets cover what the trip requested ...
			self.assertTrue(ts._requested_buckets_shelved(doc))
			# ... yet neither the shelving scan, nor the sweep (truck loading its next run)
			# ends it: 32 of the buckets it carried are still on the truck.
			self.assertEqual(ts.auto_receive_trucks_for_bucket("E000"), [])
			self.assertEqual(ts.end_shelved_trips(), [])
		receive.assert_not_called()
		carry.assert_not_called()
		self.assertEqual(doc.status, "Dispatched")
		rows = doc.get("trip_buckets")
		self.assertEqual(sum(r.shelved for r in rows), 68)
		self.assertFalse(any(r.off_truck for r in rows))


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
