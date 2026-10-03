# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `transfer-control` page API — ported from the kaitet-group v15 LIVE
# reference (never migrated to v16). Field names are translated throughout to
# their real v16 equivalents (the v15 scripts used a "custom_" prefix on several
# fields — Pick List Item's awaiting_transfer/loaded_in_trolley/in_transit/shelved/
# transit_truck/trolley_id and Order Pick List's team/schedule_number/item_group/
# mix_group/farm/order_name are all now BARE fieldnames in v16). Order Pick List's
# old custom_is_mixed_box_pick_list/custom_status/custom_consignee no longer exist
# at all — "mixed" is now derived from Sales Order Item.custom_mixed_box/
# custom_mixed_bunch (same derivation Packhouse Scheduler already uses), status/
# consignee are simply dropped.
#
# NEW v16 doctypes backing this page (none of these existed before this build):
#   Bucket Request Trip (+ child Bucket Request Trip Order)
#   Farm Distance
#   Bucket Logistics Route (+ child Bucket Logistics Route Leg)
# NEW Vehicle fields: custom_trolley_capacity, custom_buckets_per_trolley,
#   custom_is_internal_logistics_truck.
#
# Farm Distance is currently seeded with a PLACEHOLDER star topology (every farm
# 10.0km direct from Kapkolia, except Kaptumbo which sits behind Simotwo per the
# one real adjacency documented in the v15 source) — flagged for the ops team to
# correct with real road distances before this is trusted for real dispatch.
#
# Bucket transfer state on a Pick List Item row, in lifecycle order:
#   awaiting_transfer=1          allocated from a remote (non-sales) shelf
#   in_transit=1 + transit_truck set when a Bucket Request Trip carrying it is
#                                dispatched (awaiting_transfer stays 1 so every
#                                existing "not yet at the sales farm" check —
#                                opl_submit_blockers, shelving self-heal — still
#                                holds the OPL back until it is shelved)
#   shelved=1, rest cleared      shelved at the sales farm (mobile shelveBucket)
#
# A bucket is OPEN for planning while it is awaiting, not yet on a truck and not
# shelved. Draft/Scheduled trips from TODAY claim open buckets; a dispatched trip
# no longer claims anything because its buckets are flagged in_transit instead.

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

# A transfer bucket's farm is where its stock is: the farm of its source warehouse
# (kept as the remote farm's cold store until the bucket is shelved at the packhouse),
# else the row's farm. The shelf's farm can disagree (a mislabelled shelf, an old row)
# and used to put a bucket on the wrong farm's trip.
FARM_EXPR = "COALESCE(NULLIF(SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1), ''), NULLIF(pli.farm, ''))"
# Pick-list creation pre-fills transit_truck with the ORDER's delivery truck label
# (Sales Order custom_truck, e.g. "SIM Truck", "RAMBO" — not even Vehicle records),
# the same field the transfer truck is written to on load/dispatch. A value only
# names a transfer truck when it is an internal Vehicle (not a customer dispatch
# truck); everything shown as "the truck" goes through this test.
TRANSFER_TRUCK_OK = (
	"EXISTS (SELECT 1 FROM `tabVehicle` v WHERE v.name = pli.transit_truck"
	" AND IFNULL(v.custom_dispatch_truck, 0) = 0)"
)

#: Trip statuses that still claim open buckets (planned, truck not yet gone).
ACTIVE_TRIP_STATUSES = ("Draft", "Scheduled")
# A pick row that is ready for packing or issued is at the packhouse, whatever its
# transfer flags still say (a bucket packed without being scanned in kept
# awaiting_transfer=1 and showed as waiting at the farm for good).
PACKED_SQL = "(IFNULL(pli.custom_ready_for_packing, 0) = 1 OR IFNULL(pli.issued, 0) = 1)"
#: How far back to look for trips that were never received (still on the road).
TRIP_LOOKBACK_DAYS = 7


def transfer_hub(required=True):
	"""The sales farm remote buckets are trucked to: Production Settings > Remote
	Transfer Hub Farm. Every truck route starts and ends here, and a transfer bucket
	only counts as arrived once it is shelved here.

	Unset, it falls back to the one enabled sales-shelf farm — but only when there is
	exactly one; with several (e.g. Kapkolia and Karen) there is no safe guess, so it
	throws (or returns None when not `required`)."""
	# .get() on the cached doc, not get_single_value: that throws on a site where
	# the custom field hasn't been migrated in yet.
	ps = frappe.get_cached_doc("Production Settings")
	hub = ps.get("custom_transfer_hub_farm")
	if hub:
		return hub
	sales_farms = [
		row.farm for row in (ps.shelf_locations or []) if row.enabled and row.sales_shelf and row.farm
	]
	if len(sales_farms) == 1:
		return sales_farms[0]
	if not required:
		return None
	frappe.throw(
		_(
			"Set <b>Remote Transfer Hub Farm</b> in Production Settings — the sales farm "
			"remote buckets are trucked to."
		)
	)


def _dt(value):
	"""A Datetime value as "YYYY-MM-DD HH:MM", or "" when blank."""
	return frappe.utils.get_datetime(value).strftime("%Y-%m-%d %H:%M") if value else ""


def _day_window(date):
	"""The whole-day window a route gets when no From/To is given."""
	return "{0} 00:00:00".format(date), "{0} 23:59:59".format(date)


def _hub_company_farms(hub):
	"""Farms of the hub farm's company — the ones a transfer truck can be routed through."""
	company = frappe.db.get_value("Farm", hub, "company") if hub else None
	filters = {"company": company} if company else {}
	return frappe.get_all("Farm", filters=filters, pluck="name", order_by="name")


def _auto_planning_status():
	# Lazy import: auto_transfer builds on this module.
	from upande_packhouse.api.auto_transfer import status

	return status()


def _bucket_state(row):
	# The flags are authoritative. A bucket that was never flagged for transfer is
	# already at its sales farm ("home"), whichever farm that is — keying this off
	# `farm == hub` mislabelled a sales farm other than the hub as remote.
	if int(row.get("shelved") or 0):
		return "home"
	if int(row.get("in_transit") or 0):
		return "transit"
	if int(row.get("loaded_in_trolley") or 0):
		return "loaded"
	if int(row.get("awaiting_transfer") or 0):
		return "farm"
	return "home"


def _mixed_map(opl_names):
	# Sales Order Item.custom_opl -> {box, bunch} — same derivation as getSchedulerFeed.
	mixed = {}
	if not opl_names:
		return mixed
	rows = frappe.get_all(
		"Sales Order Item",
		filters=[["custom_opl", "in", opl_names]],
		fields=["custom_opl", "custom_mixed_box", "custom_mixed_bunch"],
		limit_page_length=0,
	)
	for r in rows:
		op = r.get("custom_opl")
		m = mixed.setdefault(op, {"box": 0, "bunch": 0})
		if int(r.get("custom_mixed_box") or 0):
			m["box"] = 1
		if int(r.get("custom_mixed_bunch") or 0):
			m["bunch"] = 1
	return mixed


def _mixed_label(m):
	if not m:
		return "Straight box"
	if m.get("bunch"):
		return "Mixed bunch"
	if m.get("box"):
		return "Mixed box"
	return "Straight box"


def _schedule_map(lookback_days=14):
	# OPL -> {team, schedule} from the most recent Packhouse Schedule containing it
	# (schedule membership only — NOT tied to the delivery-date window; a schedule is
	# created the day before delivery, so gating on the delivery window misses it).
	# This is the ONE source of schedule/team for every transfer view: the per-team
	# sequence the scheduler saved, not Order Pick List.schedule_number (a separate
	# global number the mobile setSchedulerOrder writes).
	from_date = frappe.utils.add_days(frappe.utils.today(), -lookback_days)
	names = frappe.get_all(
		"Packhouse Schedule",
		filters={"schedule_date": [">=", from_date]},
		fields=["name", "schedule_date", "team"],
		order_by="schedule_date desc, modified desc",
	)
	out = {}
	for sc in names:
		rows = frappe.get_all(
			"Packhouse Schedule Order",
			filters={"parent": sc.name},
			fields=["order_pick_list", "sequence"],
		)
		for r in rows:
			op = r.get("order_pick_list")
			if op and op not in out:
				out[op] = {"team": sc.team, "schedule": int(r.get("sequence") or 0)}
	return out


def _transfer_buckets(opl_names):
	"""Every transfer bucket (ever flagged awaiting/loaded/in-transit, or shelved) of
	these OPLs, ONE entry per (opl, bucket): a bucket carrying several varieties has
	several Pick List Item rows, and counting rows double-counted it. Stems are
	summed across the rows; `names` are the row names (for flag updates)."""
	if not opl_names:
		return []
	rows = frappe.db.sql(
		"""
		SELECT pli.name AS name, pli.parent AS opl, pli.idx AS idx, pli.bucket AS bucket,
		       pli.item_code AS variety, pli.stock_qty AS stems, """
		+ FARM_EXPR
		+ """ AS farm,
		       pli.awaiting_transfer AS awaiting, pli.loaded_in_trolley AS loaded,
		       pli.in_transit AS in_transit,
		       GREATEST(IFNULL(pli.shelved, 0), IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0)) AS shelved,
		       pli.transit_truck AS truck
		FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1
		       OR pli.in_transit = 1 OR pli.shelved = 1)
		ORDER BY pli.parent, pli.idx
		""",
		{"opls": tuple(opl_names)},
		as_dict=True,
	)
	out, index = [], {}
	for r in rows:
		bucket = (r.get("bucket") or "").upper()
		key = (r["opl"], bucket or r["name"])
		entry = index.get(key)
		if entry is None:
			entry = {
				"opl": r["opl"],
				"bucket": r.get("bucket"),
				"variety": r.get("variety"),
				"farm": r.get("farm") or "",
				"stems": 0.0,
				"names": [],
				"awaiting": 0,
				"loaded": 0,
				"in_transit": 0,
				"shelved": 0,
				"truck": None,
			}
			index[key] = entry
			out.append(entry)
		entry["stems"] += float(r.get("stems") or 0)
		entry["names"].append(r["name"])
		for flag in ("awaiting", "loaded", "in_transit", "shelved"):
			if int(r.get(flag) or 0):
				entry[flag] = 1
		entry["truck"] = entry["truck"] or r.get("truck")
	for e in out:
		e["open"] = 1 if (e["awaiting"] or e["loaded"]) and not e["in_transit"] and not e["shelved"] else 0
		e["on_road"] = 1 if e["in_transit"] and not e["shelved"] else 0
	return out


def _open_counts(opl_names):
	"""(opl, farm) -> number of OPEN buckets (awaiting, not on a truck, not shelved)."""
	counts = {}
	for b in _transfer_buckets(opl_names):
		if b["open"]:
			key = (b["opl"], b["farm"])
			counts[key] = counts.get(key, 0) + 1
	return counts


def _trip_claims(opl_names, exclude_trip=None):
	"""(opl, farm) -> open buckets still claimed by planned trips — every active trip
	of today or later, plus any earlier one the truck already started (loads or a closed
	stop). Only an earlier day's untouched draft is ignored: its truck never went, so
	its buckets are open again (the dashboard flags it for cleanup). A row's loaded
	buckets are no longer open, so only its unloaded remainder is claimed."""
	if not opl_names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT o.order_pick_list AS opl, o.farm AS farm,
		       SUM(GREATEST(IFNULL(o.buckets, 0) - LEAST(IFNULL(o.loaded_buckets, 0), IFNULL(o.buckets, 0)), 0)) AS buckets
		FROM `tabBucket Request Trip Order` o
		JOIN `tabBucket Request Trip` t ON t.name = o.parent
		WHERE t.status IN %(active)s
		  AND (t.trip_date >= %(today)s OR IFNULL(t.loaded_buckets, 0) > 0 OR IFNULL(t.departed_stops, '') != '')
		  AND o.order_pick_list IN %(opls)s AND t.name != %(exclude)s
		GROUP BY o.order_pick_list, o.farm
		""",
		{
			"active": ACTIVE_TRIP_STATUSES,
			"today": frappe.utils.today(),
			"opls": tuple(opl_names),
			"exclude": exclude_trip or "",
		},
		as_dict=True,
	)
	return {(r.opl, r.farm or ""): int(r.buckets or 0) for r in rows}


def _truck_status(today):
	# Truck status — today's transit_truck activity only (an unbounded window pulls
	# in stale historical loads since the flag is set once and never cleared).
	# Per bucket the most-advanced phase counts; the latest-modified bucket places
	# the truck (same rule as the mobile getTransferScheduleData).
	ts_rows = frappe.db.sql(
		"""
		SELECT pli.transit_truck AS truck, """
		+ FARM_EXPR
		+ """ AS farm,
		       pli.awaiting_transfer AS aw, pli.loaded_in_trolley AS ld,
		       pli.in_transit AS tr, pli.shelved AS sh, pli.modified AS modified
		FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.transit_truck IS NOT NULL
		  AND pli.transit_truck != '' AND DATE(pli.modified) = %(today)s
		  AND """
		+ TRANSFER_TRUCK_OK
		+ """
		""",
		{"today": today},
		as_dict=True,
	)
	by_truck = {}
	for r in ts_rows:
		st = by_truck.setdefault(
			r["truck"],
			{
				"truck": r["truck"],
				"total": 0,
				"awaiting": 0,
				"loaded": 0,
				"in_transit": 0,
				"shelved": 0,
				"last": "",
				"last_phase": "",
				"farm": r.get("farm") or "",
			},
		)
		st["total"] += 1
		# Most-advanced phase first; None when the bucket carries no flag at all.
		phase = next(
			(
				name
				for flag, name in (
					("sh", "shelved"),
					("tr", "in_transit"),
					("ld", "loaded"),
					("aw", "awaiting"),
				)
				if int(r.get(flag) or 0)
			),
			None,
		)
		if phase is not None:
			st[phase] += 1
		mod = str(r.get("modified") or "")
		if mod > st["last"]:
			st["last"] = mod
			st["last_phase"] = phase
			st["farm"] = r.get("farm") or ""
	out = []
	for st in by_truck.values():
		st["loading_pct"] = (
			round((st["loaded"] + st["in_transit"] + st["shelved"]) / st["total"] * 100) if st["total"] else 0
		)
		lp = st.pop("last_phase")
		st["location"] = {
			"shelved": "arrived",
			"in_transit": "in_transit",
			"loaded": "loading",
			"awaiting": "loading",
		}.get(lp, "unknown")
		out.append(st)
	return out


def _trip_run_info(doc):
	"""Which run of which route this trip drives, for the dashboard and the farm app."""
	if not (doc.get("route") and doc.get("run")):
		return {
			"route": doc.get("route") or "",
			"run": 0,
			"runs": 0,
			"run_stops": [],
			"run_chain": "",
			"window": "",
		}
	runs = _route_runs_by_name(doc.route)
	run = next((r for r in runs if r["run"] == int(doc.run)), None)
	window = frappe.db.get_value("Bucket Logistics Route", doc.route, ["from_datetime", "to_datetime"])
	stops = run["stops"] if run else []
	return {
		"route": doc.route,
		"run": int(doc.run),
		"runs": len(runs),
		"run_stops": stops,
		"run_chain": _run_chain(stops) if stops else "",
		"window": "{0}–{1}".format(_dt(window[0])[11:], _dt(window[1])[11:]) if window and window[0] else "",
	}


def _trip_dict(doc, today):
	return {
		**_trip_run_info(doc),
		"name": doc.name,
		"vehicle": doc.vehicle,
		"trip_date": str(doc.trip_date),
		"status": doc.status,
		# A Draft/Scheduled trip from an earlier day never left: it claims nothing
		# any more and should be deleted or re-planned.
		"stale": 1
		if (
			str(doc.trip_date) < str(today)
			and doc.status in ACTIVE_TRIP_STATUSES
			and not _trip_has_loads(doc)
		)
		else 0,
		# Buckets are on the truck but it hasn't been dispatched yet.
		"loading": 1 if doc.status in ACTIVE_TRIP_STATUSES and _trip_has_loads(doc) else 0,
		"notes": doc.notes,
		"collection_order": doc.collection_order,
		"farm": doc.farm,
		"total_buckets": doc.total_buckets,
		"total_stems": doc.total_stems,
		"capacity_buckets": doc.capacity_buckets,
		"loaded_buckets": int(doc.get("loaded_buckets") or 0),
		"unscheduled": int(doc.get("unscheduled") or 0),
		"stops": _trip_stops(doc) if doc.status in ACTIVE_TRIP_STATUSES + ("Dispatched",) else [],
		"departed_stops": [f for f in (doc.get("departed_stops") or "").split(",") if f],
		"heading_to": doc.get("heading_to") or "",
		"last_departed_at": str(doc.last_departed_at) if doc.get("last_departed_at") else None,
		"tracked_buckets": len(doc.get("trip_buckets") or []),
		"shelved_buckets": sum(1 for b in doc.get("trip_buckets") or [] if b.shelved),
		"off_truck_buckets": sum(1 for b in doc.get("trip_buckets") or [] if b.off_truck and not b.shelved),
		"auto_planned": int(doc.get("auto_planned") or 0),
		"dispatched_at": str(doc.dispatched_at) if doc.dispatched_at else None,
		"received_at": str(doc.received_at) if doc.received_at else None,
		"orders": [
			{
				"order_pick_list": o.order_pick_list,
				"order_name": o.order_name,
				"customer": o.customer,
				"farm": o.farm,
				"varieties": o.varieties,
				"buckets": o.buckets,
				"stems": o.stems,
				"full_farm_buckets": o.full_farm_buckets,
				"is_partial": o.is_partial,
				"loaded_buckets": int(o.get("loaded_buckets") or 0),
				"loaded_stems": int(o.get("loaded_stems") or 0),
				"unscheduled": int(o.get("unscheduled") or 0),
			}
			for o in doc.orders
		],
	}


def _route_farms(legs):
	# Parenthesised on purpose: `a | b - {hub}` binds as `a | (b - ...)` and
	# kept the hub (every route's first from_farm) in the list.
	return sorted(({l.from_farm for l in legs} | {l.to_farm for l in legs}) - {transfer_hub(), None, ""})


def route_runs(legs, hub=None):
	"""A route's runs in driving order: [{"run": 1, "stops": [farm, ...], "legs": [...]}].

	A run leaves the packhouse, collects from its farms and ends back at the packhouse;
	the next run starts from the packhouse again (Kapkolia → Chepsito → Kapkolia →
	Simotwo → Kapkolia is two runs). Each run is driven as ONE trip. Derived from the
	legs (split where they come back to the hub), so routes saved before runs existed
	read the same way."""
	hub = hub or transfer_hub(required=False)
	runs, cur = [], None
	for leg in legs or []:
		if cur is None:
			cur = {"run": len(runs) + 1, "stops": [], "legs": []}
			runs.append(cur)
		cur["legs"].append(leg)
		to = leg.get("to_farm")
		if to and to != hub and to not in cur["stops"]:
			cur["stops"].append(to)
		if to == hub:
			cur = None
	return runs


def _route_runs_by_name(name):
	if not name or not frappe.db.exists("Bucket Logistics Route", name):
		return []
	legs = frappe.get_all(
		"Bucket Logistics Route Leg",
		filters={"parent": name, "parenttype": "Bucket Logistics Route"},
		fields=["from_farm", "to_farm", "leg", "distance_km"],
		order_by="idx asc",
	)
	return route_runs(legs)


def _run_of(route, run):
	return next((r for r in _route_runs_by_name(route) if r["run"] == int(run or 0)), None)


def _run_trip(route, run):
	"""The trip driving this run of the route (one trip per run), else None."""
	found = frappe.get_all(
		"Bucket Request Trip",
		filters={"route": route, "run": int(run or 0)},
		fields=["name", "status"],
		order_by="creation asc",
		limit=1,
	)
	return found[0] if found else None


def _day_runs(vehicle, date):
	"""Every run the truck drives on `date`, in driving order (route From time, then run)."""
	ensure_day_routes(date)
	out = []
	for r in frappe.get_all(
		"Bucket Logistics Route",
		filters={"vehicle": vehicle, "route_date": date},
		fields=["name", "from_datetime", "to_datetime"],
		order_by="from_datetime asc, name asc",
	):
		runs = _route_runs_by_name(r.name)
		for run in runs:
			trip = _run_trip(r.name, run["run"])
			out.append(
				{
					"route": r.name,
					"run": run["run"],
					"runs": len(runs),
					"stops": run["stops"],
					"from_datetime": _dt(r.from_datetime),
					"to_datetime": _dt(r.to_datetime),
					"trip": trip.name if trip else None,
					"trip_status": trip.status if trip else None,
				}
			)
	return out


def _run_open(run):
	"""A run can still take buckets: no trip yet, or its trip hasn't left."""
	return not run["trip"] or run["trip_status"] in ACTIVE_TRIP_STATUSES


def _run_chain(stops, hub=None):
	hub = hub or transfer_hub(required=False) or ""
	return " → ".join([hub] + list(stops) + [hub])


def _vehicle_on_road(vehicle, exclude_trip=None):
	"""The trip this truck is out on right now (Dispatched, not yet Received), else None.

	A truck on the road can't be routed, planned or dispatched again until it is back
	at its route's final destination — which is when its trip is received (by the
	Receive button, or automatically once the last bucket it carried is shelved)."""
	if not vehicle:
		return None
	filters = {"vehicle": vehicle, "status": "Dispatched"}
	if exclude_trip:
		filters["name"] = ["!=", exclude_trip]
	for name in frappe.get_all(
		"Bucket Request Trip", filters=filters, pluck="name", order_by="dispatched_at desc"
	):
		# Settle it first: a trip whose buckets are all shelved is over even when the
		# automatic end on shelving didn't run (old code on the server, an error), so
		# the truck must not stay "on the road" because of it.
		if not _settle_finished_trip(name):
			return name
	return None


