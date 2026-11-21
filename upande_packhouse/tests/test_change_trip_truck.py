from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as tc

# Change truck moves every bucket of the trip to the new truck: onto its open trip to the same farm for the same
# sales farm that day, else onto a new run on its route for that day (as Add to truck does). The database is mocked.


def _order(opl, buckets):
	o = SimpleNamespace(order_pick_list=opl, farm="Kipsolu", buckets=buckets, stems=buckets * 100)
	o.as_dict = lambda: dict(vars(o), name="row-" + opl, parent="BRT-OLD", idx=1)
	return o


def _trip():
	doc = MagicMock()
	doc.name = "BRT-OLD"
	doc.status = "Draft"
	doc.vehicle = "KDP 283L"
	doc.trip_date = "2026-11-22"
	doc.total_buckets = 8
	doc.route, doc.run = "R-OLD", 3
	doc.orders = [_order("OPL-1", 5), _order("OPL-2", 3)]
	doc.get = lambda k, d=None: [] if k == "trip_buckets" else d
	doc.loaded_buckets = 0
	return doc


def _change(doc, joined=None, run=None, open_trips=()):
	frappe.local.form_dict = frappe._dict(name=doc.name, vehicle="KDG 076V", planning=1)
	frappe.local.response = frappe._dict()
	vehicle = frappe._dict(
		name="KDG 076V", custom_trolley_capacity=10, custom_buckets_per_trolley=5, custom_dispatch_truck=0
	)
	with (
		patch.object(tc.frappe.db, "exists", return_value=True),
		patch.object(tc.frappe, "get_doc", return_value=doc),
		patch.object(tc.frappe.db, "get_value", return_value=vehicle),
		patch.object(tc.frappe.db, "sql", return_value=[]),
		patch.object(tc.frappe.db, "commit"),
		patch.object(tc.frappe.db, "rollback") as rollback,
		patch.object(tc, "_plan_lock"),
		patch.object(tc, "trip_farms", return_value={"Kipsolu"}),
		patch.object(tc, "trip_hub", return_value="Kapkolia"),
		patch.object(tc, "open_trips_to_farm", return_value=list(open_trips)),
		patch.object(tc.truck_routes, "add_farm_run", return_value=run) as add_run,
		patch.object(tc.frappe, "delete_doc") as delete,
		patch.object(tc, "_add_rows_to_trip", return_value=(joined or [], [])) as add_rows,
	):
		tc.changeTripVehicle()
	return frappe.local.response["message"], add_run, delete, add_rows, rollback


class UnitTestChangeTripTruck(UnitTestCase):
	def test_trip_goes_on_a_new_run_of_the_new_trucks_route(self):
		doc = _trip()
		msg, add_run, delete, _rows, _rb = _change(doc, run={"status": "success", "route": "R-NEW", "run": 2})
		self.assertEqual(msg["status"], "success")
		add_run.assert_called_once_with(
			"KDG 076V", "Kipsolu", "2026-11-22", hub="Kapkolia", exclude_trip="BRT-OLD"
		)
		self.assertEqual((doc.vehicle, doc.route, doc.run), ("KDG 076V", "R-NEW", 2))
		doc.save.assert_called_once()
		delete.assert_not_called()

	def test_route_refusal_leaves_the_trip_on_its_truck(self):
		doc = _trip()
		msg, _run, _d, _rows, rollback = _change(
			doc, run={"status": "error", "reason": "open_trip", "message": "KTCB 443K's trip …"}
		)
		self.assertEqual(msg["reason"], "open_trip")
		self.assertEqual((doc.vehicle, doc.route, doc.run), ("KDP 283L", "R-OLD", 3))
		doc.save.assert_not_called()
		rollback.assert_called_once()

	def test_every_bucket_joins_the_new_trucks_open_trip_to_the_farm(self):
		doc = _trip()
		target = {"trip": "BRT-NEW", "vehicle": "KDG 076V", "room": 20, "route": "R-NEW", "run": 1}
		msg, add_run, delete, add_rows, _rb = _change(doc, joined=["x"], open_trips=[target])
		self.assertEqual((msg["status"], msg["name"], msg["merged"]), ("success", "BRT-NEW", "BRT-OLD"))
		delete.assert_called_once_with("Bucket Request Trip", "BRT-OLD", ignore_permissions=True, force=1)
		rows = add_rows.call_args[0][1]
		self.assertEqual(
			sorted((r["order_pick_list"], r["buckets"]) for r in rows), [("OPL-1", 5), ("OPL-2", 3)]
		)
		self.assertFalse(any("name" in r or "parent" in r for r in rows))
		add_run.assert_not_called()

	def test_another_trucks_open_trip_is_not_joined(self):
		doc = _trip()
		other = {"trip": "BRT-X", "vehicle": "KTCB 443K", "room": 20, "route": "R-X", "run": 1}
		msg, add_run, delete, _rows, _rb = _change(
			doc, run={"status": "success", "route": "R-NEW", "run": 2}, open_trips=[other]
		)
		self.assertEqual(msg["name"], "BRT-OLD")
		delete.assert_not_called()
		add_run.assert_called_once()

	def test_new_trucks_trip_without_room_refuses(self):
		doc = _trip()
		target = {"trip": "BRT-NEW", "vehicle": "KDG 076V", "room": 4, "route": "R-NEW", "run": 1}
		msg, _run, delete, _rows, _rb = _change(doc, open_trips=[target])
		self.assertEqual(msg["reason"], "over_capacity")
		delete.assert_not_called()
