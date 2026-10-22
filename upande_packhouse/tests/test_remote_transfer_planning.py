from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api import auto_transfer as at
from upande_packhouse.api.remote_transfer import bucket_logistics as bl
from upande_packhouse.api.remote_transfer import transfer_scheduling as tc

# Remote Transfers planning rules: a trip carries only orders delivering tomorrow or the
# day after; every scheduled order's waiting buckets end up on a trip (a full run gets
# the truck's next run); a shared bucket is planned once; Bucket Logistics lists orders
# without a trip only as "Not scheduled". The database is mocked: these run on a bare
# site (CI) as well.

FARM = "Ravine"


def _order(opl, delivery, schedule=1, buckets=5):
	return {
		"opl": opl,
		"order_name": opl.replace("OPL", "ORD"),
		"customer": "C",
		"delivery_date": str(delivery),
		"schedule": schedule,
		"team": "A",
		"farms": [
			{
				"farm": FARM,
				"buckets": buckets,
				"stems": buckets * 100,
				"varieties": [{"variety": "Rose", "buckets": buckets, "stems": buckets * 100}],
			}
		],
	}


def _bucket(opl, bucket, rides=None):
	return {
		"opl": opl,
		"bucket": bucket,
		"farm": FARM,
		"open": 0 if rides else 1,
		"rides": rides,
		"shelved": 0,
	}


class _Fleet:
	"""One truck whose runs to FARM have the room given; add_farm_run appends a run."""

	def __init__(self, rooms):
		self.rooms = list(rooms)
		self.saved = []

	def runs(self):
		return [{"route": "R1", "run": i + 1, "stops": [FARM]} for i in range(len(self.rooms))]

	def open_run_room(self, vehicle, date, farm):
		return list(zip(self.runs(), self.rooms, strict=True))

	def add_farm_run(self, vehicle, farm, date, enforce_room=False, while_on_road=False):
		if enforce_room and any(self.rooms):
			return {"status": "error", "reason": "has_room"}
		self.rooms.append(10)
		return {"status": "success", "route": "R1", "run": len(self.rooms)}

	def save_trip(self, **kw):
		n = sum(r["buckets"] for r in kw["rows"])
		self.rooms[kw["run"] - 1] -= n
		self.saved.append(kw)
		return {"status": "success", "name": "BRT-{0}".format(kw["run"])}

	def claims(self, opls):
		out = {}
		for kw in self.saved:
			for r in kw["rows"]:
				key = (r["order_pick_list"], r["farm"])
				out[key] = out.get(key, 0) + r["buckets"]
		return out


class UnitTestPlacingScheduledOrders(UnitTestCase):
	def setUp(self):
		self.today = frappe.utils.getdate(frappe.utils.today())
		self.tomorrow = frappe.utils.add_days(self.today, 1)

	def _run(self, fleet, orders, open_counts, call=None):
		cache = MagicMock()
		cache.set.return_value = True
		truck = {"name": "KDA 001A", "capacity_buckets": 20, "runs": fleet.runs()}
		with (
			patch.object(at.frappe, "cache", return_value=cache),
			patch.object(at.frappe.db, "savepoint"),
			patch.object(at.frappe.db, "commit"),
			patch.object(at, "_trucks", return_value=[truck]),
			patch.object(at, "_run_candidates", return_value=[truck]),
			patch.object(tc, "transfer_hub", return_value="Kapkolia"),
			patch.object(tc, "farm_holders", return_value={}),
			patch.object(tc, "schedule_orders", return_value=orders),
			patch.object(tc, "_open_counts", return_value=open_counts),
			patch.object(tc, "_bucket_claims", side_effect=fleet.claims),
			patch.object(tc, "_open_run_room", side_effect=fleet.open_run_room),
			patch.object(tc, "_save_trip", side_effect=fleet.save_trip),
			patch.object(at.truck_routes, "add_farm_run", side_effect=fleet.add_farm_run) as add,
		):
			return (call or at._place_scheduled)(), add

	def test_due_today_order_is_never_planned(self):
		orders = [_order("OPL-TODAY", self.today), _order("OPL-TMRW", self.tomorrow)]
		with (
			patch.object(tc, "transfer_hub", return_value="Kapkolia"),
			patch.object(tc, "schedule_orders", return_value=orders),
			patch.object(tc, "_open_counts", return_value={(o["opl"], FARM): 5 for o in orders}),
			patch.object(tc, "_bucket_claims", return_value={}),
		):
			portions = at._scheduled_portions(str(self.today))
		self.assertEqual([r["order_pick_list"] for r in portions[FARM]], ["OPL-TMRW"])
		# The overdue path is gone: nothing plans an order delivering today or earlier.
		self.assertFalse(hasattr(at, "_overdue_orders"))

	def test_trip_drops_an_order_due_on_its_date(self):
		doc = frappe.new_doc("Bucket Request Trip")
		doc.status, doc.trip_date = "Draft", str(self.today)
		doc.append("orders", {"order_pick_list": "OPL-TODAY", "farm": FARM, "buckets": 4, "stems": 400})
		doc.append("orders", {"order_pick_list": "OPL-TMRW", "farm": FARM, "buckets": 3, "stems": 300})
		with patch.object(frappe.db, "sql_list", return_value=["OPL-TODAY"]):
			doc.drop_due_orders()
		self.assertEqual([o.order_pick_list for o in doc.orders], ["OPL-TMRW"])
		self.assertEqual(doc.total_buckets, 3)

	def test_later_step_order_gets_a_trip_after_distribute(self):
		fleet = _Fleet([20])
		# Step 3: _distribute holds it back while step 1 is on the road.
		orders = [_order("OPL-STEP3", self.tomorrow, schedule=3)]
		with (
			patch.object(at, "plan", return_value={"status": "success"}),
			patch.object(at, "_plan_waiting"),
		):
			summary, _add = self._run(fleet, orders, {("OPL-STEP3", FARM): 5}, call=at.distributeTrips)
		self.assertEqual(summary["status"], "success")
		self.assertEqual(len(fleet.saved), 1)
		saved = fleet.saved[0]
		self.assertEqual([(r["order_pick_list"], r["buckets"]) for r in saved["rows"]], [("OPL-STEP3", 5)])
		self.assertEqual((saved["route"], saved["run"], saved["status"]), ("R1", 1, "Draft"))
		self.assertEqual(str(saved["trip_date"]), str(self.today))

	def test_full_run_gets_a_second_run_for_the_leftover(self):
		fleet = _Fleet([15])
		orders = [_order("OPL-BIG", self.tomorrow, buckets=25)]
		out, add = self._run(fleet, orders, {("OPL-BIG", FARM): 25})
		add.assert_called_once_with("KDA 001A", FARM, str(self.today), enforce_room=True, while_on_road=True)
		self.assertEqual([(kw["run"], kw["rows"][0]["buckets"]) for kw in fleet.saved], [(1, 15), (2, 10)])
		self.assertEqual(out["runs_added"], [{"vehicle": "KDA 001A", "farm": FARM, "run": 2}])

	def test_no_second_run_while_the_first_has_room(self):
		fleet = _Fleet([15])
		fleet.save_trip = MagicMock(return_value={"status": "error", "reason": "over_claimed"})
		out, add = self._run(fleet, [_order("OPL-X", self.tomorrow)], {("OPL-X", FARM): 5})
		add.assert_not_called()
		self.assertEqual(out, {"placed": [], "runs_added": []})