def _settle_finished_trip(name):
	"""End a Dispatched trip whose buckets have all been shelved. Returns True if it is over."""
	doc = frappe.get_doc("Bucket Request Trip", name)
	if doc.status != "Dispatched":
		return doc.status == "Received"
	if doc.get("trip_buckets"):
		ended = _end_trip_if_shelved(doc)
	else:
		# Trip from before buckets were recorded: nothing it carried is still in transit.
		since = doc.dispatched_at or doc.trip_date
		left = frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabPick List Item`
			WHERE parenttype = 'Order Pick List' AND in_transit = 1 AND IFNULL(shelved, 0) = 0
			  AND transit_truck = %s AND modified >= DATE(%s)""",
			(doc.vehicle, since),
		)[0][0]
		ended = not left and bool(
			frappe.db.sql(
				"""SELECT 1 FROM `tabPick List Item` WHERE parenttype = 'Order Pick List'
				AND transit_truck = %s AND shelved = 1 AND modified >= DATE(%s) LIMIT 1""",
				(doc.vehicle, since),
			)
		)
		if ended:
			_receive_trip(name)
	if ended:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- read paths (GET) must keep the fix
	return ended


def _route_end(vehicle, date):
	"""Final destination of a truck's last route that day (last leg's to_farm), else the hub."""
	last_route = frappe.get_all(
		"Bucket Logistics Route",
		filters={"route_date": date, "vehicle": vehicle},
		pluck="name",
		order_by="from_datetime desc",
		limit=1,
	)
	name = last_route[0] if last_route else None
	if name:
		last = frappe.get_all(
			"Bucket Logistics Route Leg",
			filters={"parent": name, "parenttype": "Bucket Logistics Route"},
			fields=["to_farm"],
			order_by="idx desc",
			limit=1,
		)
		if last and last[0].to_farm:
			return last[0].to_farm
	return transfer_hub()


def _on_road_message(vehicle, trip, what):
	return "{0} is on the road (trip {1}) — it can be {2} once it is back at {3} and the trip is received.".format(
		vehicle, trip, what, _route_end(vehicle, frappe.utils.today())
	)


def _receive_trip(name):
	frappe.db.set_value(
		"Bucket Request Trip", name, {"status": "Received", "received_at": frappe.utils.now()}
	)


def _trip_has_loads(doc):
	"""True once buckets were loaded onto this trip's truck (mobile loading)."""
	return bool(doc.get("trip_buckets")) or int(doc.get("loaded_buckets") or 0) > 0


def _loaded_trip_refusal(name, what):
	doc = frappe.get_doc("Bucket Request Trip", name)
	if not _trip_has_loads(doc):
		return None
	return {
		"status": "error",
		"reason": "loading",
		"message": "Trip {0} already has {1} bucket(s) on the truck — it can't be {2}. Dispatch it instead.".format(
			name, int(doc.loaded_buckets or 0), what
		),
	}


def _add_trip_buckets(doc, entries, stamp=True):
	"""Record the buckets a trip carries (one row per order + bucket), stamped with when
	and by whom they went on the truck (an entry may carry its own loaded_at/loaded_by;
	stamp=False leaves them blank when the load time isn't known)."""
	have = {(r.order_pick_list, (r.bucket or "").upper()) for r in doc.get("trip_buckets") or []}
	now, user = frappe.utils.now(), frappe.session.user
	for e in entries:
		key = (e["opl"], (e["bucket"] or "").upper())
		if not e["bucket"] or key in have:
			continue
		have.add(key)
		doc.append(
			"trip_buckets",
			{
				"bucket": e["bucket"],
				"order_pick_list": e["opl"],
				"farm": e.get("farm"),
				"loaded_at": e.get("loaded_at") or (now if stamp else None),
				"loaded_by": e.get("loaded_by") or (user if stamp else None),
			},
		)


def _end_trip_if_shelved(doc):
	"""Tick off the trip's shelved buckets and end the trip once none is still on the
	truck. A bucket that left the truck without being shelved (its truck flag moved to
	another bucket of the same order, or it was unloaded) does not hold the trip open.
	Returns True when the trip ended."""
	rows = doc.get("trip_buckets") or []
	if not rows:
		return False
	states = {}
	for r in frappe.db.sql(
		"""SELECT parent, UPPER(bucket) AS bucket, MAX(IFNULL(shelved, 0)) AS shelved,
		       MAX(IFNULL(in_transit, 0)) AS in_transit
		FROM `tabPick List Item`
		WHERE parenttype = 'Order Pick List' AND parent IN %(opls)s AND bucket IN %(buckets)s
		GROUP BY parent, UPPER(bucket)""",
		{
			"opls": tuple({r.order_pick_list for r in rows}),
			"buckets": tuple({r.bucket for r in rows}),
		},
		as_dict=True,
	):
		states[(r.parent, r.bucket)] = r
	now = frappe.utils.now()
	changed = False
	for row in rows:
		st = states.get((row.order_pick_list, (row.bucket or "").upper()))
		if st and int(st.shelved) and not row.shelved:
			row.shelved = 1
			row.shelved_at = now
			changed = True
		off = 0 if row.shelved or (st and int(st.in_transit)) else 1
		if int(row.off_truck or 0) != off:
			row.off_truck = off
			changed = True
	ended = any(r.shelved for r in rows) and all(r.shelved or r.off_truck for r in rows)
	if ended:
		doc.status = "Received"
		doc.received_at = now
		changed = True
	if changed:  # also called on every read (_vehicle_on_road): don't save — and version — for nothing
		doc.save(ignore_permissions=True)
	return ended


def _loading_trip(truck):
	"""The truck's planned trip that already has buckets loaded (not dispatched yet)."""
	for name in frappe.get_all(
		"Bucket Request Trip",
		filters={
			"vehicle": truck,
			"status": ["in", ACTIVE_TRIP_STATUSES],
			"trip_date": [">=", frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)],
		},
		pluck="name",
		order_by="trip_date desc, creation desc",
	):
		if _trip_has_loads(frappe.get_doc("Bucket Request Trip", name)):
			return name
	return None


def _requested_buckets_shelved(doc):
	"""True once every bucket this trip was planned to bring is shelved at the sales
	farm: per order row, the (opl, farm)'s shelved buckets cover what this trip
	requested plus what earlier trips for the same (opl, farm) requested -- a split
	order's first trip must not end on the second trip's buckets."""
	wanted = [(o.order_pick_list, o.farm or "") for o in doc.orders if int(o.buckets or 0) > 0]
	if not wanted:
		return False
	opls = tuple({opl for opl, _farm in wanted})
	shelved = {}
	for b in _transfer_buckets(opls):
		if b["shelved"]:
			key = (b["opl"], b["farm"])
			shelved[key] = shelved.get(key, 0) + 1
	requested = {}
	for r in frappe.db.sql(
		"""SELECT o.order_pick_list AS opl, IFNULL(o.farm, '') AS farm, o.buckets AS buckets,
		       t.name AS trip
		FROM `tabBucket Request Trip Order` o
		JOIN `tabBucket Request Trip` t ON t.name = o.parent
		WHERE o.parenttype = 'Bucket Request Trip' AND o.order_pick_list IN %(opls)s
		ORDER BY t.trip_date, t.creation""",
		{"opls": opls},
		as_dict=True,
	):
		key = (r.opl, r.farm)
		requested[key] = requested.get(key, 0) + int(r.buckets or 0)
		if r.trip == doc.name and key in wanted:
			wanted.remove(key)
			if shelved.get(key, 0) < requested[key]:
				return False
	return not wanted


def _end_trips_with_requests_shelved(bucket_id, already=()):
	"""End (Received) every not-yet-ended trip whose requested buckets are all shelved,
	whether or not it was dispatched or its buckets were loaded in the app -- trips the
	truck-flag path above cannot find (the bucket's transit_truck is the order's
	delivery-truck label, or the trip never left Draft)."""
	trips = frappe.db.sql_list(
		"""SELECT DISTINCT t.name
		FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
		JOIN `tabPick List Item` pli ON pli.parent = o.order_pick_list AND pli.parenttype = 'Order Pick List'
		WHERE pli.bucket = %(bucket)s AND t.status != 'Received' AND t.trip_date >= %(since)s""",
		{"bucket": bucket_id, "since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)},
	)
	ended = []
	for name in trips:
		if name in already:
			continue
		doc = frappe.get_doc("Bucket Request Trip", name)
		if not _requested_buckets_shelved(doc):
			continue
		if doc.get("trip_buckets"):
			_end_trip_if_shelved(doc)  # ticks off the carried buckets it can
		_receive_trip(name)
		ended.append(name)
	return ended


def auto_receive_trucks_for_bucket(bucket_id):
	"""Called after a bucket is shelved at the sales farm. The trip of the truck that
	carried it ticks the bucket off, and ends (Received) once every bucket on it is
	shelved, so the truck can be routed and planned again. Any trip -- Draft included
	-- also ends once every bucket it requested is shelved. Returns the trips ended."""
	received = _arrive_trips_carrying(bucket_id)
	trucks = frappe.db.sql_list(
		"""SELECT DISTINCT pli.transit_truck FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s AND """
		+ TRANSFER_TRUCK_OK,
		bucket_id,
	)
	for truck in trucks:
		trip = _vehicle_on_road(truck) or _loading_trip(truck)
		if not trip:
			continue
		doc = frappe.get_doc("Bucket Request Trip", trip)
		# A trip nobody dispatched still ends once its buckets arrive; count from its day.
		since = doc.dispatched_at or doc.trip_date
		if doc.get("trip_buckets"):
			# A bucket that took over another's truck flag on arrival (_take_over_truck)
			# was never recorded on the trip: add it as it is shelved.
			arrived = frappe.db.sql(
				"""SELECT pli.parent AS opl, pli.bucket AS bucket, """
				+ FARM_EXPR
				+ """ AS farm FROM `tabPick List Item` pli
				WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s
				  AND pli.transit_truck = %s AND pli.shelved = 1 AND pli.modified >= DATE(%s)""",
				(bucket_id, truck, since),
				as_dict=True,
			)
			# Only if it isn't recorded on another trip already: one bucket, one trip.
			elsewhere = [w for w in _bucket_trip_rows({bucket_id.upper()}).get(bucket_id.upper(), []) if w.trip != trip]
			if not elsewhere:
				_add_trip_buckets(doc, arrived, stamp=False)
			if _end_trip_if_shelved(doc):
				received.append(trip)
			continue
		# Trips dispatched before buckets were recorded: nothing of the truck's left in
		# transit since it left. Only rows touched since then — in_transit is never
		# cleared on rows that were not shelved, and an old one would hold it forever.
		left = frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabPick List Item`
			WHERE parenttype = 'Order Pick List' AND in_transit = 1 AND IFNULL(shelved, 0) = 0
			  AND transit_truck = %s AND modified >= DATE(%s)""",
			(truck, since),
		)[0][0]
		if not left:
			_receive_trip(trip)
			received.append(trip)
	received += _end_trips_with_requests_shelved(bucket_id, already=received)
	for name in received:
		carry_over_to_next_run(name)
	return received


def _arrive_trips_carrying(bucket_id):
	"""A bucket recorded on a trip was just shelved at the packhouse: that run's truck
	is back. A trip nobody dispatched (a stop only part-loaded never completes) is
	dispatched now, and the trip ends once nothing it carried is still on the truck.
	Returns the trips ended."""
	ended = []
	for name in frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Bucket` b ON b.parent = t.name AND b.parenttype = 'Bucket Request Trip'
		WHERE UPPER(b.bucket) = UPPER(%(bucket)s) AND t.status != 'Received' AND t.trip_date >= %(since)s""",
		{"bucket": bucket_id, "since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)},
	):
		doc = frappe.get_doc("Bucket Request Trip", name)
		if doc.status in ACTIVE_TRIP_STATUSES:
			doc.status = "Dispatched"
			doc.dispatched_at = doc.get("last_departed_at") or frappe.utils.now()
			doc.heading_to = transfer_hub(required=False) or ""
			doc.add_comment("Info", "Dispatched: its first bucket was shelved at {0}".format(doc.heading_to))
			doc.save(ignore_permissions=True)
		if _end_trip_if_shelved(doc):
			ended.append(name)
	return ended


def carry_over_to_next_run(name):
	"""Planned buckets a finished run didn't load move to the truck's next open run
	that visits their farm (as far as its room allows) — the plan keeps going on the
	same route instead of the buckets silently dropping back to "open"."""
	doc = frappe.get_doc("Bucket Request Trip", name)
	if doc.status != "Received":
		return []
	return _carry_over(doc)


def _plan_lock():
	"""Serialise trip planning and loading: the free-bucket check and the write it
	guards must not interleave with another planner, the scheduler or a truck load.
	A named MariaDB lock: it lasts until this request's (or job's) DB connection
	closes, which Frappe does at the end of each; taking it again in the same session
	is a no-op."""
	got = frappe.db.sql("SELECT GET_LOCK('upande_bucket_trip_plan', 30)")[0][0]
	if not got:
		frappe.throw("Trip planning is busy — try again in a moment.")


def _carry_over(doc, farm=None, date=None, report=None):
	"""Move the trip's planned-but-not-loaded buckets (one farm's, or every farm's) to
	the truck's next open run. Idempotent: what another trip already claims is skipped."""
	if not (doc.get("route") and doc.get("run")):
		return []
	_plan_lock()
	rows = []
	for o in doc.orders:
		if farm and (o.farm or "") != farm:
			continue
		left = int(o.buckets or 0) - int(o.loaded_buckets or 0)
		if left > 0 and not int(o.unscheduled or 0):
			per = (int(o.stems or 0) / int(o.buckets)) if int(o.buckets or 0) else 0
			rows.append(
				{
					"order_pick_list": o.order_pick_list,
					"order_name": o.order_name,
					"customer": o.customer,
					"farm": o.farm,
					"varieties": o.varieties,
					"buckets": left,
					"stems": round(per * left),
					"full_farm_buckets": left,
					"is_partial": 0,
				}
			)
	if not rows:
		return []
	# Only what is still open (not shelved, not on another trip).
	opls = list({r["order_pick_list"] for r in rows})
	open_counts, claims = _open_counts(opls), _trip_claims(opls)
	for r in rows:
		key = (r["order_pick_list"], r["farm"] or "")
		free = max(0, open_counts.get(key, 0) - claims.get(key, 0))
		r["buckets"] = min(r["buckets"], free)
		claims[key] = claims.get(key, 0) + r["buckets"]
	rows = [r for r in rows if r["buckets"] > 0]
	if report is not None:
		report["open"] = sum(r["buckets"] for r in rows)
	if not rows:
		return []
	# Always onto today's (or later) runs: a trip dated earlier would not count as a
	# claim and the same buckets would be planned again.
	date = max(str(date or frappe.utils.today()), str(frappe.utils.today()))
	plan, _unplaced = _split_into_runs(doc.vehicle, date, rows, skip_full=True)
	saved = [_write_run_trip(slot, doc.vehicle, date, "Draft", "", 0) for slot in plan]
	if saved:
		doc.add_comment("Info", "Not loaded on this trip, moved to {0}".format(", ".join(saved)))
	return saved


def _trip_stops(doc):
	"""The farms this trip collects from, in the order the truck drives them: its
	run's farms (a trip drives exactly one run of its route), then any planned farm
	the run doesn't list. A trip without a run (saved before runs existed) uses the
	truck's routes for the trip date, in leg order."""
	planned = []
	for o in doc.orders:
		if o.farm and (int(o.buckets or 0) > 0 or int(o.loaded_buckets or 0) > 0) and o.farm not in planned:
			planned.append(o.farm)
	if doc.get("route") and doc.get("run"):
		run = _run_of(doc.route, doc.run)
		if run:
			ordered = [f for f in run["stops"] if f in planned]
			return ordered + [f for f in planned if f not in ordered]
	hub = transfer_hub(required=False)
	ordered = []
	route = frappe.get_all(
		"Bucket Logistics Route",
		filters={"vehicle": doc.vehicle, "route_date": doc.trip_date},
		pluck="name",
		order_by="from_datetime asc",
	)
	if route:
		for leg in frappe.get_all(
			"Bucket Logistics Route Leg",
			filters={"parent": ["in", route], "parenttype": "Bucket Logistics Route"},
			fields=["from_farm", "to_farm"],
			order_by="parent asc, idx asc",
		):
			for f in (leg.from_farm, leg.to_farm):
				if f and f != hub and f in planned and f not in ordered:
					ordered.append(f)
	return ordered + [f for f in planned if f not in ordered]


def _stop_complete(doc, farm):
	"""Every bucket planned at this farm is on the truck — not counting orders whose
	delivery date has passed with nothing of them loaded (the farm app no longer shows
	them, so waiting for them would hold the truck at the stop for good)."""
	rows = [o for o in doc.orders if (o.farm or "") == farm and int(o.buckets or 0) > 0]
	past = _past_delivery_opls([o.order_pick_list for o in rows])
	rows = [o for o in rows if o.order_pick_list not in past or int(o.loaded_buckets or 0) > 0]
	# A row is done once it is fully loaded — or once nothing of that order is still
	# waiting at this farm (planned higher than the farm really had: a bucket counted
	# under the wrong farm, replaced, not found, or taken by another trip).
	open_now = _open_counts([o.order_pick_list for o in rows])
	return bool(rows) and all(
		int(o.loaded_buckets or 0) >= int(o.buckets or 0) or not open_now.get((o.order_pick_list, farm), 0)
		for o in rows
	)


def _past_delivery_opls(opls):
	"""Of these Order Pick Lists, the ones whose Sales Order delivered before today."""
	opls = [o for o in set(opls) if o]
	if not opls:
		return set()
	return set(
		frappe.db.sql_list(
			"""SELECT opl.name FROM `tabOrder Pick List` opl
			JOIN `tabSales Order` so ON so.name = opl.sales_order
			WHERE opl.name IN %(opls)s AND so.delivery_date < %(today)s""",
			{"opls": tuple(opls), "today": frappe.utils.today()},
		)
	)


def _advance_stops(doc):
	"""After a load: a stop whose planned buckets are all on the truck is left behind —
	the truck heads to the next stop, and once the last stop is loaded the trip is
	dispatched to the packhouse. Stops are left in route order only (a later farm
	loading early doesn't skip an earlier one). Returns the farms left by this call."""
	if doc.status not in ACTIVE_TRIP_STATUSES:
		return []
	stops = _trip_stops(doc)
	if not stops:
		return []
	departed = [f for f in (doc.get("departed_stops") or "").split(",") if f]
	newly = []
	for f in stops:
		if f in departed:
			continue
		if not _stop_complete(doc, f):
			break
		departed.append(f)
		newly.append(f)
	if not newly:
		return []
	now = frappe.utils.now()
	doc.departed_stops = ",".join(departed)
	doc.last_departed_at = now
	remaining = [f for f in stops if f not in departed]
	if remaining:
		doc.heading_to = remaining[0]
	else:
		hub = transfer_hub(required=False) or ""
		doc.heading_to = hub
		doc.status = "Dispatched"
		doc.dispatched_at = now
		doc.add_comment("Info", "Dispatched to {0}: every stop loaded".format(hub or "the packhouse"))
	return newly


def farm_departed(truck, farm):
	"""True once the truck carrying this farm's buckets has left the farm: its trip is
	dispatched, or the farm is one of the trip's departed stops."""
	if _vehicle_on_road(truck):
		return True
	return any(farm in _departed(d) for d in _truck_open_trips(truck) if _trip_has_loads(d))


def record_truck_load(pli_names):
	"""Put buckets the mobile app just loaded onto a truck on that truck's trip.

	Loading (loadTrolleyInTruck / setOfflineTrolleyFlags) only flags the Pick List
	Item rows; this puts each bucket on the trip (run) that planned its order at its
	farm — the earliest run with room — else the run that visits the farm, else a new
	unscheduled trip on the next free run. Every run's loads stay on that run's one
	trip (loads used to pile onto the truck's first trip while its later planned trips
	stayed empty). The trip is dispatched once its last stop is loaded, from the
	dashboard, or when its first bucket is shelved at the packhouse
	(auto_receive_trucks_for_bucket), which also ends it.
	Pass only rows that just went in transit, or a re-scan is counted twice.
	Returns the trips touched."""
	if not pli_names:
		return []
	rows = frappe.db.sql(
		"""
		SELECT pli.name AS name, pli.parent AS opl, pli.bucket AS bucket, pli.item_code AS variety,
		       pli.stock_qty AS stems, pli.transit_truck AS truck, pli.modified AS modified, """
		+ FARM_EXPR
		+ """ AS farm
		FROM `tabPick List Item` pli
		WHERE pli.name IN %(names)s AND pli.parenttype = 'Order Pick List'
		  AND pli.in_transit = 1 AND IFNULL(pli.shelved, 0) = 0
		  AND pli.transit_truck IS NOT NULL AND pli.transit_truck != '' AND """
		+ TRANSFER_TRUCK_OK,
		{"names": tuple(pli_names)},
		as_dict=True,
	)
	by_truck = {}
	for r in rows:
		by_truck.setdefault(r["truck"], []).append(r)
	if not rows:
		return []

	_plan_lock()
	# Where each bucket already is: one physical bucket belongs on one trip only.
	on_trips = _bucket_trip_rows({(r["bucket"] or "").upper() for r in rows if r["bucket"]})

	today = frappe.utils.today()
	touched = []
	for truck, truck_rows in by_truck.items():
		# One entry per (order, bucket): a bucket spans several pick rows (one per box).
		buckets = {}
		for r in truck_rows:
			key = (r["opl"], (r["bucket"] or r["name"]).upper())
			b = buckets.setdefault(
				key, {"opl": r["opl"], "farm": r["farm"] or "", "bucket": key[1], "rows": []}
			)
			b["rows"].append(r)
		trips = _truck_open_trips(truck)
		by_trip, docs = {}, {d.name: d for d in trips}
		chosen = {}  # bucket -> trip doc: a bucket split over two orders rides one trip
		for b in buckets.values():
			doc = chosen.get(b["bucket"])
			if doc is None:
				where = on_trips.get(b["bucket"], [])
				left = [w for w in where if w.status not in ACTIVE_TRIP_STATUSES]
				if left:
					# Already on a trip that has left the farm: never add it again.
					frappe.log_error(
						"Truck load: bucket already on a dispatched trip",
						"{0} is on {1} ({2}); not added to {3}".format(
							b["bucket"], left[0].trip, left[0].vehicle, truck
						),
					)
					continue
				same = next((w for w in where if w.vehicle == truck), None)
				if same:
					doc = docs.get(same.trip) or frappe.get_doc("Bucket Request Trip", same.trip)
				else:
					# On another truck's trip that hasn't left: it moved trucks — take it off.
					_drop_bucket_from_trips(
						b["bucket"], [w for w in where if w.vehicle != truck], "loaded onto {0}".format(truck)
					)
					doc = _pick_trip_for_load(trips, b["opl"], b["farm"], by_trip)
					if doc is None:
						doc = _vehicle_trip_doc(truck) or _new_load_trip(truck, today, b["farm"])
						trips.append(doc)
				chosen[b["bucket"]] = doc
			# Already recorded on this trip for this order (a re-scan): don't count it twice.
			if any(
				r.order_pick_list == b["opl"] and (r.bucket or "").upper() == b["bucket"]
				for r in doc.get("trip_buckets") or []
			):
				continue
			docs[doc.name or id(doc)] = doc
			by_trip.setdefault(doc.name or id(doc), []).append(b)
		for key, entries in by_trip.items():
			doc = docs[key]
			_record_loads(doc, truck, entries)
			touched.append(doc.name)
	return touched


