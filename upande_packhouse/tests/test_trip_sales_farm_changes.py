from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as tc
from upande_packhouse.upande_packhouse.doctype.bucket_request_trip.bucket_request_trip import (
	BucketRequestTrip,
)

# One sales farm per trip. An order already on a trip whose planned sales farm changes afterwards (Allocation
# Planning moved it to a team of another sales farm) never blocks the trip: what is loaded rides on, the rest comes
# off. An order of another sales farm newly added is still refused. The database is mocked: runs on a bare site.


def _row(opl, buckets, loaded=0, stems=None):
	return SimpleNamespace(
		order_pick_list=opl,
		order_name=opl,
		buckets=buckets,
		loaded_buckets=loaded,
		stems=stems if stems is not None else buckets * 100,
		full_farm_buckets=buckets,
		is_partial=0,
	)


def _trip(rows, before_rows, status="Draft", sales_farm="Kapkolia"):
	doc = MagicMock()
	doc.orders = list(rows)
	doc.get = lambda k, d=None: {"orders": doc.orders}.get(k, d)

	def set_(k, v):
		if k == "orders":
			doc.orders = list(v)

	doc.set = set_
	doc.status = status
	doc.sales_farm = sales_farm
	doc.route = None
	doc.name = "BRT-1"
	doc.is_new = lambda: False
	doc.flags = frappe._dict()
	doc.get_doc_before_save = lambda: SimpleNamespace(orders=before_rows, sales_farm=sales_farm)
	doc.move_off_other_sales_farms = lambda by: BucketRequestTrip.move_off_other_sales_farms(doc, by)
	return doc


HUBS = {"OPL-K": "Kapkolia", "OPL-MOVED": "Chepsito", "OPL-NEW": "Chepsito"}


def _validate(doc):
	with patch.object(tc, "opl_hubs", side_effect=lambda opls: {o: HUBS[o] for o in opls}):
		BucketRequestTrip.validate_sales_farm(doc)


class UnitTestTripSalesFarmChanges(UnitTestCase):
	def test_loaded_buckets_ride_on_and_the_rest_come_off(self):
		rows = [_row("OPL-K", 5), _row("OPL-MOVED", 6, loaded=4)]
		doc = _trip(rows, before_rows=[_row("OPL-K", 5), _row("OPL-MOVED", 6, loaded=4)])
		_validate(doc)
		moved = next(o for o in doc.orders if o.order_pick_list == "OPL-MOVED")
		self.assertEqual(moved.buckets, 4)
		self.assertEqual(moved.loaded_buckets, 4)
		self.assertEqual(doc.sales_farm, "Kapkolia")

	def test_unloaded_order_of_the_new_sales_farm_comes_off_whole(self):
		rows = [_row("OPL-K", 5), _row("OPL-MOVED", 3)]
		doc = _trip(rows, before_rows=list(rows))
		_validate(doc)
		self.assertEqual([o.order_pick_list for o in doc.orders], ["OPL-K"])
		self.assertEqual(doc.total_buckets, 5)

	def test_a_trip_that_left_keeps_everything(self):
		rows = [_row("OPL-K", 5, loaded=5), _row("OPL-MOVED", 3, loaded=2)]
		doc = _trip(rows, before_rows=list(rows), status="Dispatched")
		_validate(doc)
		self.assertEqual(len(doc.orders), 2)

	def test_adding_an_order_of_another_sales_farm_is_refused(self):
		rows = [_row("OPL-K", 5), _row("OPL-NEW", 2)]
		doc = _trip(rows, before_rows=[_row("OPL-K", 5)])
		with self.assertRaises(frappe.ValidationError):
			_validate(doc)