class UnitTestSharedBucketCountedOnce(UnitTestCase):
	"""Bucket B3 is shared by OPL-L (its lead now) and OPL-R, whose trip planned it when
	OPL-R led: OPL-R's trip claims 3 while OPL-R itself has only 2 buckets of its own."""

	def _patches(self):
		entries = [
			_bucket("OPL-L", "B1"),
			_bucket("OPL-L", "B2"),
			_bucket("OPL-L", "B3"),
			_bucket("OPL-R", "B4"),
			_bucket("OPL-R", "B5"),
			_bucket("OPL-R", "B3", rides="OPL-L"),
		]
		groups = {("B3", FARM): {"lead": "OPL-L", "opls": ["OPL-L", "OPL-R"], "rows": {}}}
		own = {("OPL-L", FARM): 3, ("OPL-R", FARM): 2}
		trip = {("OPL-R", FARM): 3}

		def transfer_buckets(opls):
			return [e for e in entries if e["opl"] in opls]

		return (
			patch.object(tc, "_transfer_buckets", side_effect=transfer_buckets),
			patch.object(tc, "shared_buckets", return_value=groups),
			patch.object(
				tc, "_open_counts", side_effect=lambda opls: {k: v for k, v in own.items() if k[0] in opls}
			),
			patch.object(
				tc,
				"_trip_claims",
				side_effect=lambda opls, ex=None: {k: v for k, v in trip.items() if k[0] in opls},
			),
		)

	def test_bucket_on_another_orders_trip_is_claimed_for_its_lead(self):
		p = self._patches()
		with p[0], p[1], p[2], p[3]:
			self.assertEqual(tc._shared_covered(["OPL-L"]), {("OPL-L", FARM): 1})
			self.assertEqual(tc._bucket_claims(["OPL-L"]), {("OPL-L", FARM): 1})

	def test_save_trip_refuses_the_shared_bucket_under_a_second_order(self):
		p = self._patches()
		row = {
			"order_pick_list": "OPL-L",
			"order_name": "ORD-L",
			"farm": FARM,
			"buckets": 3,
			"stems": 300,
		}
		with (
			p[0],
			p[1],
			p[2],
			p[3],
			patch.object(tc, "_plan_lock"),
			patch.object(tc, "_day_runs", return_value=[]),
			patch.object(tc, "_vehicle_on_road", return_value=None),
		):
			res = tc._save_trip(
				name=None,
				vehicle="KDA 001A",
				trip_date=frappe.utils.today(),
				status="Draft",
				notes="",
				collection_order="",
				farm="",
				rows=[row],
			)
		self.assertEqual(res["reason"], "over_claimed")
		self.assertEqual(res["conflicts"][0]["free"], 2)

	def test_trip_room_counts_a_shared_bucket_once(self):
		p = self._patches()
		with p[0]:
			self.assertEqual(tc._shared_overlap({("OPL-L", FARM): 3, ("OPL-R", FARM): 3}), 1)
			# Lead not on this trip: nothing is counted twice here.
			self.assertEqual(tc._shared_overlap({("OPL-R", FARM): 3, ("OPL-X", FARM): 1}), 0)


class UnitTestBucketLogisticsSections(UnitTestCase):
	def test_only_unscheduled_orders_have_a_no_trip_section(self):
		self.assertEqual(bl.no_trip_section({"scheduled": 0}), "Not scheduled")
		self.assertIsNone(bl.no_trip_section({"scheduled": 1}))

	def test_page_has_no_no_trip_section(self):
		path = frappe.get_app_path(
			"upande_packhouse", "templates", "remote_transfer", "bucket_logistics.html"
		)
		with open(path) as f:
			html = f.read()
		self.assertNotIn("No trip", html)
		self.assertNotIn("no trip yet", html)
		self.assertIn("<th>Not scheduled</th>", html)


class UnitTestBucketLogisticsTripStages(UnitTestCase):
	"""SANDY's 10 Torongo buckets: 7 went on run 1 (dispatched, arrived), 3 are planned
	on run 2, still a draft at the farm with one of them in a trolley."""

	def test_order_split_over_two_trips_gets_each_trips_own_stages(self):
		def trip(name, status, run, planned):
			return {
				"trip": name,
				"status": status,
				"run": run,
				"trip_date": str(frappe.utils.today()),
				"farm": "Torongo",
				"buckets": planned,
				"row_buckets": planned,
			}

		rows = [{"opl": "OPL-S", "trips": [trip("BRT-1", "Dispatched", 1, 7), trip("BRT-2", "Draft", 2, 3)]}]
		reached = {"awaiting": 7, "trolley": 7, "on_truck": 7, "transit": 7, "arrived_hub": 7}
		recorded = [frappe._dict(trip="BRT-1", opl="OPL-S", farm="Torongo", total=7, **reached)]
		in_trolley = [frappe._dict(opl="OPL-S", farm="Torongo", n=1)]
		with patch.object(bl.frappe.db, "sql", side_effect=[recorded, in_trolley]) as sql:
			bl.trip_stages(rows)
		self.assertEqual(sql.call_count, 2)
		first, second = (t["stages"] for t in rows[0]["trips"])
		self.assertEqual(first, dict(dict.fromkeys(bl.STAGE_KEYS, 0), **reached))
		self.assertEqual(second, dict(dict.fromkeys(bl.STAGE_KEYS, 0), awaiting=3, trolley=1))


