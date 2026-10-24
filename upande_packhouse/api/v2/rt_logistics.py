# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Bucket Logistics v2 (remote-transfer-v2?tab=logistics).

The numbers are v1's, exactly: every day of the delivery-date range is read with
`api.remote_transfer.bucket_logistics.getBucketLogistics` (v1 at HEAD: one transfer
truck per bucket, trips close at midnight, every bucket counted once, packing stops
at what was issued) and the day's orders, by-farm counts, trips, runs, arrival times
and farms are used as v1 returned them. Nothing in v1 is edited.

What this module adds on top of v1:

* region / search filters (applied to v1's rows; the order-level counts are rebuilt
  from the farms kept, so they stay v1's per-farm numbers);
* the tile totals (`bucket_totals`) recomputed over the kept orders with v1's own
  stage definitions, every physical bucket counted once across the whole range;
* the truck's times per trip, trip leg and bucket: when it LEFT the remote farm, when
  it ARRIVED at the hub, and the minutes in transit (a trip still on the road shows
  the time so far; one not yet gone shows none). Per leg the times are the earliest
  Pick List Item stamps (`left_farm_at`, `arrived_at`, written by `transfer_steps`) of
  the buckets that rode that trip, else the trip's own `last_departed_at` /
  `dispatched_at` and `arrived_at`;
* the order's creator, Bucket Replacement rows, trip capacity on the runs.

Reads only: nothing here writes or commits.
"""

import frappe
from frappe.utils import add_days, get_datetime, getdate, now

from upande_packhouse.api.remote_transfer import bucket_logistics as v1
from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

MAX_DAYS = 62

# v1's source-farm expression and stage definitions (bucket_logistics.getBucketLogistics),
# used only for the whole-range totals query.
FARM_EXPR = "COALESCE(NULLIF(pli.farm, ''), NULLIF(SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1), ''))"
TRANSFER = (
	"(pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1 OR pli.shelved = 1"
	" OR pli.not_found = 1 OR pli.custom_ready_for_packing = 1 OR pli.issued = 1)"
)
_ready = "IFNULL(pli.custom_ready_for_packing, 0) = 1 OR IFNULL(pli.issued, 0) = 1"
_shelved = "IFNULL(pli.shelved, 0) = 1 OR " + _ready
_transit = "IFNULL(pli.in_transit, 0) = 1 OR " + _shelved
_truck = "IFNULL(pli.loaded_on_truck, 0) = 1 OR " + _transit
_trolley = "IFNULL(pli.loaded_in_trolley, 0) = 1 OR IFNULL(pli.trolley_id, '') != '' OR " + _truck
REACHED_SHELVED = "(" + _shelved + ")"
REACHED_TRANSIT = "(" + _transit + ")"
REACHED_TROLLEY = "(" + _trolley + ")"
REACHED_AWAITING = "(IFNULL(pli.awaiting_transfer, 0) = 1 OR " + _trolley + ")"
BKT = "COALESCE(NULLIF(UPPER(pli.bucket), ''), pli.name)"

SUM_KEYS = (
	"total",
	"awaiting",
	"trolley",
	"on_truck",
	"transit",
	"shelved",
	"ready",
	"issued",
	"issued_offline",
	"not_found",
	"arrived_hub",
)


def _minutes(a, b):
	"""Minutes from a to b; None when either is missing or b is before a."""
	if not a or not b:
		return None
	m = (get_datetime(b) - get_datetime(a)).total_seconds() / 60
	return None if m < 0 else round(m, 1)


def _s(v):
	return str(v) if v else ""


def _trip_state(status, left_at, arrived_at):
	"""not_left | in_transit | arrived."""
	if arrived_at:
		return "arrived"
	if left_at and status in ("Dispatched", "Received"):
		return "in_transit"
	return "not_left"


def _days(from_date, to_date):
	f = getdate(from_date) if from_date else getdate(add_days(frappe.utils.today(), 1))
	t = getdate(to_date) if to_date else f
	if f > t:
		f, t = t, f
	if (t - f).days >= MAX_DAYS:
		f = getdate(add_days(t, -(MAX_DAYS - 1)))
	out, d = [], f
	while d <= t:
		out.append(str(d))
		d = getdate(add_days(d, 1))
	return out


def _v1_day(day, team):
	"""v1's getBucketLogistics for one delivery date, returned as a dict (v1 writes
	frappe.response; the caller's own form_dict / response are put back)."""
	old_fd, old_resp = frappe.local.form_dict, frappe.local.response
	try:
		frappe.local.form_dict = frappe._dict(delivery_date=day, team=team or "")
		frappe.local.response = frappe._dict()
		v1.getBucketLogistics()
		return dict(frappe.local.response)
	finally:
		frappe.local.form_dict, frappe.local.response = old_fd, old_resp


def _rebuild(o, farms):
	"""An order restricted to the origin farms in `farms`: its counts are the sums of the
	kept farms' own (v1) counts."""
	keep = [f for f in o["by_farm"] if f["farm"] in farms]
	o = dict(o, by_farm=keep)
	for k in SUM_KEYS:
		o[k] = sum(f.get(k, 0) for f in keep)
	o["farm"] = ", ".join(f["farm"] for f in keep)
	vs = []
	for f in keep:
		for v in (f.get("varieties") or "").split(", "):
			if v and v not in vs:
				vs.append(v)
	o["varieties"] = ", ".join(vs)
	return o


@frappe.whitelist()
def get_logistics_view(from_date=None, to_date=None, region=None, farm=None, team=None, q=None):
	from upande_packhouse.api.v2.core import region as region_core

	hub = transfer_hub(required=False) or ""
	days = _days(from_date, to_date)
	orders, farms, runs, seen = [], set(), [], set()
	for day in days:
		r = _v1_day(day, team)
		orders += r.get("orders", [])
		farms |= set(r.get("farms", []))
		for run in r.get("runs", []):
			if run["name"] not in seen:
				seen.add(run["name"])
				runs.append(run)

	# Region / origin farm: v1's farm filter is the by-farm row, so it is applied to them.
	keep_farms = None
	region_farms = region_core.farms_for(region=region, farm=farm)
	if region_farms is not None:
		keep_farms = set(region_farms)
		farms = {f for f in farms if f in keep_farms}
		orders = [
			_rebuild(o, keep_farms) for o in orders if any(f["farm"] in keep_farms for f in o["by_farm"])
		]
	if q:
		ql = q.lower()
		orders = [
			o
			for o in orders
			if ql
			in " ".join(
				str(o.get(k) or "") for k in ("order_name", "customer", "sales_order", "opl", "varieties")
			).lower()
		]
	runs.sort(key=lambda t: (t["vehicle"] or "", t["trip_date"], t["run"] or 99, t["name"]))
	now_s = now()
	opls = tuple(o["opl"] for o in orders)

	# Tiles: every stage once per physical bucket over exactly the orders listed (v1's query).
	totals = {}
	if opls:
		sales_farms = set(
			frappe.get_all(
				"Shelf Locations",
				filters={"parent": "Production Settings", "sales_shelf": 1, "enabled": 1},
				pluck="farm",
			)
		)
		sales_farms.add(hub)
		p = {"opls": opls, "sales_farms": tuple(f for f in sales_farms if f) or ("",)}
		extra = ""
		if keep_farms is not None:
			extra = " AND " + FARM_EXPR + " IN %(keep)s"
			p["keep"] = tuple(keep_farms) or ("",)
		bt = frappe.db.sql(
			"SELECT COUNT(DISTINCT "
			+ BKT
			+ ") AS total"
			+ "".join(
				", COUNT(DISTINCT CASE WHEN " + cond + " THEN " + BKT + " END) AS " + k
				for k, cond in (
					("awaiting", REACHED_AWAITING),
					("trolley", REACHED_TROLLEY),
					("transit", REACHED_TRANSIT),
					("shelved", REACHED_SHELVED),
					("issued", "pli.issued = 1"),
					("not_found", "pli.not_found = 1"),
				)
			)
			+ " FROM `tabPick List Item` pli WHERE pli.parent IN %(opls)s AND pli.parenttype = 'Order Pick List' AND "
			+ TRANSFER
			+ " AND COALESCE("
			+ FARM_EXPR
			+ ", '') NOT IN %(sales_farms)s"
			+ extra,
			p,
			as_dict=True,
		)
		totals = {k: int(v or 0) for k, v in (bt[0] if bt else {}).items()}

	# Owners, replacements.
	owners, repl = {}, []
	if opls:
		for r in frappe.db.sql(
			"SELECT o.name, o.owner, u.full_name FROM `tabOrder Pick List` o"
			" LEFT JOIN `tabUser` u ON u.name = o.owner WHERE o.name IN %(opls)s",
			{"opls": opls},
			as_dict=True,
		):
			owners[r.name] = r.full_name or r.owner or ""
		if frappe.db.table_exists("Bucket Replacement"):
			repl = frappe.get_all(
				"Bucket Replacement",
				filters={"order_pick_list": ["in", list(opls)]},
				fields=[
					"name",
					"order_pick_list",
					"old_bucket",
					"new_bucket",
					"reason",
					"status",
					"reported_by",
					"reported_at",
				],
				limit_page_length=0,
			)
			for r in repl:
				r["reported_at"] = _s(r.get("reported_at"))

	# The truck's times.
	names = {x["trip"] for o in orders for x in o["trips"]} | {r["name"] for r in runs}
	trips, legs = {}, {}
	if names:
		for r in frappe.db.sql(
			"SELECT name, status, dispatched_at, last_departed_at, arrived_at FROM `tabBucket Request Trip`"
			" WHERE name IN %(n)s",
			{"n": tuple(names)},
			as_dict=True,
		):
			left = (r.last_departed_at or r.dispatched_at) if r.status in ("Dispatched", "Received") else None
			trips[r.name] = {"left_at": _s(left), "arrived_at": _s(r.arrived_at), "status": r.status}
	if names and opls:
		for r in frappe.db.sql(
			"SELECT tb.parent AS trip, pli.parent AS opl, "
			+ FARM_EXPR
			+ " AS farm, MIN(pli.left_farm_at) AS left_at, MIN(pli.arrived_at) AS arrived_at"
			" FROM `tabBucket Request Trip Bucket` tb"
			" JOIN `tabPick List Item` pli ON pli.parent = tb.order_pick_list AND pli.parenttype = 'Order Pick List'"
			" AND UPPER(pli.bucket) = UPPER(tb.bucket)"
			" WHERE tb.parenttype = 'Bucket Request Trip' AND tb.parent IN %(n)s AND tb.order_pick_list IN %(opls)s"
			" AND IFNULL(tb.off_truck, 0) = 0 GROUP BY tb.parent, pli.parent, farm",
			{"n": tuple(names), "opls": opls},
			as_dict=True,
		):
			legs[(r.trip, r.opl, r.farm or "")] = r
	for r in runs:
		tt = trips.get(r["name"]) or {}
		r["left_at"], r["arrived_at"] = tt.get("left_at", ""), tt.get("arrived_at", "")
		r["state"] = _trip_state(r["status"], r["left_at"], r["arrived_at"])
		r["transit_minutes"] = _minutes(
			r["left_at"], r["arrived_at"] or (now_s if r["state"] == "in_transit" else None)
		)
	trip_state = {}
	for o in orders:
		o["owner_name"] = owners.get(o["opl"], "")
		for x in o["trips"]:
			tt = trips.get(x["trip"]) or {}
			lg = legs.get((x["trip"], o["opl"], x["farm"])) or {}
			status = tt.get("status") or x["status"]
			left = _s(lg.get("left_at")) or tt.get("left_at", "")
			if status not in ("Dispatched", "Received") and not lg.get("left_at"):
				left = ""
			arrived = _s(lg.get("arrived_at")) or tt.get("arrived_at", "")
			x["left_at"], x["arrived_at"] = left, arrived
			x["state"] = _trip_state(status, left, arrived)
			x["transit_minutes"] = _minutes(left, arrived or (now_s if x["state"] == "in_transit" else None))
			trip_state[x["trip"]] = x["state"]

	# KPIs: v1's tiles (no-trip rule, at-farm formula, order transit) over the orders listed.
	k = {"orders": len(orders)}
	t0 = totals or {}
	k.update(
		buckets=t0.get("total", 0),
		shelved=t0.get("shelved", 0),
		transit=t0.get("transit", 0),
		trolley=t0.get("trolley", 0),
		awaiting=t0.get("awaiting", 0),
		issued=t0.get("issued", 0),
		not_found=t0.get("not_found", 0),
	)
	k["at_farm"] = max(0, k["awaiting"] - k["transit"] - k["trolley"])
	unsched = no_trip = 0
	durs = []
	for o in orders:
		at_farm = max(0, (o.get("awaiting") or 0) - (o.get("transit") or 0) - (o.get("trolley") or 0))
		if at_farm > 0:
			if not o.get("scheduled"):
				unsched += 1
			elif not any(t["status"] != "Received" for t in o["trips"]):
				no_trip += 1
		m = _minutes(o.get("initiated"), o.get("arrived"))
		if o.get("arrived") and m is not None:
			durs.append(m)
	k["not_scheduled"], k["without_trip"], k["no_trip"] = unsched, no_trip, unsched + no_trip
	k["avg_transit_minutes"] = round(sum(durs) / len(durs), 1) if durs else None
	k["arrived_orders"] = len(durs)
	k["replaced"] = len(repl)
	done = {}
	for o in orders:
		for x in o["trips"]:
			if x["state"] == "arrived" and x["transit_minutes"] is not None:
				done.setdefault(x["trip"], x["transit_minutes"])
	k["trips"] = len(trip_state)
	k["trips_not_left"] = sum(1 for s in trip_state.values() if s == "not_left")
	k["trips_in_transit"] = sum(1 for s in trip_state.values() if s == "in_transit")
	k["trips_arrived"] = sum(1 for s in trip_state.values() if s == "arrived")
	k["avg_truck_minutes"] = round(sum(done.values()) / len(done), 1) if done else None

	note = ""
	if region_farms is not None and not region_farms:
		note = "That region and farm do not overlap, so nothing matches."
	return {
		"success": True,
		"from_date": days[0],
		"to_date": days[-1],
		"orders": orders,
		"farms": sorted(farms),
		"runs": runs,
		"kpis": k,
		"bucket_totals": totals,
		"replacements": repl,
		"hub": hub,
		"now": now_s,
		"note": note,
	}


@frappe.whitelist()
def get_bucket_detail(opl=None, farm=None):
	"""v1's getBucketLogisticsDetail (one row per bucket with each stage's time and user),
	plus when the bucket's truck left the remote farm, arrived at the hub, and the minutes
	in transit."""
	old_fd, old_resp = frappe.local.form_dict, frappe.local.response
	try:
		frappe.local.form_dict = frappe._dict(opl=opl or "", farm=farm or "")
		frappe.local.response = frappe._dict()
		v1.getBucketLogisticsDetail()
		rows = list(frappe.local.response.get("buckets") or [])
	finally:
		frappe.local.form_dict, frappe.local.response = old_fd, old_resp
	trips = {}
	names = {b["trip"] for b in rows if b.get("trip")}
	if names:
		for r in frappe.db.sql(
			"SELECT name, status, dispatched_at, last_departed_at, arrived_at FROM `tabBucket Request Trip`"
			" WHERE name IN %(n)s",
			{"n": tuple(names)},
			as_dict=True,
		):
			left = (r.last_departed_at or r.dispatched_at) if r.status in ("Dispatched", "Received") else None
			trips[r.name] = (_s(left), _s(r.arrived_at))
	now_s = now()
	for b in rows:
		for k, v in list(b.items()):
			if k.endswith("_at") and v is not None:
				b[k] = str(v)
		tl, ta = trips.get(b.get("trip"), ("", ""))
		left = b.get("transit_at") or tl
		arrived = b.get("arrived_hub_at") or ta
		b["left_at"], b["arrived_at"] = left, arrived
		b["in_transit_now"] = 1 if left and not arrived and not b.get("shelved") else 0
		b["transit_minutes"] = _minutes(left, arrived or (now_s if b["in_transit_now"] else None))
	return {"success": True, "buckets": rows, "now": now_s}