def _bucket_trip_rows(buckets):
	"""UPPER(bucket) -> trips (not yet Received) it is recorded on, still on the truck."""
	if not buckets:
		return {}
	out = {}
	for r in frappe.db.sql(
		"""SELECT UPPER(tb.bucket) AS bucket, tb.name AS row, tb.order_pick_list AS opl, tb.farm,
		       t.name AS trip, t.status, t.vehicle
		FROM `tabBucket Request Trip Bucket` tb
		JOIN `tabBucket Request Trip` t ON t.name = tb.parent
		WHERE tb.parenttype = 'Bucket Request Trip' AND UPPER(tb.bucket) IN %(b)s
		  AND t.status != 'Received' AND IFNULL(tb.off_truck, 0) = 0""",
		{"b": tuple(buckets)},
		as_dict=True,
	):
		out.setdefault(r.bucket, []).append(r)
	return out


def _drop_bucket_from_trips(bucket, where, note):
	"""Take a bucket off trips (still at the farm) it was recorded on, when it was
	loaded onto another truck: it rides one truck only. Their counts follow."""
	for trip in {w.trip for w in where}:
		doc = frappe.get_doc("Bucket Request Trip", trip)
		gone = [r for r in doc.trip_buckets if (r.bucket or "").upper() == bucket]
		if not gone:
			continue
		doc.set("trip_buckets", [r for r in doc.trip_buckets if (r.bucket or "").upper() != bucket])
		for r in gone:
			o = next(
				(o for o in doc.orders if o.order_pick_list == r.order_pick_list and (o.farm or "") == (r.farm or "")
				 and int(o.loaded_buckets or 0) > 0),
				None,
			)
			if o:
				o.loaded_buckets = int(o.loaded_buckets) - 1
		doc.loaded_buckets = sum(int(o.loaded_buckets or 0) for o in doc.orders)
		doc.add_comment("Info", "Bucket {0} taken off: {1}".format(bucket, note))
		doc.save(ignore_permissions=True)


def _truck_open_trips(truck):
	"""The truck's trips that haven't left yet, in the order it drives them: by day,
	then route From time, then run (an unrouted trip after the routed ones)."""
	docs = [
		frappe.get_doc("Bucket Request Trip", n)
		for n in frappe.get_all(
			"Bucket Request Trip",
			filters={
				"vehicle": truck,
				"status": ["in", ACTIVE_TRIP_STATUSES],
				"trip_date": [">=", frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)],
			},
			pluck="name",
		)
	]
	starts = {}
	for d in docs:
		if d.get("route") and d.route not in starts:
			starts[d.route] = str(
				frappe.db.get_value("Bucket Logistics Route", d.route, "from_datetime") or ""
			)
	docs.sort(
		key=lambda d: (
			str(d.trip_date),
			0 if d.get("run") else 1,
			starts.get(d.get("route"), ""),
			int(d.get("run") or 0),
			str(d.creation),
		)
	)
	return docs


def _departed(doc):
	return [f for f in (doc.get("departed_stops") or "").split(",") if f]


def _pick_trip_for_load(trips, opl, farm, assigned):
	"""The trip a bucket of (opl, farm) just loaded belongs to: the earliest run that
	planned that order at that farm and still has room for it, else the earliest that
	planned it at all, else the run that visits the farm (and hasn't left it), else
	the trip the truck is already loading. None = no fitting trip."""

	def taken(d):
		return sum(1 for b in assigned.get(d.name, []) if b["opl"] == opl and b["farm"] == farm)

	here = [d for d in trips if farm not in _departed(d)]
	for need_room in (True, False):
		for d in here:
			for o in d.orders:
				if o.order_pick_list != opl or (o.farm or "") != farm or int(o.buckets or 0) <= 0:
					continue
				if not need_room or int(o.loaded_buckets or 0) + taken(d) < int(o.buckets or 0):
					return d
	# Nothing planned it: only today's (or later) trips take it — an earlier day's trip
	# left open is not where today's load goes (a stale 2-Oct trip took a 3-Oct bucket
	# and was then dispatched with it). None makes the caller open a trip on today's run.
	today = str(frappe.utils.today())
	current = [d for d in here if str(d.trip_date) >= today]
	for d in current:
		if d.get("run") and farm in ((_run_of(d.route, d.run) or {}).get("stops") or []):
			return d
	for d in current:
		if _trip_has_loads(d):
			return d
	return current[0] if current else None


def _vehicle_trip_doc(truck):
	"""Legacy fallback: the trip the truck is out on (loaded after its dispatch) — only
	a trip of today: an earlier day's trip never takes today's load."""
	trip = _vehicle_on_road(truck)
	if not trip or str(frappe.db.get_value("Bucket Request Trip", trip, "trip_date")) < str(frappe.utils.today()):
		return None
	return frappe.get_doc("Bucket Request Trip", trip)


def _new_load_trip(truck, today, farm):
	"""An unscheduled trip for a load nothing planned — on the first open run of the
	truck's route that visits the farm, so it still drives one run."""
	doc = frappe.new_doc("Bucket Request Trip")
	doc.vehicle = truck
	doc.trip_date = today
	doc.unscheduled = 1
	# Loaded, not gone: the run ends when its buckets are shelved at the packhouse.
	doc.status = "Scheduled"
	run = next(
		(r for r in _day_runs(truck, today) if not r["trip"] and farm in r["stops"]),
		None,
	)
	if run:
		doc.route, doc.run = run["route"], run["run"]
	return doc


def _record_loads(doc, truck, entries):
	"""Count the loaded buckets onto the trip's plan rows and record them on the trip."""
	portions = {}
	for b in entries:
		p = portions.setdefault((b["opl"], b["farm"]), {"buckets": 0, "stems": 0.0, "varieties": set()})
		p["buckets"] += 1
		for r in b["rows"]:
			p["stems"] += float(r["stems"] or 0)
			if r["variety"]:
				p["varieties"].add(r["variety"])

	# The plan (buckets/stems) is kept as scheduled; loads are counted beside it,
	# and a portion that was never planned is added as an unscheduled row.
	existing = {}
	for o in doc.orders:
		existing.setdefault((o.order_pick_list, o.farm or ""), []).append(o)
	for (opl, farm), p in portions.items():
		matches = existing.get((opl, farm))
		if not matches:
			order_name, so = frappe.db.get_value("Order Pick List", opl, ["order_name", "sales_order"]) or (
				None,
				None,
			)
			matches = [
				doc.append(
					"orders",
					{
						"order_pick_list": opl,
						"order_name": order_name or opl,
						"customer": frappe.db.get_value("Sales Order", so, "customer") if so else None,
						"farm": farm,
						"buckets": 0,
						"stems": 0,
						"unscheduled": 1,
					},
				)
			]
			existing[(opl, farm)] = matches
		# Fill planned rows in order; anything beyond the plan lands on the last one.
		left = p["buckets"]
		for i, o in enumerate(matches):
			room = max(0, int(o.buckets or 0) - int(o.loaded_buckets or 0))
			take = left if i == len(matches) - 1 else min(left, room)
			o.loaded_buckets = int(o.loaded_buckets or 0) + take
			left -= take
		o = matches[0]
		o.loaded_stems = int(o.loaded_stems or 0) + int(p["stems"])
		o.varieties = ", ".join(sorted({v for v in (o.varieties or "").split(", ") if v} | p["varieties"]))

	doc.capacity_buckets = _vehicle_capacity(truck)
	doc.total_buckets = sum(int(o.buckets or 0) for o in doc.orders)
	doc.total_stems = sum(int(o.stems or 0) for o in doc.orders)
	doc.loaded_buckets = sum(int(o.loaded_buckets or 0) for o in doc.orders)
	_add_trip_buckets(doc, [r for b in entries for r in b["rows"][:1]])
	_advance_stops(doc)
	doc.save(ignore_permissions=True)


def _vehicle_capacity(vehicle):
	v = frappe.db.get_value(
		"Vehicle", vehicle, ["custom_trolley_capacity", "custom_buckets_per_trolley"], as_dict=True
	)
	return int((v.custom_trolley_capacity or 0) * (v.custom_buckets_per_trolley or 0)) if v else 0


@frappe.whitelist()
def getTransferControlData():
	# Read-only per-bucket transfer state for a delivery-date window.
	fd = frappe.form_dict
	from_date = fd.get("from_date") or frappe.utils.today()
	to_date = fd.get("to_date") or frappe.utils.add_days(frappe.utils.today(), 2)

	frappe.response["message"] = {"success": False, "error": "Script failed"}
	try:
		opl_rows = frappe.db.sql(
			"""
			SELECT DISTINCT opl.name AS opl, opl.order_name AS order_name, opl.sales_order AS so,
			       so.customer AS customer, so.delivery_date AS delivery_date, opl.creation AS created,
			       opl.schedule_number AS schedule, opl.team AS team, opl.item_group AS item_group,
			       opl.mix_group AS mix_group
			FROM `tabOrder Pick List` opl
			JOIN `tabSales Order` so ON so.name = opl.sales_order
			JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
			WHERE opl.docstatus < 2 AND so.delivery_date BETWEEN %(f)s AND %(t)s
			  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
			  AND NOT """
			+ PACKED_SQL
			+ """
			""",
			{"f": from_date, "t": to_date},
			as_dict=True,
		)
		opl_names = [r["opl"] for r in opl_rows]
		mixed = _mixed_map(opl_names)
		sched = _schedule_map()

		buckets_by_opl = {}
		if opl_names:
			pli_rows = frappe.db.sql(
				"""
				SELECT pli.parent AS opl, pli.idx AS box, pli.bucket AS bucket, pli.item_code AS variety,
				       pli.stock_qty AS stems, """
				+ FARM_EXPR
				+ """ AS farm_src, pli.shelf AS shelf,
				       CASE WHEN """
				+ TRANSFER_TRUCK_OK
				+ """ THEN pli.transit_truck END AS transit_truck,
				       pli.awaiting_transfer AS awaiting_transfer,
				       pli.loaded_in_trolley AS loaded_in_trolley, pli.in_transit AS in_transit,
				       pli.shelved AS shelved
				FROM `tabPick List Item` pli
				WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s
				""",
				{"opls": tuple(opl_names)},
				as_dict=True,
			)
			seen = set()
			for r in pli_rows:
				key = (r["opl"], (r.get("bucket") or "").upper())
				if r.get("bucket") and key in seen:
					continue
				seen.add(key)
				buckets_by_opl.setdefault(r["opl"], []).append(
					{
						"id": r.get("bucket"),
						"box": r.get("box"),
						"variety": r.get("variety"),
						"stems": r.get("stems"),
						"farm": r.get("farm_src"),
						"shelf": r.get("shelf"),
						"state": _bucket_state(r),
						"transit_truck": r.get("transit_truck"),
					}
				)

		farms = set()
		orders = []
		for r in opl_rows:
			bl = buckets_by_opl.get(r["opl"], [])
			for b in bl:
				if b.get("farm") and b["state"] != "home":
					farms.add(b["farm"])
			sc = sched.get(r["opl"]) or {}
			orders.append(
				{
					"ref": r["opl"],
					"opl": r["opl"],
					"order_name": r.get("order_name") or r["opl"],
					"customer": r.get("customer"),
					"so": r.get("so"),
					"delivery_date": str(r.get("delivery_date") or ""),
					"truck": next((b.get("transit_truck") for b in bl if b.get("transit_truck")), None),
					"schedule": sc.get("schedule"),
					"team": sc.get("team") or r.get("team"),
					"scheduled": 1 if sc else 0,
					"item_group": r.get("item_group"),
					"mixed": _mixed_label(mixed.get(r["opl"])),
					"mix_group": r.get("mix_group"),
					"created": str(r.get("created") or ""),
					"total": len(bl),
					"buckets": bl,
				}
			)

		frappe.response["message"] = {
			"success": True,
			"orders": orders,
			"farms": sorted(farms),
			"packhouse": transfer_hub(),
			"window": {"from": str(from_date), "to": str(to_date)},
			"generated_at": str(frappe.utils.now()),
		}
	except Exception as e:
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def getTransferScheduleData():
	# Planning view: open (schedule-gated) orders aggregated by farm/variety, plus the
	# fleet, trips, truck-status snapshot, distance graph and today's routes. Also
	# lists orders that NEED a transfer but can't be planned because they aren't on a
	# team's schedule (no team, or never scheduled) — they used to vanish silently.
	fd = frappe.form_dict
	from_date = fd.get("from_date") or frappe.utils.add_days(frappe.utils.today(), 1)
	to_date = fd.get("to_date") or from_date

	frappe.response["message"] = {"success": False, "error": "Script failed"}
	try:
		frappe.response["message"] = _transfer_schedule_payload(from_date, to_date)
	except Exception as e:
		frappe.response["message"] = {"success": False, "error": str(e)}


def _transfer_schedule_payload(from_date, to_date):
	"""The Transfer Scheduling feed — shared by the page and automatic scheduling."""
	today = frappe.utils.today()
	ensure_day_routes(today)
	hub = transfer_hub()
	sched = _schedule_map()

	opl_rows = frappe.db.sql(
		"""
		SELECT DISTINCT opl.name AS opl, opl.order_name AS order_name, opl.sales_order AS so,
		       so.customer AS customer, so.delivery_date AS delivery_date, opl.team AS opl_team
		FROM `tabOrder Pick List` opl
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
		WHERE opl.docstatus < 2 AND so.delivery_date BETWEEN %(f)s AND %(t)s
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND COALESCE(pli.shelved, 0) = 0 AND NOT """
		+ PACKED_SQL
		+ """
		""",
		{"f": from_date, "t": to_date},
		as_dict=True,
	)
	opl_names = [r["opl"] for r in opl_rows]
	mixed = _mixed_map(opl_names)

	# opl -> farm -> {open: variety -> {buckets, stems}, on_road}
	# Driven by the transfer FLAGS, not "farm != Kapkolia": a bucket at its own
	# sales farm (e.g. Karen for a Karen order) was never flagged and must not be
	# planned onto a truck.
	agg = {}
	for b in _transfer_buckets(opl_names):
		if not (b["open"] or b["on_road"]) or not b["farm"]:
			continue
		# Stock already at the packhouse needs no truck: never plan it onto a trip.
		if b["open"] and hub and b["farm"].lower() == hub.lower():
			continue
		fmap = agg.setdefault(b["opl"], {})
		frow = fmap.setdefault(b["farm"], {"varieties": {}, "on_road": 0})
		if b["on_road"]:
			frow["on_road"] += 1
			continue
		vrow = frow["varieties"].setdefault(b["variety"] or "", {"buckets": 0, "stems": 0})
		vrow["buckets"] += 1
		vrow["stems"] += b["stems"]

	orders, unscheduled = [], []
	for r in opl_rows:
		fmap = agg.get(r["opl"]) or {}
		farms_out = []
		total_b, total_s, total_road = 0, 0.0, 0
		for farm, frow in sorted(fmap.items()):
			vmap = frow["varieties"]
			fb = sum(v["buckets"] for v in vmap.values())
			fs = sum(v["stems"] for v in vmap.values())
			total_b += fb
			total_s += fs
			total_road += frow["on_road"]
			farms_out.append(
				{
					"farm": farm,
					"buckets": fb,
					"stems": fs,
					"on_road": frow["on_road"],
					"varieties": [
						{"variety": k, "buckets": v["buckets"], "stems": v["stems"]} for k, v in vmap.items()
					],
				}
			)
		if not farms_out:
			continue
		sc = sched.get(r["opl"])
		if not sc:
			# Buckets already on a truck count too: an unscheduled order loaded straight
			# from the farm used to drop off the page the moment it left.
			if total_b or total_road:
				unscheduled.append(
					{
						"opl": r["opl"],
						"order_name": r.get("order_name") or r["opl"],
						"customer": r.get("customer"),
						"delivery_date": str(r.get("delivery_date") or ""),
						"team": r.get("opl_team") or "",
						"reason": "not_scheduled" if r.get("opl_team") else "no_team",
						"buckets": total_b,
						"on_road": total_road,
						"farms": sorted(f["farm"] for f in farms_out if f["buckets"] or f["on_road"]),
					}
				)
			continue
		orders.append(
			{
				"opl": r["opl"],
				"order_name": r.get("order_name") or r["opl"],
				"customer": r.get("customer"),
				"so": r.get("so"),
				"delivery_date": str(r.get("delivery_date") or ""),
				"truck": None,
				"mixed": _mixed_label(mixed.get(r["opl"])),
				"schedule": sc.get("schedule"),
				"team": sc.get("team"),
				"total_buckets": total_b,
				"total_stems": total_s,
				"on_road_buckets": total_road,
				"farms": farms_out,
			}
		)

	# Fleet
	vehicles = frappe.get_all(
		"Vehicle",
		filters={"custom_is_internal_logistics_truck": 1},
		fields=["name", "custom_trolley_capacity", "custom_buckets_per_trolley"],
	)
	veh_out = []
	for v in vehicles:
		trolleys = int(v.get("custom_trolley_capacity") or 0)
		per = int(v.get("custom_buckets_per_trolley") or 0)
		on_road = _vehicle_on_road(v.name)
		veh_out.append(
			{
				"name": v.name,
				"trolleys": trolleys,
				"buckets_per_trolley": per,
				"capacity_buckets": trolleys * per,
				# Out on a trip -> not available until it's back at route_end
				"on_road": on_road,
				"dispatched_at": str(
					frappe.db.get_value("Bucket Request Trip", on_road, "dispatched_at") or ""
				)
				if on_road
				else "",
				"route_end": _route_end(v.name, today),
				# Each run is a full truck: what the planner may still fill today.
				"runs": [
					{k: r[k] for k in ("route", "run", "runs", "stops", "trip", "trip_status")}
					for r in _day_runs(v.name, today)
				],
			}
		)
		veh_out[-1]["open_runs"] = sum(1 for r in veh_out[-1]["runs"] if _run_open(r))

	# Trips: all of today's, plus earlier ones that were never received (a
	# dispatched truck still on the road, or a stale draft that never left).
	# Only loading today's trips hid both of those.
	trip_names = set(frappe.get_all("Bucket Request Trip", filters={"trip_date": today}, pluck="name"))
	trip_names |= set(
		frappe.get_all(
			"Bucket Request Trip",
			filters={
				"trip_date": ["between", [frappe.utils.add_days(today, -TRIP_LOOKBACK_DAYS), today]],
				"status": ["!=", "Received"],
			},
			pluck="name",
		)
	)
	trips_out = [_trip_dict(frappe.get_doc("Bucket Request Trip", tn), today) for tn in trip_names]
	trips_out.sort(key=lambda t: (t["trip_date"], t["name"]), reverse=True)

	# Distance graph (via_farms is upande_quality's Custom Field — read it where present)
	dist_fields = ["name", "from_farm", "to_farm", "distance_km", "is_road_leg"]
	if frappe.db.has_column("Farm Distance", "via_farms"):
		dist_fields.append("via_farms")
	dist_rows = frappe.get_all("Farm Distance", fields=dist_fields, order_by="from_farm, to_farm")
	distances = [
		{
			"name": d.name,
			"a": d.from_farm,
			"b": d.to_farm,
			"km": d.distance_km,
			"leg": int(d.is_road_leg or 0),
			"via": d.get("via_farms") or "",
		}
		for d in dist_rows
	]

	# Today's routes
	route_docs = frappe.get_all(
		"Bucket Logistics Route",
		filters={"route_date": today},
		pluck="name",
		order_by="vehicle asc, from_datetime asc",
	)
	routes_out = []
	for rn in route_docs:
		doc = frappe.get_doc("Bucket Logistics Route", rn)
		legs = [
			{
				"leg": l.leg,
				"from_farm": l.from_farm,
				"to_farm": l.to_farm,
				"distance_km": l.distance_km,
				"run": l.get("run"),
			}
			for l in doc.legs
		]
		routes_out.append(
			{
				"name": doc.name,
				"vehicle": doc.vehicle,
				"from_datetime": _dt(doc.from_datetime),
				"to_datetime": _dt(doc.to_datetime),
				"total_km": doc.total_km,
				"auto_planned": int(doc.get("auto_planned") or 0),
				"legs": legs,
				"farms": _route_farms(doc.legs),
				"runs": [r["stops"] for r in route_runs(legs)],
			}
		)

	return {
		"success": True,
		"orders": orders,
		"unscheduled": unscheduled,
		"vehicles": veh_out,
		"trips": trips_out,
		"truck_status": _truck_status(today),
		"distances": distances,
		"farm_list": _hub_company_farms(hub),
		"routes": routes_out,
		"packhouse": hub,
		"auto_planning": _auto_planning_status(),
		"duplicate_trips": duplicate_trips(today),
		"today": str(today),
		"window": {"from": str(from_date), "to": str(to_date)},
		"generated_at": str(frappe.utils.now()),
	}


@frappe.whitelist(methods=["POST"])
def saveBucketTrip():
	# Upsert a Bucket Request Trip. orders = rows joined by \x1e, fields within a row
	# by \x1f: [order_pick_list, order_name, customer, farm, varieties, buckets, stems,
	# full_farm_buckets(optional)]. A trip saved here is a person's: it is never
	# auto-planned, and automatic scheduling leaves it alone from now on.
	fd = frappe.form_dict
	frappe.response["message"] = _save_trip(
		name=fd.get("name"),
		vehicle=fd.get("vehicle"),
		trip_date=fd.get("trip_date") or frappe.utils.today(),
		status=fd.get("status") or "Draft",
		notes=fd.get("notes") or "",
		collection_order=fd.get("collection_order") or "",
		farm=fd.get("farm") or "",
		rows=_parse_trip_rows(fd.get("orders") or ""),
		route=fd.get("route") or None,
		run=frappe.utils.cint(fd.get("run")) or None,
	)


def _parse_trip_rows(orders_raw):
	rows = []
	for line in orders_raw.split("\x1e"):
		if not line:
			continue
		parts = line.split("\x1f")
		if len(parts) < 7:
			continue
		row = {
			"order_pick_list": parts[0],
			"order_name": parts[1],
			"customer": parts[2],
			"farm": parts[3],
			"varieties": parts[4],
			"buckets": int(float(parts[5] or 0)),
			"stems": int(float(parts[6] or 0)),
			"full_farm_buckets": int(float(parts[7])) if len(parts) > 7 and parts[7] else None,
		}
		if row["full_farm_buckets"] is None:
			row["full_farm_buckets"] = row["buckets"]
		row["is_partial"] = 1 if row["buckets"] < row["full_farm_buckets"] else 0
		if row["buckets"] > 0:
			rows.append(row)
	return rows