class UnitTestTripLeavesAFarm(UnitTestCase):
	"""KDG 076V trip 1: planned 10 of SANDY at Torongo, left with 7 on. The trip keeps the
	7; the 3 go on the truck's next run to Torongo as it leaves, not once it is received."""

	FARM = "Torongo"

	def setUp(self):
		self.today = str(frappe.utils.today())
		self.tomorrow = str(frappe.utils.add_days(self.today, 1))

	def _trip(self, status="Dispatched", departed=""):
		doc = frappe.new_doc("Bucket Request Trip")
		doc.name, doc.vehicle, doc.status, doc.trip_date = "BRT-1", "KDG 076V", status, self.today
		doc.departed_stops = departed
		doc.add_comment = MagicMock()
		return doc

	def _row(self, doc, opl, planned, loaded, farm=None):
		doc.append(
			"orders",
			{
				"order_pick_list": opl,
				"order_name": opl.replace("OPL", "ORD"),
				"farm": farm or self.FARM,
				"buckets": planned,
				"stems": planned * 100,
				"loaded_buckets": loaded,
				"full_farm_buckets": planned,
			},
		)

	def _leave(self, doc, open_counts, claims=None, dds=None):
		"""Trim the trip as its save does, then carry the rest. Returns (trimmed, run 2's rows)."""
		runs = [
			{"route": "R1", "run": 1, "stops": [self.FARM, FARM], "trip": "BRT-1", "trip_status": doc.status},
			{"route": "R1", "run": 2, "stops": [self.FARM, FARM], "trip": None, "trip_status": None},
		]
		written = []

		def write(slot, *_a):
			written.extend((slot["run"], r["order_pick_list"], r["buckets"]) for r in slot["rows"])
			return "BRT-{0}".format(slot["run"])

		with (
			patch.object(tc, "transfer_hub", return_value="Kapkolia"),
			patch.object(tc, "_plan_lock"),
			patch.object(tc.frappe.db, "savepoint"),
			patch.object(tc, "_opl_delivery_dates", return_value=dds or {}),
			patch.object(tc, "_open_counts", return_value=open_counts),
			patch.object(tc, "_bucket_claims", side_effect=lambda opls: dict(claims or {})),
			patch.object(tc, "_day_runs", return_value=runs),
			patch.object(tc, "_vehicle_capacity", return_value=100),
			# Run 1 is this trip: still open on a multi-stop run, but it has left the farm.
			patch.object(tc.frappe, "get_doc", return_value=doc),
			patch.object(tc, "_live_planned", return_value=0),
			patch.object(tc, "_write_run_trip", side_effect=write),
			patch.object(tc, "farm_holders", return_value={}),
			patch.object(tc.truck_routes, "add_farm_run") as add,
		):
			left = tc.trim_left_farms(doc)
			tc.carry_left_behind(doc, left)
		add.assert_not_called()
		return left, written

	def test_dispatched_trip_keeps_what_it_loaded_and_the_rest_goes_on_the_next_run(self):
		doc = self._trip()
		self._row(doc, "OPL-SANDY", 10, 7)
		self._row(doc, "OPL-ONE", 1, 0)
		left, written = self._leave(doc, {("OPL-SANDY", self.FARM): 3, ("OPL-ONE", self.FARM): 1})
		self.assertEqual(
			[(o.order_pick_list, o.buckets, o.stems) for o in doc.orders], [("OPL-SANDY", 7, 700)]
		)
		self.assertEqual(doc.orders[0].is_partial, 1)
		self.assertEqual(doc.orders[0].full_farm_buckets, 10)
		self.assertEqual((doc.total_buckets, doc.total_stems), (7, 700))
		self.assertEqual(sorted(written), [(2, "OPL-ONE", 1), (2, "OPL-SANDY", 3)])
		doc.add_comment.assert_called_once_with("Info", "Left Torongo with 7 of 11 planned; 4 moved to BRT-2")
		self.assertEqual(left[self.FARM]["planned"], 11)

	def test_next_run_already_holding_the_rest_is_not_topped_up_again(self):
		doc = self._trip()
		self._row(doc, "OPL-SANDY", 10, 7)
		_left, written = self._leave(doc, {("OPL-SANDY", self.FARM): 3}, claims={("OPL-SANDY", self.FARM): 3})
		self.assertEqual(doc.orders[0].buckets, 7)
		self.assertEqual(written, [])
		doc.add_comment.assert_called_once_with("Info", "Left Torongo with 7 of 10 planned")

	def test_departed_stop_trims_only_that_farm(self):
		doc = self._trip(status="Scheduled", departed=self.FARM)
		self._row(doc, "OPL-SANDY", 10, 7)
		self._row(doc, "OPL-RAV", 5, 2, farm=FARM)
		left, written = self._leave(doc, {("OPL-SANDY", self.FARM): 3, ("OPL-RAV", FARM): 3})
		self.assertEqual(
			[(o.order_pick_list, o.farm, o.buckets) for o in doc.orders],
			[("OPL-SANDY", self.FARM, 7), ("OPL-RAV", FARM, 5)],
		)
		self.assertEqual(list(left), [self.FARM])
		self.assertEqual(written, [(2, "OPL-SANDY", 3)])
		self.assertEqual(doc.total_buckets, 12)

	def test_order_due_today_is_trimmed_but_not_carried(self):
		doc = self._trip()
		self._row(doc, "OPL-TODAY", 4, 1)
		self._row(doc, "OPL-TMRW", 4, 2)
		_left, written = self._leave(
			doc,
			{("OPL-TODAY", self.FARM): 3, ("OPL-TMRW", self.FARM): 2},
			dds={"OPL-TODAY": self.today, "OPL-TMRW": self.tomorrow},
		)
		self.assertEqual(
			[(o.order_pick_list, o.buckets) for o in doc.orders], [("OPL-TODAY", 1), ("OPL-TMRW", 2)]
		)
		self.assertEqual(written, [(2, "OPL-TMRW", 2)])

	def test_receive_time_carry_over_is_a_no_op_once_trimmed(self):
		doc = self._trip()
		doc.route, doc.run = "R1", 1
		self._row(doc, "OPL-SANDY", 10, 7)
		self._leave(doc, {("OPL-SANDY", self.FARM): 3})
		with patch.object(tc, "_carry_rows") as carry:
			self.assertEqual(tc._carry_over(doc), [])
		carry.assert_called_once_with(doc, [], None, None)

	def test_full_next_run_gets_another_run(self):
		doc = self._trip()
		self._row(doc, "OPL-SANDY", 10, 7)
		runs = [{"route": "R1", "run": 1, "stops": [self.FARM], "trip": "BRT-1", "trip_status": "Dispatched"}]
		after = runs + [{"route": "R1", "run": 2, "stops": [self.FARM], "trip": None, "trip_status": None}]
		day = [runs, after]
		with (
			patch.object(tc, "transfer_hub", return_value="Kapkolia"),
			patch.object(tc, "_plan_lock"),
			patch.object(tc.frappe.db, "savepoint"),
			patch.object(tc, "_opl_delivery_dates", return_value={}),
			patch.object(tc, "_open_counts", return_value={("OPL-SANDY", self.FARM): 3}),
			patch.object(tc, "_bucket_claims", return_value={}),
			patch.object(tc, "_day_runs", side_effect=lambda *a: day.pop(0)),
			patch.object(tc, "_vehicle_capacity", return_value=100),
			patch.object(tc, "_write_run_trip", return_value="BRT-2") as write,
			patch.object(tc, "farm_holders", return_value={}),
			patch.object(
				tc.truck_routes, "add_farm_run", return_value={"status": "success", "route": "R1", "run": 2}
			) as add,
		):
			tc.carry_left_behind(doc, tc.trim_left_farms(doc))
		add.assert_called_once_with("KDG 076V", self.FARM, self.today, enforce_room=True, while_on_road=True)
		self.assertEqual([r["buckets"] for r in write.call_args[0][0]["rows"]], [3])


