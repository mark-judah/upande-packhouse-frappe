# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Automatic remote transfer scheduling — Production Settings > "Enable Automatic
# Remote Transfers Scheduling", on the Remote Transfers tab. At the frequency set
# there ("Re-plan Every (Minutes)"; hooks.py fires every 5 minutes and run() skips
# until that much time has passed since the last run) this
# does, server-side, what a planner does on the Transfer Scheduling page:
#
#   1. Demand: every team's scheduled Order Pick Lists with buckets still open at a
#      remote farm (the same getTransferScheduleData feed the page reads).
#   2. Distribute across teams: each team's schedule is a queue — it packs #1 before
#      #2 — so orders are taken round by round (every team's next order, smallest
#      first), a team stops at an order no truck can reach, and the first round
#      that no longer fits whole fills the remaining truck space with a partial
#      load and ends the plan. Port of computeDistribution in transfer-control.html.
#   3. Route: a truck without a hand-set route today is routed from the farms its
#      load is at — the real road legs out from the hub, every farm passed on the
#      way included, cheapest visiting order.
#   4. Save Draft trips (and the routes) through the same validation the page uses —
#      one trip per run: a hand-set route's runs (packhouse → farms → packhouse) are
#      each planned as a full truck that serves only that run's farms.
#
# Ownership: whatever this saves is flagged auto_planned. Each run replaces only
# its own still-Draft trips and routes; a trip or route a person saves is theirs
# and is never touched, and a truck with a person's trip today is left alone.
# Dispatch stays manual — only a person knows the truck has actually left.

import itertools
from collections import deque

import frappe

from upande_packhouse.api.remote_transfer import transfer_scheduling as tc
from upande_packhouse.api.remote_transfer import truck_routes

SETTING = "auto_remote_transfer_scheduling"
FREQUENCY = "auto_transfer_frequency"
DEFAULT_FREQUENCY_MINUTES = 10
#: A cron tick lands a little after the interval (the last run's own duration);
#: without slack a 10-minute setting would drift to every 15.
TICK_SLACK_SECONDS = 90
LAST_RUN_KEY = "upande_packhouse:auto_transfer:last_run"
LOCK_KEY = "upande_packhouse:auto_transfer:lock"
#: Set when a change lands while a run is going: that run plans once more at its end.
RERUN_KEY = "upande_packhouse:auto_transfer:rerun"
#: Stops beyond this are visited in road order instead of trying every permutation.
MAX_PERMUTED_STOPS = 7


def enabled():
	return bool(frappe.get_cached_doc("Production Settings").get(SETTING))


def frequency_minutes():
	return frappe.utils.cint(frappe.get_cached_doc("Production Settings").get(FREQUENCY)) or (
		DEFAULT_FREQUENCY_MINUTES
	)


def status():
	"""For the page: is automatic scheduling on, how often, and what did its last run do."""
	return {
		"enabled": 1 if enabled() else 0,
		"frequency": frequency_minutes(),
		"last_run": frappe.cache().get_value(LAST_RUN_KEY),
	}


def run():
	"""Scheduler entry point (every 5 minutes): re-plans once the configured
	frequency has passed since the last run."""
	if not enabled():
		return
	last = (frappe.cache().get_value(LAST_RUN_KEY) or {}).get("at")
	if last:
		elapsed = frappe.utils.time_diff_in_seconds(frappe.utils.now(), last)
		if elapsed + TICK_SLACK_SECONDS < frequency_minutes() * 60:
			return
	plan()


@frappe.whitelist(methods=["POST"])
def runNow():
	"""Re-plan immediately (the page's "Re-plan now" button)."""
	if not enabled():
		return {"status": "error", "message": "Automatic remote transfer scheduling is switched off."}
	return plan()