def _save_trip(
	name,
	vehicle,
	trip_date,
	status,
	notes,
	collection_order,
	farm,
	rows,
	auto_planned=0,
	route=None,
	run=None,
):
	"""Validate and upsert a Bucket Request Trip; returns the response message.

	Capacity is recomputed here and is authoritative — over-capacity refuses the whole
	save, no partial write. Also refuses to claim more buckets of an (order, farm) than
	are still open once every other planned trip today is counted. That is what stops a
	double-click (or a second planner, or the automatic scheduler) from putting the
	same buckets on two trucks."""
	if not vehicle:
		return {"status": "error", "message": "A vehicle is required."}
	existing = name and frappe.db.exists("Bucket Request Trip", name)
	# A routed truck is planned run by run: a run already on the road is skipped, the
	# truck isn't blocked. Only an unrouted truck waits until it is back.
	runs = [] if existing else _day_runs(vehicle, trip_date)
	only = (route, int(run)) if route and run else None
	if only and not any((r["route"], r["run"]) == only for r in runs):
		return {"status": "error", "message": "{0} has no trip {1} on route {2}.".format(vehicle, run, route)}
	busy = None if runs else _vehicle_on_road(vehicle)
	if busy and str(trip_date) == str(frappe.utils.today()):
		return {
			"status": "error",
			"reason": "on_road",
			"message": _on_road_message(vehicle, busy, "planned"),
		}
	if status not in ACTIVE_TRIP_STATUSES:
		return {
			"status": "error",
			"message": "A trip can only be saved as Draft or Scheduled — use dispatch/receive for the rest.",
		}

	if existing:
		refusal = _loaded_trip_refusal(name, "re-planned")
		if refusal:
			return refusal
		current = frappe.db.get_value("Bucket Request Trip", name, "status")
		if current not in ACTIVE_TRIP_STATUSES:
			return {
				"status": "error",
				"message": "Trip {0} is already {1} — it can't be edited.".format(name, current),
			}

	if not rows:
		return {"status": "error", "message": "The trip has no buckets on it."}

	# Claim check, whatever the trip's date: claims span every active trip, so the same
	# buckets can't be planned on two trips or two trucks (tomorrow's included).
	_plan_lock()
	opls = list({r["order_pick_list"] for r in rows})
	open_counts = _open_counts(opls)
	claims = _trip_claims(opls, exclude_trip=name if existing else None)
	wanted = {}
	for r in rows:
		key = (r["order_pick_list"], r["farm"])
		wanted[key] = wanted.get(key, 0) + r["buckets"]
	if existing:
		# Buckets this trip already carries are no longer open: only the rest must be free.
		for o in frappe.get_all(
			"Bucket Request Trip Order",
			filters={"parent": name, "parenttype": "Bucket Request Trip"},
			fields=["order_pick_list", "farm", "loaded_buckets"],
		):
			key = (o.order_pick_list, o.farm or "")
			if key in wanted:
				wanted[key] = max(0, wanted[key] - int(o.loaded_buckets or 0))
	conflicts = []
	for (opl, fm), want in wanted.items():
		free = open_counts.get((opl, fm), 0) - claims.get((opl, fm), 0)
		if want > free:
			label = next((r["order_name"] for r in rows if r["order_pick_list"] == opl), "") or opl
			conflicts.append(
				{"opl": opl, "order_name": label, "farm": fm, "wanted": want, "free": max(0, free)}
			)
	if conflicts:
		return {
			"status": "error",
			"reason": "over_claimed",
			"message": "Already planned on another trip: "
			+ "; ".join(
				"{0} @ {1} — {2} wanted, only {3} still open".format(
					c["order_name"], c["farm"], c["wanted"], c["free"]
				)
				for c in conflicts
			),
			"conflicts": conflicts,
		}

	if runs:
		plan, unplaced = _split_into_runs(vehicle, trip_date, rows, only=only)
		if unplaced:
			return {
				"status": "error",
				"reason": "over_capacity",
				"message": "{0}'s trips today can't take: {1}. Add a trip to its route or use another truck.".format(
					vehicle,
					"; ".join(
						"{0} @ {1} — {2} bkt ({3})".format(
							r["order_name"] or r["order_pick_list"], r["farm"], n, why
						)
						for r, n, why in unplaced
					),
				),
			}
		names = [_write_run_trip(slot, vehicle, trip_date, status, notes, auto_planned) for slot in plan]
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return {
			"status": "success",
			"name": names[0],
			"names": names,
			"runs": [
				{"trip": nm, "run": slot["run"], "buckets": sum(r["buckets"] for r in slot["rows"])}
				for nm, slot in zip(names, plan, strict=True)
			],
			"total_buckets": sum(r["buckets"] for r in rows),
			"total_stems": sum(r["stems"] for r in rows),
			"capacity_buckets": _vehicle_capacity(vehicle),
		}

	total_buckets = sum(r["buckets"] for r in rows)
	total_stems = sum(r["stems"] for r in rows)

	v = frappe.db.get_value(
		"Vehicle", vehicle, ["custom_trolley_capacity", "custom_buckets_per_trolley"], as_dict=True
	)
	cap = int((v.custom_trolley_capacity or 0) * (v.custom_buckets_per_trolley or 0)) if v else 0
	if cap > 0 and total_buckets > cap:
		return {
			"status": "error",
			"reason": "over_capacity",
			"message": "{0} buckets exceeds {1}'s capacity of {2}.".format(total_buckets, vehicle, cap),
			"total_buckets": total_buckets,
			"capacity_buckets": cap,
		}

	if existing:
		doc = frappe.get_doc("Bucket Request Trip", name)
	else:
		doc = frappe.new_doc("Bucket Request Trip")

	doc.vehicle = vehicle
	doc.trip_date = trip_date
	doc.status = status
	doc.notes = notes
	doc.collection_order = collection_order
	doc.farm = farm
	doc.auto_planned = 1 if auto_planned else 0
	doc.total_buckets = total_buckets
	doc.total_stems = total_stems
	doc.capacity_buckets = cap
	doc.set("orders", [])
	for r in rows:
		doc.append("orders", r)
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit

	return {
		"status": "success",
		"name": doc.name,
		"total_buckets": total_buckets,
		"total_stems": total_stems,
		"capacity_buckets": cap,
	}


def _split_into_runs(vehicle, date, rows, skip_full=False, only=None):
	"""Spread plan rows over the truck's still-open runs that day, earliest first: a
	row goes to the first run that visits its farm (and hasn't left it) with room, and
	is split across runs when one run can't hold it all. Each run is a full truck.
	Returns (slots with their rows, unplaced [(row, buckets, reason)])."""
	cap = _vehicle_capacity(vehicle)
	slots = []
	for run in _day_runs(vehicle, date):
		if not _run_open(run) or (only and (run["route"], run["run"]) != only):
			continue
		used, departed = 0, []
		if run["trip"]:
			t = frappe.get_doc("Bucket Request Trip", run["trip"])
			used = max(int(t.total_buckets or 0), int(t.loaded_buckets or 0))
			departed = _departed(t)
		slots.append({**run, "room": (cap - used) if cap else 10**9, "departed": departed, "rows": []})
	unplaced = []
	for row in rows:
		left = int(row["buckets"])
		visits = [sl for sl in slots if row["farm"] in sl["stops"] and row["farm"] not in sl["departed"]]
		for sl in visits:
			take = min(left, sl["room"])
			if take <= 0:
				continue
			per = (row["stems"] / row["buckets"]) if row["buckets"] else 0
			sl["rows"].append(
				{
					**row,
					"buckets": take,
					"stems": round(per * take),
					"is_partial": 1 if take < int(row.get("full_farm_buckets") or take) else 0,
				}
			)
			sl["room"] -= take
			left -= take
			if not left:
				break
		if left and not skip_full:
			why = "no open trip visits {0}".format(row["farm"]) if not visits else "its trips there are full"
			unplaced.append((row, left, why))
	return [sl for sl in slots if sl["rows"]], unplaced


def _write_run_trip(slot, vehicle, date, status, notes, auto_planned):
	"""Add plan rows to the run's one trip (creating it on first use). A row for an
	(order, farm) the trip already has adds to it — re-planning a run tops up its trip
	instead of making another trip for the same truck. Returns the trip name."""
	if slot.get("trip"):
		doc = frappe.get_doc("Bucket Request Trip", slot["trip"])
	else:
		doc = frappe.new_doc("Bucket Request Trip")
		doc.vehicle, doc.trip_date, doc.status = vehicle, date, status
		doc.route, doc.run = slot["route"], slot["run"]
		doc.auto_planned = 1 if auto_planned else 0
	if not auto_planned:
		doc.auto_planned = 0  # a person planned onto it: automatic scheduling leaves it alone
	if status == "Scheduled":
		doc.status = "Scheduled"
	if notes and notes not in (doc.notes or ""):
		doc.notes = ((doc.notes or "") + "\n" + notes).strip()
	doc.collection_order = _run_chain(slot["stops"])
	for r in slot["rows"]:
		match = next(
			(
				o
				for o in doc.orders
				if o.order_pick_list == r["order_pick_list"] and (o.farm or "") == (r["farm"] or "")
			),
			None,
		)
		if match:
			match.buckets = int(match.buckets or 0) + int(r["buckets"])
			match.stems = int(match.stems or 0) + int(r["stems"])
			match.full_farm_buckets = max(
				int(match.full_farm_buckets or 0), int(r.get("full_farm_buckets") or 0), match.buckets
			)
			match.is_partial = 1 if match.buckets < match.full_farm_buckets else 0
			match.unscheduled = 0
			match.varieties = ", ".join(
				sorted(
					{v for v in (match.varieties or "").split(", ") if v}
					| {v for v in (r.get("varieties") or "").split(", ") if v}
				)
			)
		else:
			doc.append("orders", r)
	doc.capacity_buckets = _vehicle_capacity(vehicle)
	doc.total_buckets = sum(int(o.buckets or 0) for o in doc.orders)
	doc.total_stems = sum(int(o.stems or 0) for o in doc.orders)
	doc.save(ignore_permissions=True)
	return doc.name


@frappe.whitelist(methods=["POST"])
def deleteBucketTrip():
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	current = frappe.db.get_value("Bucket Request Trip", name, "status")
	if current not in ACTIVE_TRIP_STATUSES:
		# Its buckets are flagged in transit / shelved — deleting the trip would
		# erase the only record of which truck carried them.
		frappe.response["message"] = {
			"status": "error",
			"message": "Trip {0} is {1} — only Draft or Scheduled trips can be deleted.".format(
				name, current
			),
		}
		return
	refusal = _loaded_trip_refusal(name, "deleted")
	if refusal:
		frappe.response["message"] = refusal
		return
	frappe.delete_doc("Bucket Request Trip", name, ignore_permissions=True, force=1)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success"}


@frappe.whitelist(methods=["POST"])
def saveBucketLogisticsRoute():
	# Upsert a Bucket Logistics Route for (date, vehicle). legs = "|~|"-joined Farm
	# Distance record names, in travel order. Empty legs = clear the route (the
	# vehicle goes back to unusable for distribution until routed again).
	#
	# The legs must form one continuous round trip from the packhouse back to the
	# packhouse. Farm Distance records are undirected (FD-A-B serves A→B and B→A),
	# so each leg is stored in the direction it is actually driven.
	#
	# A truck can run several routes a day, each in its own From/To window: `name`
	# edits that route, `new=1` adds another one. With neither, the truck's only route
	# that day is replaced (or created). The route's date is the day of From.
	fd = frappe.form_dict
	from_datetime = fd.get("from_datetime")
	frappe.response["message"] = _save_route(
		date=str(frappe.utils.getdate(from_datetime))
		if from_datetime
		else (fd.get("date") or frappe.utils.today()),
		vehicle=fd.get("vehicle"),
		leg_names=(fd.get("legs") or "").split("|~|"),
		name=fd.get("name"),
		new=frappe.utils.cint(fd.get("new")),
		from_datetime=from_datetime,
		to_datetime=fd.get("to_datetime"),
	)


def _route_to_update(date, vehicle, name=None, new=0, auto_planned=0):
	"""The Bucket Logistics Route a save writes into, or None for a new one.
	Returns (doc_or_None, error_message)."""
	if name:
		if not frappe.db.exists("Bucket Logistics Route", name):
			return None, "Route {0} no longer exists. Refresh the page and try again.".format(name)
		doc = frappe.get_doc("Bucket Logistics Route", name)
		if doc.vehicle != vehicle:
			return None, "Route {0} belongs to {1}.".format(name, doc.vehicle)
		return doc, None
	if new:
		return None, None
	filters = {"route_date": date, "vehicle": vehicle}
	if auto_planned:
		filters["auto_planned"] = 1
	existing = frappe.get_all("Bucket Logistics Route", filters=filters, pluck="name")
	if len(existing) == 1:
		return frappe.get_doc("Bucket Logistics Route", existing[0]), None
	if len(existing) > 1:
		return None, "{0} has {1} routes on {2} — edit the one you mean under Truck Routes.".format(
			vehicle, len(existing), date
		)
	return None, None


def _route_path(leg_names):
	"""Farm Distance names in driving order -> (path, error). The path is one continuous
	round trip from the packhouse back to it, each leg stored in the direction driven
	(roads are undirected) and numbered with its run (trip)."""
	leg_names = [n for n in (leg_names or []) if n]
	leg_docs = []
	if leg_names:
		leg_docs = frappe.get_all(
			"Farm Distance",
			filters={"name": ["in", leg_names]},
			fields=["name", "from_farm", "to_farm", "distance_km"],
		)
	by_name = {d.name: d for d in leg_docs}

	unknown = [ln for ln in leg_names if ln not in by_name]
	if unknown:
		return None, "Unknown road leg(s): {0}. Refresh the page and try again.".format(", ".join(unknown))

	hub = transfer_hub()
	path, here = [], hub
	for ln in leg_names:
		d = by_name[ln]
		if d.from_farm == here:
			start, end = d.from_farm, d.to_farm
		elif d.to_farm == here:
			start, end = d.to_farm, d.from_farm
		else:
			return None, "Route is broken at {0}: leg {1} ({2}–{3}) doesn't start there.".format(
				here, ln, d.from_farm, d.to_farm
			)
		path.append({"leg": d.name, "from_farm": start, "to_farm": end, "distance_km": d.distance_km})
		here = end
	# Runs: each time the route is back at the packhouse a run ends; the next leg starts
	# the next run (Kapkolia → A → Kapkolia → B → Kapkolia = run 1 to A, run 2 to B).
	run = 1
	for leg in path:
		leg["run"] = run
		if leg["to_farm"] == hub:
			run += 1
	if path and here != hub:
		return None, "Route ends at {0} — it must return to {1}.".format(here, hub)
	return path, None


def _save_route(
	date,
	vehicle,
	leg_names,
	auto_planned=0,
	name=None,
	new=0,
	from_datetime=None,
	to_datetime=None,
	template=None,
	check_on_road=True,
):
	"""Validate and save a truck's Bucket Logistics Route; returns the response message.
	A route saved by hand (auto_planned=0) is never re-routed by automatic scheduling.
	`template` marks a day copy of a saved route (made even while the truck is out)."""
	if not vehicle:
		return {"status": "error", "message": "A vehicle is required."}
	busy = _vehicle_on_road(vehicle) if check_on_road else None
	if busy and str(date) == str(frappe.utils.today()):
		return {
			"status": "error",
			"reason": "on_road",
			"message": _on_road_message(vehicle, busy, "re-routed"),
		}
	path, err = _route_path(leg_names)
	if err:
		return {"status": "error", "message": err}
	hub = transfer_hub()

	doc, err = _route_to_update(date, vehicle, name=name, new=new, auto_planned=auto_planned)
	if err:
		return {"status": "error", "message": err}
	if doc is not None:
		# A run that already has a trip keeps its farms: its trip (and the farm app)
		# drive it. Runs can be added after it, or changed once the trip is gone.
		new_runs = {r["run"]: r["stops"] for r in route_runs(path, hub)}
		old_runs = {r["run"]: r["stops"] for r in _route_runs_by_name(doc.name)}
		for t in frappe.get_all(
			"Bucket Request Trip",
			filters={"route": doc.name, "status": ["!=", "Received"]},
			fields=["name", "run"],
		):
			if new_runs.get(t.run) != old_runs.get(t.run):
				return {
					"status": "error",
					"message": "Trip {0} of {1} is planned as {2} — keep that trip's farms ({3}) or delete the trip first.".format(
						t.run, doc.name, t.name, ", ".join(old_runs.get(t.run) or [])
					),
				}
	if doc is None:
		doc = frappe.new_doc("Bucket Logistics Route")
		doc.route_date = date
		doc.vehicle = vehicle
	if from_datetime or to_datetime:
		if not (from_datetime and to_datetime):
			return {"status": "error", "message": "Set both From and To."}
		doc.from_datetime, doc.to_datetime = from_datetime, to_datetime
	elif not (doc.from_datetime and doc.to_datetime):
		doc.from_datetime, doc.to_datetime = _day_window(date)

	doc.set("legs", [])
	total_km = 0.0
	for leg in path:
		doc.append("legs", leg)
		total_km += float(leg["distance_km"] or 0)
	doc.total_km = total_km
	doc.auto_planned = 1 if auto_planned else 0
	if template:
		doc.template = template
	try:
		doc.save(ignore_permissions=True)
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return {"status": "error", "reason": "overlap", "message": str(e)}
	frappe.db.commit()  # nosemgrep: frappe-manual-commit

	return {
		"status": "success",
		"name": doc.name,
		"legs": len(doc.legs),
		"total_km": total_km,
	}


@frappe.whitelist()
def getBucketLogisticsRoutes():
	# Every truck's Bucket Logistics Route for one date (default today), legs in
	# driving order — for the Truck Routes list / route builder.
	date = frappe.form_dict.get("date") or frappe.utils.today()
	ensure_day_routes(date)
	out = []
	for name in frappe.get_all(
		"Bucket Logistics Route",
		filters={"route_date": date},
		pluck="name",
		order_by="vehicle asc, from_datetime asc",
	):
		doc = frappe.get_doc("Bucket Logistics Route", name)
		legs = [
			{
				"leg": l.leg,
				"from_farm": l.from_farm,
				"to_farm": l.to_farm,
				"distance_km": l.distance_km,
				"run": l.get("run"),
			}
			for l in doc.legs
		]
		stops = [legs[0]["from_farm"]] + [l["to_farm"] for l in legs] if legs else []
		out.append(
			{
				"name": doc.name,
				"vehicle": doc.vehicle,
				"route_date": str(doc.route_date),
				"from_datetime": _dt(doc.from_datetime),
				"to_datetime": _dt(doc.to_datetime),
				"total_km": doc.total_km,
				"auto_planned": int(doc.get("auto_planned") or 0),
				"legs": legs,
				"stops": stops,
				"runs": [r["stops"] for r in route_runs(legs)],
				"farms": _route_farms(doc.legs),
				"modified": str(doc.modified),
			}
		)
	frappe.response["message"] = {"status": "success", "date": str(date), "routes": out}


@frappe.whitelist(methods=["POST"])
def saveTransferTruck():
	# Add a truck from the Remote Transfers page. `truck_type` says what it is used for:
	# "internal" (remote transfers — Internal Logistics Truck?), "dispatch" (Dispatch
	# Truck?) or "both"; left out (capacity edit) it stays an internal truck and its
	# dispatch flag is untouched. An existing registration is updated.
	fd = frappe.form_dict
	plate = (fd.get("license_plate") or "").strip().upper()
	if not plate:
		frappe.response["message"] = {"status": "error", "message": "Registration is required."}
		return
	truck_type = (fd.get("truck_type") or "").strip().lower()
	if truck_type and truck_type not in ("internal", "dispatch", "both"):
		frappe.response["message"] = {"status": "error", "message": "Unknown truck type."}
		return
	internal = truck_type in ("", "internal", "both")
	trolleys = frappe.utils.cint(fd.get("trolley_capacity"))
	per = frappe.utils.cint(fd.get("buckets_per_trolley"))
	if internal and (trolleys <= 0 or per <= 0):
		frappe.response["message"] = {
			"status": "error",
			"message": "Trolleys and buckets per trolley must be more than 0.",
		}
		return
	exists = frappe.db.exists("Vehicle", plate)
	if not frappe.has_permission("Vehicle", "write" if exists else "create"):
		frappe.response["message"] = {"status": "error", "message": "You can't add vehicles."}
		return
	if exists:
		doc = frappe.get_doc("Vehicle", plate)
	else:
		doc = frappe.new_doc("Vehicle")
		doc.license_plate = plate
		doc.make = (fd.get("make") or "").strip() or "Unknown"
		doc.model = (fd.get("model") or "").strip() or "Unknown"
		doc.fuel_type = fd.get("fuel_type") or "Diesel"
		doc.uom = "Litre" if frappe.db.exists("UOM", "Litre") else "Nos"
		doc.last_odometer = 0
	doc.custom_is_internal_logistics_truck = 1 if internal else 0
	if truck_type:
		doc.custom_dispatch_truck = 1 if truck_type in ("dispatch", "both") else 0
	if trolleys > 0 and per > 0:
		doc.custom_trolley_capacity = trolleys
		doc.custom_buckets_per_trolley = per
	try:
		doc.save()
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		frappe.response["message"] = {"status": "error", "message": str(e)}
		return
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": doc.name,
		"updated": 1 if exists else 0,
		"capacity_buckets": trolleys * per,
		"internal": 1 if internal else 0,
	}


@frappe.whitelist(methods=["POST"])
def deleteBucketLogisticsRoute():
	# Remove a truck's route for a day. The truck is then not used by ⚡ Distribute
	# for that day until it is routed again; trips already planned are untouched.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Bucket Logistics Route", name):
		frappe.response["message"] = {"status": "error", "message": "Route not found."}
		return
	live = frappe.get_all(
		"Bucket Request Trip",
		filters={"route": name, "status": ["!=", "Received"]},
		fields=["name", "run", "status"],
		order_by="run asc",
	)
	if live:
		frappe.response["message"] = {
			"status": "error",
			"message": "Route {0} still has trips: {1} — delete or finish them first.".format(
				name, ", ".join("trip {0} {1} ({2})".format(t.run, t.name, t.status) for t in live)
			),
		}
		return
	# Finished trips keep their record; only the link check is skipped.
	frappe.delete_doc("Bucket Logistics Route", name, ignore_permissions=True, force=1)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