class UnitTestMinimumTruckLoad(UnitTestCase):
	"""A truck goes to Kapkolia only once its loaded buckets reach its minimum load: all
	of its capacity, or Production Settings > Minimum Truck Load (%) when Allow Sending
	Partly Loaded Trucks is on. Below it, only with a reason; arriving there anyway is
	noted, never held."""

	CAP = 144  # 12 trolleys x 12 buckets

	def _trip(self, loaded, stops=("Torongo",), status="Scheduled", departed=""):
		doc = frappe.new_doc("Bucket Request Trip")
		doc.name, doc.vehicle, doc.status = "BRT-MIN", "KDG 076V", status
		doc.trip_date = frappe.utils.today()
		doc.departed_stops = departed
		per = {f: 0 for f in stops}
		for i in range(loaded):
			per[stops[i % len(stops)]] += 1
		for f in stops:
			doc.append(
				"orders",
				{
					"order_pick_list": "OPL-{0}".format(f),
					"farm": f,
					"buckets": per[f],
					"loaded_buckets": per[f],
				},
			)
		for i in range(loaded):
			doc.append(
				"trip_buckets",
				{
					"bucket": "B{0}".format(i),
					"order_pick_list": "OPL-{0}".format(stops[i % len(stops)]),
					"farm": stops[i % len(stops)],
					"loaded_at": frappe.utils.now(),
				},
			)
		doc.total_buckets = loaded
		doc.add_comment = MagicMock()
		doc.save = MagicMock()
		doc.reload = MagicMock()
		return doc

	def _settings(self, partial=0, pct=None):
		values = {"allow_partial_truck_load": partial, "min_truck_load_pct": pct}
		return patch.object(tc, "_production_setting", side_effect=lambda f: values.get(f))

	def _common(self, stack, doc, stops, partial=0, pct=None, skip=()):
		"""Patch the trip's world (the database mocked) onto `stack`, but those in skip."""
		patches = {
			"settings": self._settings(partial, pct),
			"transfer_hub": patch.object(tc, "transfer_hub", return_value="Kapkolia"),
			"_vehicle_capacity": patch.object(tc, "_vehicle_capacity", return_value=self.CAP),
			"_trip_stops": patch.object(tc, "_trip_stops", return_value=list(stops)),
			"_stop_complete": patch.object(tc, "_stop_complete", return_value=True),
			"_hold_for_waiting": patch.object(tc, "_hold_for_waiting", return_value=False),
			"_truck_out_on": patch.object(tc, "_truck_out_on", return_value=None),
			"_vehicle_on_road": patch.object(tc, "_vehicle_on_road", return_value=None),
			"_put_loads_in_transit": patch.object(tc, "_put_loads_in_transit"),
			"log_transfer_event": patch.object(tc, "log_transfer_event"),
			"exists": patch.object(tc.frappe.db, "exists", return_value=True),
			"commit": patch.object(tc.frappe.db, "commit"),
			"get_doc": patch.object(tc.frappe, "get_doc", return_value=doc),
		}
		for key, p in patches.items():
			if key not in skip:
				stack.enter_context(p)

	def _call(self, fn, doc, form, stops=("Torongo",), partial=0, pct=None, skip=()):
		"""Call a whitelisted method with this form; returns its response message."""
		saved_form, saved_resp = frappe.local.form_dict, getattr(frappe.local, "response", None)
		frappe.local.form_dict = frappe._dict(form)
		frappe.local.response = frappe._dict()
		try:
			with ExitStack() as stack:
				self._common(stack, doc, stops, partial, pct, skip)
				fn()
			return frappe.local.response.get("message")
		finally:
			frappe.local.form_dict = saved_form
			frappe.local.response = saved_resp

	def _close(self, doc, reason=None, **kw):
		form = {"name": doc.name, "farm": "Torongo", "complete": 1}
		if reason:
			form["reason"] = reason
		return self._call(tc.closeTripStop, doc, form, **kw)

	def _comments(self, doc):
		return [c.args[1] for c in doc.add_comment.call_args_list]

	def test_threshold_numbers(self):
		with patch.object(tc, "_vehicle_capacity", return_value=self.CAP):
			with self._settings(0):
				self.assertEqual(tc.min_load_needed("KDG 076V"), (144, 144, 100))
			with self._settings(1, 80):
				self.assertEqual(tc.min_load_needed("KDG 076V"), (116, 144, 80))
		with patch.object(tc, "_vehicle_capacity", return_value=0), self._settings(0):
			self.assertEqual(tc.min_load_needed("KDG 076V"), (0, 0, 0))

	def test_settings_fields_missing_means_full_load(self):
		with (
			patch.object(tc, "_vehicle_capacity", return_value=self.CAP),
			patch.object(tc.frappe.db, "get_single_value", side_effect=frappe.ValidationError("no field")),
		):
			self.assertEqual(tc.min_load_needed("KDG 076V"), (144, 144, 100))

	def test_a_checkbox_off_one_short_of_full_is_refused(self):
		doc = self._trip(143)
		res = self._close(doc)
		self.assertEqual(res["status"], "error")
		self.assertEqual(res["reason"], "below_minimum")
		self.assertEqual((res["loaded"], res["capacity"], res["needed"], res["pct"]), (143, 144, 144, 100))
		self.assertIn("KDG 076V has 143 of 144 buckets — it leaves at 144 (100%)", res["message"])
		self.assertEqual(doc.status, "Scheduled")
		doc.save.assert_not_called()

	def test_b_checkbox_off_full_truck_is_sent(self):
		doc = self._trip(144)
		res = self._close(doc)
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["trip_status"], "Dispatched")
		self.assertEqual(res["below_minimum"], 0)

	def test_c_checkbox_on_at_80_pct(self):
		res = self._close(self._trip(116), partial=1, pct=80)
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["trip_status"], "Dispatched")
		res = self._close(self._trip(115), partial=1, pct=80)
		self.assertEqual(res["reason"], "below_minimum")
		self.assertEqual((res["loaded"], res["needed"], res["pct"]), (115, 116, 80))

	def test_d_reason_overrides_the_minimum(self):
		doc = self._trip(12)
		res = self._close(doc, reason="Rain, road closing")
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["trip_status"], "Dispatched")
		self.assertEqual(res["below_minimum"], 1)
		self.assertIn("Sent to Kapkolia with 12 of 144 (min 144): Rain, road closing", self._comments(doc))

	def test_d_reason_records_on_the_bucket_events(self):
		doc = self._trip(12)
		with patch.object(tc, "log_transfer_event") as log:
			self._call(
				tc.closeTripStop,
				doc,
				{"name": doc.name, "farm": "Torongo", "reason": "Rain"},
				skip=("log_transfer_event",),
			)
		self.assertEqual(log.call_count, 12)
		self.assertIn("below minimum load (12 of 144, min 144): Rain", log.call_args.kwargs["details"])

	def test_earlier_stop_of_a_multi_stop_trip_is_not_checked(self):
		stops = ("Torongo", "Kapsoit")
		doc = self._trip(10, stops=stops)
		with patch.object(tc, "_stop_complete", side_effect=lambda d, f: f == "Torongo"):
			res = self._call(tc.closeTripStop, doc, {"name": doc.name, "farm": "Torongo"}, stops=stops)
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["heading_to"], "Kapsoit")
		self.assertNotEqual(doc.status, "Dispatched")

	def test_desk_dispatch_needs_a_reason_below_the_minimum(self):
		doc = self._trip(100)
		res = self._call(tc.dispatchBucketTrip, doc, {"name": doc.name})
		self.assertEqual(res["reason"], "below_minimum")
		self.assertEqual(doc.status, "Scheduled")
		res = self._call(tc.dispatchBucketTrip, doc, {"name": doc.name, "reason": "Truck needed elsewhere"})
		self.assertEqual(res["status"], "success")
		self.assertEqual(doc.status, "Dispatched")
		self.assertIn(
			"Sent to Kapkolia with 100 of 144 (min 144): Truck needed elsewhere", self._comments(doc)
		)

	def test_desk_dispatch_of_a_truck_with_unknown_capacity_is_not_checked(self):
		doc = self._trip(5)
		with patch.object(tc, "_vehicle_capacity", return_value=0):
			res = self._call(tc.dispatchBucketTrip, doc, {"name": doc.name}, skip=("_vehicle_capacity",))
		self.assertEqual(res["status"], "success")

	def test_e_automatic_dispatch_below_the_minimum_is_held(self):
		# Torongo closed; Kapsoit (the last stop) is fully loaded but the truck is not.
		stops = ("Torongo", "Kapsoit")
		doc = self._trip(100, stops=stops, departed="Torongo")
		with ExitStack() as stack:
			self._common(stack, doc, stops)
			left = tc._advance_stops(doc, hold=False)
		self.assertEqual(left, [])
		self.assertEqual(doc.status, "Scheduled")
		self.assertNotIn("Kapsoit", doc.departed_stops.split(","))
		self.assertTrue(any(c.startswith("Held at Kapsoit: 100 of 144") for c in self._comments(doc)))

	def test_e_full_truck_still_dispatches_itself(self):
		stops = ("Torongo", "Kapsoit")
		doc = self._trip(144, stops=stops, departed="Torongo")
		with ExitStack() as stack:
			self._common(stack, doc, stops)
			left = tc._advance_stops(doc)
		self.assertEqual(left, ["Kapsoit"])
		self.assertEqual(doc.status, "Dispatched")

	def test_f_arrival_at_kapkolia_below_the_minimum_still_dispatches(self):
		doc = self._trip(12)
		with (
			self._settings(0),
			patch.object(tc, "transfer_hub", return_value="Kapkolia"),
			patch.object(tc, "_vehicle_capacity", return_value=self.CAP),
			patch.object(tc, "_trip_stops", return_value=["Torongo"]),
			patch.object(tc, "_held_loads_at_hub", return_value=doc.trip_buckets[:1]),
			patch.object(tc, "_put_loads_in_transit"),
			patch.object(tc, "_mark_arrived"),
			patch.object(tc, "_end_trip_if_shelved", return_value=False),
		):
			self.assertTrue(tc._settle_held_trip(doc, "bucket B0 shelved there"))
		self.assertEqual(doc.status, "Dispatched")
		self.assertIn(
			"Arrived at Kapkolia with 12 of 144 (min 144) — sent without pressing Send", self._comments(doc)
		)


