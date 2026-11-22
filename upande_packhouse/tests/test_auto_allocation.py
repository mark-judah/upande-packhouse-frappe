"""Automatic Allocation: Run Times run once a day per Sales Farm, allocate tomorrow's planned orders the way
Allocate does, and only a Run Time with Release releases, schedules and plans the trips.

Run:  bench --site kaitet.local run-tests --app upande_packhouse --module upande_packhouse.tests.test_auto_allocation
"""

import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from upande_packhouse.api import auto_allocation as aa


def _settings(enabled=1, times=(("00:00:00", 0),), farms=(("Kapkolia", ""),), unplanned=0):
	return frappe._dict(
		enable_auto_allocation=enabled,
		auto_allocation_include_unplanned=unplanned,
		auto_allocation_times=[frappe._dict(run_time=t, release=r, enabled=1) for t, r in times],
		auto_allocation_farms=[frappe._dict(sales_farm=f, stock_farms=s, enabled=1) for f, s in farms],
	)


class TestTick(unittest.TestCase):
	def _tick(self, settings, now, existing=None):
		made, queued = [], []
		with (
			patch.object(aa, "_settings", return_value=settings),
			patch.object(aa, "nowdate", return_value=now.date().isoformat()),
			patch.object(aa, "now_datetime", return_value=now),
			patch.object(aa.frappe.db, "get_value", return_value=existing),
			patch.object(aa.frappe.db, "set_value"),
			patch.object(aa.frappe.db, "commit"),
			patch.object(
				aa, "_new_run", side_effect=lambda *a, **k: made.append((a, k)) or f"AAR-{len(made)}"
			),
			patch.object(aa.frappe, "enqueue", side_effect=lambda *a, **k: queued.append(k)),
		):
			started = aa.tick()
		return started, made, queued

	def test_disabled_does_nothing(self):
		started, made, queued = self._tick(_settings(enabled=0), datetime.datetime(2026, 11, 23, 0, 5))
		self.assertEqual((started, made, queued), ([], [], []))

	def test_due_time_starts_one_run_per_farm_for_tomorrow(self):
		s = _settings(farms=(("Kapkolia", ""), ("Chepsito", "Chepsito")))
		started, made, queued = self._tick(s, datetime.datetime(2026, 11, 23, 0, 5))
		self.assertEqual(len(started), 2)
		self.assertEqual([m[0][2] for m in made], ["Kapkolia", "Chepsito"])
		self.assertEqual(queued[1]["stock_farms"], ["Chepsito"])
		self.assertIsNone(queued[0]["stock_farms"])

	def test_not_due_yet(self):
		s = _settings(times=(("10:00:00", 1),))
		started, made, _q = self._tick(s, datetime.datetime(2026, 11, 23, 9, 55))
		self.assertEqual((started, made), ([], []))

	def test_a_run_time_runs_once(self):
		existing = frappe._dict(name="AAR-1", status="Done")
		started, made, queued = self._tick(_settings(), datetime.datetime(2026, 11, 23, 0, 10), existing)
		self.assertEqual((started, made, queued), ([], [], []))

	def test_busy_run_is_tried_again(self):
		existing = frappe._dict(name="AAR-1", status="Busy")
		started, made, queued = self._tick(_settings(), datetime.datetime(2026, 11, 23, 0, 10), existing)
		self.assertEqual(started, ["AAR-1"])
		self.assertEqual(made, [])
		self.assertEqual(queued[0]["run_name"], "AAR-1")

	def test_missed_by_more_than_an_hour_is_recorded_not_run(self):
		started, made, queued = self._tick(_settings(), datetime.datetime(2026, 11, 23, 1, 30))
		self.assertEqual(started, [])
		self.assertEqual(queued, [])
		self.assertEqual(made[0][1].get("status"), "Missed")


