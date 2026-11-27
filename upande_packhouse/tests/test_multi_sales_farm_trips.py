from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api import auto_transfer as at
from upande_packhouse.api.remote_transfer import transfer_scheduling as tc
from upande_packhouse.upande_packhouse.doctype.bucket_request_trip.bucket_request_trip import (
	BucketRequestTrip,
)

# One trip from a farm carries every sales farm's buckets (MULTI_SALES_FARM_TRIPS): the trip keeps its truck's home
# sales farm, arrives at each sales farm in turn (only that sales farm's buckets arrive there), and counts as
# arrived -- its truck free again -- once it has reached them all. The database is mocked: runs on a bare site.

HUBS = {"OPL-K": "Kapkolia", "OPL-C": "Karen"}


def _orders(*opls):
	return [
		SimpleNamespace(
			order_pick_list=o, order_name=o, buckets=3, loaded_buckets=0, stems=300, farm="Simotwo"
		)
		for o in opls
	]


class UnitTestMultiSalesFarmTrip(UnitTestCase):
	def test_a_trip_keeps_orders_of_two_sales_farms(self):
		doc = MagicMock()
		doc.orders = _orders("OPL-K", "OPL-C")
		doc.get = lambda k, d=None: {"orders": doc.orders}.get(k, d)
		doc.sales_farm = "Kapkolia"
		doc.route = None
		doc.status = "Draft"
		doc.is_new = lambda: False
		doc.flags = frappe._dict()
		with patch.object(tc, "opl_hubs", side_effect=lambda opls: {o: HUBS[o] for o in opls}):
			BucketRequestTrip.validate_sales_farm(doc)
		self.assertEqual(doc.sales_farm, "Kapkolia")
		self.assertEqual(len(doc.orders), 2)

	def test_any_sales_farm_shares_a_trip(self):
		self.assertTrue(tc.hubs_share_trip("Kapkolia", "Karen"))

	def _trip(self, arrived=""):
		rows = [
			frappe._dict(order_pick_list="OPL-K", bucket="K1", farm="Simotwo", shelved=0, off_truck=0),
			frappe._dict(order_pick_list="OPL-C", bucket="C1", farm="Simotwo", shelved=0, off_truck=0),
		]
		doc = MagicMock()
		doc.name, doc.vehicle = "BRT-1", "KAA 1"
		data = {"trip_buckets": rows, "orders": [], "arrived_sales_farms": arrived, "arrived_at": None}
		doc.get = lambda k, d=None: data.get(k, d)
		doc.db_set.side_effect = lambda upd: data.update(upd)
		return doc, data

	def _arrive(self, doc, hub):
		logged = []
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(tc, "opl_hubs", side_effect=lambda opls: {o: HUBS[o] for o in opls}),
			patch.object(tc, "trip_hub", return_value="Kapkolia"),
			patch.object(tc.frappe, "get_meta", return_value=meta),
			patch.object(tc, "log_transfer_event", side_effect=lambda b, *a, **k: logged.append(b)),
		):
			tc._mark_arrived(doc, hub, "test")
		return logged

	def test_first_sales_farm_only_its_buckets_arrive(self):
		doc, data = self._trip()
		self.assertEqual(self._arrive(doc, "Kapkolia"), ["K1"])
		self.assertEqual(data["arrived_sales_farms"], "Kapkolia")
		self.assertFalse(data.get("arrived_at"))

	def test_last_sales_farm_arrives_the_trip(self):
		doc, data = self._trip(arrived="Kapkolia")
		self.assertEqual(self._arrive(doc, "Karen"), ["C1"])
		self.assertTrue(data.get("arrived_at"))

	def test_a_sales_farm_arrives_once(self):
		doc, data = self._trip(arrived="Kapkolia")
		self.assertEqual(self._arrive(doc, "Kapkolia"), [])


class UnitTestRideAlong(UnitTestCase):
	def test_another_sales_farms_buckets_fill_the_trip_at_the_farm_first(self):
		load = {
			"vehicle": "KAA 1",
			"rem": 4,
			"stops": ["Simotwo"],
			"passes": set(),
			"rows": [],
			"fixed": None,
		}
		load["_hub"] = "Kapkolia"
		graph = MagicMock()
		graph.path.return_value = (0, ["Simotwo"])
		order = {
			"opl": "OPL-C",
			"order_name": "OPL-C",
			"open_farms": [{"farm": "Simotwo", "buckets": 6, "per_bucket": 100, "varieties": ""}],
			"open": 6,
		}
		placed = at._ride_along([order], [load], {"Kapkolia": graph})
		self.assertEqual(placed, 4)
		self.assertEqual(load["rem"], 0)
		self.assertEqual(load["rows"][0]["buckets"], 4)
		self.assertEqual(order["open"], 2)

	def test_a_trip_at_another_farm_takes_none(self):
		load = {
			"vehicle": "KAA 1",
			"rem": 4,
			"stops": ["Kaptumbo"],
			"passes": set(),
			"rows": [],
			"fixed": None,
		}
		order = {
			"opl": "OPL-C",
			"order_name": "OPL-C",
			"open_farms": [{"farm": "Simotwo", "buckets": 6, "per_bucket": 100, "varieties": ""}],
			"open": 6,
		}
		self.assertEqual(at._ride_along([order], [load], {"Kapkolia": MagicMock()}), 0)
		self.assertEqual(order["open"], 6)