class _Trips:
	"""Planned trips (Bucket Request Trip + Order rows) and what is loaded for each
	(_trip_loads: truck, trip record or trolley), served to _trip_claims' queries."""

	def __init__(self, trips, loads, buckets):
		self.trips, self.loads, self.buckets = trips, loads, buckets

	def sql(self, query, values=None, as_dict=False):
		out = []
		for name, t in self.trips.items():
			if t["status"] not in values["active"] or name == values["exclude"]:
				continue
			for (opl, farm), n in t["plan"].items():
				if opl in values["opls"]:
					out.append(frappe._dict(trip=name, opl=opl, farm=farm, buckets=n))
		return out

	def get_all(self, doctype, filters=None, fields=None, **kw):
		return [
			frappe._dict(
				name=name,
				trip_date=t.get("trip_date") or frappe.utils.today(),
				loaded_buckets=t.get("loaded_buckets", 0),
				departed_stops=t.get("departed", ""),
			)
			for name, t in self.trips.items()
			if name in filters["name"][1] and t["status"] in filters["status"][1]
		]

	def patches(self, stack):
		stack.enter_context(patch.object(tc.frappe.db, "sql", side_effect=self.sql))
		stack.enter_context(patch.object(tc.frappe, "get_all", side_effect=self.get_all))
		stack.enter_context(patch.object(tc, "_trip_loads", return_value=self.loads))
		stack.enter_context(patch.object(tc, "shared_buckets", return_value={}))
		stack.enter_context(
			patch.object(
				tc,
				"_transfer_buckets",
				side_effect=lambda opls: [b for b in self.buckets if b["opl"] in opls],
			)
		)