@frappe.whitelist(methods=["POST"])
def addFarmTrip():
	# Send a truck to one farm: a new trip (packhouse → farm → packhouse) added to the end
	# of its latest route on `date`, or a new all-day route when it has none that day.
	# The truck's saved route gets the trip too, so it is driven every day from now on.
	# Returns the route and the new trip's number, so buckets can be planned straight in.
	fd = frappe.form_dict
	vehicle, farm = fd.get("vehicle"), fd.get("farm")
	date = str(frappe.utils.getdate(fd.get("date") or frappe.utils.today()))
	if not vehicle or not farm:
		frappe.response["message"] = {"status": "error", "message": "A truck and a farm are required."}
		return
	ensure_day_routes(date)
	hub = transfer_hub()
	road = _farm_distance_between(hub, farm)
	if not road:
		frappe.response["message"] = {
			"status": "error",
			"message": "No road between {0} and {1} — add it under Road network on the Truck routes tab.".format(
				hub, farm
			),
		}
		return
	latest = frappe.get_all(
		"Bucket Logistics Route",
		filters={"vehicle": vehicle, "route_date": date},
		pluck="name",
		order_by="from_datetime desc",
		limit=1,
	)
	if latest:
		legs = frappe.get_all(
			"Bucket Logistics Route Leg",
			filters={"parent": latest[0], "parenttype": "Bucket Logistics Route"},
			pluck="leg",
			order_by="idx asc",
		)
		res = _save_route(date=date, vehicle=vehicle, leg_names=legs + [road, road], name=latest[0])
	else:
		res = _save_route(date=date, vehicle=vehicle, leg_names=[road, road], new=1)
	if res.get("status") == "success":
		res["route"] = res["name"]
		res["run"] = len(_route_runs_by_name(res["name"]))
		res["saved_route"] = _save_day_route_as_template(res["name"])
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = res


def _save_day_route_as_template(day_route):
	"""Carry a day route's trips back to its saved route (or save it as a new one), so the
	change applies every day. Copies already made for later days are made again; the
	day's own copy is kept (trips are being planned onto it). Returns the saved route's
	name, or "" when it can't be saved (its time overlaps another saved route)."""
	day = frappe.get_doc("Bucket Logistics Route", day_route)
	legs = [l.leg for l in day.legs if l.leg]
	path, err = _route_path(legs)
	if err or not path:
		return ""
	start, end = _dt(day.from_datetime)[11:] or "00:00", _dt(day.to_datetime)[11:] or "23:59"
	tpl = frappe.get_doc(TEMPLATE, day.template) if day.get("template") and frappe.db.exists(TEMPLATE, day.template) else None
	if tpl is None:
		for o in frappe.get_all(TEMPLATE, filters={"vehicle": day.vehicle, "active": 1}, fields=["from_time", "to_time"]):
			if _windows_clash(start, end, _hhmm(o.from_time), _hhmm(o.to_time)):
				return ""
		tpl = frappe.new_doc(TEMPLATE)
		tpl.vehicle, tpl.active = day.vehicle, 1
		tpl.from_time, tpl.to_time = start + ":00", end + ":00"
	tpl.set("legs", [])
	for leg in path:
		tpl.append("legs", leg)
	tpl.total_km = sum(float(l["distance_km"] or 0) for l in path)
	tpl.save(ignore_permissions=True)
	if day.get("template") != tpl.name:
		frappe.db.set_value("Bucket Logistics Route", day.name, "template", tpl.name, update_modified=False)
	for r in frappe.get_all(
		"Bucket Logistics Route",
		filters={"template": tpl.name, "route_date": [">", day.route_date]},
		pluck="name",
	):
		if _day_copy_unused(r):
			frappe.delete_doc("Bucket Logistics Route", r, ignore_permissions=True, force=1)
	return tpl.name


# ---- Saved routes (no date) ---------------------------------------------------------
# A truck's route is saved once, with times of day, and used every day: the first time a
# day is planned (Distribute, Add trip, the farm app, automatic scheduling) each active
# saved route is copied onto it as a dated Bucket Logistics Route, which trips link to.

TEMPLATE = "Bucket Logistics Route Template"


