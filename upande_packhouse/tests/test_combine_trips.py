from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as tc
from upande_packhouse.upande_packhouse.doctype.bucket_request_trip.bucket_request_trip import (
	BucketRequestTrip,
)

# Combining trips (Bucket Logistics): one truck collects once at a farm for several sales farms, each sales
# farm's buckets dropped at that sales farm. The database is mocked.


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
	doc.combined_sales_farms = 0
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
	def test_two_sales_farms_at_one_farm_become_one_trip(self):
		kap = _trip("T-KAP", 2, "Kapkolia", 40)
		chep = _trip("T-CHEP", 3, "Chepsito", 8)
		out, fold = _combine([chep, kap])
		self.assertEqual((out["status"], out["trip"], out["combined"]), ("success", "T-KAP", ["T-CHEP"]))
		self.assertEqual(out["sales_farms"], ["Chepsito", "Kapkolia"])
		self.assertEqual(kap.combined_sales_farms, 1)
		keep, rest = fold.call_args[0][0], fold.call_args[0][1]
		self.assertEqual((keep.name, [d.name for d in rest]), ("T-KAP", ["T-CHEP"]))

	def test_trips_of_other_farms_or_trucks_are_refused(self):
		out, fold = _combine([_trip("A", 1, "Kapkolia", 5), _trip("B", 2, "Chepsito", 5, farm="Kaptumbo")])
		self.assertEqual(out["status"], "error")
		out, fold = _combine([_trip("A", 1, "Kapkolia", 5), _trip("B", 2, "Chepsito", 5, vehicle="KDP 283L")])
		self.assertEqual(out["status"], "error")
		fold.assert_not_called()

	def test_a_trip_that_left_the_farm_is_refused(self):
		out, fold = _combine([_trip("A", 1, "Kapkolia", 5), _trip("B", 2, "Chepsito", 5, departed="Torongo")])
		self.assertIn("left the farm", out["message"])
		fold.assert_not_called()

	def test_more_than_one_load_is_refused(self):
		out, fold = _combine([_trip("A", 1, "Kapkolia", 80), _trip("B", 2, "Chepsito", 40)], cap=108)
		self.assertIn("don't fit", out["message"])
		fold.assert_not_called()


class UnitTestCombinedTripValidates(UnitTestCase):
	def _validate(self, combined):
		doc = MagicMock()
		doc.orders = [
			SimpleNamespace(order_pick_list="OPL-K", buckets=40),
			SimpleNamespace(order_pick_list="OPL-C", buckets=8),
		]
		doc.get = lambda k, d=None: {"orders": doc.orders, "combined_sales_farms": combined}.get(k, d)
		doc.sales_farm = "Kapkolia"
		doc.is_new = lambda: False
		doc.flags = frappe._dict()
		doc.move_off_other_sales_farms = lambda by: None
		with patch.object(tc, "opl_hubs", return_value={"OPL-K": "Kapkolia", "OPL-C": "Chepsito"}):
			BucketRequestTrip.validate_sales_farm(doc)
		return doc

	def test_a_combined_trip_carries_two_sales_farms(self):
		self.assertEqual(self._validate(1).sales_farm, "Kapkolia")

	def test_any_other_trip_still_refuses_two_sales_farms(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(0)
