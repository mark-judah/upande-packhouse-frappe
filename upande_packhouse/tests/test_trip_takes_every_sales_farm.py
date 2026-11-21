from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api import auto_transfer as at
from upande_packhouse.api.remote_transfer import transfer_scheduling as tc
from upande_packhouse.upande_packhouse.doctype.bucket_request_trip.bucket_request_trip import (
	BucketRequestTrip,
)

# One trip per farm on a truck, whichever sales farm each order is packed at: the truck drops each order's
# buckets at its own sales farm. The database is mocked: these run on a bare site (CI) as well.

HUBS = {"OPL-K1": "Kapkolia", "OPL-K2": "Kapkolia", "OPL-C1": "Chepsito"}


def _row(opl, buckets):
	return SimpleNamespace(order_pick_list=opl, buckets=buckets)


def _trip(rows, sales_farm=None):
	doc = MagicMock()
	doc.get = lambda k, d=None: {"orders": rows}.get(k, d)
	doc.sales_farm = sales_farm
	doc.route = None
	doc.name = "BRT-1"
	return doc


class UnitTestTripTakesEverySalesFarm(UnitTestCase):
	def _hub(self, doc):
		with patch.object(tc, "opl_hubs", side_effect=lambda opls: {o: HUBS[o] for o in opls}):
			BucketRequestTrip.validate_sales_farm(doc)
		return doc.sales_farm

	def test_orders_of_two_sales_farms_share_one_trip(self):
		doc = _trip([_row("OPL-K1", 30), _row("OPL-C1", 12), _row("OPL-K2", 25)])
		self.assertEqual(self._hub(doc), "Kapkolia")

	def test_the_trip_heads_where_most_of_its_buckets_go(self):
		doc = _trip([_row("OPL-K1", 4), _row("OPL-C1", 20)])
		self.assertEqual(self._hub(doc), "Chepsito")

	def test_a_sales_farm_already_set_is_kept_while_it_has_buckets(self):
		doc = _trip([_row("OPL-K1", 4), _row("OPL-C1", 20)], sales_farm="Kapkolia")
		self.assertEqual(self._hub(doc), "Kapkolia")

	def test_another_sales_farms_order_goes_on_the_farms_trip(self):
		doc = MagicMock(trip_date=frappe.utils.today(), orders=[])
		row = {
			"order_pick_list": "OPL-C1",
			"farm": "Torongo",
			"buckets": 6,
			"stems": 600,
			"sales_farm": "Chepsito",
		}
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(tc, "trip_room", return_value=50),
			patch.object(tc, "trip_farms", return_value={"Torongo"}),
			patch.object(tc, "trip_hub", return_value="Kapkolia"),
		):
			added, left = tc._add_rows_to_trip("BRT-1", [row])
		self.assertEqual([a["buckets"] for a in added], [6])
		self.assertEqual(left, [])
		doc.save.assert_called_once()

	def test_another_farms_rows_still_get_their_own_trip(self):
		doc = MagicMock(trip_date=frappe.utils.today(), orders=[])
		row = {
			"order_pick_list": "OPL-C1",
			"farm": "Simotwo",
			"buckets": 6,
			"stems": 600,
			"sales_farm": "Chepsito",
		}
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(tc, "trip_room", return_value=50),
			patch.object(tc, "trip_farms", return_value={"Torongo"}),
			patch.object(tc, "trip_hub", return_value="Kapkolia"),
		):
			added, left = tc._add_rows_to_trip("BRT-1", [row])
		self.assertEqual(added, [])
		self.assertEqual(left[0][1], 6)

	def test_the_planner_combines_a_trucks_trips_at_one_farm(self):
		with (
			patch.object(tc, "duplicate_trips", return_value={"KDG 076V": ["BRT-1", "BRT-2"]}),
			patch.object(tc, "merge_truck_trips", return_value={"status": "success"}) as merge,
		):
			at._combine_farm_trips("2026-11-21")
		merge.assert_called_once_with("KDG 076V", "2026-11-21")