def _hhmm(value):
	"""A Time value as "HH:MM" ("6:00:00" and timedelta both)."""
	if value is None or value == "":
		return ""
	if hasattr(value, "total_seconds"):
		mins = int(value.total_seconds() // 60)
		return "{0:02d}:{1:02d}".format(mins // 60 % 24, mins % 60)
	parts = str(value).split(":")
	return "{0:02d}:{1:02d}".format(int(parts[0]), int(parts[1] if len(parts) > 1 else 0))


def _window_on(date, start, end):
	"""(from, to) datetimes of a times-of-day window on `date`; To at or before From
	runs into the next day."""
	spill = 1 if end <= start else 0
	return "{0} {1}:00".format(date, start), "{0} {1}:00".format(frappe.utils.add_days(date, spill), end)


def _minutes_window(start, end):
	a = int(start[:2]) * 60 + int(start[3:5])
	b = int(end[:2]) * 60 + int(end[3:5])
	return a, (b if b > a else b + 1440)


def _windows_clash(s1, e1, s2, e2):
	"""Two times-of-day windows overlap on some day (a window may run past midnight)."""
	a1, b1 = _minutes_window(s1, e1)
	a2, b2 = _minutes_window(s2, e2)
	return any(a1 < b2 + k and a2 + k < b1 for k in (-1440, 0, 1440))


def _template_dict(doc):
	legs = [
		{"leg": l.leg, "from_farm": l.from_farm, "to_farm": l.to_farm, "distance_km": l.distance_km, "run": l.get("run")}
		for l in doc.legs
	]
	return {
		"name": doc.name,
		"vehicle": doc.vehicle,
		"active": int(doc.active or 0),
		"from_time": _hhmm(doc.from_time),
		"to_time": _hhmm(doc.to_time),
		"total_km": doc.total_km,
		"legs": legs,
		"stops": ([legs[0]["from_farm"]] + [l["to_farm"] for l in legs]) if legs else [],
		"runs": [r["stops"] for r in route_runs(legs)],
		"farms": _route_farms(doc.legs),
		"modified": str(doc.modified),
	}


@frappe.whitelist()
def getRouteTemplates():
	# Every saved route, by truck then start time — the Truck routes list.
	out = [
		_template_dict(frappe.get_doc(TEMPLATE, n))
		for n in frappe.get_all(TEMPLATE, pluck="name", order_by="vehicle asc, from_time asc")
	]
	frappe.response["message"] = {"status": "success", "routes": out}


def _day_copy_unused(name):
	"""A day copy no trip has used yet — safe to drop and make again."""
	return not frappe.db.exists("Bucket Request Trip", {"route": name})


def _drop_future_copies(template):
	"""Remove the template's day copies from today on that no trip uses, so the next
	plan makes them again from the saved route as it is now. Used copies are kept."""
	for r in frappe.get_all(
		"Bucket Logistics Route",
		filters={"template": template, "route_date": [">=", frappe.utils.today()]},
		pluck="name",
	):
		if _day_copy_unused(r):
			frappe.delete_doc("Bucket Logistics Route", r, ignore_permissions=True, force=1)


@frappe.whitelist(methods=["POST"])
def saveRouteTemplate():
	# Create or edit a saved route: vehicle, legs ("|~|"-joined Farm Distance names in
	# driving order), from_time / to_time ("HH:MM"), active. `name` edits that route.
	fd = frappe.form_dict
	vehicle = fd.get("vehicle")
	start, end = (fd.get("from_time") or "")[:5], (fd.get("to_time") or "")[:5]
	if not vehicle:
		frappe.response["message"] = {"status": "error", "message": "A truck is required."}
		return
	if len(start) != 5 or len(end) != 5 or start == end:
		frappe.response["message"] = {"status": "error", "message": "Set a From and a To time."}
		return
	path, err = _route_path((fd.get("legs") or "").split("|~|"))
	if err or not path:
		frappe.response["message"] = {"status": "error", "message": err or "Add at least one farm."}
		return
	name = fd.get("name")
	for o in frappe.get_all(
		TEMPLATE, filters={"vehicle": vehicle, "active": 1, "name": ["!=", name or ""]}, fields=["name", "from_time", "to_time"]
	):
		if _windows_clash(start, end, _hhmm(o.from_time), _hhmm(o.to_time)):
			frappe.response["message"] = {
				"status": "error",
				"message": "{0} already has route {1} from {2} to {3} — pick a time that doesn’t overlap.".format(
					vehicle, o.name, _hhmm(o.from_time), _hhmm(o.to_time)
				),
			}
			return
	doc = frappe.get_doc(TEMPLATE, name) if name and frappe.db.exists(TEMPLATE, name) else frappe.new_doc(TEMPLATE)
	if doc.name and doc.vehicle and doc.vehicle != vehicle:
		frappe.response["message"] = {"status": "error", "message": "Route {0} belongs to {1}.".format(doc.name, doc.vehicle)}
		return
	doc.vehicle = vehicle
	doc.from_time, doc.to_time = start + ":00", end + ":00"
	if fd.get("active") is not None:
		doc.active = cint(fd.get("active"))
	doc.set("legs", [])
	for leg in path:
		doc.append("legs", leg)
	doc.total_km = sum(float(l["distance_km"] or 0) for l in path)
	doc.save(ignore_permissions=True)
	_drop_future_copies(doc.name)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": doc.name, "total_km": doc.total_km}


@frappe.whitelist(methods=["POST"])
def setRouteTemplateActive():
	# Turn a saved route on or off (off = not copied onto new days).
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists(TEMPLATE, name):
		frappe.response["message"] = {"status": "error", "message": "Route not found."}
		return
	frappe.db.set_value(TEMPLATE, name, "active", 1 if cint(fd.get("active")) else 0)
	if not cint(fd.get("active")):
		_drop_future_copies(name)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


@frappe.whitelist(methods=["POST"])
def deleteRouteTemplate():
	# Delete a saved route. Its unused day copies go too; copies a trip already uses stay
	# (the trip needs them) but no longer point at it.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists(TEMPLATE, name):
		frappe.response["message"] = {"status": "error", "message": "Route not found."}
		return
	_drop_future_copies(name)
	frappe.db.sql("UPDATE `tabBucket Logistics Route` SET template = NULL WHERE template = %s", name)
	frappe.delete_doc(TEMPLATE, name, ignore_permissions=True, force=1)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


def ensure_day_routes(date=None):
	"""Copy every active saved route onto `date` (default today) unless it already has
	its copy. A copy that clashes with a route the truck already has that day is
	skipped. Runs once per date per request."""
	date = str(frappe.utils.getdate(date or frappe.utils.today()))
	done = frappe.flags.setdefault("rt_day_routes", set())
	if date in done:
		return
	done.add(date)
	if not frappe.db.table_exists(TEMPLATE):
		return
	have = set(
		frappe.get_all(
			"Bucket Logistics Route",
			filters={"route_date": date, "template": ["is", "set"]},
			pluck="template",
		)
	)
	made = 0
	for t in frappe.get_all(TEMPLATE, filters={"active": 1}, fields=["name", "vehicle", "from_time", "to_time"]):
		if t.name in have:
			continue
		legs = frappe.get_all(
			"Bucket Logistics Route Leg", filters={"parent": t.name, "parenttype": TEMPLATE}, pluck="leg", order_by="idx asc"
		)
		if not legs:
			continue
		from_dt, to_dt = _window_on(date, _hhmm(t.from_time), _hhmm(t.to_time))
		res = _save_route(
			date=date,
			vehicle=t.vehicle,
			leg_names=legs,
			new=1,
			from_datetime=from_dt,
			to_datetime=to_dt,
			template=t.name,
			check_on_road=False,
		)
		if res.get("status") == "success":
			made += 1
	if made:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit


def _farm_distance_between(a, b):
	"""The Farm Distance record for the pair in either direction (roads are two-way)."""
	return frappe.db.get_value("Farm Distance", {"from_farm": a, "to_farm": b}) or frappe.db.get_value(
		"Farm Distance", {"from_farm": b, "to_farm": a}
	)


@frappe.whitelist(methods=["POST"])
def saveFarmDistance():
	# Add a road to the network the truck routes are built from — or update the km /
	# road-leg flag of an existing pair (either direction). is_road_leg=1 is a real
	# road a truck drives; 0 is a direct-distance figure used for totals only.
	fd = frappe.form_dict
	a = (fd.get("from_farm") or "").strip()
	b = (fd.get("to_farm") or "").strip()
	km = frappe.utils.flt(fd.get("distance_km"))
	road = 1 if frappe.utils.cint(fd.get("is_road_leg")) else 0

	if not a or not b:
		frappe.response["message"] = {"status": "error", "message": "Pick both farms."}
		return
	if a == b:
		frappe.response["message"] = {"status": "error", "message": "A road needs two different farms."}
		return
	missing = [f for f in (a, b) if not frappe.db.exists("Farm", f)]
	if missing:
		frappe.response["message"] = {"status": "error", "message": "Unknown farm: " + ", ".join(missing)}
		return
	if km <= 0:
		frappe.response["message"] = {"status": "error", "message": "Distance must be more than 0 km."}
		return

	name = _farm_distance_between(a, b)
	doc = frappe.get_doc("Farm Distance", name) if name else frappe.new_doc("Farm Distance")
	if not name:
		doc.from_farm, doc.to_farm = a, b
	doc.distance_km = km
	doc.is_road_leg = road
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": doc.name,
		"updated": bool(name),
		"via_farms": doc.get("via_farms") or "",
	}


@frappe.whitelist(methods=["POST"])
def deleteFarmDistance():
	# Remove a road. Refused while a saved truck route still drives it — deleting it
	# would silently break that route's legs.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Farm Distance", name):
		frappe.response["message"] = {"status": "error", "message": "Road not found."}
		return
	used = frappe.get_all(
		"Bucket Logistics Route Leg", filters={"leg": name}, pluck="parent", distinct=True, limit=5
	)
	if used:
		frappe.response["message"] = {
			"status": "error",
			"message": "{0} is used by route(s) {1} — change those routes first.".format(
				name, ", ".join(used)
			),
		}
		return
	frappe.delete_doc("Farm Distance", name, ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


def _mark_trip_in_transit(doc):
	"""Flag the buckets a dispatched trip carries: in_transit=1, transit_truck=vehicle.

	A trip row says "N buckets of order X from farm F", not which buckets, so the
	first N open buckets of that (order, farm) in pick-list order are taken.
	awaiting_transfer is deliberately left at 1 — see the module header.
	Returns how many buckets were flagged, and any shortfall per row."""
	opls = list({o.order_pick_list for o in doc.orders if o.order_pick_list})
	all_buckets = _transfer_buckets(opls)
	# Not a bucket already on a trolley (meant for another truck) or recorded on another
	# trip: dispatching this one must not claim it too.
	elsewhere = {
		k
		for k, ws in _bucket_trip_rows({(b["bucket"] or "").upper() for b in all_buckets if b["bucket"]}).items()
		if any(w.trip != doc.name for w in ws)
	}
	pool = {}
	for b in all_buckets:
		if b["open"] and not b["loaded"] and (b["bucket"] or "").upper() not in elsewhere:
			pool.setdefault((b["opl"], b["farm"]), []).append(b)
	marked, short = 0, []
	for o in doc.orders:
		bucket_list = pool.get((o.order_pick_list, o.farm or "")) or []
		take, rest = bucket_list[: int(o.buckets or 0)], bucket_list[int(o.buckets or 0) :]
		pool[(o.order_pick_list, o.farm or "")] = rest
		for b in take:
			for row_name in b["names"]:
				frappe.db.set_value(
					"Pick List Item",
					row_name,
					{"in_transit": 1, "loaded_in_trolley": 0, "transit_truck": doc.vehicle},
				)
			marked += 1
		o.loaded_buckets = len(take)
		o.loaded_stems = int(sum(b["stems"] for b in take))
		_add_trip_buckets(doc, take)
		if len(take) < int(o.buckets or 0):
			short.append(
				{
					"opl": o.order_pick_list,
					"farm": o.farm,
					"planned": int(o.buckets or 0),
					"flagged": len(take),
				}
			)
	return marked, short


@frappe.whitelist(methods=["POST"])
def changeTripVehicle():
	# Swap the truck on a planned trip — the farm loading app's "Change truck" when the
	# planned pick-up truck isn't the one that came. Only before anything is loaded
	# onto the planned truck, and only to a free transfer truck with room.
	fd = frappe.form_dict
	name, vehicle = fd.get("name"), (fd.get("vehicle") or "").strip()
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	doc = frappe.get_doc("Bucket Request Trip", name)
	if doc.status not in ACTIVE_TRIP_STATUSES:
		frappe.response["message"] = {
			"status": "error",
			"message": "Trip {0} is already {1} — its truck can't be changed.".format(name, doc.status),
		}
		return
	if vehicle == doc.vehicle:
		frappe.response["message"] = {"status": "success", "name": name, "vehicle": vehicle}
		return
	v = frappe.db.get_value(
		"Vehicle",
		vehicle,
		["name", "custom_trolley_capacity", "custom_buckets_per_trolley", "custom_dispatch_truck"],
		as_dict=True,
	)
	if not v or int(v.custom_dispatch_truck or 0):
		frappe.response["message"] = {
			"status": "error",
			"message": "{0} is not a transfer truck.".format(vehicle),
		}
		return
	if _trip_has_loads(doc):
		frappe.response["message"] = {
			"status": "error",
			"message": "Buckets are already on {0} for trip {1} — finish loading that truck.".format(
				doc.vehicle, name
			),
		}
		return
	busy = _vehicle_on_road(vehicle)
	if busy:
		frappe.response["message"] = {
			"status": "error",
			"reason": "on_road",
			"message": _on_road_message(vehicle, busy, "loaded"),
		}
		return
	cap = int((v.custom_trolley_capacity or 0) * (v.custom_buckets_per_trolley or 0))
	if cap and int(doc.total_buckets or 0) > cap:
		frappe.response["message"] = {
			"status": "error",
			"reason": "over_capacity",
			"message": "{0} holds {1} buckets — the trip has {2}.".format(
				vehicle, cap, int(doc.total_buckets or 0)
			),
		}
		return
	_plan_lock()
	# The new truck must not already carry the same order from the same farm on
	# another open trip — that would put the same buckets on two trucks.
	mine = {(o.order_pick_list, o.farm or "") for o in doc.orders if int(o.buckets or 0) > 0}
	clash = [
		r
		for r in frappe.db.sql(
			"""SELECT t.name, o.order_pick_list, o.farm FROM `tabBucket Request Trip` t
			JOIN `tabBucket Request Trip Order` o ON o.parent = t.name
			WHERE t.vehicle = %(v)s AND t.status IN %(active)s AND t.name != %(name)s
			  AND IFNULL(o.buckets, 0) > 0""",
			{"v": vehicle, "active": ACTIVE_TRIP_STATUSES, "name": name},
			as_dict=True,
		)
		if (r.order_pick_list, r.farm or "") in mine
	]
	if clash:
		frappe.response["message"] = {
			"status": "error",
			"reason": "already_on_truck",
			"message": "{0} already has {1} from {2} planned on {3} — load it there.".format(
				vehicle, clash[0].order_pick_list, clash[0].farm, clash[0].name
			),
		}
		return
	previous = doc.vehicle
	doc.vehicle = vehicle
	doc.capacity_buckets = cap
	# The route/run belonged to the old truck: keep it and that truck's later plans
	# would keep topping up this trip, now on another truck.
	doc.route = None
	doc.run = None
	doc.add_comment("Info", "Truck changed from {0} to {1} at loading".format(previous, vehicle))
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name, "vehicle": vehicle, "previous": previous}


def _pli_flag_times(opl, row_names, bucket_id=""):
	"""When this OPL's pick rows first went on a trolley / in transit / shelved, and the
	remote shelf they left — read from the OPL's Version log (row_changed entries)."""
	out = {}
	if not row_names:
		return out
	for v in frappe.get_all(
		"Version",
		filters={"ref_doctype": "Order Pick List", "docname": opl},
		fields=["creation", "owner", "data"],
		order_by="creation asc",
	):
		try:
			data = json.loads(v.data or "{}")
		except ValueError:
			continue
		# Allocation rewrites the OPL's rows on every save: the LAST time this bucket was
		# (re)added is when it was allocated for this transfer.
		for added in data.get("added") or []:
			row = added[1] if len(added) > 1 and isinstance(added[1], dict) else {}
			if row.get("name") in row_names and (row.get("bucket") or "").lower() == bucket_id.lower():
				out["allocated"] = (v.creation, v.owner, row.get("shelf"))
		for ch in data.get("row_changed") or []:
			if len(ch) < 4 or ch[2] not in row_names:
				continue
			changes = {c[0]: c for c in ch[3] if len(c) == 3}
			for field in ("loaded_in_trolley", "in_transit", "shelved", "custom_ready_for_packing", "issued"):
				c = changes.get(field)
				if c and cint(c[2]) and not cint(c[1]):
					out.setdefault(field, (v.creation, v.owner, None))
			# The edit that ends the transfer (awaiting cleared / shelved set) carries the
			# shelf swap remote → sales farm. A bucket replacement also changes the shelf,
			# but leaves the row awaiting.
			ended = ("shelved" in changes and cint(changes["shelved"][2])) or (
				"awaiting_transfer" in changes and not cint(changes["awaiting_transfer"][2])
			)
			if ended and "shelf" in changes and "shelved_shelf" not in out:
				out["remote_shelf"] = changes["shelf"][1]
				out["shelved_shelf"] = (v.creation, v.owner, changes["shelf"][2])
	return out


def bucket_transfer_trace(bucket_id, limit=5, after_packhouse=False):
	"""A bucket's remote transfers, newest first — for bucket traceability (the app's
	getTraceability and the dashboard's Bucket Journey). One entry per order the bucket
	was moved for, each with its events in time order:
	  Awaiting transfer (allocated off a remote farm's shelf) → Not found at farm →
	  Left remote shelf → On trolley → Loaded on truck (truck, run, trip) → In transit → Shelved at sales
	  farm → Stock moved (the Remote Transfers stock entry). The source farm stays the
	  remote farm all the way; only shelving at the sales farm changes the shelf."""
	if not bucket_id:
		return []
	hub = transfer_hub(required=False) or ""
	rows = frappe.db.sql(
		"""SELECT pli.parent AS opl, opl.order_name, opl.creation AS requested_at, opl.owner AS requested_by,
		       so.customer, so.delivery_date, """
		+ FARM_EXPR
		+ """ AS farm,
		       MAX(pli.source_warehouse) AS source_warehouse, MAX(pli.shelf) AS shelf,
		       GROUP_CONCAT(pli.name) AS row_names,
		       MAX(pli.awaiting_transfer) AS awaiting, MAX(pli.loaded_in_trolley) AS on_trolley,
		       MAX(pli.trolley_id) AS trolley_id, MAX(pli.in_transit) AS in_transit,
		       MAX(pli.transit_truck) AS transit_truck,
		       MAX(pli.shelved) AS shelved, MAX(IFNULL(pli.not_found, 0)) AS not_found,
		       MAX(pli.not_found_at) AS not_found_at, MAX(pli.not_found_by) AS not_found_by,
		       MAX(IFNULL(pli.custom_ready_for_packing, 0)) AS ready, MAX(IFNULL(pli.issued, 0)) AS issued,
		       MAX(pli.custom_box_id) AS box_id,
		       SUM(pli.stock_qty) AS stems, GROUP_CONCAT(DISTINCT pli.item_code) AS variety
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE pli.parenttype = 'Order Pick List' AND UPPER(pli.bucket) = UPPER(%(b)s)
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1
		       OR pli.shelved = 1 OR IFNULL(pli.not_found, 0) = 1
		       OR EXISTS (SELECT 1 FROM `tabBucket Request Trip Bucket` tb
		                  WHERE tb.order_pick_list = pli.parent AND UPPER(tb.bucket) = UPPER(pli.bucket)))
		GROUP BY pli.parent
		ORDER BY opl.creation DESC
		LIMIT %(limit)s""",
		{"b": bucket_id, "limit": limit},
		as_dict=True,
	)
	out = []
	for r in rows:
		farm = r.farm or ""
		times = _pli_flag_times(r.opl, set((r.row_names or "").split(",")), bucket_id)
		allocated = times.get("allocated") or (r.requested_at, r.requested_by, None)
		remote_shelf = times.get("remote_shelf") or allocated[2] or (r.shelf if not int(r.shelved or 0) else "")
		if not remote_shelf or (hub and int(r.shelved or 0) and remote_shelf == r.shelf):
			# Last shelf it sat on at the remote farm before the order took it.
			remote_shelf = (
				frappe.db.sql(
					"""SELECT shelf FROM `tabShelving Log` WHERE UPPER(bucket_id) = UPPER(%(b)s)
					AND farm = %(farm)s AND COALESCE(shelved_on, creation) <= %(at)s
					ORDER BY COALESCE(shelved_on, creation) DESC LIMIT 1""",
					{"b": bucket_id, "farm": farm, "at": allocated[0] or r.requested_at},
				)
				or [[remote_shelf]]
			)[0][0]
		origin_wh = _farm_receiving_warehouse(farm, r.source_warehouse)
		stock_note = ""
		if r.source_warehouse:
			stock_note = " · stock in {0}".format(origin_wh)
			if origin_wh != r.source_warehouse:
				stock_note += " (pick row wrongly says {0})".format(r.source_warehouse)
		events = [
			{
				"stage": "Awaiting transfer",
				"datetime": str(allocated[0] or ""),
				"user": allocated[1] or "",
				"detail": "{0}{1} → {2} for {3}{4}".format(
					farm or "?",
					" shelf {0}".format(remote_shelf) if remote_shelf else "",
					hub or "packhouse",
					r.order_name or r.opl,
					stock_note,
				),
			}
		]
		if int(r.not_found or 0):
			events.append(
				{
					"stage": "Not found at farm",
					"datetime": str(r.not_found_at or ""),
					"user": r.not_found_by or "",
					"detail": "Not in the {0} cold room, no bucket to replace it — left out of the transfer".format(
						farm
					),
				}
			)
		# Taken off the remote shelf onto the trolley: the Shelving Log row of that shelf.
		left = frappe.db.sql(
			"""SELECT removed_on, modified_by, shelf FROM `tabShelving Log`
			WHERE UPPER(bucket_id) = UPPER(%(b)s) AND reason = 'Transferred (Trolley/Truck)'
			  AND removed_on >= %(since)s AND (%(farm)s = '' OR farm = %(farm)s)
			ORDER BY removed_on LIMIT 1""",
			{"b": bucket_id, "since": allocated[0] or r.requested_at, "farm": farm},
			as_dict=True,
		)
		if left:
			events.append(
				{
					"stage": "Left remote shelf",
					"datetime": str(left[0].removed_on),
					"user": left[0].modified_by or "",
					"detail": "Off {0} shelf {1}".format(farm, left[0].shelf or remote_shelf or "?"),
				}
			)
		loaded = times.get("loaded_in_trolley") or ("", "", None)
		if r.trolley_id:
			events.append(
				{
					"stage": "On trolley",
					"datetime": str(loaded[0] or ""),
					"user": loaded[1] or "",
					"detail": "Trolley {0}".format(r.trolley_id),
				}
			)
		trips = frappe.db.sql(
			"""SELECT t.name, t.vehicle, t.route, t.run, t.status, t.dispatched_at, t.received_at,
			       t.departed_stops, tb.loaded_at, tb.loaded_by, tb.shelved,
			       tb.shelved_at, tb.off_truck
			FROM `tabBucket Request Trip Bucket` tb
			JOIN `tabBucket Request Trip` t ON t.name = tb.parent
			WHERE tb.parenttype = 'Bucket Request Trip' AND tb.order_pick_list = %(opl)s
			  AND UPPER(tb.bucket) = UPPER(%(b)s)
			ORDER BY tb.loaded_at, tb.idx""",
			{"opl": r.opl, "b": bucket_id},
			as_dict=True,
		)
		for t in trips:
			info = _trip_run_info(frappe._dict(route=t.route, run=t.run)) if t.route and t.run else {}
			run_label = (
				" · trip {0} of {1} ({2})".format(info["run"], info["runs"], info["run_chain"])
				if info.get("run")
				else ""
			)
			events.append(
				{
					"stage": "Loaded on truck",
					"datetime": str(t.loaded_at or loaded[0] or ""),
					"user": t.loaded_by or loaded[1] or "",
					"detail": "{0}{1} · {2}".format(t.vehicle, run_label, t.name),
					"trip": t.name,
					"vehicle": t.vehicle,
					"run": int(t.run or 0),
				}
			)
			if t.dispatched_at:
				events.append(
					{
						"stage": "In transit",
						"datetime": str(t.dispatched_at),
						"user": "",
						"detail": "{0} left for {1}".format(t.vehicle, hub or "the packhouse"),
						"trip": t.name,
					}
				)
			if int(t.shelved or 0):
				events.append(
					{
						"stage": "Shelved at sales farm",
						"datetime": str(t.shelved_at or t.received_at or ""),
						"user": "",
						"detail": "Shelved at {0} off {1}".format(hub or "the packhouse", t.vehicle),
						"trip": t.name,
					}
				)
			elif int(t.off_truck or 0):
				events.append(
					{
						"stage": "Off the truck",
						"datetime": "",
						"user": "",
						"detail": "No longer on {0} and not shelved (unloaded or its truck flag moved)".format(
							t.vehicle
						),
						"trip": t.name,
					}
				)
		# The farm app's offline sync: "loaded" = on the truck it names, no trip row.
		if r.transit_truck and loaded[0] and not any(e["stage"] == "Loaded on truck" for e in events):
			events.append(
				{
					"stage": "Loaded on truck",
					"datetime": str(loaded[0]),
					"user": loaded[1] or "",
					"detail": r.transit_truck,
				}
			)
		went = times.get("in_transit")
		if (went or int(r.in_transit or 0)) and not any(e["stage"] == "In transit" for e in events):
			went = went or ("", "", None)
			events.append(
				{
					"stage": "In transit",
					"datetime": str(went[0] or ""),
					"user": went[1] or "",
					"detail": "On {0} to {1}".format(r.transit_truck or "the truck", hub or "the packhouse"),
				}
			)
		shelved = times.get("shelved_shelf") or times.get("shelved")
		if int(r.shelved or 0) and not any(e["stage"] == "Shelved at sales farm" for e in events):
			shelved = shelved or ("", "", None)
			events.append(
				{
					"stage": "Shelved at sales farm",
					"datetime": str(shelved[0] or ""),
					"user": shelved[1] or "",
					"detail": "Shelved at {0}".format(hub),
				}
			)
		# The shelving log knows when (and who) when the trip didn't tick it off itself.
		for e in events:
			if e["stage"] != "Shelved at sales farm":
				continue
			log = frappe.db.sql(
				"""SELECT COALESCE(shelved_on, creation) AS at, owner, shelf FROM `tabShelving Log`
				WHERE UPPER(bucket_id) = UPPER(%(b)s) AND farm = %(hub)s
				  AND COALESCE(shelved_on, creation) >= %(since)s
				ORDER BY COALESCE(shelved_on, creation) LIMIT 1""",
				{"b": bucket_id, "hub": hub, "since": r.requested_at},
				as_dict=True,
			)
			if log:
				if not e["datetime"]:
					e["datetime"], e["user"] = str(log[0].at), log[0].owner or ""
				if log[0].shelf:
					e["detail"] += " · shelf {0}".format(log[0].shelf)
			elif r.shelved and r.shelf:
				e["detail"] += " · shelf {0}".format(r.shelf)
		moved = _transfer_stock_entry(bucket_id, origin_wh, allocated[0] or r.requested_at)
		if moved:
			shelved_at = next((e["datetime"] for e in events if e["stage"] == "Shelved at sales farm"), "")
			early = not shelved_at or str(moved.at) < shelved_at[:19]
			events.append(
				{
					"stage": "Stock moved",
					"datetime": str(moved.at),
					"user": moved.owner or "",
					"detail": "{0} → {1} ({2}){3}".format(
						moved.s_warehouse,
						moved.t_warehouse,
						moved.name,
						" — before it was shelved at {0}".format(hub) if early else "",
					),
				}
			)
		# After the packhouse shelf: ready for packing (with its box) and issued — for the
		# dashboard's Bucket Journey. The farm app shows Issue as its own stage already.
		if after_packhouse and int(r.ready or 0):
			at = times.get("custom_ready_for_packing") or ("", "", None)
			events.append(
				{
					"stage": "Ready for packing",
					"datetime": str(at[0] or ""),
					"user": at[1] or "",
					"detail": "At {0}{1}".format(hub or "the packhouse", " · box {0}".format(r.box_id) if r.box_id else ""),
				}
			)
		if after_packhouse and int(r.issued or 0):
			# Issuing takes the bucket off its packhouse shelf: that Shelving Log row
			# dates it (the issued flag itself isn't versioned).
			log = frappe.db.sql(
				"""SELECT removed_on, modified_by, shelf FROM `tabShelving Log`
				WHERE UPPER(bucket_id) = UPPER(%(b)s) AND reason = 'Issued to Sales Order'
				  AND removed_on >= %(since)s ORDER BY removed_on LIMIT 1""",
				{"b": bucket_id, "since": allocated[0] or r.requested_at},
				as_dict=True,
			)
			at = (log[0].removed_on, log[0].modified_by, log[0].shelf) if log else (times.get("issued") or ("", "", None))
			events.append(
				{
					"stage": "Issued",
					"datetime": str(at[0] or ""),
					"user": at[1] or "",
					"detail": "Issued to the packhouse for {0}{1}".format(
						r.order_name or r.opl, " · off shelf {0}".format(at[2]) if at[2] else ""
					),
				}
			)
		# Logged steps (refusals, wrong-farm shelving, truck left / arrived) from the
		# Bucket Transfer Event log, for this order (or not tied to one).
		if frappe.db.table_exists("Bucket Transfer Event"):
			for ev in frappe.get_all(
				"Bucket Transfer Event",
				filters={
					"bucket": bucket_id,
					"stage": ["in", LOGGED_STAGES],
					"event_time": [">=", allocated[0] or r.requested_at],
				},
				or_filters=[["order_pick_list", "=", r.opl], ["order_pick_list", "is", "not set"]],
				fields=["stage", "outcome", "event_time", "user", "farm", "shelf", "trip", "vehicle", "details"],
				order_by="event_time asc",
			):
				bits = [ev.details or ""]
				if ev.shelf:
					bits.append("shelf {0}".format(ev.shelf))
				if ev.farm:
					bits.append("app farm {0}".format(ev.farm))
				if ev.vehicle:
					bits.append(ev.vehicle)
				events.append(
					{
						"stage": ev.stage,
						"datetime": str(ev.event_time),
						"user": ev.user or "",
						"detail": " · ".join(b for b in bits if b),
						"trip": ev.trip or "",
						"refused": ev.outcome == "Refused",
					}
				)
		# Steps in the order they happen (a step's time isn't always recorded, so time
		# alone put e.g. an undated "Loaded on truck" after "Issued"); time within a step.
		order = [
			"Awaiting transfer", "Not found at farm", "Left remote shelf", "On trolley", "Load refused",
			"Loaded on truck", "Left the farm", "In transit", "Arrived at packhouse", "Off the truck",
			"Shelving farm corrected", "Shelving refused", "Shelved at remote farm", "Removed from wrong shelf",
			"Shelved at sales farm", "Stock moved", "Ready for packing", "Issued",
		]
		events.sort(key=lambda e: (order.index(e["stage"]) if e["stage"] in order else 99, e["datetime"] or "9999"))
		state = (
			"not_found"
			if int(r.not_found or 0)
			else "issued"
			if int(r.issued or 0)
			else "arrived"
			if int(r.shelved or 0)
			else "in_transit"
			if int(r.in_transit or 0)
			else "on_trolley"
			if int(r.on_trolley or 0)
			else "at_farm"
		)
		out.append(
			{
				"opl": r.opl,
				"order_name": r.order_name or r.opl,
				"customer": r.customer or "",
				"delivery_date": str(r.delivery_date or ""),
				"from_farm": farm,
				"to_farm": hub,
				"source_warehouse": r.source_warehouse or "",
				"remote_shelf": remote_shelf or "",
				"variety": r.variety or "",
				"stems": float(r.stems or 0),
				"state": state,
				"events": events,
			}
		)
	return out


def _farm_receiving_warehouse(farm, source_warehouse):
	"""The remote farm's own cold store: the pick row's source warehouse when it is that
	farm's, else the farm's row in the warehouse mapping."""
	if source_warehouse and (
		not farm or frappe.db.get_value("Warehouse", source_warehouse, "custom_farm") == farm
	):
		return source_warehouse
	from upande_packhouse import stock_movement

	row = stock_movement.mapping_row_for_farm(farm, "Roses")
	return (row.source_warehouse if row else None) or source_warehouse


def _transfer_stock_entry(bucket_id, source_warehouse, since):
	"""The stock entry that moved this bucket out of its remote farm's cold store for
	this transfer: the first one out of `source_warehouse` after the receiving that
	preceded the order."""
	if not source_warehouse:
		return None
	received = frappe.db.sql(
		"""SELECT MAX(TIMESTAMP(se.posting_date, se.posting_time)) FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` d ON d.parent = se.name
		WHERE UPPER(se.custom_bucket_id) = UPPER(%(b)s) AND se.docstatus = 1
		  AND d.t_warehouse = %(wh)s AND se.stock_entry_type IN ('Receiving', 'Late Receipt')
		  AND TIMESTAMP(se.posting_date, se.posting_time) <= %(since)s""",
		{"b": bucket_id, "wh": source_warehouse, "since": since},
	)[0][0]
	if not received:
		return None
	row = frappe.db.sql(
		"""SELECT se.name, TIMESTAMP(se.posting_date, se.posting_time) AS at, se.owner,
		       d.s_warehouse, d.t_warehouse
		FROM `tabStock Entry` se JOIN `tabStock Entry Detail` d ON d.parent = se.name
		WHERE UPPER(se.custom_bucket_id) = UPPER(%(b)s) AND se.docstatus = 1
		  AND d.s_warehouse = %(wh)s AND TIMESTAMP(se.posting_date, se.posting_time) >= %(received)s
		ORDER BY se.posting_date, se.posting_time LIMIT 1""",
		{"b": bucket_id, "wh": source_warehouse, "received": received},
		as_dict=True,
	)
	return row[0] if row else None


@frappe.whitelist(methods=["POST"])
def markRequestedBucketNotFound():
	# Farm app: a requested bucket isn't in the cold room and there is no bucket to
	# replace it with. It is left out of the transfer (not_found, no longer awaiting),
	# the trip planned to collect it expects one bucket fewer at that farm — so the
	# stop can be finished and the order loaded with the buckets that are there.
	# Payload: pick_list_item (any row of the bucket), notes (optional).
	fd = frappe.form_dict
	frappe.response["message"] = mark_bucket_not_found(fd.get("pick_list_item"), fd.get("notes"))


def mark_bucket_not_found(pick_list_item, notes=None):
	row = (
		frappe.db.sql(
			"""SELECT pli.name, pli.parent AS opl, pli.parenttype, pli.bucket, pli.in_transit, pli.shelved,
			       """
			+ FARM_EXPR
			+ """ AS farm
			FROM `tabPick List Item` pli WHERE pli.name = %s""",
			pick_list_item,
			as_dict=True,
		)
		if pick_list_item
		else None
	)
	if not row or row[0].parenttype != "Order Pick List":
		return {"status": "error", "message": "Pick list row not found — download the picklist again."}
	row = row[0]
	if int(row.in_transit or 0) or int(row.shelved or 0):
		return {
			"status": "error",
			"message": "{0} is already {1}.".format(
				row.bucket, "shelved" if int(row.shelved or 0) else "on a truck"
			),
		}
	# The bucket is missing for every order still waiting on it (a bucket can be split
	# across orders) — every row of it (one per box) not yet on a truck or shelved.
	if row.bucket:
		waiting = frappe.db.sql(
			"""SELECT pli.name, pli.parent AS opl, """
			+ FARM_EXPR
			+ """ AS farm FROM `tabPick List Item` pli
			WHERE pli.parenttype = 'Order Pick List' AND UPPER(pli.bucket) = UPPER(%(b)s)
			  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1)
			  AND IFNULL(pli.in_transit, 0) = 0 AND IFNULL(pli.shelved, 0) = 0 AND NOT """
			+ PACKED_SQL,
			{"b": row.bucket},
			as_dict=True,
		)
	else:
		waiting = [frappe._dict(name=row.name, opl=row.opl, farm=row.farm)]
	now, user = frappe.utils.now(), frappe.session.user
	for w in waiting:
		frappe.db.set_value(
			"Pick List Item",
			w.name,
			{
				"awaiting_transfer": 0,
				"loaded_in_trolley": 0,
				"trolley_id": "",
				"not_found": 1,
				"not_found_at": now,
				"not_found_by": user,
			},
		)
	orders = {}
	for w in waiting:
		orders.setdefault(w.opl, w.farm or "")
	trips, departed = [], []
	for opl, farm in orders.items():
		frappe.get_doc("Order Pick List", opl).add_comment(
			"Info",
			"Bucket {0} not found at {1} — no bucket to replace it; left out of the transfer.{2}".format(
				row.bucket or row.name, farm, (" " + notes) if notes else ""
			),
		)
		trip, gone = _plan_one_fewer(opl, farm, row.bucket)
		if trip:
			trips.append(trip)
			departed += gone
	trip = trips[0] if trips else None
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {
		"status": "success",
		"message": "{0} marked not found.".format(row.bucket or "Bucket"),
		"bucket": row.bucket,
		"orders": list(orders),
		"trip": trip,
		"trips": trips,
		"departed": departed,
	}


def _plan_one_fewer(opl, farm, bucket):
	"""The earliest still-planned trip collecting this order at this farm expects one
	bucket fewer (so the stop can finish). Returns (trip, stops it left) or (None, [])."""
	for name in frappe.db.sql_list(
		"""SELECT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
		WHERE t.status IN %(active)s AND o.order_pick_list = %(opl)s AND IFNULL(o.farm, '') = %(farm)s
		  AND o.buckets > IFNULL(o.loaded_buckets, 0)
		ORDER BY t.trip_date, t.run, t.creation""",
		{"active": ACTIVE_TRIP_STATUSES, "opl": opl, "farm": farm},
	):
		doc = frappe.get_doc("Bucket Request Trip", name)
		o = next(
			x
			for x in doc.orders
			if x.order_pick_list == opl
			and (x.farm or "") == farm
			and int(x.buckets or 0) > int(x.loaded_buckets or 0)
		)
		per = (int(o.stems or 0) / int(o.buckets)) if int(o.buckets or 0) else 0
		o.buckets = int(o.buckets) - 1
		o.stems = max(0, round(int(o.stems or 0) - per))
		doc.total_buckets = sum(int(x.buckets or 0) for x in doc.orders)
		doc.total_stems = sum(int(x.stems or 0) for x in doc.orders)
		doc.add_comment("Info", "{0}: bucket {1} not found at {2}".format(o.order_name, bucket, farm))
		gone = _advance_stops(doc)
		doc.save(ignore_permissions=True)
		return doc.name, gone
	return None, []


@frappe.whitelist(methods=["POST"])
def closeTripStop():
	# Farm app: "Truck leaving" — the farm is done loading this trip, whatever is still
	# missing. The truck heads to the next stop of its run, or (last stop) is dispatched
	# to the packhouse. Planned buckets that didn't go on stay on the plan as left
	# behind and move to the truck's next run once this one ends.
	# `reason`: why the farm sends fewer buckets than planned (the app asks for it when
	# the stop is short). Optional here so an older app that never sends one still works.
	fd = frappe.form_dict
	name, farm = fd.get("name"), (fd.get("farm") or "").strip()
	reason = (fd.get("reason") or "").strip()[:500]
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	doc = frappe.get_doc("Bucket Request Trip", name)
	if doc.status not in ACTIVE_TRIP_STATUSES:
		frappe.response["message"] = {
			"status": "error",
			"message": "Trip {0} is already {1}.".format(name, doc.status),
		}
		return
	stops = _trip_stops(doc)
	if farm not in stops:
		frappe.response["message"] = {
			"status": "error",
			"message": "{0} is not a stop of trip {1} ({2}).".format(farm, name, ", ".join(stops)),
		}
		return
	departed = _departed(doc)
	remaining = [f for f in stops if f not in departed and f != farm]
	if not remaining and not _trip_has_loads(doc):
		frappe.response["message"] = {
			"status": "error",
			"message": "Nothing is on {0} yet — load the trolleys first.".format(doc.vehicle),
		}
		return
	rows = [o for o in doc.orders if (o.farm or "") == farm]
	planned = sum(int(o.buckets or 0) for o in rows)
	loaded = sum(int(o.loaded_buckets or 0) for o in rows)
	if reason:
		for o in rows:
			if int(o.loaded_buckets or 0) < int(o.buckets or 0):
				o.short_reason = reason
	now = frappe.utils.now()
	if farm not in departed:
		departed.append(farm)
	doc.departed_stops = ",".join(departed)
	doc.last_departed_at = now
	if remaining:
		doc.heading_to = remaining[0]
		# Stops after it that are already fully loaded are left too (and the last one
		# dispatches the trip).
		_advance_stops(doc)
		remaining = [f for f in stops if f not in _departed(doc)]
	else:
		doc.heading_to = transfer_hub(required=False) or ""
		doc.status = "Dispatched"
		doc.dispatched_at = now
	doc.add_comment(
		"Info",
		"Stop {0} closed by {1}: {2} of {3} planned buckets loaded{4}{5}".format(
			farm,
			frappe.session.user,
			loaded,
			planned,
			" — reason: {0}".format(frappe.utils.escape_html(reason)) if reason else "",
			"; dispatched" if not remaining else "",
		),
	)
	doc.save(ignore_permissions=True)
	# Traceability: each bucket of this farm on the truck left the farm now.
	for b in doc.get("trip_buckets") or []:
		if (b.farm or "") == farm:
			log_transfer_event(
				b.bucket, "Left the farm", opl=b.order_pick_list, farm=farm, trip=name, vehicle=doc.vehicle,
				details="Truck leaving {0}{1}".format(farm, " — short: {0}".format(reason) if reason else ""),
			)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": name,
		"trip_status": doc.status,
		"heading_to": doc.heading_to,
		"loaded": loaded,
		"left_behind": max(0, planned - loaded),
	}