def _loaded(opl, bucket, farm=FARM):
	"""A bucket on a trolley / truck at its farm, its trip not gone yet: still open."""
	return {"opl": opl, "bucket": bucket, "farm": farm, "open": 1, "rides": None, "shelved": 0}


class UnitTestLoadedBucketClaimedByItsTrip(UnitTestCase):
	"""G's one bucket at Ravine is loaded on run 6 (a Draft, not dispatched): planned 1,
	loaded 1. It is still open, and run 6's -- for nobody else, and not again for run 6."""

	def setUp(self):
		self.today = str(frappe.utils.today())
		self.tomorrow = str(frappe.utils.add_days(self.today, 1))
		self.world = _Trips(
			{"BRT-6": {"status": "Draft", "plan": {("OPL-G", FARM): 1}, "loaded_buckets": 1}},
			{"BRT-6": {("OPL-G", FARM): 1}},
			[_loaded("OPL-G", "ZL0141")],
		)

	def _save(self, name=None, buckets=1, runs=(), route=None, run=None):
		row = {
			"order_pick_list": "OPL-G",
			"order_name": "ORD-G",
			"farm": FARM,
			"buckets": buckets,
			"stems": 80,
		}
		with ExitStack() as stack:
			self.world.patches(stack)
			stack.enter_context(patch.object(tc, "_plan_lock"))
			stack.enter_context(patch.object(tc, "_day_runs", return_value=list(runs)))
			stack.enter_context(patch.object(tc, "_vehicle_on_road", return_value=None))
			stack.enter_context(patch.object(tc, "_loaded_trip_refusal", return_value=None))
			stack.enter_context(patch.object(tc.frappe.db, "exists", return_value=bool(name)))
			stack.enter_context(patch.object(tc.frappe.db, "get_value", return_value="Draft"))
			return tc._save_trip(
				name=name,
				vehicle="KDA 001A",
				trip_date=self.today,
				status="Draft",
				notes="",
				collection_order="",
				farm="",
				rows=[row],
				route=route,
				run=run,
			)

	def test_1a_not_offered_again_and_not_free(self):
		with ExitStack() as stack:
			self.world.patches(stack)
			stack.enter_context(patch.object(tc, "transfer_hub", return_value="Kapkolia"))
			stack.enter_context(
				patch.object(tc, "schedule_orders", return_value=[_order("OPL-G", self.tomorrow, buckets=1)])
			)
			opn, claims = tc._open_counts(["OPL-G"]), tc._bucket_claims(["OPL-G"])
			portions = at._scheduled_portions(self.today)
		self.assertEqual(opn, {("OPL-G", FARM): 1})
		self.assertEqual(claims, {("OPL-G", FARM): 1})
		self.assertEqual(opn[("OPL-G", FARM)] - claims[("OPL-G", FARM)], 0)
		self.assertEqual(portions, {})

	def test_1a_trolley_bucket_beyond_the_plan_is_claimed_too(self):
		# Not on the trip's record, only in a trolley run 6 takes: planned 0, loaded 1.
		self.world.trips["BRT-6"]["plan"] = {("OPL-G", FARM): 0}
		self.world.trips["BRT-6"]["loaded_buckets"] = 0
		with ExitStack() as stack:
			self.world.patches(stack)
			self.assertEqual(tc._bucket_claims(["OPL-G"]), {("OPL-G", FARM): 1})

	def test_1b_save_trip_refuses_it_on_another_trip(self):
		res = self._save()
		self.assertEqual(res["reason"], "over_claimed")
		self.assertEqual(res["conflicts"][0]["free"], 0)

	def test_1b_save_trip_refuses_it_again_on_its_own_run(self):
		runs = [{"route": "R1", "run": 6, "trip": "BRT-6", "stops": [FARM]}]
		res = self._save(runs=runs, route="R1", run=6)
		self.assertEqual(res["reason"], "over_claimed")

	def test_1b_save_trip_refuses_raising_its_own_plan_beyond_the_bucket(self):
		res = self._save(name="BRT-6", buckets=2)
		self.assertEqual(res["reason"], "over_claimed")
		self.assertEqual((res["conflicts"][0]["wanted"], res["conflicts"][0]["free"]), (2, 1))

	def test_1b_validate_refuses_raising_the_plan(self):
		doc = frappe.new_doc("Bucket Request Trip")
		doc.name, doc.status, doc.trip_date = "BRT-6", "Draft", self.today
		doc.append("orders", {"order_pick_list": "OPL-G", "farm": FARM, "buckets": 2, "loaded_buckets": 1})
		before = frappe._dict(
			orders=[frappe._dict(order_pick_list="OPL-G", farm=FARM, buckets=1)], trip_buckets=[]
		)
		doc.is_new = lambda: False
		doc.get_doc_before_save = lambda: before
		with ExitStack() as stack:
			self.world.patches(stack)
			with self.assertRaises(frappe.ValidationError):
				doc.validate_no_double_booking()

	def test_1c_departed_farm_claims_what_it_loaded(self):
		self.world.trips = {
			"BRT-7": {
				"status": "Scheduled",
				"plan": {("OPL-G", FARM): 3, ("OPL-G", "Torongo"): 2},
				"loaded_buckets": 5,
				"departed": FARM,
			},
			"BRT-8": {"status": "Dispatched", "plan": {("OPL-G", FARM): 4}},
		}
		# 2 loaded at the departed farm; 3 at Torongo, where it is still loading.
		self.world.loads = {"BRT-7": {("OPL-G", FARM): 2, ("OPL-G", "Torongo"): 3}}
		with ExitStack() as stack:
			self.world.patches(stack)
			self.assertEqual(tc._trip_claims(["OPL-G"]), {("OPL-G", FARM): 2, ("OPL-G", "Torongo"): 3})
			# Still to load: nothing at the farm it left, none beyond Torongo's plan.
			self.assertEqual(tc._trip_claims(["OPL-G"], unloaded=True), {})


