from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as tc
from upande_packhouse.upande_packhouse.doctype.bucket_request_trip.bucket_request_trip import (
	BucketRequestTrip,
)

# Combining trips (Bucket Logistics): one truck collects once at a farm; a trip drives to one sales farm, so
# only trips to the same sales farm combine. The database is mocked.


def _trip(name, run, hub, buckets, vehicle="KDG 076V", farm="Torongo", status="Draft", departed=None):
	doc = MagicMock()
	doc.name = name
	doc.run = run
	doc.vehicle = vehicle
	doc.trip_date = "2026-11-22"
	doc.status = status
	doc.creation = "2026-11-22 12:00:0%d" % run
	doc.sales_farm = hub
	doc.orders = [SimpleNamespace(buckets=buckets, order_pick_list="OPL-" + name, farm=farm)]
	doc.departed_stops = departed
	doc.get = lambda k, d=None, _doc=doc: getattr(_doc, k, d)
	doc._farm = farm
	return doc


def _combine(docs, cap=108):
	by = {d.name: d for d in docs}
	with (
		patch.object(tc, "_plan_lock"),
		patch.object(tc.frappe, "get_doc", side_effect=lambda dt, n: by[n]),
		patch.object(tc, "trip_farms", side_effect=lambda d: {d._farm}),
		patch.object(tc, "trip_hub", side_effect=lambda d: d.sales_farm),
		patch.object(
			tc, "_departed", side_effect=lambda d: [f for f in (d.departed_stops or "").split(",") if f]
		),
		patch.object(tc, "_vehicle_capacity", return_value=cap),
		patch.object(tc, "_fold_trips") as fold,
		patch.object(tc.frappe.db, "commit"),
	):
		out = tc.combineTrips([d.name for d in docs])
	return out, fold


class UnitTestCombineTrips(UnitTestCase):
	def test_trips_to_one_sales_farm_at_one_farm_become_one_trip(self):
		one = _trip("T-1", 2, "Kapkolia", 40)
		two = _trip("T-2", 3, "Kapkolia", 8)
		out, fold = _combine([two, one])
		self.assertEqual((out["status"], out["trip"], out["combined"]), ("success", "T-1", ["T-2"]))
		keep, rest = fold.call_args[0][0], fold.call_args[0][1]
		self.assertEqual((keep.name, [d.name for d in rest]), ("T-1", ["T-2"]))

	@patch.object(tc, "MULTI_SALES_FARM_TRIPS", False)
	def test_trips_to_two_sales_farms_stay_apart(self):
		out, fold = _combine([_trip("A", 1, "Kapkolia", 40), _trip("B", 2, "Chepsito", 8)])
		self.assertEqual(out["status"], "error")
		self.assertIn("one sales farm", out["message"])
		fold.assert_not_called()

	def test_trips_of_other_farms_or_trucks_are_refused(self):
		out, fold = _combine([_trip("A", 1, "Kapkolia", 5), _trip("B", 2, "Kapkolia", 5, farm="Kaptumbo")])
		self.assertEqual(out["status"], "error")
		out, fold = _combine([_trip("A", 1, "Kapkolia", 5), _trip("B", 2, "Kapkolia", 5, vehicle="KDP 283L")])
		self.assertEqual(out["status"], "error")
		fold.assert_not_called()

	def test_a_trip_that_left_the_farm_is_refused(self):
		out, fold = _combine([_trip("A", 1, "Kapkolia", 5), _trip("B", 2, "Kapkolia", 5, departed="Torongo")])
		self.assertIn("left the farm", out["message"])
		fold.assert_not_called()

	def test_more_than_one_load_is_refused(self):
		out, fold = _combine([_trip("A", 1, "Kapkolia", 80), _trip("B", 2, "Kapkolia", 40)], cap=108)
		self.assertIn("don't fit", out["message"])
		fold.assert_not_called()


class UnitTestTripValidates(UnitTestCase):
	@patch.object(tc, "MULTI_SALES_FARM_TRIPS", False)
	def test_a_trip_refuses_two_sales_farms(self):
		doc = MagicMock()
		doc.orders = [
			SimpleNamespace(order_pick_list="OPL-K", buckets=40),
			SimpleNamespace(order_pick_list="OPL-C", buckets=8),
		]
		doc.get = lambda k, d=None: {"orders": doc.orders}.get(k, d)
		doc.sales_farm = "Kapkolia"
		doc.is_new = lambda: False
		doc.flags = frappe._dict()
		doc.move_off_other_sales_farms = lambda by: None
		with (
			patch.object(tc, "opl_hubs", return_value={"OPL-K": "Kapkolia", "OPL-C": "Chepsito"}),
			self.assertRaises(frappe.ValidationError),
		):
			BucketRequestTrip.validate_sales_farm(doc)