def _match_run(runs, farms):
	"""The run a trip saved before runs existed belongs to: the first that visits all its
	farms, else the first that visits any of them."""
	for need_all in (True, False):
		for r in runs:
			hits = [f for f in farms if f in r["stops"]]
			if farms and (len(hits) == len(farms) if need_all else hits):
				return r
	return None


def _trip_groups(date):
	"""Not-yet-gone trips on `date`, grouped by the run they drive:
	{(vehicle, route, run): [trip, ...]} (earliest first). A trip without a run is put
	on the run of its truck's route that visits its farms."""
	runs_of, groups = {}, {}
	for t in frappe.get_all(
		"Bucket Request Trip",
		filters={"trip_date": date, "status": ["in", ACTIVE_TRIP_STATUSES]},
		fields=["name", "vehicle", "route", "run"],
		order_by="creation asc",
	):
		route, run = t.route or "", int(t.run or 0)
		if not run:
			runs = runs_of.setdefault(t.vehicle, [r for r in _day_runs(t.vehicle, date) if _run_open(r)])
			farms = sorted(
				set(
					frappe.get_all(
						"Bucket Request Trip Order",
						filters={"parent": t.name, "parenttype": "Bucket Request Trip"},
						pluck="farm",
					)
				)
				- {None, ""}
			)
			m = _match_run(runs, farms)
			if m:
				route, run = m["route"], m["run"]
		groups.setdefault((t.vehicle, route, run), []).append(t.name)
	return groups


def duplicate_trips(date=None):
	"""Trucks whose trips need folding together on `date` (default today): several open
	trips for the same run — the "same truck, same farm, two trips, one with 0 planned"
	mix-up, loads on one trip and the plan on another — or a trip saved before runs
	existed that belongs to one of its truck's runs. {vehicle: [trip, ...]}. Trips on
	different runs are separate trips on purpose and don't count."""
	date = date or frappe.utils.today()
	out = {}
	for (vehicle, _route, run), names in _trip_groups(date).items():
		legacy = run and any(not frappe.db.get_value("Bucket Request Trip", n, "run") for n in names)
		if len(names) > 1 or legacy:
			out.setdefault(vehicle, []).extend(names)
	return out


@frappe.whitelist(methods=["POST"])
def mergeTruckTrips():
	# Fold a truck's duplicate trips for one run (same day, not dispatched) into its
	# earliest trip: each (order, farm) becomes one row with plan and loads added up,
	# the buckets on the truck move with it, the emptied trips are deleted.
	fd = frappe.form_dict
	frappe.response["message"] = merge_truck_trips(fd.get("vehicle"), fd.get("date") or None)


def merge_truck_trips(vehicle, date=None):
	date = date or frappe.utils.today()
	todo = duplicate_trips(date).get(vehicle) or []
	if not todo:
		return {"status": "success", "merged": [], "message": "{0} has no duplicate trips.".format(vehicle)}
	cap = _vehicle_capacity(vehicle)
	kept, removed, refused = [], [], []
	for (veh, route, run), names in _trip_groups(date).items():
		if veh != vehicle or not set(names) & set(todo):
			continue
		group = [frappe.get_doc("Bucket Request Trip", n) for n in names]
		# Keep the run's own trip when it has one, else the earliest.
		group.sort(key=lambda d: (0 if int(d.get("run") or 0) else 1, str(d.creation)))
		keep, rest = group[0], group[1:]
		if run and not int(keep.get("run") or 0):
			keep.route, keep.run = route, run
		planned = sum(int(o.buckets or 0) for d in group for o in d.orders)
		if cap and planned > cap:
			refused.append(
				"{0}: {1} planned buckets don't fit one {2}-bucket load".format(
					", ".join(d.name for d in group), planned, cap
				)
			)
			continue
		rows = {}
		for d in group:
			for o in d.orders:
				key = (o.order_pick_list, o.farm or "")
				r = rows.get(key)
				if r is None:
					rows[key] = r = o.as_dict()
					for k in ("name", "parent", "idx", "creation", "modified"):
						r.pop(k, None)
					continue
				for k in ("buckets", "stems", "loaded_buckets", "loaded_stems"):
					r[k] = int(r.get(k) or 0) + int(o.get(k) or 0)
				r["full_farm_buckets"] = max(
					int(r.get("full_farm_buckets") or 0), int(o.full_farm_buckets or 0)
				)
				r["varieties"] = ", ".join(
					sorted(
						{v for v in (r.get("varieties") or "").split(", ") if v}
						| {v for v in (o.varieties or "").split(", ") if v}
					)
				)
		for r in rows.values():
			# Planned somewhere = no longer an unplanned load.
			r["unscheduled"] = 0 if int(r.get("buckets") or 0) > 0 else int(r.get("unscheduled") or 0)
			r["full_farm_buckets"] = max(int(r.get("full_farm_buckets") or 0), int(r.get("buckets") or 0))
			r["is_partial"] = 1 if int(r.get("buckets") or 0) < int(r["full_farm_buckets"]) else 0
		keep.set("orders", [])
		for r in rows.values():
			keep.append("orders", r)
		for d in rest:
			_add_trip_buckets(
				keep,
				[
					{
						"opl": b.order_pick_list,
						"bucket": b.bucket,
						"farm": b.farm,
						"loaded_at": b.get("loaded_at"),
						"loaded_by": b.get("loaded_by"),
					}
					for b in d.get("trip_buckets") or []
				],
				stamp=False,
			)
			departed = _departed(keep)
			for f in _departed(d):
				if f not in departed:
					departed.append(f)
			keep.departed_stops = ",".join(departed)
			if d.notes and d.notes not in (keep.notes or ""):
				keep.notes = ((keep.notes or "") + "\n" + d.notes).strip()
		keep.unscheduled = 1 if all(int(o.unscheduled or 0) for o in keep.orders) else 0
		keep.auto_planned = 1 if all(int(d.get("auto_planned") or 0) for d in group) else 0
		keep.capacity_buckets = cap
		keep.total_buckets = sum(int(o.buckets or 0) for o in keep.orders)
		keep.total_stems = sum(int(o.stems or 0) for o in keep.orders)
		keep.loaded_buckets = sum(int(o.loaded_buckets or 0) for o in keep.orders)
		if rest:
			keep.add_comment(
				"Info",
				"Merged {0} into this trip (same truck, same trip number)".format(", ".join(d.name for d in rest)),
			)
		# Remove the folded trips first: while they exist their buckets and claims would
		# make the merged trip look double-booked (Bucket Request Trip.validate).
		for d in rest:
			frappe.delete_doc("Bucket Request Trip", d.name, ignore_permissions=True, force=1)
			removed.append(d.name)
		keep.save(ignore_permissions=True)
		kept.append(keep.name)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	if refused and not kept:
		return {"status": "error", "message": "; ".join(refused)}
	return {
		"status": "success",
		"kept": kept,
		"merged": removed,
		"refused": refused,
		"message": "Merged {0} into {1}".format(", ".join(removed), ", ".join(kept))
		if removed
		else "{0} is now on its route's trip".format(", ".join(kept)),
	}


@frappe.whitelist(methods=["POST"])
def dispatchBucketTrip():
	# Dispatch = the truck is loaded and leaves for the sales farm. When the buckets were
	# loaded through the mobile app (loadTrolleyInTruck -> record_truck_load) they are
	# already flagged in transit and recorded on the trip, so dispatch only stamps the
	# trip; planned buckets that never went on the truck are reported as left behind.
	# A trip with nothing loaded (no app in use) flags its planned buckets here.
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	doc = frappe.get_doc("Bucket Request Trip", name)
	if doc.status not in ACTIVE_TRIP_STATUSES:
		frappe.response["message"] = {
			"status": "error",
			"message": "Trip is already {0} — cannot dispatch.".format(doc.status),
		}
		return
	busy = _vehicle_on_road(doc.vehicle, exclude_trip=name)
	if busy:
		frappe.response["message"] = {
			"status": "error",
			"reason": "on_road",
			"message": _on_road_message(doc.vehicle, busy, "dispatched again"),
		}
		return
	if _trip_has_loads(doc):
		marked = int(doc.loaded_buckets or 0)
		short = [
			{
				"opl": o.order_pick_list,
				"farm": o.farm,
				"planned": int(o.buckets or 0),
				"flagged": int(o.loaded_buckets or 0),
			}
			for o in doc.orders
			if int(o.loaded_buckets or 0) < int(o.buckets or 0)
		]
	else:
		marked, short = _mark_trip_in_transit(doc)
		doc.loaded_buckets = marked
	doc.status = "Dispatched"
	doc.dispatched_at = frappe.utils.now()
	doc.heading_to = transfer_hub(required=False) or ""
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": name,
		"trip_status": "Dispatched",
		"buckets_in_transit": marked,
		"short": short,
	}


@frappe.whitelist(methods=["POST"])
def receiveBucketTrip():
	# Receive = the truck is back. The buckets themselves only count as arrived once
	# they are SHELVED at the sales farm (mobile shelveBucket clears in_transit and
	# sets shelved) — this just closes the trip and reports what is still unshelved.
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	doc = frappe.get_doc("Bucket Request Trip", name)
	if doc.status != "Dispatched":
		frappe.response["message"] = {
			"status": "error",
			"message": "Trip is {0} — must be Dispatched before it can be received.".format(doc.status),
		}
		return
	frappe.db.set_value(
		"Bucket Request Trip", name, {"status": "Received", "received_at": frappe.utils.now()}
	)
	moved = carry_over_to_next_run(name)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	opls = list({o.order_pick_list for o in doc.orders if o.order_pick_list})
	unshelved = sum(
		1 for b in _transfer_buckets(opls) if b["on_road"] and (b["truck"] or "") == (doc.vehicle or "")
	)
	frappe.response["message"] = {
		"status": "success",
		"name": name,
		"trip_status": "Received",
		"unshelved_on_truck": unshelved,
		"carried_to": moved,
	}


def _trip_shelf_state(doc):
	"""Each bucket the trip carried, and whether it is shelved at the hub yet (read only)."""
	rows = doc.get("trip_buckets") or []
	if not rows:
		return []
	states = {
		(r.parent, r.bucket): r
		for r in frappe.db.sql(
			"""SELECT parent, UPPER(bucket) AS bucket, MAX(IFNULL(shelved, 0)) AS shelved,
			       MAX(IFNULL(in_transit, 0)) AS in_transit
			FROM `tabPick List Item`
			WHERE parenttype = 'Order Pick List' AND parent IN %(opls)s AND bucket IN %(buckets)s
			GROUP BY parent, UPPER(bucket)""",
			{
				"opls": tuple({r.order_pick_list for r in rows}),
				"buckets": tuple({r.bucket for r in rows}),
			},
			as_dict=True,
		)
	}
	out = []
	for r in rows:
		st = states.get((r.order_pick_list, (r.bucket or "").upper()))
		shelved = bool(r.shelved or (st and int(st.shelved)))
		out.append(
			{
				"bucket": r.bucket,
				"opl": r.order_pick_list,
				"farm": r.farm or "",
				"shelved": shelved,
				# Left the truck without being shelved (unloaded / swapped): not waited on.
				"off_truck": not shelved and not (st and int(st.in_transit)),
			}
		)
	return out


@frappe.whitelist(methods=["POST"])
def tripArrival(name=None, farm=None, action="status"):
	"""Farm app, In Transit: the truck reached the transfer hub.

	action "status"   — arrival time and every carried bucket's shelved state.
	action "arrive"   — the farm confirms the truck is at the hub (stamped once).
	action "complete" — end the trip (Received), only once every bucket it carried is
	                    shelved at the hub; otherwise the unshelved buckets are listed."""
	name = name or frappe.form_dict.get("name")
	action = (action or frappe.form_dict.get("action") or "status").strip()
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		return {"status": "error", "message": "Trip not found."}
	doc = frappe.get_doc("Bucket Request Trip", name)
	hub = transfer_hub(required=False) or "the packhouse"
	if doc.status not in ("Dispatched", "Received"):
		return {"status": "error", "message": "Trip {0} has not left for {1} yet.".format(name, hub)}

	if action == "arrive" and doc.status == "Dispatched" and not doc.get("arrived_at"):
		doc.arrived_at = frappe.utils.now()
		doc.arrived_by = frappe.session.user
		# Traceability: every bucket still on this truck reached the packhouse now.
		for b in doc.get("trip_buckets") or []:
			if not b.get("off_truck") and not b.get("shelved"):
				log_transfer_event(
					b.bucket, "Arrived at packhouse", opl=b.order_pick_list, farm=b.farm, trip=name,
					vehicle=doc.vehicle, details="Truck arrived at {0}".format(hub or "the packhouse"),
				)
		doc.add_comment(
			"Info",
			"Truck {0} arrived at {1} — confirmed by {2}".format(doc.vehicle, hub, frappe.session.user),
		)
		doc.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	elif action == "complete" and doc.status == "Dispatched":
		if not doc.get("arrived_at"):
			return {"status": "error", "message": "Confirm the truck has arrived at {0} first.".format(hub)}
		buckets = _trip_shelf_state(doc)
		waiting = [b for b in buckets if not b["shelved"] and not b["off_truck"]]
		if waiting or not any(b["shelved"] for b in buckets):
			return {
				"status": "error",
				"reason": "unshelved",
				"message": "{0} of {1} bucket(s) are not shelved at {2} yet — shelve them, then complete the trip.".format(
					len(waiting), len(buckets), hub
				),
				"unshelved": [b["bucket"] for b in waiting],
			}
		if _end_trip_if_shelved(doc):
			carry_over_to_next_run(name)
			doc.add_comment(
				"Info", "Trip completed at {0} by {1}: every bucket shelved".format(hub, frappe.session.user)
			)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
		doc.reload()

	buckets = _trip_shelf_state(doc)
	mine = [b for b in buckets if not farm or b["farm"] == farm]
	return {
		"status": "success",
		"name": name,
		"hub": hub,
		"trip_status": doc.status,
		"arrived_at": str(doc.get("arrived_at") or ""),
		"received_at": str(doc.get("received_at") or ""),
		"total": len(buckets),
		"shelved": sum(1 for b in buckets if b["shelved"]),
		"waiting": [b["bucket"] for b in buckets if not b["shelved"] and not b["off_truck"]],
		"farm_total": len(mine),
		"farm_shelved": sum(1 for b in mine if b["shelved"]),
		# Per bucket with its order's delivery date: the farm app shows the trip for the
		# delivery date on screen (a truck can carry today's and tomorrow's orders).
		"buckets": _with_delivery_dates(buckets),
	}


def _with_delivery_dates(buckets):
	dd = _opl_delivery_dates({b["opl"] for b in buckets})
	return [
		{
			"bucket": b["bucket"],
			"opl": b["opl"],
			"farm": b["farm"],
			"shelved": bool(b["shelved"]),
			"off_truck": bool(b["off_truck"]),
			"delivery_date": dd.get(b["opl"], ""),
		}
		for b in buckets
	]


def _opl_delivery_dates(opls):
	"""Order Pick List -> its Sales Order's delivery date (YYYY-MM-DD)."""
	opls = [o for o in opls if o]
	if not opls:
		return {}
	return {
		r[0]: str(r[1] or "")
		for r in frappe.db.sql(
			"""SELECT opl.name, so.delivery_date FROM `tabOrder Pick List` opl
			LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order WHERE opl.name IN %s""",
			(tuple(opls),),
		)
	}