class UnitTestMidnightReturnToFarm(UnitTestCase):
	"""A trip that never left closes at midnight: its loaded bucket comes off trolley and
	truck but is NOT put back on a shelf nor set waiting for a truck -- it is kept as an
	anomaly (left overnight) for someone to look into."""

	def setUp(self):
		self.today = str(frappe.utils.today())
		self.tomorrow = str(frappe.utils.add_days(self.today, 1))

	def _return(self, due, transit_truck="KDA 001A"):
		doc = frappe._dict(name="BRT-6", vehicle="KDA 001A")
		self.tb = frappe._dict(bucket="ZL0141", order_pick_list="OPL-G", farm=FARM, db_set=MagicMock())
		self.row = frappe._dict(
			name="pli-1", parent="OPL-G", shelf="", transit_truck=transit_truck, trolley_id="TRL-1"
		)
		with (
			patch.object(tc.frappe.db, "sql", return_value=[self.row]),
			patch.object(tc.frappe.db, "has_column", return_value=True),
			patch.object(tc.frappe.db, "get_value", return_value="SO-G"),
			patch.object(tc, "_opl_delivery_dates", return_value={"OPL-G": due}),
			patch.object(tc.frappe.db, "set_value") as set_value,
			patch.object(tc, "_restore_farm_shelf") as restore,
		):
			out = tc._return_loads_to_farm(doc, [self.tb], self.today)
		self.restore = restore
		return out, set_value

	def test_2a_loaded_bucket_is_kept_as_anomaly_not_reshelved(self):
		out, set_value = self._return(due=self.tomorrow)
		self.assertEqual(out, {"ZL0141": {"due": self.tomorrow, "so": "SO-G", "trolley": "TRL-1"}})
		self.tb.db_set.assert_called_once_with("off_truck", 1, update_modified=False)
		self.restore.assert_not_called()
		set_value.assert_called_once_with(
			"Pick List Item",
			"pli-1",
			{"loaded_in_trolley": 0, "loaded_on_truck": 0, "trolley_id": None, "transit_truck": None},
		)

	def test_2a_trip_comment_says_kept_as_anomaly(self):
		out, _set_value = self._return(due=self.today)
		doc = frappe._dict(trip_date=self.today)
		self.assertEqual(
			tc._close_comment(doc, "Draft", [self.tb], out),
			"Closed at the end of {0} (Draft) — 1 loaded bucket(s) kept as anomaly (left overnight, "
			"not carried over): ZL0141 (order due {0})".format(self.today),
		)

	def test_2a_delivery_label_in_transit_truck_is_kept(self):
		_out, set_value = self._return(due=self.tomorrow, transit_truck="SIM Truck")
		values = set_value.call_args[0][2]
		self.assertNotIn("transit_truck", values)
		self.assertNotIn("awaiting_transfer", values)
		self.assertNotIn("shelf", values)