def replan_soon(doc=None, method=None):
	"""An order was scheduled (or its waiting buckets changed): put it on a trip now,
	in the background, instead of waiting for the next timed run. Repeated triggers
	share one queued job; one landing mid-run makes that run plan again at its end."""
	if not enabled():
		return
	cache = frappe.cache()
	if cache.get(cache.make_key(LOCK_KEY)):
		cache.set(cache.make_key(RERUN_KEY), 1, ex=600)
	frappe.enqueue(
		"upande_packhouse.api.auto_transfer.plan",
		queue="short",
		job_id="upande_packhouse:auto_transfer:replan",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def order_pick_list_changed(doc, method=None):
	"""Re-plan when a picklist is new, changes team, or its buckets waiting at a farm change."""

	def waiting(d):
		return sorted(
			(r.get("bucket") or "", r.get("source_warehouse") or "")
			for r in d.get("table_ytkc") or []
			if r.get("bucket") and int(r.get("awaiting_transfer") or 0)
		)

	before = doc.get_doc_before_save()
	if before is None:
		if waiting(doc):
			replan_soon()
		return
	if (before.get("team") or "") != (doc.get("team") or "") or waiting(before) != waiting(doc):
		replan_soon()


def plan():
	cache = frappe.cache()
	# One run at a time — a manual "Re-plan now" can land while the cron run is going.
	if not cache.set(cache.make_key(LOCK_KEY), 1, ex=600, nx=True):
		cache.set(cache.make_key(RERUN_KEY), 1, ex=600)
		return {"status": "busy", "message": "Automatic scheduling is already running."}
	try:
		for _ in range(3):
			cache.delete(cache.make_key(RERUN_KEY))
			summary = _plan()
			if not cache.get(cache.make_key(RERUN_KEY)):
				break
	except Exception:
		frappe.db.rollback()
		frappe.log_error("Automatic remote transfer scheduling failed", frappe.get_traceback())
		summary = {"status": "error", "message": "The run failed — see Error Log."}
	finally:
		cache.delete(cache.make_key(LOCK_KEY))
	summary["at"] = str(frappe.utils.now())
	cache.set_value(LAST_RUN_KEY, summary, expires_in_sec=86400)
	return summary


# ============================================================
# THE RUN
# ============================================================
def _plan():
	hub = tc.transfer_hub(required=False)
	if not hub:
		return {"status": "error", "message": "Set Remote Transfer Hub Farm in Production Settings."}

	today = frappe.utils.today()
	# Same delivery window the page opens on: tomorrow's orders are packed today.
	window = frappe.utils.add_days(today, 1)
	data = tc._transfer_schedule_payload(window, window)

	graph = _RoadGraph(hub, data["distances"])
	# Farms other trucks already collect from today (its own drafts are re-planned):
	# one truck per farm at a time — see transfer_scheduling.farm_holders.
	graph.holders = tc.farm_holders(today, skip_auto_drafts=True)
	trucks = _fleet(data, today)
	orders = _open_orders(data)
	# Bring only what the hub has shelf space for (its own drafts are re-planned).
	space = tc.hub_shelf_space(skip_auto_drafts=True)
	dist = _distribute(orders, trucks, graph, budget=space["free"], moving=_moving(data))

	loads = [t for t in trucks if t["rows"]]
	summary = {
		"status": "success",
		"window": str(window),
		"trips": [],
		"failed": [],
		"unreachable": [
			{"opl": o["opl"], "order_name": o["order_name"], "team": o["team"]} for o in dist["unreachable"]
		],
		"held": len(dist["held"]),
		"step": dist.get("step"),
		"partial": [p["order_name"] for p in dist["partial"]],
		"waiting": [
			{"order_name": w["order_name"], "team": w["team"], "why": w["why"]} for w in dist["waiting"]
		],
		"hub_space": space,
		"buckets": sum(r["buckets"] for t in loads for r in t["rows"]),
	}

	if _signature(loads) == _current_signature(today):
		summary["unchanged"] = 1
		return summary

	_clear_own_plan(today)
	for t in loads:
		chain = []
		if t["auto_route"]:
			chain, legs = graph.route_for(t["stops"])
			res = truck_routes._save_route(today, t["vehicle"], legs, auto_planned=1)
			if res.get("status") != "success":
				summary["failed"].append({"vehicle": t["vehicle"], "message": res.get("message")})
				continue
		res = tc._save_trip(
			name=None,
			vehicle=t["vehicle"],
			trip_date=today,
			status="Draft",
			notes="Auto-planned for deliveries on {0}.".format(window),
			collection_order=" → ".join([hub] + chain + [hub]) if chain else "",
			farm="",
			rows=t["rows"],
			auto_planned=1,
			route=t.get("route"),
			run=t.get("run"),
		)
		if res.get("status") == "success":
			summary["trips"].append(
				{
					"name": res["name"],
					"vehicle": t["vehicle"],
					"buckets": res["total_buckets"],
					"run": t.get("run"),
					"farms": ", ".join(sorted({r.get("farm") for r in t["rows"] if r.get("farm")})),
					"stems": sum(int(r.get("stems") or 0) for r in t["rows"]),
					"orders": sorted({r.get("order_name") or r.get("order_pick_list") for r in t["rows"]}),
				}
			)
		else:
			summary["failed"].append({"vehicle": t["vehicle"], "message": res.get("message")})
	# Each re-plan that changed the trips is its own entry on the page's Distributed list.
	tc.log_distribution(window, [{**x, "trip": x["name"]} for x in summary["trips"]], source="Automatic")
	return summary


def _fleet(data, today):
	"""Trucks this run may plan: internal logistics trucks with a capacity, a route
	made for today, and no trip a person planned today. Each open run of the route is
	a full truck that only serves that run's farms."""
	manual = set(
		frappe.get_all(
			"Bucket Request Trip",
			filters={
				"trip_date": today,
				"status": ["in", tc.ACTIVE_TRIP_STATUSES],
				"auto_planned": 0,
			},
			pluck="vehicle",
		)
	)
	# A truck may run several routes a day; it serves every farm on its hand-made ones.
	routes = {}
	for r in data["routes"]:
		cur = routes.setdefault(r["vehicle"], {"auto_planned": 1, "farms": []})
		if not r.get("auto_planned"):
			cur["auto_planned"] = 0
			cur["farms"] = cur["farms"] + [f for f in r.get("farms") or [] if f not in cur["farms"]]
	trucks = []
	for v in data["vehicles"]:
		cap = int(v.get("capacity_buckets") or 0)
		if cap <= 0 or v["name"] in manual:
			continue
		route = routes.get(v["name"])
		if route and not route.get("auto_planned") and route.get("farms"):
			# A hand-set route: every run still open is a full truck that only serves
			# that run's farms (one trip per run). A run on the road is skipped.
			for r in v.get("runs") or []:
				if r.get("trip_status") in ("Dispatched", "Received") or not r.get("stops"):
					continue
				used = 0
				if r.get("trip"):
					t = frappe.db.get_value(
						"Bucket Request Trip",
						r["trip"],
						["total_buckets", "loaded_buckets", "auto_planned"],
						as_dict=True,
					)
					used = (
						int(t.loaded_buckets or 0)
						if t.auto_planned
						else max(int(t.total_buckets or 0), int(t.loaded_buckets or 0))
					)
				if cap - used > 0:
					trucks.append(
						_truck(
							v["name"],
							cap - used,
							set(r["stops"]),
							route=r["route"],
							run=r["run"],
							window=tc._trip_window(r["route"], today),
						)
					)
		# No route today = not working today (in the garage, or not needed): routes are
		# made for each day on the Truck routes tab, and only routed trucks are planned.
	# Biggest first, as the page's fleetSorted() does.
	trucks.sort(key=lambda t: -t["rem"])
	return trucks


def _truck(vehicle, rem, fixed, route=None, run=None, window=None):
	return {
		"vehicle": vehicle,
		"window": window,  # (from, to) the run is out — for one truck per farm at a time
		"route": route,
		"run": run,
		"rem": rem,
		"fixed": fixed,
		"auto_route": fixed is None,
		"stops": [],  # farms it collects from, in the order they were added
		"passes": set(),  # every farm on its roads (stops + on the way)
		"rows": [],
	}


def _open_orders(data):
	"""Scheduled orders with their still-open farm portions. Buckets on a person's
	planned trip today are covered; this run's own drafts are about to be replaced,
	so they cover nothing."""
	covered = {}
	for t in data["trips"]:
		if t["status"] in tc.ACTIVE_TRIP_STATUSES and not t["stale"] and not t.get("auto_planned"):
			for r in t["orders"]:
				key = (r["order_pick_list"], r["farm"])
				covered[key] = covered.get(key, 0) + int(r["buckets"] or 0)
	out = []
	for o in data["orders"]:
		if not o.get("schedule"):
			continue
		farms = []
		for f in o["farms"]:
			open_b = max(0, int(f["buckets"] or 0) - covered.get((o["opl"], f["farm"]), 0))
			if not open_b:
				continue
			farms.append(
				{
					"farm": f["farm"],
					"buckets": open_b,
					# Left behind by a truck that already came: planned first (_place_left_behind).
					"left_behind": min(open_b, int((f.get("left_behind") or {}).get("buckets") or 0)),
					# Quality-issue replacements requested ASAP: before anything else.
					"asap": min(open_b, int(f.get("asap") or 0)),
					"per_bucket": (f["stems"] / f["buckets"]) if f["buckets"] else 0,
					"varieties": ", ".join(v["variety"] for v in f["varieties"] if v["variety"]),
				}
			)
		if farms:
			out.append({**o, "open_farms": farms, "open": sum(f["buckets"] for f in farms)})
	return out


# ============================================================
# DISTRIBUTION ACROSS TEAMS  (port of computeDistribution)
# ============================================================
def _place_left_behind(orders, trucks, graph, key="left_behind", room=None):
	"""Buckets a truck left behind go FIRST — before any team's queue — on the next
	trip to their farm. What fits comes off the order's open portions. With
	key="asap" the same for quality-issue replacements requested ASAP, which go
	before even those. `room`: [free hub shelf space] (None = no limit), used up
	as they are placed -- they need a shelf at the hub like everything else."""
	for o in orders:
		for f in o["open_farms"]:
			n = min(f["buckets"], f.get(key) or 0)
			if room is not None:
				n = min(n, max(0, room[0]))
			if not n:
				continue
			for truck, k in _place_farm(trucks, f["farm"], n, graph):
				truck["rows"].append(
					{
						"order_pick_list": o["opl"],
						"order_name": o["order_name"],
						"customer": o.get("customer") or "",
						"farm": f["farm"],
						"varieties": f["varieties"],
						"buckets": k,
						"stems": round(f["per_bucket"] * k),
						"full_farm_buckets": f["buckets"],
						"is_partial": 1 if k < f["buckets"] else 0,
					}
				)
				f["buckets"] -= k
				o["open"] -= k
				if room is not None:
					room[0] -= k
		o["open_farms"] = [f for f in o["open_farms"] if f["buckets"] > 0]


def _loaded(trucks):
	return sum(r["buckets"] for t in trucks for r in t["rows"])


def _moving(data):
	"""opl -> {schedule, at_farms, on_road} for every scheduled order still moving
	(buckets at a farm or on the road): its sequence step hasn't arrived yet."""
	out = {}
	for o in data.get("orders") or []:
		k = int(o.get("schedule") or 0)
		at, road = int(o.get("total_buckets") or 0), int(o.get("on_road_buckets") or 0)
		if k and (at > 0 or road > 0):
			out[o["opl"]] = {"schedule": k, "at_farms": at, "on_road": road}
	return out


def _distribute(orders, trucks, graph, budget=None, moving=None):
	"""Plan the scheduled orders onto the trucks, ONE SEQUENCE STEP AT A TIME: step k
	is every team's schedule #k. The first step that hasn't fully arrived at the hub
	is planned -- its whole orders not on a truck yet, each on one truck where one can
	take it all -- and the next step waits until it has arrived, so the hub shelves
	one step while the line issues the one before. `budget`: free hub shelf space in
	buckets (None = no limit); nothing is brought that can't be shelved. Left-behind
	buckets and ASAP replacements go first. `moving`: _moving(data)."""
	room = None if budget is None else [budget]
	_place_left_behind(orders, trucks, graph, key="asap", room=room)
	_place_left_behind(orders, trucks, graph, room=room)
	if room is not None:
		budget = room[0]
	orders = [o for o in orders if o["open"] > 0 and int(o.get("schedule") or 0) > 0]
	by_opl = {o["opl"]: o for o in orders}
	unreachable = [
		o for o in orders if not any(_any_serves(trucks, f["farm"], graph) for f in o["open_farms"])
	]
	stuck = {o["opl"] for o in unreachable}
	# The step being worked on: the first with an order on the road, planned on a
	# truck, or still to plan on one that can reach it. An order no truck can reach
	# is reported but doesn't hold the next steps back -- one farm without a route
	# must never stop every team.
	steps = set()
	for opl, m in (moving or {}).items():
		o = by_opl.get(opl)
		if m["on_road"] > 0 or o is None or o["open"] < m["at_farms"] or opl not in stuck:
			steps.add(m["schedule"])
	for o in orders:
		if o["opl"] not in stuck:
			steps.add(int(o["schedule"]))
	if not steps:
		return {"unreachable": unreachable, "held": [], "partial": [], "waiting": [], "step": None}
	step = min(steps)
	held, waiting = [], []
	# A trip goes to its own farm(s) only, so orders are planned farm by farm: an order's
	# buckets at each farm are a portion for the trips to that farm. Every team's #step,
	# then every team's next step, and so on -- each portion on the first trip to its
	# farm with room (_place_in_trip_order), so a trip fills before the next one to that
	# farm gets anything. A team keeps its queue per farm: once one of its portions fits
	# on no trip, its later ones at that farm wait.
	blocked = set()

	def truck_room():
		return sum(max(0, int(t.get("rem") or 0)) for t in trucks)

	for k in sorted({int(o["schedule"]) for o in orders if int(o["schedule"]) >= step}):
		portions = [
			{**o, "open_farms": [f], "open": f["buckets"], "farm": f["farm"]}
			for o in orders
			if int(o["schedule"]) == k and o["opl"] not in stuck
			for f in o["open_farms"]
		]
		for o in sorted(portions, key=lambda o: o["open"]):
			key = (o.get("team") or "", o["farm"])
			if key in blocked or truck_room() <= 0 or (budget is not None and budget <= 0):
				blocked.add(key)
				o["why"] = "trucks full: waits for the next trip"
				(waiting if k == step else held).append(o)
				continue
			if budget is not None and o["open"] > budget:
				blocked.add(key)
				o["why"] = "hub shelf space: {0} bkt needed, {1} free".format(o["open"], max(0, budget))
				(waiting if k == step else held).append(o)
				continue
			scratch = _clone(trucks)
			placed = _place_in_trip_order(scratch, o, graph)
			if placed >= o["open"]:
				_commit(trucks, scratch)
				if budget is not None:
					budget -= o["open"]
			else:
				# No partial loads: what doesn't fit whole waits for the next run.
				blocked.add(key)
				o["why"] = "truck room at {0}: {1} bkt needed, {2} fit".format(o["farm"], o["open"], placed)
				(waiting if k == step else held).append(o)
	return {"unreachable": unreachable, "held": held, "partial": [], "waiting": waiting, "step": step}


def _trip_rank(trucks, graph, farms):
	"""Sort key putting trips in the order they are filled: every truck's trip 1, then
	trip 2, ...; within one round the truck already collecting from `farms` (see
	_tier), then fleet order -- the first truck takes the sequence until it is full,
	and the next truck picks up from there with what is at the farms it can still
	collect from."""
	pos = {id(t): i for i, t in enumerate(trucks)}
	return lambda t: (
		int(t.get("run") or 1),
		sum(_tier(t, f, trucks, graph) for f in farms),
		pos[id(t)],
	)


def _place_in_trip_order(trucks, order, graph):
	"""The whole order on the FIRST trip (in _trip_rank order) that serves all its
	farms and has room for it, so trips fill up with the earliest sequences before the
	next trip gets any. Only an order no single trip can take is split, farm by farm,
	earliest trips first. Returns the buckets placed (MUTATES trucks)."""
	farms = [f["farm"] for f in order["open_farms"]]
	fits = [
		t
		for t in trucks
		if t["rem"] >= order["open"]
		and all(_serves(t, f, graph) and not _clashes(t, f, trucks, graph) for f in farms)
	]
	if fits:
		truck = min(fits, key=_trip_rank(trucks, graph, farms))
		for f in order["open_farms"]:
			_load(truck, f["farm"], f["buckets"], graph)
			truck["rows"].append(_trip_row(order, f, f["buckets"]))
		return order["open"]
	placed = 0
	for f in order["open_farms"]:
		left = f["buckets"]
		elig = [
			t
			for t in trucks
			if t["rem"] > 0 and _serves(t, f["farm"], graph) and not _clashes(t, f["farm"], trucks, graph)
		]
		for t in sorted(elig, key=_trip_rank(trucks, graph, [f["farm"]])):
			if not left:
				break
			take = min(t["rem"], left)
			_load(t, f["farm"], take, graph)
			t["rows"].append(_trip_row(order, f, take))
			placed += take
			left -= take
	return placed


def _trip_row(order, f, n):
	return {
		"order_pick_list": order["opl"],
		"order_name": order["order_name"],
		"customer": order.get("customer") or "",
		"farm": f["farm"],
		"varieties": f["varieties"],
		"buckets": n,
		"stems": round(f["per_bucket"] * n),
		"full_farm_buckets": f["buckets"],
		"is_partial": 1 if n < f["buckets"] else 0,
	}


def _place_farm(trucks, farm, amount, graph):
	"""Put `amount` buckets of `farm` on trucks; returns [(truck, n), ...].

	Best fit first, as on the page: if one truck can take the whole portion, the one
	with the least room that still fits takes it (splitting a portion means the order
	waits for the slowest truck). Among trucks that fit, one already driving past the
	farm beats one already out on that branch of the road, which beats an idle one,
	which beats one going somewhere else — that is what keeps auto-routed trucks on
	one branch instead of criss-crossing. Only when
	no single truck fits is the portion split, biggest truck first."""
	if amount <= 0:
		return []
	elig = [
		t for t in trucks if t["rem"] > 0 and _serves(t, farm, graph) and not _clashes(t, farm, trucks, graph)
	]

	def tier(t):
		return _tier(t, farm, trucks, graph)

	fits = [t for t in elig if t["rem"] >= amount]
	if fits:
		best = min(fits, key=lambda t: (tier(t), t["rem"]))
		_load(best, farm, amount, graph)
		return [(best, amount)]
	out, left = [], amount
	for t in sorted(elig, key=lambda t: (tier(t), -t["rem"])):
		take = min(t["rem"], left)
		_load(t, farm, take, graph)
		out.append((t, take))
		left -= take
		if not left:
			break
	return out


def _tier(truck, farm, trucks, graph):
	"""How well `truck` suits collecting from `farm` (lower is better): the vehicle
	already collecting there, then one that drives past it, one already on that branch
	of the road, an idle one, and last one going somewhere else."""
	holders = {v for v, _trip, _w in getattr(graph, "holders", {}).get(farm, [])}
	if truck["vehicle"] in holders or any(
		farm in t["stops"] and t["vehicle"] == truck["vehicle"] for t in trucks
	):
		return -1
	if truck["fixed"] is not None or farm in truck["passes"]:
		return 0  # already drives past it
	if truck["passes"] & (set(graph.path(graph.hub, farm)[1]) - {farm}):
		return 1  # already on this branch — just drives a little further
	return 2 if not truck["stops"] else 3


def _load(truck, farm, n, graph):
	truck["rem"] -= n
	if farm not in truck["stops"]:
		truck["stops"].append(farm)
		truck["passes"] |= set(graph.path(graph.hub, farm)[1]) | {farm}


def _overlap(a, b):
	return not a or not b or (a[0] < b[1] and b[0] < a[1])


def _clashes(truck, farm, trucks, graph):
	"""Another truck of this plan already collects from `farm` while this one would be
	out. (A farm a SAVED trip already collects from is fine to plan: _save_trip puts
	those buckets on that trip instead.)"""
	return any(
		t["vehicle"] != truck["vehicle"] and farm in t["stops"] and _overlap(truck["window"], t["window"])
		for t in trucks
	)


def _serves(truck, farm, graph):
	if truck["fixed"] is not None:
		return farm in truck["fixed"]
	return graph.reaches(farm)


def _any_serves(trucks, farm, graph):
	return any(_serves(t, farm, graph) for t in trucks)


def _clone(trucks):
	return [
		{**t, "stops": list(t["stops"]), "passes": set(t["passes"]), "rows": list(t["rows"])} for t in trucks
	]


def _commit(trucks, scratch):
	for i, t in enumerate(scratch):
		trucks[i] = t


# ============================================================
# ROAD NETWORK  (port of roadPath / expandViaRoadLegs / bestRoute)
# ============================================================
class _RoadGraph:
	"""Farm Distance records flagged is_road_leg are the real roads (a tree rooted at
	the hub); every other record is a pairwise convenience figure, used only to score
	a visiting order, never to drive."""

	def __init__(self, hub, distances):
		self.hub = hub
		self.adj, self.km, self.leg_km = {}, {}, {}
		for d in distances:
			a, b = d.get("a"), d.get("b")
			if not (a and b):
				continue
			self.km[frozenset((a, b))] = float(d.get("km") or 0)
			if d.get("leg"):
				self.leg_km[d["name"]] = float(d.get("km") or 0)
				self.adj.setdefault(a, []).append((b, d["name"]))
				self.adj.setdefault(b, []).append((a, d["name"]))
		self._paths = {}

	def path(self, a, b):
		"""(leg names, farms passed excluding a / including b) along the road; ([], [])
		when no road joins them."""
		if a == b:
			return [], []
		key = (a, b)
		if key not in self._paths:
			self._paths[key] = self._bfs(a, b)
		return self._paths[key]

	def _bfs(self, a, b):
		seen, queue = {a}, deque([(a, [], [])])
		while queue:
			node, legs, farms = queue.popleft()
			for nxt, leg in self.adj.get(node, []):
				if nxt in seen:
					continue
				if nxt == b:
					return legs + [leg], [f for f in farms + [nxt] if f != self.hub]
				seen.add(nxt)
				queue.append((nxt, legs + [leg], farms + [nxt]))
		return [], []

	def reaches(self, farm):
		return farm == self.hub or bool(self.path(self.hub, farm)[0])

	def _dist(self, a, b):
		"""km actually driven between two stops: along the road legs when a road joins
		them, else the pairwise figure (a truck can't take a road that isn't there, so
		scoring Torongo→Kaptumbo by a direct 34 km would pick the wrong order)."""
		if a == b:
			return 0.0
		legs = self.path(a, b)[0]
		if legs:
			return sum(self.leg_km[leg] for leg in legs)
		return self.km.get(frozenset((a, b)))

	def _round_trip_km(self, order):
		seq = [self.hub, *order, self.hub]
		total = 0.0
		for a, b in itertools.pairwise(seq):
			d = self._dist(a, b)
			if d is None:
				return None
			total += d
		return total

	def route_for(self, stops):
		"""(visiting order, leg names) for a round trip hub → stops → hub. Every farm
		passed on the way is a stop too; the cheapest order wins."""
		farms = []
		for s in stops:
			for f in [*self.path(self.hub, s)[1], s]:
				if f != self.hub and f not in farms:
					farms.append(f)
		order = farms
		if 1 < len(farms) <= MAX_PERMUTED_STOPS:
			best = None
			for perm in itertools.permutations(farms):
				km = self._round_trip_km(perm)
				if km is not None and (best is None or km < best[0]):
					best = (km, list(perm))
			if best:
				order = best[1]
		seq = [self.hub, *order, self.hub]
		legs = []
		for a, b in itertools.pairwise(seq):
			legs += self.path(a, b)[0]
		return order, legs


# ============================================================
# OWN PLAN — compare / replace
# ============================================================
def _signature(loads):
	return sorted(
		(
			t["vehicle"],
			tuple(sorted((r["order_pick_list"], r["farm"], r["buckets"]) for r in t["rows"])),
		)
		for t in _by_vehicle(loads)
	)


def _by_vehicle(loads):
	"""Run loads of one truck as one entry (the stored drafts are compared per truck)."""
	out = {}
	for t in loads:
		out.setdefault(t["vehicle"], {"vehicle": t["vehicle"], "rows": []})["rows"] += t["rows"]
	return list(out.values())


def _own_drafts(today):
	# A draft whose truck is already being loaded is no longer the scheduler's to replace.
	return [
		t
		for t in frappe.get_all(
			"Bucket Request Trip",
			filters={"status": "Draft", "auto_planned": 1, "trip_date": ["<=", today]},
			fields=["name", "vehicle", "trip_date", "loaded_buckets"],
		)
		if not int(t.loaded_buckets or 0)
		and not frappe.db.exists("Bucket Request Trip Bucket", {"parent": t.name})
	]


def _current_signature(today):
	by_vehicle = {}
	for t in _own_drafts(today):
		if str(t.trip_date) != str(today):
			return None  # a stale draft from an earlier day — always replace
		rows = frappe.get_all(
			"Bucket Request Trip Order",
			filters={"parent": t.name, "parenttype": "Bucket Request Trip"},
			fields=["order_pick_list", "farm", "buckets"],
		)
		by_vehicle.setdefault(t.vehicle, []).extend(
			(r.order_pick_list, r.farm, int(r.buckets or 0)) for r in rows
		)
	return sorted((v, tuple(sorted(rows))) for v, rows in by_vehicle.items())


def _clear_own_plan(today):
	"""Delete this scheduler's undispatched drafts and today's routes it set — except
	the route of a truck that is on the road or carries a person's trip."""
	for t in _own_drafts(today):
		frappe.delete_doc("Bucket Request Trip", t.name, ignore_permissions=True, force=1)
	for r in frappe.get_all(
		"Bucket Logistics Route",
		filters={"route_date": today, "auto_planned": 1},
		fields=["name", "vehicle"],
	):
		busy = tc._vehicle_on_road(r.vehicle) or frappe.db.exists(
			"Bucket Request Trip",
			{"vehicle": r.vehicle, "trip_date": today, "status": ["in", tc.ACTIVE_TRIP_STATUSES]},
		)
		if not busy:
			frappe.delete_doc("Bucket Logistics Route", r.name, ignore_permissions=True, force=1)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