@frappe.whitelist()
def getFarmShelvedBuckets(farm=None, days=3, delivery_date=None):
	"""Farm app "Shelved": every bucket this farm sent on a dispatched / received trip,
	per trip, and whether it is shelved at the hub yet (with the shelf and when) — so the
	remote QC sees what reached the sales farm.

	`delivery_date` (YYYY-MM-DD) keeps only buckets of orders delivering that day — the
	date the app is working on; without it, trips of the last `days`."""
	farm = (farm or frappe.form_dict.get("farm") or "").strip()
	delivery_date = delivery_date or frappe.form_dict.get("delivery_date")
	if not farm:
		return {"status": "error", "message": "farm is required", "trips": []}
	hub = transfer_hub(required=False) or ""
	day_opls = None
	if delivery_date:
		day_opls = set(
			frappe.db.sql_list(
				"""SELECT opl.name FROM `tabOrder Pick List` opl
				JOIN `tabSales Order` so ON so.name = opl.sales_order
				WHERE so.delivery_date = %s AND opl.docstatus < 2""",
				getdate(delivery_date),
			)
		)
		if not day_opls:
			return {"status": "success", "farm": farm, "hub": hub, "trips": []}
		# Trucked a day or two ahead of delivery at most; a week covers late runs.
		since = frappe.utils.add_days(getdate(delivery_date), -7)
	else:
		since = frappe.utils.add_days(frappe.utils.today(), -max(1, min(cint(days or 3), 14)))
	names = frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Bucket` b ON b.parent = t.name AND b.farm = %(farm)s
		WHERE t.status IN ('Dispatched', 'Received') AND t.trip_date >= %(since)s
		  AND (%(any)s OR b.order_pick_list IN %(opls)s)
		ORDER BY t.trip_date DESC, t.dispatched_at DESC""",
		{"farm": farm, "since": since, "any": day_opls is None, "opls": tuple(day_opls or ("",))},
	)
	trips = []
	for name in names:
		doc = frappe.get_doc("Bucket Request Trip", name)
		mine = [
			b
			for b in _trip_shelf_state(doc)
			if b["farm"] == farm and not b["off_truck"] and (day_opls is None or b["opl"] in day_opls)
		]
		if not mine:
			continue
		ids = [b["bucket"] for b in mine]
		on_shelf = {}
		if hub:
			for r in frappe.db.sql(
				"""SELECT UPPER(si.bucket_id) AS bucket, si.parent AS shelf,
				       COALESCE(si.date_added, si.creation) AS at
				FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
				WHERE s.farm = %(hub)s AND si.bucket_id IN %(ids)s""",
				{"hub": hub, "ids": tuple(ids)},
				as_dict=True,
			):
				on_shelf.setdefault(r.bucket, r)
		stamped = {(r.bucket or "").upper(): r.shelved_at for r in doc.trip_buckets if r.shelved_at}
		orders = {o.order_pick_list: o.order_name or o.order_pick_list for o in doc.orders}
		buckets = []
		for b in mine:
			key = (b["bucket"] or "").upper()
			shelf = on_shelf.get(key)
			at = (stamped.get(key) or (shelf.at if shelf else "")) if b["shelved"] else ""
			buckets.append(
				{
					"bucket": b["bucket"],
					"opl": b["opl"],
					"order_name": orders.get(b["opl"]) or b["opl"],
					"shelved": b["shelved"],
					"shelf": shelf.shelf if shelf else "",
					"shelved_at": str(at or ""),
				}
			)
		# Waiting first, then by when they were shelved.
		buckets.sort(key=lambda x: (x["shelved"], x["shelved_at"] or "", x["bucket"]))
		trips.append(
			{
				"trip": name,
				"vehicle": doc.vehicle,
				"status": doc.status,
				"trip_date": str(doc.trip_date or ""),
				"dispatched_at": str(doc.get("dispatched_at") or ""),
				"arrived_at": str(doc.get("arrived_at") or ""),
				"received_at": str(doc.get("received_at") or ""),
				"total": len(buckets),
				"shelved": sum(1 for x in buckets if x["shelved"]),
				"buckets": buckets,
			}
		)
	return {"status": "success", "farm": farm, "hub": hub, "trips": trips}


def repair_remote_source_warehouse(dry_run=1, delivery_date=None):
	"""Point remote farms' pick rows and shelf rows back at their own farm's cold store.

	Shelving at a remote farm used to post the Arrival leg straight away, so its Shelf
	Item (and every pick row allocated off it) said "Kapkolia Receiving" while the bucket
	still sat at the farm — the farm's bucket requests never saw those buckets. Only
	rows still at the farm (not shelved at the sales farm) are touched; posted stock
	entries are left as they are (later legs skip what has already moved).

	bench --site kaitet execute upande_packhouse.api.transfer_control.repair_remote_source_warehouse \\
	    --kwargs "{'dry_run': 0, 'delivery_date': '2026-10-03'}"
	"""
	from upande_packhouse import stock_movement

	own = {}

	def own_wh(farm):
		if farm not in own:
			row = stock_movement.mapping_row_for_farm(farm, "Roses")
			own[farm] = row.source_warehouse if row else None
		return own[farm]

	cond, args = "", {}
	if delivery_date:
		cond = " AND so.delivery_date = %(dd)s"
		args["dd"] = delivery_date
	plis = frappe.db.sql(
		"""SELECT pli.name, pli.parent, pli.bucket, pli.farm, pli.source_warehouse, pli.shelf
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
		JOIN `tabWarehouse` w ON w.name = pli.source_warehouse
		WHERE pli.parenttype = 'Order Pick List' AND IFNULL(pli.farm, '') != ''
		  AND w.custom_farm != pli.farm AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0"""
		+ cond,
		args,
		as_dict=True,
	)
	shelf_items = frappe.db.sql(
		"""SELECT si.name, si.parent, si.bucket_id, si.variety, s.farm, si.warehouse
		FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		JOIN `tabWarehouse` w ON w.name = si.warehouse
		WHERE IFNULL(s.farm, '') != '' AND w.custom_farm != s.farm AND COALESCE(si.stem_qty, 0) > 0""",
		as_dict=True,
	)
	fixed = {"pick_rows": [], "shelf_items": []}
	for r in plis:
		wh = own_wh(r.farm)
		if wh and wh != r.source_warehouse:
			fixed["pick_rows"].append({"row": r.name, "opl": r.parent, "bucket": r.bucket, "from": r.source_warehouse, "to": wh})
			if not cint(dry_run):
				frappe.db.set_value("Pick List Item", r.name, "source_warehouse", wh, update_modified=False)
	for r in shelf_items:
		wh = own_wh(r.farm)
		# Only where the ledger agrees the stems are at the farm: a row whose stems really
		# moved (e.g. shelved at Kapkolia) must keep pointing at where they are.
		if wh and wh != r.warehouse and stock_movement.bucket_balance(r.bucket_id, r.variety, wh) > 0:
			fixed["shelf_items"].append({"row": r.name, "shelf": r.parent, "bucket": r.bucket_id, "from": r.warehouse, "to": wh})
			if not cint(dry_run):
				frappe.db.set_value("Shelf Item", r.name, "warehouse", wh, update_modified=False)
	if not cint(dry_run):
		frappe.db.commit()
	return {"dry_run": bool(cint(dry_run)), "pick_row_count": len(fixed["pick_rows"]), "shelf_item_count": len(fixed["shelf_items"]), **fixed}


def _farm_rows(doc, farm):
	return [o for o in doc.orders if (o.farm or "") == farm]


@frappe.whitelist()
def getFarmCompletedTrips(farm=None, days=3):
	"""Farm app "Completed": this farm's trips the truck has left — the stop was closed
	or the trip dispatched / received — newest first, with what was planned, loaded and
	left behind (and the open trip that now carries what was left)."""
	farm = (farm or frappe.form_dict.get("farm") or "").strip()
	if not farm:
		return {"status": "error", "message": "farm is required", "trips": []}
	since = frappe.utils.add_days(frappe.utils.today(), -int(days or 3))
	names = frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.farm = %(farm)s
		WHERE t.trip_date >= %(since)s
		  AND (t.status IN ('Dispatched', 'Received') OR FIND_IN_SET(%(farm)s, IFNULL(t.departed_stops, '')))
		ORDER BY t.trip_date DESC, t.run DESC""",
		{"farm": farm, "since": since},
	)
	out = []
	for name in names:
		doc = frappe.get_doc("Bucket Request Trip", name)
		rows = _farm_rows(doc, farm)
		planned = sum(int(o.buckets or 0) for o in rows)
		loaded = sum(int(o.loaded_buckets or 0) for o in rows)
		# Per order, the same count reopenTripStop uses: an order loaded over its plan
		# must not hide another that went short.
		left = sum(max(0, int(o.buckets or 0) - int(o.loaded_buckets or 0)) for o in rows)
		carried = []
		if left:
			opls = [o.order_pick_list for o in rows if int(o.buckets or 0) > int(o.loaded_buckets or 0)]
			carried = frappe.db.sql_list(
				"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
				JOIN `tabBucket Request Trip Order` o ON o.parent = t.name
				WHERE t.name != %(name)s AND t.status IN %(active)s AND o.farm = %(farm)s
				  AND o.order_pick_list IN %(opls)s""",
				{"name": name, "active": tuple(ACTIVE_TRIP_STATUSES), "farm": farm, "opls": tuple(opls) or ("",)},
			)
		info = _trip_run_info(doc) if doc.get("route") and doc.get("run") else {}
		dds = _opl_delivery_dates({o.order_pick_list for o in rows})
		out.append(
			{
				"trip": name,
				"vehicle": doc.vehicle,
				"trip_date": str(doc.trip_date or ""),
				"status": doc.status,
				"run": int(info.get("run") or 0),
				"runs": int(info.get("runs") or 0),
				"run_chain": info.get("run_chain") or "",
				"left_at": str(doc.get("last_departed_at") or doc.get("dispatched_at") or ""),
				"dispatched_at": str(doc.get("dispatched_at") or ""),
				"planned": planned,
				"loaded": loaded,
				"left_behind": left,
				"carried_to": carried,
				# Still on its run (not yet sent to the packhouse): the stop itself can reopen.
				"stop_reopenable": doc.status in ACTIVE_TRIP_STATUSES,
				"orders": [
					{
						"opl": o.order_pick_list,
						"delivery_date": dds.get(o.order_pick_list, ""),
						"order_name": o.order_name or o.order_pick_list,
						"customer": o.customer or "",
						"varieties": o.varieties or "",
						"buckets": int(o.buckets or 0),
						"loaded": int(o.loaded_buckets or 0),
					}
					for o in rows
				],
			}
		)
	return {"status": "success", "farm": farm, "trips": out}


@frappe.whitelist(methods=["POST"])
def reopenTripStop(name=None, farm=None):
	"""Farm app: the truck left this farm (stop closed / trip dispatched) before every
	planned bucket was loaded. While the trip is still on its run the stop reopens — the
	truck is expected back and the rest can load onto it. Once the trip has gone to the
	packhouse, the buckets left behind move to the truck's next run now, so the farm can
	load them there."""
	name = name or frappe.form_dict.get("name")
	farm = (farm or frappe.form_dict.get("farm") or "").strip()
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		return {"status": "error", "message": "Trip not found."}
	doc = frappe.get_doc("Bucket Request Trip", name)
	rows = _farm_rows(doc, farm)
	if not rows:
		return {"status": "error", "message": "{0} has nothing on trip {1}.".format(farm, name)}
	left = sum(max(0, int(o.buckets or 0) - int(o.loaded_buckets or 0)) for o in rows)
	# A stop that sent every planned bucket is done: nothing to come back for, on the
	# run or after it.
	if not left:
		return {"status": "error", "message": "Every planned bucket from {0} went on {1}.".format(farm, name)}
	if doc.status in ACTIVE_TRIP_STATUSES:
		departed = [f for f in _departed(doc) if f != farm]
		if len(departed) == len(_departed(doc)):
			return {"status": "error", "message": "{0}'s stop on {1} is not closed.".format(farm, name)}
		doc.departed_stops = ",".join(departed)
		doc.heading_to = farm
		doc.add_comment("Info", "Stop {0} reopened by {1} ({2} left to load)".format(farm, frappe.session.user, left))
		doc.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return {"status": "success", "mode": "stop", "name": name, "left_behind": left, "trips": [name]}
	# Onto today's runs: the truck collects them on its next run from now.
	report = {}
	moved = _carry_over(doc, farm, frappe.utils.today(), report)
	if not moved:
		if report.get("open"):
			msg = "No run of {0} left today to take the {1} bucket(s) — plan a trip on the transfer dashboard.".format(
				doc.vehicle, report["open"]
			)
		else:
			msg = "The {0} bucket(s) left behind are already on another trip or no longer waiting.".format(left)
		return {"status": "error", "message": msg}
	doc.add_comment(
		"Info",
		"Reopened for {0} by {1}: {2} left-behind bucket(s) moved to {3}".format(
			farm, frappe.session.user, left, ", ".join(moved)
		),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"status": "success", "mode": "next_trip", "name": name, "left_behind": left, "trips": moved}


def return_early_arrivals(dry_run=1, business_unit="Roses", limit=None):
	"""Put back stock that moved to the packhouse before its bucket did.

	Remote-farm shelving used to post the Remote Transfers leg at once (and allocation
	then sold it from Kapkolia), so the ledger shows the stems at Kapkolia while the
	bucket still sits on its farm's shelf. For every such bucket — on a remote shelf,
	this harvest's, not on a trolley / truck / issued — reverse any sale of its open
	orders, move the stems back to the farm's cold store and point the shelf row there.
	Shelving at Kapkolia then posts both legs at the right time.

	bench --site kaitet execute upande_packhouse.api.transfer_control.return_early_arrivals \\
	    --kwargs "{'dry_run': 0}"
	"""
	from upande_packhouse import stock_movement as sm

	hub = transfer_hub(required=False)
	items = frappe.db.sql(
		"""SELECT si.name, si.parent, si.bucket_id, si.variety, si.stem_qty, si.warehouse,
		       si.date_added, s.farm
		FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		WHERE COALESCE(si.stem_qty, 0) > 0 AND IFNULL(s.farm, '') NOT IN ('', %(hub)s)
		ORDER BY si.date_added"""
		+ (" LIMIT {0}".format(int(limit)) if limit else ""),
		{"hub": hub or ""},
		as_dict=True,
	)
	done, skipped = [], []
	for si in items:
		row = sm.mapping_row_for_farm(si.farm, business_unit)
		farm_wh = row.source_warehouse if row else None
		route = sm.resolve_route(farm_wh, business_unit) if farm_wh else []
		arrival = next((h["to"] for h in route if h["stage"] == sm.ARRIVAL_STAGE), None)
		if not arrival:
			continue  # farm with no transfer leg
		qty = flt(si.stem_qty)
		if sm.bucket_balance(si.bucket_id, si.variety, farm_wh) + sm.QTY_TOLERANCE >= qty:
			if si.warehouse != farm_wh:
				done.append({"shelf_item": si.name, "bucket": si.bucket_id, "action": "row only"})
				if not cint(dry_run):
					frappe.db.set_value("Shelf Item", si.name, "warehouse", farm_wh, update_modified=False)
					frappe.db.commit()  # nosemgrep: frappe-manual-commit
			continue
		# This harvest's stems must have been received into the farm's own store.
		if sm.receiving_warehouse(si.bucket_id, si.variety) != farm_wh:
			skipped.append({"shelf_item": si.name, "bucket": si.bucket_id, "reason": "received elsewhere"})
			continue
		rows = frappe.db.sql(
			"""SELECT pli.name, pli.sales_order_item, pli.issued, pli.loaded_in_trolley, pli.in_transit
			FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
			WHERE pli.parenttype = 'Order Pick List' AND opl.docstatus < 2
			  AND UPPER(pli.bucket) = UPPER(%(b)s) AND pli.item_code = %(item)s
			  AND pli.modified >= %(since)s""",
			{"b": si.bucket_id, "item": si.variety, "since": si.date_added},
			as_dict=True,
		)
		if any(cint(r.issued) or cint(r.loaded_in_trolley) or cint(r.in_transit) for r in rows):
			skipped.append({"shelf_item": si.name, "bucket": si.bucket_id, "reason": "left the shelf (trolley/truck/issued)"})
			continue
		plan = {"shelf_item": si.name, "shelf": si.parent, "bucket": si.bucket_id, "item": si.variety, "qty": qty}
		if cint(dry_run):
			plan["sold_orders"] = list(dict.fromkeys(r.sales_order_item for r in rows if r.sales_order_item))
			plan["at_arrival"] = sm.bucket_balance(si.bucket_id, si.variety, arrival)
			done.append(plan)
			continue
		try:
			for so_item in dict.fromkeys(r.sales_order_item for r in rows if r.sales_order_item):
				sm.reverse_allocation_movement(
					so_item, bucket_id=si.bucket_id, item_code=si.variety, business_unit=business_unit
				)
			back = min(qty, sm.bucket_balance(si.bucket_id, si.variety, arrival))
			if back > sm.QTY_TOLERANCE:
				sm.post_transfer(
					entry_type=sm.TYPE_HOP,
					source=arrival,
					target=farm_wh,
					item_code=si.variety,
					lines=[{"bucket_id": si.bucket_id, "qty": back}],
					farm=si.farm,
					business_unit=business_unit,
					remarks="Returned to {0}: moved before the bucket left the farm".format(si.farm),
				)
			frappe.db.set_value("Shelf Item", si.name, "warehouse", farm_wh, update_modified=False)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
			plan["returned"] = back
			done.append(plan)
		except Exception as e:
			frappe.db.rollback()
			skipped.append({"shelf_item": si.name, "bucket": si.bucket_id, "reason": str(e)[:200]})
	return {"dry_run": bool(cint(dry_run)), "done": len(done), "skipped": len(skipped), "rows": done, "skips": skipped}


def clear_stale_shelf_items(dry_run=1, since="2026-09-20"):
	"""Delete Shelf Items for buckets that have left the shelf but whose row stayed
	behind (farm users had no delete on Shelf, and the trolley sync swallowed the error):
	  * on a remote farm's shelf, while its pick rows say loaded and in transit;
	  * on the sales farm's shelf, while every pick row of that variety is issued.
	Only rows shelved BEFORE that happened (a reused bucket's new harvest stays)."""
	hub = transfer_hub(required=False) or ""
	items = frappe.db.sql(
		"""SELECT si.name, si.parent, si.bucket_id, si.variety, si.date_added, s.farm
		FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		WHERE COALESCE(si.stem_qty, 0) > 0
		  AND EXISTS (SELECT 1 FROM `tabPick List Item` p WHERE p.parenttype = 'Order Pick List'
		              AND UPPER(p.bucket) = UPPER(si.bucket_id) AND p.modified >= %(since)s)""",
		{"since": since},
		as_dict=True,
	)
	removed = []
	for si in items:
		rows = frappe.db.sql(
			"""SELECT p.item_code, p.farm, p.loaded_in_trolley, p.in_transit, p.shelved, p.issued, p.modified
			FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2
			WHERE p.parenttype = 'Order Pick List' AND UPPER(p.bucket) = UPPER(%(b)s) AND p.modified >= %(since)s""",
			{"b": si.bucket_id, "since": since},
			as_dict=True,
		)
		why = None
		if si.farm and si.farm != hub:
			gone = [
				r
				for r in rows
				if cint(r.loaded_in_trolley) and cint(r.in_transit) and not cint(r.shelved)
				and not cint(r.issued) and (r.farm or "") == si.farm
			]
			if gone and str(si.date_added) < str(max(r.modified for r in gone)):
				why = "Transferred (Trolley/Truck)"
		elif si.farm == hub:
			mine = [r for r in rows if r.item_code == si.variety]
			if mine and all(cint(r.issued) for r in mine) and str(si.date_added) < str(max(r.modified for r in mine)):
				why = "Issued to Sales Order"
		if not why:
			continue
		removed.append({"shelf_item": si.name, "shelf": si.parent, "bucket": si.bucket_id, "reason": why})
		if cint(dry_run):
			continue
		frappe.db.set_value(
			"Shelving Log",
			{"shelf_item": si.name, "reason": "Shelved"},
			{"reason": why, "removed_on": frappe.utils.now_datetime()},
			update_modified=False,
		)
		frappe.delete_doc("Shelf Item", si.name, force=1, ignore_permissions=True)
		frappe.db.set_value("Shelf", si.parent, "modified", frappe.utils.now(), update_modified=False)
	if not cint(dry_run):
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"dry_run": bool(cint(dry_run)), "removed": len(removed), "rows": removed}


def fix_bucket_stock_location(dry_run=1, since="2026-09-20", business_unit="Roses"):
	"""Square each bucket's stems between its farm's cold store and the arrival store.

	Offline Issuing used to take stems from the shelf row's warehouse, which for
	remote-shelved buckets said Kapkolia Receiving (and the reverse), so a bucket can
	be negative in one of the two stores and still hold the same stems in the other.
	Move the overlap across so neither is negative. Buckets moved since `since`."""
	from upande_packhouse import stock_movement as sm

	hub = transfer_hub(required=False) or ""
	pairs = []
	for row in frappe.get_doc(sm.MAPPING_DT, frappe.db.get_value(sm.MAPPING_DT, {"business_unit": business_unit})).items:
		route = sm.resolve_route(row.source_warehouse, business_unit, upto=sm.ARRIVAL_STAGE) if row.source_warehouse else []
		if route:
			pairs.append((row.source_warehouse, route[-1]["to"]))
	fixed = []
	for farm_wh, arrival in pairs:
		bucket = sm._bucket_expr()  # line bucket where the site has it, else the entry's
		lines = frappe.db.sql(
			f"""SELECT DISTINCT {bucket} AS bucket, sed.item_code
			FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
			WHERE se.docstatus = 1 AND se.posting_date >= %(since)s
			  AND (sed.s_warehouse IN %(whs)s OR sed.t_warehouse IN %(whs)s)
			  AND {bucket} IS NOT NULL
			  AND se.stock_entry_type IN ('Offline Issuing', 'Move To Graded Sold', 'Remote Transfers', 'Material Transfer')""",
			{"since": since, "whs": (farm_wh, arrival)},
			as_dict=True,
		)
		for ln in lines:
			# A bucket Offline-Issued twice from one store is negative there: cancel the
			# repeat issue(s), newest first, while that keeps it at or above zero.
			for wh in (farm_wh, arrival):
				bal = sm.bucket_balance(ln.bucket, ln.item_code, wh)
				if bal >= -sm.QTY_TOLERANCE:
					continue
				dups = frappe.db.sql(
					"""SELECT se.name, SUM(sed.qty) AS qty FROM `tabStock Entry` se
					JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
					WHERE se.docstatus = 1 AND se.stock_entry_type = 'Offline Issuing'
					  AND se.custom_bucket_id = %(b)s AND sed.item_code = %(item)s AND sed.s_warehouse = %(wh)s
					  AND se.posting_date >= %(since)s
					GROUP BY se.name ORDER BY se.posting_date DESC, se.posting_time DESC""",
					{"b": ln.bucket, "item": ln.item_code, "wh": wh, "since": since},
					as_dict=True,
				)
				for d in dups[:-1]:  # always keep the first (real) issue
					if bal + flt(d.qty) > sm.QTY_TOLERANCE:
						break
					fixed.append({"bucket": ln.bucket, "item": ln.item_code, "cancel": d.name, "qty": flt(d.qty)})
					if cint(dry_run):
						bal += flt(d.qty)
						continue
					try:
						frappe.get_doc("Stock Entry", d.name).cancel()
						frappe.db.commit()  # nosemgrep: frappe-manual-commit
						bal += flt(d.qty)
					except Exception as e:
						frappe.db.rollback()
						fixed[-1]["error"] = str(e)[:200]
						break
			at_farm = sm.bucket_balance(ln.bucket, ln.item_code, farm_wh)
			at_arrival = sm.bucket_balance(ln.bucket, ln.item_code, arrival)
			if at_farm < -sm.QTY_TOLERANCE and at_arrival > sm.QTY_TOLERANCE:
				src, dst, qty = arrival, farm_wh, min(-at_farm, at_arrival)
			elif at_arrival < -sm.QTY_TOLERANCE and at_farm > sm.QTY_TOLERANCE:
				src, dst, qty = farm_wh, arrival, min(-at_arrival, at_farm)
			else:
				continue
			fixed.append({"bucket": ln.bucket, "item": ln.item_code, "from": src, "to": dst, "qty": qty})
			if cint(dry_run):
				continue
			try:
				sm.post_transfer(
					entry_type=sm.TYPE_HOP,
					source=src,
					target=dst,
					item_code=ln.item_code,
					lines=[{"bucket_id": ln.bucket, "qty": qty}],
					farm=frappe.db.get_value("Warehouse", farm_wh, "custom_farm"),
					business_unit=business_unit,
					remarks="Stock repair: bucket issued from the wrong cold store",
				)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit
			except Exception as e:
				frappe.db.rollback()
				fixed[-1]["error"] = str(e)[:200]
	return {"dry_run": bool(cint(dry_run)), "moves": len(fixed), "rows": fixed, "hub": hub}


def remote_shelving_block(bucket_id, farm):
	"""Why a bucket may NOT be shelved at `farm` (a remote farm), or None.

	Once a bucket's transfer has started it never goes back on that farm's shelf:
	  * an open order has it on a trolley / truck / in transit (not shelved or issued
	    yet) — it left the shelf for the packhouse;
	  * its stock already moved out of the farm's cold store after its latest
	    receiving (the Remote Transfers leg posted).
	Finished transfers (shelved / issued) don't count, so a reused bucket received
	again at the farm can be shelved there."""
	hub = transfer_hub(required=False) or ""
	if not bucket_id or not farm or (hub and farm.lower() == hub.lower()):
		return None
	moving = frappe.db.sql(
		"""SELECT pli.parent, MAX(pli.in_transit) AS transit, MAX(pli.transit_truck) AS truck
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND UPPER(pli.bucket) = UPPER(%(b)s)
		  AND (pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0
		GROUP BY pli.parent LIMIT 1""",
		{"b": bucket_id},
		as_dict=True,
	)
	if moving:
		m = moving[0]
		where = "on {0}".format(m.truck) if int(m.transit or 0) and m.truck else "on a trolley"
		return "Bucket {0} is already being transferred to {1} ({2}, {3}) — it can't go back on a {4} shelf.".format(
			bucket_id, hub or "the packhouse", where, m.parent, farm
		)
	from upande_packhouse import stock_movement as sm

	row = sm.mapping_row_for_farm(farm, "Roses")
	farm_wh = row.source_warehouse if row else None
	if farm_wh:
		received = frappe.db.sql(
			"""SELECT MAX(TIMESTAMP(se.posting_date, se.posting_time)) FROM `tabStock Entry` se
			WHERE UPPER(se.custom_bucket_id) = UPPER(%(b)s) AND se.docstatus = 1
			  AND se.stock_entry_type IN ('Receiving', 'Late Receipt')""",
			{"b": bucket_id},
		)[0][0]
		if received:
			left = frappe.db.sql(
				"""SELECT se.name FROM `tabStock Entry` se JOIN `tabStock Entry Detail` d ON d.parent = se.name
				WHERE UPPER(se.custom_bucket_id) = UPPER(%(b)s) AND se.docstatus = 1
				  AND se.stock_entry_type = %(t)s AND d.s_warehouse = %(wh)s
				  AND TIMESTAMP(se.posting_date, se.posting_time) >= %(r)s LIMIT 1""",
				{"b": bucket_id, "t": sm.TYPE_REMOTE_TRANSFER, "wh": farm_wh, "r": received},
			)
			if left:
				return "Bucket {0} was already transferred to {1} ({2}) — it can't go back on a {3} shelf.".format(
					bucket_id, hub or "the packhouse", left[0][0], farm
				)
	return None


# Steps the transfer trace can't work out from flags, trips or the stock ledger —
# taken from the Bucket Transfer Event log.
LOGGED_STAGES = (
	"Shelving farm corrected",
	"Shelving refused",
	"Shelved at remote farm",
	"Removed from wrong shelf",
	"Left the farm",
	"Arrived at packhouse",
	"Load refused",
)


def log_transfer_event(
	bucket,
	stage,
	outcome="Done",
	opl=None,
	farm=None,
	shelf=None,
	trip=None,
	vehicle=None,
	stock_entry=None,
	details=None,
):
	"""Append one step (or refused attempt) of a bucket's remote transfer to the
	Bucket Transfer Event log. Never fails the action it records."""
	if not bucket or not stage:
		return
	try:
		if not frappe.db.table_exists("Bucket Transfer Event"):
			return
		frappe.get_doc(
			{
				"doctype": "Bucket Transfer Event",
				"bucket": str(bucket).strip(),
				"stage": stage,
				"outcome": outcome,
				"event_time": frappe.utils.now(),
				"user": frappe.session.user,
				"farm": farm or "",
				"shelf": shelf or "",
				"order_pick_list": opl if opl and frappe.db.exists("Order Pick List", opl) else None,
				"trip": trip if trip and frappe.db.exists("Bucket Request Trip", trip) else None,
				"vehicle": vehicle or "",
				"stock_entry": stock_entry if stock_entry and frappe.db.exists("Stock Entry", stock_entry) else None,
				"details": details or "",
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error("Bucket transfer event not logged: {0} {1}".format(bucket, stage), frappe.get_traceback())


def open_transfer_opls(bucket):
	"""Order Pick Lists (not cancelled) on which this bucket is in an open transfer."""
	if not bucket:
		return []
	return frappe.db.sql_list(
		"""SELECT DISTINCT pli.parent FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND UPPER(pli.bucket) = UPPER(%s)
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0""",
		bucket,
	)