class TestRun(unittest.TestCase):
	def _run(self, release, proposal=True):
		doc = frappe._dict(name="AAR-1", sales_farm="Kapkolia", delivery_date="2026-11-24", released=release)
		doc.save = MagicMock()
		# a namespace, not a MagicMock: a mock has its own `_lock` attribute
		day = SimpleNamespace(
			_lock=MagicMock(), _run_day=MagicMock(), _proposal=MagicMock(), _run_confirm=MagicMock()
		)
		day._run_day.return_value = {"new_orders": 3, "new_boxes": 12, "new_stems": 2400, "shortages": []}
		day._proposal.return_value = {"orders": [1]} if proposal else None
		day._run_confirm.return_value = {"saved": 2, "errors": []}
		sched, trips = MagicMock(), MagicMock()
		sched.auto_schedule.return_value = {"2026-11-23": 2}
		trips.enabled.return_value = True
		trips.LAST_RUN_KEY = "k"
		modules = {
			"upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation": day,
			"upande_packhouse.api.remote_transfer.scheduler": sched,
			"upande_packhouse.api.auto_transfer": trips,
		}
		pkg_attr = {
			"upande_packhouse.upande_packhouse.page.sales_allocation_v2": ("day_allocation", day),
			"upande_packhouse.api.remote_transfer": ("scheduler", sched),
			"upande_packhouse.api": ("auto_transfer", trips),
		}
		import importlib

		with (
			patch.dict("sys.modules", modules),
			patch.object(aa.frappe, "get_doc", return_value=doc),
			patch.object(aa, "now_datetime", return_value=datetime.datetime(2026, 11, 23, 0, 5)),
			patch.object(aa.frappe.db, "commit"),
			patch.object(aa.frappe.db, "rollback"),
			patch.object(aa.frappe.cache, "delete_value"),
			patch.object(aa, "_pick_lists", side_effect=[["OPL-1"], ["OPL-1", "OPL-2"]]),
			patch.object(aa, "_open_trips", return_value=["BRT-1"]),
		):
			patches = [
				patch.object(importlib.import_module(pkg), attr, mod, create=True)
				for pkg, (attr, mod) in pkg_attr.items()
			]
			for p in patches:
				p.start()
			try:
				status = aa.run("AAR-1", stock_farms=None, include_unplanned=0)
			finally:
				for p in patches:
					p.stop()
		return status, doc, day, sched, trips

	def test_allocates_tomorrows_planned_orders_only(self):
		status, doc, day, _s, _t = self._run(release=0)
		self.assertEqual(status, "Done")
		args, kwargs = day._run_day.call_args
		self.assertEqual(args[:3], ("Kapkolia", "2026-11-24", "2026-11-24"))
		self.assertTrue(kwargs["only_planned"])
		self.assertEqual((doc.orders, doc.boxes), (3, 12))

	def test_run_time_without_release_does_not_release_schedule_or_plan_trips(self):
		_status, _doc, day, sched, trips = self._run(release=0)
		day._run_confirm.assert_not_called()
		sched.auto_schedule.assert_not_called()
		trips.run.assert_not_called()

	def test_run_time_with_release_releases_whole_then_schedules_then_plans_trips(self):
		status, doc, day, sched, trips = self._run(release=1)
		self.assertEqual(status, "Done")
		self.assertIsNone(day._run_confirm.call_args.kwargs["budget"])
		sched.auto_schedule.assert_called_once_with(["2026-11-24"])
		trips.run.assert_called_once()
		self.assertEqual(
			(doc.released_orders, doc.pick_lists, doc.scheduled, doc.trips), (2, "OPL-2", 2, "BRT-1")
		)

	def test_nothing_waiting_release_is_not_an_error(self):
		status, _doc, day, _s, _t = self._run(release=1, proposal=False)
		self.assertEqual(status, "Done")
		day._run_confirm.assert_not_called()


class TestStockFarms(unittest.TestCase):
	def test_list_or_every_farm(self):
		self.assertEqual(
			aa._stock_farms(frappe._dict(stock_farms="Kaptumbo, Simotwo\nTorongo")),
			["Kaptumbo", "Simotwo", "Torongo"],
		)
		self.assertIsNone(aa._stock_farms(frappe._dict(stock_farms="")))

	def test_time_key(self):
		self.assertEqual(aa._time_key(datetime.timedelta(hours=0)), "00:00:00")
		self.assertEqual(aa._time_key("14:30:00"), "14:30:00")