class UnitTestMidnightCleanSlate(UnitTestCase):
	"""After the trips of an earlier day close, nothing of that day's trolleys or trucks
	survives: a bucket still in a trolley (never loaded) comes off it -- its saved trolley
	is gone -- and is kept as an anomaly, not re-shelved and not waiting for a truck; a
	bucket on the road (in transit on a Dispatched trip) stays as it is."""

	def setUp(self):
		self.today = str(frappe.utils.today())
		self.tomorrow = str(frappe.utils.add_days(self.today, 1))
		self.yesterday = str(frappe.utils.add_days(self.today, -1))

	def _clear(self, due, left=None, planning_trip="BRT-5"):
		"""clear_left_trolleys for today with `left` the rows its query finds."""
		if left is None:
			left = [
				frappe._dict(
					name="pli-1", opl="OPL-G", bucket="ZL0141", trolley="TRL-1", lot=0, farm=FARM, truck=""
				)
			]
		pick_row = frappe._dict(
			name="pli-1", parent="OPL-G", shelf="TRG-B7B", transit_truck="SIM Truck", trolley_id="TRL-1"
		)
		self.queries = []

		def sql(query, values=None, **kwargs):
			self.queries.append(query)
			if "JOIN `tabOrder Pick List` o" in query:
				return left
			if query.startswith("SELECT 1 FROM `tabPick List Item`"):
				return [(1,)]
			if "`tabBucket Request Trip Order` o" in query:
				return [frappe._dict(name=planning_trip, farm=FARM)] if planning_trip else []
			return [pick_row]

		with (
			patch.object(tc.frappe.db, "sql", side_effect=sql),
			patch.object(tc.frappe.db, "has_column", return_value=True),
			patch.object(tc.frappe.db, "get_value", return_value="SO-G"),
			patch.object(tc.frappe.db, "commit") as commit,
			patch.object(tc, "_opl_delivery_dates", return_value={"OPL-G": due}),
			patch.object(tc.frappe.db, "set_value") as set_value,
			patch.object(tc, "_restore_farm_shelf") as restore,
			patch.object(tc, "log_transfer_event") as event,
			patch.object(tc.frappe, "get_doc", return_value=MagicMock()) as get_doc,
		):
			out = tc.clear_left_trolleys(self.today)
		self.restore, self.event, self.get_doc, self.commit = restore, event, get_doc, commit
		return out, set_value

	def _comments(self):
		return {c[0][0]["reference_doctype"]: c[0][0]["content"] for c in self.get_doc.call_args_list}

	def test_a_trolley_bucket_is_kept_as_anomaly_not_reshelved(self):
		out, set_value = self._clear(due=self.tomorrow)
		self.assertEqual(out, ["ZL0141"])
		self.restore.assert_not_called()
		# Off the trolley (its saved trolley is gone), not waiting, shelf untouched.
		set_value.assert_called_once_with(
			"Pick List Item", "pli-1", {"loaded_in_trolley": 0, "loaded_on_truck": 0, "trolley_id": None}
		)
		self.assertEqual(self.event.call_args[0][1], "Anomaly: left overnight")
		self.assertEqual(self.event.call_args[1]["trip"], "BRT-5")
		self.assertEqual(
			self.event.call_args[1]["details"],
			"left in trolley overnight; left at the end of {0}; trolley TRL-1; trip BRT-5; "
			"sales order SO-G; delivery {1}".format(self.yesterday, self.tomorrow),
		)
		comments = self._comments()
		self.assertEqual(
			comments["Order Pick List"],
			"Left in a trolley or on a truck at the end of {0} — 1 bucket(s) kept as anomaly (left "
			"overnight, not carried over): ZL0141 (left in trolley overnight, order due {1})".format(
				self.yesterday, self.tomorrow
			),
		)
		self.assertEqual(
			comments["Bucket Request Trip"],
			"Kept as anomaly at the end of {0} — 1 bucket(s) left in a trolley or on a truck: ZL0141 "
			"(left in trolley overnight, OPL-G, order due {1})".format(self.yesterday, self.tomorrow),
		)
		# One commit for the bucket, one per comment.
		self.assertEqual(self.commit.call_count, 3)

	def test_a_due_order_is_kept_the_same_way(self):
		_out, set_value = self._clear(due=self.today, planning_trip=None)
		values = set_value.call_args[0][2]
		self.assertNotIn("awaiting_transfer", values)
		self.assertNotIn("shelf", values)
		self.restore.assert_not_called()
		self.assertEqual(self.event.call_args[0][1], "Anomaly: left overnight")
		self.assertIsNone(self.event.call_args[1]["trip"])
		self.assertNotIn("Bucket Request Trip", self._comments())

	def test_a_bucket_flagged_on_a_truck_is_loaded_on_a_truck_that_never_left(self):
		left = [
			frappe._dict(
				name="pli-1", opl="OPL-G", bucket="ZL0141", trolley="", lot=1, farm=FARM, truck="KDA 001A"
			)
		]
		_out, set_value = self._clear(due=self.tomorrow, left=left)
		self.assertTrue(self.event.call_args[1]["details"].startswith("loaded on a truck that never left;"))
		self.assertIn("truck KDA 001A", self.event.call_args[1]["details"])
		self.assertEqual(self.event.call_args[1]["vehicle"], "KDA 001A")

	def test_a_only_earlier_rows_not_on_the_road_are_taken(self):
		self._clear(due=self.tomorrow)
		query = self.queries[0]
		for cond in (
			"IFNULL(pli.in_transit, 0) = 0",
			"IFNULL(pli.shelved, 0) = 0",
			"pli.modified < %(before)s",
			"t.status != 'Received'",
		):
			self.assertIn(cond, query)

	def test_a_one_failure_does_not_stop_the_rest(self):
		rows = [
			frappe._dict(
				name="pli-1", opl="OPL-G", bucket="BAD001", trolley="TRL-1", lot=0, farm=FARM, truck=""
			),
			frappe._dict(
				name="pli-2", opl="OPL-G", bucket="ZL0141", trolley="TRL-1", lot=0, farm=FARM, truck=""
			),
		]
		calls = []

		def keep(bucket, *args, **kwargs):
			calls.append(bucket)
			if bucket == "BAD001":
				raise Exception("boom")
			return {"due": self.tomorrow, "so": "SO-G", "trolley": "TRL-1"}

		with (
			patch.object(tc.frappe.db, "sql", side_effect=[rows, [(1,)], [], [(1,)], []]),
			patch.object(tc.frappe.db, "has_column", return_value=True),
			patch.object(tc.frappe.db, "commit"),
			patch.object(tc.frappe.db, "rollback") as rollback,
			patch.object(tc.frappe, "log_error") as log_error,
			patch.object(tc, "keep_as_anomaly", side_effect=keep),
			patch.object(tc, "log_transfer_event"),
			patch.object(tc.frappe, "get_doc", return_value=MagicMock()),
		):
			out = tc.clear_left_trolleys(self.today)
		self.assertEqual(calls, ["BAD001", "ZL0141"])
		self.assertEqual(out, ["ZL0141"])
		rollback.assert_called_once()
		log_error.assert_called_once()

	def _close(self, trips, left=()):
		with (
			patch.object(tc.frappe, "get_all", return_value=[t.name for t in trips]),
			patch.object(tc.frappe, "get_doc", side_effect=list(trips)),
			patch.object(tc, "_settle_held_trip", return_value=False),
			patch.object(tc, "_receive_trip") as receive,
			patch.object(
				tc, "keep_as_anomaly", return_value={"due": self.today, "so": "SO-G", "trolley": ""}
			) as keep,
			patch.object(tc, "back_to_farm_shelf") as back,
			patch.object(tc, "log_transfer_event") as event,
			patch.object(tc.frappe.db, "sql", return_value=list(left)) as sql,
			patch.object(tc.frappe.db, "has_column", return_value=True),
			patch.object(tc.frappe.db, "commit") as commit,
			patch.object(tc.frappe.db, "set_value") as set_value,
		):
			closed = tc.close_previous_day_trips(self.today)
		return frappe._dict(
			closed=closed,
			receive=receive,
			keep=keep,
			back=back,
			event=event,
			sql=sql,
			commit=commit,
			set_value=set_value,
		)

	def _trip(self, status):
		tb = frappe._dict(
			bucket="ZL0141", order_pick_list="OPL-G", farm=FARM, shelved=0, off_truck=0, db_set=MagicMock()
		)
		doc = MagicMock()
		doc.name, doc.status, doc.vehicle, doc.trip_date = "BRT-7", status, "KDA 001A", self.yesterday
		doc.get.return_value = [tb]
		return doc

	def test_trip_that_never_left_keeps_its_loads_as_anomalies(self):
		doc = self._trip("Scheduled")
		r = self._close([doc])
		self.assertEqual(r.closed, ["BRT-7"])
		r.keep.assert_called_once_with("ZL0141", "OPL-G", FARM, "KDA 001A")
		r.back.assert_not_called()
		self.assertEqual(r.event.call_args[0][1], "Anomaly: left overnight")
		self.assertEqual(
			r.event.call_args[1]["details"],
			"loaded on a truck that never left; left at the end of {0}; trip BRT-7; truck KDA 001A; "
			"sales order SO-G; delivery {1}".format(self.yesterday, self.today),
		)
		self.assertIn("kept as anomaly", doc.add_comment.call_args[0][1])

	def test_b_in_transit_bucket_on_a_dispatched_trip_is_untouched(self):
		r = self._close([self._trip("Dispatched")])
		self.assertEqual(r.closed, ["BRT-7"])
		r.keep.assert_not_called()
		r.back.assert_not_called()
		r.set_value.assert_not_called()
		self.assertEqual(r.event.call_args[0][1], "Not shelved when trip ended")
		# The clean-slate sweep never takes a bucket on the road.
		self.assertIn("IFNULL(pli.in_transit, 0) = 0", r.sql.call_args[0][0])

	def test_d_nothing_to_close_is_a_no_op(self):
		r = self._close([])
		self.assertEqual(r.closed, [])
		r.receive.assert_not_called()
		r.keep.assert_not_called()
		r.event.assert_not_called()
		r.commit.assert_not_called()
		r.set_value.assert_not_called()
		r.sql.assert_called_once()  # the sweep's one query, finding nothing
