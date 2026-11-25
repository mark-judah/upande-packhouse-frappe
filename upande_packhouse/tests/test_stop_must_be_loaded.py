from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as ts


def _bucket(bucket, farm="Simotwo", opl="OPL-1", loaded=0, in_transit=0, truck=None, shelved=0):
	return {
		"opl": opl,
		"bucket": bucket,
		"farm": farm,
		"names": [bucket + "-row"],
		"awaiting": 1,
		"loaded": loaded,
		"in_transit": in_transit,
		"shelved": shelved,
		"truck": truck,
	}


def _trip(rows, vehicle="KTCB 443K"):
	return SimpleNamespace(
		vehicle=vehicle,
		orders=[
			SimpleNamespace(order_pick_list=o, farm=f, buckets=n, loaded_buckets=loaded)
			for o, f, n, loaded in rows
		],
	)


class UnitTestStopMustBeLoaded(UnitTestCase):
	"""A truck leaves a farm only once every bucket on its list there is on it -- counted from the buckets flagged
	on the truck, so a load the trip record lost still counts."""

	def _missing(self, doc, buckets, farm="Simotwo", not_found=()):
		with (
			patch.object(ts, "_transfer_buckets", return_value=buckets),
			patch.object(ts, "_past_delivery_opls", return_value=set()),
			patch.object(ts, "_is_not_found", side_effect=lambda b: b["bucket"] in not_found),
			patch.object(frappe.db, "sql", return_value=[("OPL-1", "AOA - FLA 44826-05529")]),
		):
			return [m["bucket"] for m in ts._stop_missing(doc, farm)]

	def test_a_bucket_still_on_the_trolley_is_missing(self):
		doc = _trip([("OPL-1", "Simotwo", 3, 0)])
		buckets = [
			_bucket("A", loaded=1, truck="KTCB 443K"),
			_bucket("B", loaded=1, truck="KTCB 443K"),
			_bucket("C", loaded=1, truck=None),  # on a trolley, not on the truck
		]
		self.assertEqual(self._missing(doc, buckets), ["C"])

	def test_loads_the_trip_record_lost_still_count(self):
		# the trip says 0 loaded (its write failed); the buckets say all three are on the truck
		doc = _trip([("OPL-1", "Simotwo", 3, 0)])
		buckets = [_bucket(x, loaded=1, truck="KTCB 443K") for x in "ABC"]
		self.assertEqual(self._missing(doc, buckets), [])

	def test_a_bucket_marked_not_found_is_not_waited_for(self):
		doc = _trip([("OPL-1", "Simotwo", 2, 0)])
		buckets = [_bucket("A", loaded=1, truck="KTCB 443K"), _bucket("B")]
		self.assertEqual(self._missing(doc, buckets, not_found={"B"}), [])

	def test_only_what_this_trip_carries_is_asked_for(self):
		# the order has 4 buckets at the farm, this trip carries 2 of them: 2 on the truck is complete
		doc = _trip([("OPL-1", "Simotwo", 2, 0)])
		buckets = [
			_bucket("A", loaded=1, truck="KTCB 443K"),
			_bucket("B", loaded=1, truck="KTCB 443K"),
			_bucket("C"),
			_bucket("D"),
		]
		self.assertEqual(self._missing(doc, buckets), [])

	def test_a_bucket_on_another_truck_is_not_this_trips(self):
		doc = _trip([("OPL-1", "Simotwo", 2, 0)])
		buckets = [_bucket("A", loaded=1, truck="KTCB 443K"), _bucket("B", in_transit=1, truck="KAS 866Q")]
		self.assertEqual(self._missing(doc, buckets), [])

	def test_the_message_names_the_buckets(self):
		msg = ts._missing_message([{"bucket": "c3", "order_name": "AOA - FLA"}], "KTCB 443K", "Kaptumbo")
		self.assertEqual(msg, "1 bucket at Kaptumbo not on KTCB 443K: C3 (AOA - FLA).")


class UnitTestTruckLeavesOnlyLoaded(UnitTestCase):
	"""closeTripStop (the farm app's Leave / Send): refused while a bucket on the list is not on the truck; leaving
	the last farm for the sales farm, every farm it has not left yet must be complete too."""

	def _close(self, farm, missing_at, departed=()):
		doc = SimpleNamespace(
			name="BRT-1",
			vehicle="KAS 866Q",
			status="Draft",
			orders=[
				SimpleNamespace(order_pick_list="OPL-K", farm="Kaptumbo", buckets=2, loaded_buckets=2),
				SimpleNamespace(order_pick_list="OPL-S", farm="Simotwo", buckets=1, loaded_buckets=1),
			],
		)
		frappe.local.response = frappe._dict()
		real = frappe.get_doc
		with (
			patch.object(
				frappe, "form_dict", frappe._dict(name="BRT-1", farm=farm, reason="Truck full", complete=0)
			),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda dt, *a, **k: doc if dt == "Bucket Request Trip" else real(dt, *a, **k),
			),
			patch.object(ts, "_trip_stops", return_value=["Kaptumbo", "Simotwo"]),
			patch.object(ts, "_departed", return_value=list(departed)),
			patch.object(ts, "_trip_has_loads", return_value=True),
			patch.object(ts, "_on_this_truck", return_value={}),
			patch.object(
				ts,
				"_stop_missing",
				side_effect=lambda d, f: (
					[{"opl": "x", "order_name": "ORD", "bucket": "b1"}] if f in missing_at else []
				),
			),
			patch.object(ts, "below_minimum", return_value=None),
		):
			ts.closeTripStop()
		return frappe.local.response.get("message") or {}

	def test_a_reason_no_longer_lets_it_leave_short(self):
		r = self._close("Kaptumbo", {"Kaptumbo"})
		self.assertEqual(
			(r.get("status"), r.get("reason"), r.get("farm")), ("error", "incomplete", "Kaptumbo")
		)

	def test_leaving_the_last_farm_checks_the_farm_before_it(self):
		# the truck came from Kaptumbo (left), and a bucket on Kaptumbo's list is not on it
		r = self._close("Simotwo", {"Kaptumbo"}, departed=["Kaptumbo"])
		self.assertEqual((r.get("reason"), r.get("farm")), ("incomplete", "Kaptumbo"))
		self.assertIn("at Kaptumbo", r.get("message"))
