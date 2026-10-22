# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Remote Transfers — Transfer Scheduling tab (/remote-transfer/transfer-scheduling):
# trips, Distribute, truck loading, dispatch and arrival. Formerly api/transfer_control
# (that path is an alias of this module); the Truck Routes endpoints moved to
# truck_routes.py.
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
import math

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

# A transfer bucket's farm is where its stock is: the farm of its source warehouse
# (kept as the remote farm's cold store until the bucket is shelved at the packhouse),
# else the row's farm. The shelf's farm can disagree (a mislabelled shelf, an old row)
# and used to put a bucket on the wrong farm's trip.
# The farm a pick row's bucket is at. One still at a remote farm: the row's own farm
# (the shelf it was allocated from) -- a reused bucket id leaves an old farm's store
# in the warehouse, which sent its transfer to a farm no truck goes to. One that has
# reached the hub (shelved, ready for packing or issued): its warehouse, which
# shelving moves to the hub's store.
_WH_FARM = "NULLIF(SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1), '')"
FARM_EXPR = (
	"(CASE WHEN IFNULL(pli.shelved, 0) = 1 OR IFNULL(pli.custom_ready_for_packing, 0) = 1 OR IFNULL(pli.issued, 0) = 1"
	" THEN COALESCE(" + _WH_FARM + ", NULLIF(pli.farm, ''))"
	" ELSE COALESCE(NULLIF(pli.farm, ''), " + _WH_FARM + ") END)"
)
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
ACTIVE_TRIP_STATUSES = ("Draft", "Requested", "Scheduled")
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
	hub = ps.get("transfer_hub_farm")
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


def order_locations(opl_names):
	"""opl -> its dispatch location (Loading Plan > Location: Ravine, Karen), from its
	Sales Order's farm -- Karen's orders vs Kapkolia and the farms trucking to it."""
	from upande_packhouse.mobile.api import dispatch_locations, location_farms

	opl_names = [o for o in opl_names or [] if o]
	if not opl_names:
		return {}
	locations = dispatch_locations()
	farms = {loc: location_farms(loc) for loc in locations}
	out = {}
	for r in frappe.db.sql(
		"""SELECT o.name, so.farm FROM `tabOrder Pick List` o
		LEFT JOIN `tabSales Order` so ON so.name = o.sales_order WHERE o.name IN %(o)s""",
		{"o": tuple(opl_names)},
		as_dict=True,
	):
		out[r.name] = next((loc for loc in locations if (r.farm or "") in farms[loc]), "")
	return out


def _schedule_map(lookback_days=14, opls=None):
	# OPL -> {team, schedule} from the most recent Packhouse Schedule containing it
	# (schedule membership only — NOT tied to the delivery-date window; a schedule is
	# created the day before delivery, so gating on the delivery window misses it).
	# This is the ONE source of schedule/team for every transfer view: the per-team
	# sequence the scheduler saved, not Order Pick List.schedule_number (a separate
	# global number the mobile setSchedulerOrder writes).
	# `opls`: only these orders' entries (same answer for each, one smaller read).
	from_date = frappe.utils.add_days(frappe.utils.today(), -lookback_days)
	if opls is not None:
		opls = tuple({o for o in opls if o})
		if not opls:
			return {}
	rows = frappe.db.sql(
		"""SELECT pso.order_pick_list AS opl, pso.sequence AS sequence, ps.team AS team
		FROM `tabPackhouse Schedule` ps
		JOIN `tabPackhouse Schedule Order` pso ON pso.parent = ps.name
		  AND pso.parenttype = 'Packhouse Schedule'
		WHERE ps.schedule_date >= %(since)s AND IFNULL(pso.order_pick_list, '') != ''"""
		+ (" AND pso.order_pick_list IN %(opls)s" if opls is not None else "")
		+ """
		ORDER BY ps.schedule_date DESC, ps.modified DESC, ps.name, pso.creation DESC""",
		{"since": from_date, "opls": opls},
		as_dict=True,
	)
	out = {}
	for r in rows:
		if r.opl not in out:
			out[r.opl] = {"team": r.team, "schedule": int(r.sequence or 0)}
	# Only orders on a Packhouse Schedule: an order goes to the farms and the planner
	# once it is scheduled (by the page, or by automatic scheduling).
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
		       pli.transit_truck AS truck,
		       IF(IFNULL(pli.transfer_priority, '') = 'ASAP', 1, 0) AS asap
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
				"asap": 0,
				"truck": None,
			}
			index[key] = entry
			out.append(entry)
		entry["stems"] += float(r.get("stems") or 0)
		entry["names"].append(r["name"])
		for flag in ("awaiting", "loaded", "in_transit", "shelved", "asap"):
			if int(r.get(flag) or 0):
				entry[flag] = 1
		entry["truck"] = entry["truck"] or r.get("truck")
	for e in out:
		e["open"] = 1 if (e["awaiting"] or e["loaded"]) and not e["in_transit"] and not e["shelved"] else 0
		e["on_road"] = 1 if e["in_transit"] and not e["shelved"] else 0
		e["rides"] = None
	# A bucket several orders take stems from travels once, for one of them (its lead):
	# the others' rows of it ride along and count nowhere as a bucket to collect.
	groups = shared_buckets({e["bucket"] for e in out if e["bucket"] and (e["open"] or e["on_road"])})
	for e in out:
		lead = shared_lead(groups, e["bucket"], e["farm"])
		if lead and lead != e["opl"] and (e["open"] or e["on_road"]):
			e["rides"] = lead
			e["open"] = e["on_road"] = 0
	return out


def shared_buckets(buckets):
	"""Buckets several live orders take stems from at one farm (a 200-stem bucket split
	100/100 over two orders): one physical bucket, requested, planned, loaded and
	counted ONCE. Its LEAD is the order it travels for; the others ride along.

	Lead, in this order: the order whose rows are already on a trolley / truck, the one
	it is recorded on a trip for, one on a Packhouse Schedule (only those are planned),
	then the earliest delivery date, schedule sequence and name. Never which trip plans
	it: the planner counts open buckets with these leads, and a lead that moved to the
	order its first saved run took made the truck's next run over-claim (and refused). Only orders not yet past their delivery
	date share: a past order's row left waiting, or one shelved / packed, is an earlier
	harvest of a reused bucket id.

	Returns {(UPPER(bucket), farm): {"lead": opl, "opls": [opl, ...], "rows": {opl: [pick row]}}}."""
	buckets = {(b or "").strip().upper() for b in buckets or [] if b}
	if not buckets:
		return {}
	lot = (
		"IFNULL(pli.loaded_on_truck, 0)" if frappe.db.has_column("Pick List Item", "loaded_on_truck") else "0"
	)
	rows = frappe.db.sql(
		"""SELECT pli.name, pli.parent AS opl, UPPER(pli.bucket) AS bucket, """
		+ FARM_EXPR
		+ """ AS farm,
		       GREATEST(IFNULL(pli.loaded_in_trolley, 0), IFNULL(pli.in_transit, 0), """
		+ lot
		+ """) AS moving, so.delivery_date AS delivery_date
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket IN %(b)s
		  AND IFNULL(pli.not_found, 0) = 0 AND IFNULL(pli.shelved, 0) = 0 AND NOT """
		+ PACKED_SQL
		+ """
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND (so.delivery_date IS NULL OR so.delivery_date >= %(today)s)""",
		{"b": tuple(buckets), "today": frappe.utils.today()},
		as_dict=True,
	)
	groups = {}
	for r in rows:
		g = groups.setdefault((r.bucket, r.farm or ""), {"rows": {}, "moving": set(), "due": {}})
		g["rows"].setdefault(r.opl, []).append(r.name)
		g["due"][r.opl] = str(r.delivery_date or "9999-12-31")
		if int(r.moving or 0):
			g["moving"].add(r.opl)
	groups = {k: g for k, g in groups.items() if len(g["rows"]) > 1}
	if not groups:
		return {}
	opls = tuple({o for g in groups.values() for o in g["rows"]})
	pinned = {
		(r.opl, r.bucket)
		for r in frappe.db.sql(
			"""SELECT tb.order_pick_list AS opl, UPPER(tb.bucket) AS bucket
			FROM `tabBucket Request Trip Bucket` tb
			JOIN `tabBucket Request Trip` t ON t.name = tb.parent
			WHERE tb.parenttype = 'Bucket Request Trip' AND tb.bucket IN %(b)s
			  AND tb.order_pick_list IN %(o)s AND t.status != 'Received'
			  AND IFNULL(tb.off_truck, 0) = 0 AND IFNULL(tb.shelved, 0) = 0""",
			{"b": tuple({k[0] for k in groups}), "o": opls},
			as_dict=True,
		)
	}
	sequence = {}
	for r in frappe.db.sql(
		"""SELECT pso.order_pick_list AS opl, pso.sequence FROM `tabPackhouse Schedule Order` pso
		JOIN `tabPackhouse Schedule` ps ON ps.name = pso.parent
		WHERE pso.order_pick_list IN %(o)s AND ps.schedule_date >= %(since)s
		ORDER BY ps.schedule_date DESC, ps.modified DESC""",
		{"o": opls, "since": frappe.utils.add_days(frappe.utils.today(), -14)},
		as_dict=True,
	):
		sequence.setdefault(r.opl, int(r.sequence or 0))
	out = {}
	for (bucket, farm), g in groups.items():

		def rank(opl, bucket=bucket, g=g):
			return (
				0 if opl in g["moving"] else 1,
				0 if (opl, bucket) in pinned else 1,
				0 if opl in sequence else 1,
				g["due"][opl],
				sequence.get(opl, 0),
				opl,
			)

		ordered = sorted(g["rows"], key=rank)
		out[(bucket, farm)] = {"lead": ordered[0], "opls": ordered, "rows": g["rows"]}
	return out


def shared_lead(groups, bucket, farm):
	"""The order a shared bucket travels for (see shared_buckets); None when not shared."""
	g = shared_group(groups, bucket, farm)
	return g["lead"] if g else None


def shared_group(groups, bucket, farm):
	"""The shared_buckets entry of this bucket at this farm, or None."""
	bucket = (bucket or "").strip().upper()
	if not bucket:
		return None
	return groups.get((bucket, farm or "")) or next(
		(g for (b, f), g in groups.items() if b == bucket and _same_farm(f, farm)), None
	)


def _open_counts(opl_names):
	"""(opl, farm) -> number of OPEN buckets: waiting at the farm, or loaded (trolley /
	truck) but not on the road yet; not shelved. A loaded one is claimed by its trip
	(_trip_claims), so it is open for no other."""
	counts = {}
	for b in _transfer_buckets(opl_names):
		if b["open"]:
			key = (b["opl"], b["farm"])
			counts[key] = counts.get(key, 0) + 1
	return counts


def _left_behind(opl_names):
	"""(opl, farm) -> buckets a truck LEFT BEHIND there: planned on a trip that has since
	left that farm (stop closed, dispatched or ended) without loading them, and still
	waiting at the farm. With the latest such trip, its truck, when it left, the farm's
	reason (short_reason) and the open trips that now carry them. These go first on the
	next trip to the farm (_carry_over, auto_transfer)."""
	if not opl_names:
		return {}
	rows = frappe.db.sql(
		"""SELECT t.name AS trip, t.vehicle AS vehicle, o.order_pick_list AS opl, o.farm AS farm,
		       GREATEST(IFNULL(o.buckets, 0) - IFNULL(o.loaded_buckets, 0), 0) AS left_n,
		       o.short_reason AS reason,
		       COALESCE(t.last_departed_at, t.dispatched_at, t.received_at, t.modified) AS left_at
		FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
		WHERE o.order_pick_list IN %(opls)s AND t.trip_date >= %(since)s AND IFNULL(o.unscheduled, 0) = 0
		  AND IFNULL(o.buckets, 0) > IFNULL(o.loaded_buckets, 0)
		  AND (t.status IN ('Dispatched', 'Received') OR FIND_IN_SET(o.farm, IFNULL(t.departed_stops, '')))
		ORDER BY left_at DESC""",
		{"opls": tuple(opl_names), "since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)},
		as_dict=True,
	)
	if not rows:
		return {}
	waiting = _open_counts(opl_names)
	out = {}
	for r in rows:
		key = (r.opl, r.farm or "")
		e = out.get(key)
		if e is None:  # the latest trip that left it
			e = out[key] = {
				"buckets": 0,
				"trip": r.trip,
				"vehicle": r.vehicle,
				"left_at": str(r.left_at or "")[:16],
				"reason": r.reason or "",
				"now_on": [],
			}
		e["buckets"] += int(r.left_n or 0)
	for key, e in out.items():
		e["buckets"] = min(e["buckets"], waiting.get(key, 0))  # loaded or shelved since: no longer behind
	out = {k: v for k, v in out.items() if v["buckets"] > 0}
	if out:
		for r in frappe.db.sql(
			"""SELECT DISTINCT t.name, t.vehicle, o.order_pick_list AS opl, o.farm
			FROM `tabBucket Request Trip` t
			JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
			WHERE t.status IN %(active)s AND t.trip_date >= %(today)s AND o.order_pick_list IN %(opls)s
			  AND IFNULL(o.buckets, 0) > IFNULL(o.loaded_buckets, 0)""",
			{
				"active": ACTIVE_TRIP_STATUSES,
				"today": frappe.utils.today(),
				"opls": tuple({k[0] for k in out}),
			},
			as_dict=True,
		):
			e = out.get((r.opl, r.farm or ""))
			if e and r.name != e["trip"]:
				e["now_on"].append({"trip": r.name, "vehicle": r.vehicle})
	return out


def _trip_claims(opl_names, exclude_trip=None, unloaded=False):
	"""(opl, farm) -> open buckets still claimed by planned trips — every active trip
	of today or later, plus any earlier one the truck already started (loads, a closed
	stop or a bucket in a trolley for it). Only an earlier day's untouched draft is
	ignored: its truck never went, so its buckets are open again (the dashboard flags it
	for cleanup).

	A bucket loaded for a trip (on its truck, recorded on it, or in a trolley it takes:
	_trip_loads) is still open -- it hasn't left -- and is that trip's: at a farm the
	truck is still at, a trip claims its plan or what it loaded there, whichever is
	more; at a farm it has left (a departed stop), exactly what it loaded there. A
	dispatched trip claims nothing open: its loads are in transit.

	unloaded: only the part still to be loaded (the plan less what is loaded, nothing at
	a departed farm) -- for counting against buckets still waiting on the shelf."""
	if not opl_names:
		return {}
	plans = {}
	for r in frappe.db.sql(
		"""
		SELECT o.parent AS trip, o.order_pick_list AS opl, IFNULL(o.farm, '') AS farm,
		       SUM(IFNULL(o.buckets, 0)) AS buckets
		FROM `tabBucket Request Trip Order` o
		JOIN `tabBucket Request Trip` t ON t.name = o.parent
		WHERE t.status IN %(active)s AND o.order_pick_list IN %(opls)s AND t.name != %(exclude)s
		GROUP BY o.parent, o.order_pick_list, IFNULL(o.farm, '')
		""",
		{"active": ACTIVE_TRIP_STATUSES, "opls": tuple(opl_names), "exclude": exclude_trip or ""},
		as_dict=True,
	):
		plans[(r.trip, r.opl, r.farm)] = int(r.buckets or 0)
	loads = _trip_loads(opl_names)
	names = {k[0] for k in plans} | set(loads)
	names.discard(exclude_trip or "")
	if not names:
		return {}
	trips = {
		t.name: t
		for t in frappe.get_all(
			"Bucket Request Trip",
			filters={"name": ["in", list(names)], "status": ["in", ACTIVE_TRIP_STATUSES]},
			fields=["name", "trip_date", "loaded_buckets", "departed_stops"],
		)
	}
	today = str(frappe.utils.today())
	out = {}
	for trip, opl, farm in set(plans) | {(t, *k) for t, by in loads.items() for k in by}:
		t = trips.get(trip)
		if not t:
			continue
		departed = _departed(t)
		if not (str(t.trip_date) >= today or int(t.loaded_buckets or 0) or departed or loads.get(trip)):
			continue
		planned = plans.get((trip, opl, farm), 0)
		loaded = loads.get(trip, {}).get((opl, farm), 0)
		left = any(_same_farm(f, farm) for f in departed)
		if unloaded:
			n = 0 if left else max(0, planned - loaded)
		else:
			n = loaded if left else max(planned, loaded)
		if n:
			out[(opl, farm)] = out.get((opl, farm), 0) + n
	return out


def _trip_loads(opl_names):
	"""trip -> {(opl, farm): buckets loaded for it that haven't left yet}: on its truck
	or recorded on it, or in a trolley at the farm it takes (trip_bucket_shares: one
	bucket, one trip -- a trolley's bucket fills the trip still at its farm with room,
	beyond every plan the last one). The farm is the trip order row's."""
	out = {}
	for trip, by in trip_bucket_shares(opl_names).items():
		for key, s in by.items():
			n = int(s.get("trolley") or 0) + int(s.get("loaded") or 0)
			if n:
				out.setdefault(trip, {})[key] = n
	return out


def _trip_used(name):
	"""Buckets of its truck trip `name` takes: per (order, farm) its plan or what is
	loaded for it there (_trip_loads), whichever is more -- a bucket loaded beyond the
	plan, or in a trolley for it, takes a place too -- and never less than what is
	recorded loaded on it."""
	doc = frappe.db.get_value("Bucket Request Trip", name, ["name", "loaded_buckets"], as_dict=True)
	if not doc:
		return 0
	plan = {}
	for o in frappe.get_all(
		"Bucket Request Trip Order",
		filters={"parent": name, "parenttype": "Bucket Request Trip"},
		fields=["order_pick_list", "farm", "buckets"],
	):
		key = (o.order_pick_list, o.farm or "")
		plan[key] = plan.get(key, 0) + int(o.buckets or 0)
	loads = _trip_loads(list({k[0] for k in plan if k[0]})).get(name, {}) if plan else {}
	extra = sum(max(0, n - plan.get(key, 0)) for key, n in loads.items())
	return max(_live_planned(name) + extra, int(doc.loaded_buckets or 0))


def _bucket_claims(opl_names, exclude_trip=None, planning=None):
	"""_trip_claims counted by physical bucket: a shared bucket already planned on a trip
	under another of its orders (_shared_covered) is claimed for its lead too, so it is
	never open for a second order. `planning`: {(opl, farm): buckets} of the rows being
	saved, which claim like a trip's."""
	claims = _trip_claims(opl_names, exclude_trip)
	for key, n in _shared_covered(opl_names, exclude_trip, planning).items():
		claims[key] = claims.get(key, 0) + n
	return claims


def _shared_covered(opl_names, exclude_trip=None, planning=None, claims_of=None):
	"""(opl, farm) -> open buckets these orders lead (shared_buckets) that a trip already
	carries under another of the bucket's orders. Planned before the lead moved, such a
	bucket sits in the other order's claim beyond that order's own open buckets: it is
	counted against the lead, never planned twice. `claims_of(opls)`: the other orders'
	claims (default _trip_claims, plus `planning`)."""
	if not opl_names:
		return {}
	leads = [e for e in _transfer_buckets(opl_names) if e["open"] and e["bucket"]]
	groups = shared_buckets({e["bucket"] for e in leads})
	if not groups:
		return {}
	owed = {}  # (other order, farm) -> [the lead's (opl, farm) per bucket]
	for e in leads:
		g = shared_group(groups, e["bucket"], e["farm"])
		if not g or g["lead"] != e["opl"]:
			continue
		for opl in g["opls"]:
			if opl != e["opl"]:
				owed.setdefault((opl, e["farm"]), []).append((e["opl"], e["farm"]))
	if not owed:
		return {}
	others = list({k[0] for k in owed})
	own = _open_counts(others)
	if claims_of:
		claims = claims_of(others)
	else:
		claims = _trip_claims(others, exclude_trip)
		for key, n in (planning or {}).items():
			claims[key] = claims.get(key, 0) + int(n or 0)
	out = {}
	for key, lead_keys in owed.items():
		extra = max(0, claims.get(key, 0) - own.get(key, 0))
		for lead in lead_keys[:extra]:
			out[lead] = out.get(lead, 0) + 1
	return out


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


def _user_name(user):
	if not user:
		return ""
	return frappe.db.get_value("User", user, "full_name") or user


def _trip_bucket_status(numbers):
	"""This trip's own buckets by stage, for the trip card (trip_numbers): awaiting on
	the farm's shelf, trolley, loaded on its truck, in transit, shelved at the
	packhouse -- never the truck's loads for its other trips."""
	n = numbers or _blank_numbers()
	return {
		"awaiting": n["awaiting"],
		"trolley": n["trolley"],
		"loaded": n["loaded"],
		"in_transit": n["transit"],
		"shelved": n["shelved"],
		"total": n["total"],
		"planned": n["planned"],
	}


def _trip_dict(doc, today, numbers=None):
	"""A trip for the pages and apps. `numbers`: its trip_numbers entry (read here when
	not given) -- the counts every surface shows."""
	if numbers is None:
		numbers = trip_numbers([doc.name]).get(doc.name)
	# Each order's delivery date, so the page shows a trip under the delivery date
	# its orders are for (one truck run can carry orders for different days).
	opls = list({o.order_pick_list for o in doc.orders if o.order_pick_list})
	due = (
		dict(
			frappe.db.sql(
				"""SELECT opl.name, so.delivery_date FROM `tabOrder Pick List` opl
				JOIN `tabSales Order` so ON so.name = opl.sales_order WHERE opl.name IN %(opls)s""",
				{"opls": tuple(opls)},
			)
		)
		if opls
		else {}
	)
	out = {
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
		# Request -> release: who asked for the truck, who confirmed it left, and when.
		"requested_by": _user_name(doc.get("requested_by")),
		"requested_at": str(doc.requested_at) if doc.get("requested_at") else None,
		"released_by": _user_name(doc.get("released_by")),
		"released_at": str(doc.released_at) if doc.get("released_at") else None,
		"rejected_by": _user_name(doc.get("rejected_by")),
		"rejected_at": str(doc.rejected_at) if doc.get("rejected_at") else None,
		"reject_reason": doc.get("reject_reason") or "",
		"collection_order": doc.collection_order,
		"farm": doc.farm,
		"total_buckets": doc.total_buckets,
		"total_stems": doc.total_stems,
		"capacity_buckets": _trip_capacity(doc),
		"loaded_buckets": int(doc.get("loaded_buckets") or 0),
		"unscheduled": int(doc.get("unscheduled") or 0),
		"stops": _trip_stops(doc) if doc.status in ACTIVE_TRIP_STATUSES + ("Dispatched",) else [],
		"departed_stops": [f for f in (doc.get("departed_stops") or "").split(",") if f],
		"heading_to": doc.get("heading_to") or "",
		"last_departed_at": str(doc.last_departed_at) if doc.get("last_departed_at") else None,
		"tracked_buckets": len(doc.get("trip_buckets") or []),
		"shelved_buckets": sum(1 for b in doc.get("trip_buckets") or [] if b.shelved),
		"off_truck_buckets": sum(1 for b in doc.get("trip_buckets") or [] if b.off_truck and not b.shelved),
		"bucket_status": _trip_bucket_status(numbers),
		# The trip's bucket numbers (trip_numbers): whole trip, every order on it.
		"numbers": numbers or {},
		"auto_planned": int(doc.get("auto_planned") or 0),
		"dispatched_at": str(doc.dispatched_at) if doc.dispatched_at else None,
		"received_at": str(doc.received_at) if doc.received_at else None,
		# When the trip was planned — the card's "time since scheduled" counts from here.
		"scheduled_at": str(doc.creation),
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
				"delivery_date": str(due[o.order_pick_list]) if due.get(o.order_pick_list) else None,
			}
			for o in doc.orders
		],
	}
	arrived = _trip_arrived_at(doc)
	if arrived:
		out["arrived_at"] = arrived
	return out


def _trip_arrived_at(doc):
	"""When the trip's truck reached the hub: its arrival stamp (_mark_arrived), else its
	first bucket shelved there. None when neither is known."""
	if doc.get("arrived_at"):
		return str(doc.arrived_at)
	times = [str(b.shelved_at) for b in doc.get("trip_buckets") or [] if b.shelved and b.get("shelved_at")]
	return min(times) if times else None


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


def _vehicle_on_road(vehicle, exclude_trip=None, include_arrived=False):
	"""The trip this truck is out on right now (Dispatched, not yet Received), else None.

	A truck on the road can't be routed, planned or dispatched again until it is back
	at the hub: its trip has arrived (first bucket shelved, or confirmed) or is received
	(every bucket it carried shelved). include_arrived: also a trip that has arrived but
	is still waiting on buckets to be shelved — the trip that carried the truck's load."""
	if not vehicle:
		return None
	filters = {"vehicle": vehicle, "status": "Dispatched"}
	if exclude_trip:
		filters["name"] = ["!=", exclude_trip]
	if not include_arrived:
		filters["arrived_at"] = ["is", "not set"]
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
		# Over once something it carried is shelved and nothing else is in transit. A trip
		# that carried nothing has nothing to shelve: End Trip on Bucket Logistics ends it.
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
	"""Tick off the trip's shelved buckets. The trip ends (Received) once every bucket it
	carried is shelved at the hub or has left the truck; until then it stays Dispatched
	(the truck is free again as soon as it has arrived — see _vehicle_on_road). Returns
	True when the trip is over."""
	rows = doc.get("trip_buckets") or []
	if not rows:
		return False
	states = {}
	# Packed or issued since counts as shelved: the bucket got here. Ready for packing
	# only counts once the row no longer waits on its transfer -- a remote row marked
	# ready while still at its farm must not end the trip early.
	for r in frappe.db.sql(
		"""SELECT parent, UPPER(bucket) AS bucket,
		       MAX(GREATEST(IFNULL(shelved, 0), IFNULL(issued, 0),
		           IF(IFNULL(custom_ready_for_packing, 0) = 1 AND IFNULL(awaiting_transfer, 0) = 0, 1, 0))) AS shelved,
		       MAX(GREATEST(IFNULL(in_transit, 0), IFNULL(loaded_in_trolley, 0))) AS in_transit
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
	# A bucket sitting on a hub shelf since the trip's day has arrived, even when its
	# pick row was never flagged (shelved under another order's row, or the shelving
	# scan's trip update failed). Bucket ids are reused, hence the date.
	hub = transfer_hub(required=False)
	on_hub_shelf = (
		set(
			frappe.db.sql_list(
				"""SELECT UPPER(bucket_id) FROM `tabShelf Item`
				WHERE farm = %(hub)s AND UPPER(bucket_id) IN %(buckets)s AND creation >= %(since)s""",
				{
					"hub": hub,
					"buckets": tuple({(r.bucket or "").upper() for r in rows}),
					"since": str(doc.trip_date),
				},
			)
		)
		if hub
		else set()
	)
	now = frappe.utils.now()
	changed = False
	ended_already = doc.status == "Received"
	for row in rows:
		# An ended trip's bucket that left its truck (back to its farm when a trip that
		# never left was closed, or carried onto the truck's next trip) is no longer this
		# trip's: shelving it later (it came on another truck) must not tick it off here
		# or put it back "on the truck".
		if ended_already and int(row.off_truck or 0):
			continue
		st = states.get((row.order_pick_list, (row.bucket or "").upper()))
		if not row.shelved and ((st and int(st.shelved)) or (row.bucket or "").upper() in on_hub_shelf):
			row.shelved = 1
			row.shelved_at = now
			changed = True
		# Still on the truck: in transit, or in a trolley on a truck not yet gone.
		off = 0 if row.shelved or (st and int(st.in_transit)) else 1
		if int(row.off_truck or 0) != off:
			row.off_truck = off
			changed = True
	ended = any(r.shelved for r in rows) and all(r.shelved or r.off_truck for r in rows)
	if ended and doc.status != "Received":
		doc.status = "Received"
		doc.received_at = now
		changed = True
	if changed:  # also called on every read (_vehicle_on_road): don't save — and version — for nothing
		doc.save(ignore_permissions=True)
	return ended


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
	"""End (Received) every dispatched trip whose requested buckets are all shelved and
	every bucket it carried is shelved -- trips the truck-flag path above cannot find
	(the bucket's transit_truck is the order's delivery-truck label)."""
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
		# Only a dispatched trip, once every bucket it carried is shelved.
		if doc.status != "Dispatched" or not _trip_has_loads(doc) or not _end_trip_if_shelved(doc):
			continue
		ended.append(name)
	return ended


def auto_receive_trucks_for_bucket(bucket_id):
	"""Called after a bucket is shelved at the sales farm. The dispatched trip of the
	truck that carried it ticks the bucket off, and ends (Received) once every bucket on
	it is shelved. Shelving never dispatches a trip, and never ends one any other way
	(End Trip on Bucket Logistics does). Returns the trips ended.
	A trip still at the farms that has the bucket loaded is settled first: the bucket
	reaching the hub on its truck says the truck left (_settle_held_trips)."""
	_settle_held_trips(bucket_id)
	received = _arrive_trips_carrying(bucket_id)
	trucks = frappe.db.sql_list(
		"""SELECT DISTINCT pli.transit_truck FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s AND """
		+ TRANSFER_TRUCK_OK,
		bucket_id,
	)
	for truck in trucks:
		# Only a trip that left: shelving never dispatches (or ends) one still loading.
		trip = _vehicle_on_road(truck, include_arrived=True)
		if not trip:
			continue
		doc = frappe.get_doc("Bucket Request Trip", trip)
		since = doc.dispatched_at or doc.trip_date
		if doc.get("trip_buckets"):
			# A bucket that took over another's truck flag on arrival (_take_over_truck)
			# was never recorded on the trip: add it as it is shelved, under the farm it
			# came from (FARM_EXPR reads a shelved row's hub warehouse).
			arrived = frappe.db.sql(
				"""SELECT pli.parent AS opl, pli.bucket AS bucket, COALESCE(NULLIF(pli.farm, ''), """
				+ _WH_FARM
				+ """) AS farm FROM `tabPick List Item` pli
				WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s
				  AND pli.transit_truck = %s AND pli.shelved = 1 AND pli.modified >= DATE(%s)""",
				(bucket_id, truck, since),
				as_dict=True,
			)
			# Only if it isn't recorded on another trip already (an ended one too: its
			# truck is on its next run now): one bucket, one trip.
			elsewhere = frappe.db.sql(
				"""SELECT 1 FROM `tabBucket Request Trip Bucket` b
				JOIN `tabBucket Request Trip` t ON t.name = b.parent
				WHERE b.parenttype = 'Bucket Request Trip' AND UPPER(b.bucket) = %(b)s
				  AND t.name != %(trip)s AND t.trip_date >= %(since)s LIMIT 1""",
				{
					"b": bucket_id.upper(),
					"trip": trip,
					"since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS),
				},
			)
			# A packhouse bucket never rode the truck, whatever its stale truck flag says.
			arrived = [r for r in arrived if r.farm and r.farm != transfer_hub(required=False)]
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


def record_planned_load_on_arrival(bucket_id, farm):
	"""A remote bucket is being shelved at the hub but its farm's "Load to truck" never
	reached the server: its rows are in a trolley (or still waiting) with no truck. It
	came on a trip that planned its order at its farm AND has left that farm (dispatched,
	or the farm is one of its departed stops), or one of those trips that has since
	ended (within the lookback): put it on that trip now -- the trip, the dashboard and
	the farm app then see it loaded and, as it is shelved, arrived. A trip still at the
	farm (Draft / Requested / Scheduled, the farm not left) is never credited: the
	bucket can't have come on it (it was at the hub already, or came some other way),
	and crediting it there made that trip look loaded -- and a later run that had
	"started" ended the truck's earlier run early. Without such a trip nothing is
	credited; the arrival is only logged. Returns the trips touched."""
	hub = transfer_hub(required=False)
	if not bucket_id or not hub or farm != hub:
		return []
	rows = frappe.db.sql(
		"""SELECT pli.name, pli.parent AS opl, """
		+ FARM_EXPR
		+ """ AS farm FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND UPPER(pli.bucket) = UPPER(%(b)s)
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0 AND IFNULL(pli.not_found, 0) = 0
		  AND (IFNULL(pli.in_transit, 0) = 0 OR IFNULL(pli.transit_truck, '') IN ('', '0'))""",
		{"b": bucket_id},
		as_dict=True,
	)
	rows = [r for r in rows if r.farm and r.farm != hub]
	if not rows:
		return []
	since = frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)
	# Already recorded on a trip (one bucket, one trip): that trip handles its arrival --
	# an ended (Received) one too, within the lookback: crediting it again counted it
	# twice there, or put it on a second trip. Only rows still on their truck (or
	# shelved off it) for these orders: a bucket id is reused, and one taken back to its
	# farm off a trip that never left came some other way.
	if _bucket_trip_rows({bucket_id.upper()}) or frappe.db.sql(
		"""SELECT 1 FROM `tabBucket Request Trip Bucket` b
		JOIN `tabBucket Request Trip` t ON t.name = b.parent
		WHERE b.parenttype = 'Bucket Request Trip' AND UPPER(b.bucket) = %(b)s
		  AND b.order_pick_list IN %(opls)s AND t.trip_date >= %(since)s
		  AND (IFNULL(b.off_truck, 0) = 0 OR IFNULL(b.shelved, 0) = 1) LIMIT 1""",
		{"b": bucket_id.upper(), "opls": tuple({r.opl for r in rows}), "since": since},
	):
		return []
	by_opl = {}
	for r in rows:
		by_opl.setdefault((r.opl, r.farm), []).append(r)
	groups = shared_buckets({bucket_id})
	touched, missed = [], []
	for (opl, bfarm), opl_rows in by_opl.items():
		lead = shared_lead(groups, bucket_id, bfarm)
		if lead and lead != opl and (lead, bfarm) in by_opl:
			continue  # rides with its lead order's rows: credited once, for the lead
		trip = frappe.db.sql(
			"""SELECT t.name, t.vehicle, t.status FROM `tabBucket Request Trip` t
			JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
			WHERE o.order_pick_list = %(opl)s AND o.farm = %(farm)s AND t.trip_date >= %(since)s
			  AND (t.status IN ('Dispatched', 'Received')
			       OR (t.status IN %(active)s AND FIND_IN_SET(%(farm)s, IFNULL(t.departed_stops, ''))))
			ORDER BY CASE t.status WHEN 'Dispatched' THEN 0 WHEN 'Received' THEN 2 ELSE 1 END,
			         t.trip_date DESC, COALESCE(t.dispatched_at, t.last_departed_at, t.creation) DESC""",
			{"opl": opl, "farm": bfarm, "since": since, "active": ACTIVE_TRIP_STATUSES},
			as_dict=True,
		)
		# A trip still on its route (a later stop not left yet) didn't bring it: the bucket
		# can't be at the hub before its truck is.
		trip = [
			t
			for t in trip
			if t.status not in ACTIVE_TRIP_STATUSES
			or _all_stops_left(frappe.get_doc("Bucket Request Trip", t.name))
		][:1]
		if not trip:
			missed.append((opl, bfarm))
			continue
		touched.append(_credit_arrival(trip[0].name, bucket_id, opl, bfarm, opl_rows, groups))
	for opl, bfarm in missed:
		log_transfer_event(
			bucket_id,
			"Arrived without a trip",
			opl=opl,
			farm=farm,
			details="No trip that left {0} planned it; shelved without crediting a trip".format(bfarm),
		)
	return sorted(set(touched))


def _credit_arrival(trip, bucket_id, opl, farm, rows, groups):
	"""Record a bucket shelved at the hub on the trip that left its farm with its order
	planned (record_planned_load_on_arrival): its pick rows go in transit on that truck
	and the trip counts it loaded -- with no load time, as the farm never scanned it."""
	doc = frappe.get_doc("Bucket Request Trip", trip)
	g = shared_group(groups, bucket_id, farm)
	opls = {opl} | set((g or {}).get("opls") or [])
	frappe.db.sql(
		"""UPDATE `tabPick List Item` SET loaded_in_trolley = 1, in_transit = 1, transit_truck = %(v)s,
		  modified = %(now)s
		WHERE parenttype = 'Order Pick List' AND parent IN %(opls)s AND UPPER(bucket) = %(b)s
		  AND IFNULL(shelved, 0) = 0 AND IFNULL(issued, 0) = 0 AND IFNULL(not_found, 0) = 0""",
		{"v": doc.vehicle, "opls": tuple(opls), "b": bucket_id.upper(), "now": frappe.utils.now()},
	)
	cap = int(doc.capacity_buckets or 0)
	entry = {
		"opl": opl,
		"farm": farm,
		"bucket": bucket_id,
		"rows": [
			{
				"opl": opl,
				"bucket": bucket_id,
				"farm": farm,
				"stems": r.get("stems") or 0,
				"variety": r.get("variety"),
			}
			for r in frappe.db.sql(
				"""SELECT stock_qty AS stems, item_code AS variety FROM `tabPick List Item`
				WHERE name IN %(n)s""",
				{"n": tuple(r.name for r in rows)},
				as_dict=True,
			)
		]
		or [{"opl": opl, "bucket": bucket_id, "farm": farm, "stems": 0, "variety": None}],
	}
	# Unstamped: when it went on the truck isn't known (a stamp of now would also read
	# as a load on the truck's current run).
	_add_trip_buckets(doc, [entry["rows"][0]], stamp=False)
	_record_loads(doc, doc.vehicle, [entry])
	if doc.status == "Received" and cap and int(doc.capacity_buckets or 0) != cap:
		doc.db_set("capacity_buckets", cap, update_modified=False)  # an ended trip keeps its own
	log_transfer_event(
		bucket_id,
		"Load recorded on arrival",
		opl=opl,
		farm=farm,
		trip=doc.name,
		vehicle=doc.vehicle,
		details="Truck load was never recorded at the farm; credited to {0} ({1})".format(
			doc.name, doc.status
		),
	)
	return doc.name


def _arrive_trips_carrying(bucket_id):
	"""A bucket recorded on a trip was just shelved at the packhouse: the bucket is
	ticked off, and a dispatched trip ends once every bucket it carried is shelved. A
	trip that hasn't been dispatched is left as it is — shelving a bucket is no proof
	its truck left (the bucket may have been at the hub already). Returns the trips
	ended now."""
	ended = []
	for name in frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Bucket` b ON b.parent = t.name AND b.parenttype = 'Bucket Request Trip'
		WHERE UPPER(b.bucket) = UPPER(%(bucket)s) AND IFNULL(b.shelved, 0) = 0
		  AND IFNULL(b.off_truck, 0) = 0 AND t.trip_date >= %(since)s
		  AND t.status IN ('Dispatched', 'Received')""",
		{"bucket": bucket_id, "since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)},
	):
		doc = frappe.get_doc("Bucket Request Trip", name)
		if doc.status == "Received":
			_end_trip_if_shelved(doc)  # tick it off; the trip already ended
			continue
		if _end_trip_if_shelved(doc):
			ended.append(name)
	return ended


def _all_stops_left(doc):
	"""Every stop of the trip is in its departed stops."""
	stops = _trip_stops(doc)
	return bool(stops) and all(f in _departed(doc) for f in stops)


def _carried_rows(doc, rows):
	"""Of these trip bucket rows, the ones carried onto this trip from the truck's earlier
	trip (the former carry-over of unshelved buckets): that trip's row of the same bucket and order, taken off its
	truck, with the same load time. Such a bucket rode the EARLIER run to the hub."""
	if not rows:
		return set()
	carried = set()
	for r in frappe.db.sql(
		"""SELECT b.order_pick_list, UPPER(b.bucket), b.loaded_at FROM `tabBucket Request Trip Bucket` b
		JOIN `tabBucket Request Trip` t ON t.name = b.parent
		WHERE b.parenttype = 'Bucket Request Trip' AND b.parent != %(trip)s AND t.vehicle = %(v)s
		  AND IFNULL(b.off_truck, 0) = 1 AND UPPER(b.bucket) IN %(b)s AND b.order_pick_list IN %(opls)s""",
		{
			"trip": doc.name,
			"v": doc.vehicle or "",
			"b": tuple({r.bucket.upper() for r in rows}),
			"opls": tuple({r.order_pick_list for r in rows}),
		},
	):
		carried.add((r[0], r[1], str(r[2] or "")))
	return {r.name for r in rows if (r.order_pick_list, r.bucket.upper(), str(r.loaded_at or "")) in carried}


def _held_loads_at_hub(doc):
	"""The buckets loaded on this trip at its farms (still on its truck) that have
	reached the hub since: shelved, packed or issued there. A bucket the farm loaded on
	a trip went onto its truck there (one bucket, one trip -- loaded onto another truck
	it comes off this trip), so one of them at the hub means the truck left the farms
	and got there. Not evidence: a load recorded only on arrival (no load time) nor one
	carried over from the truck's earlier run -- those reached the hub on another run."""
	rows = [r for r in doc.get("trip_buckets") or [] if r.bucket and not int(r.off_truck or 0)]
	if doc.status != "Dispatched":
		rows = [r for r in rows if r.loaded_at]
		carried = _carried_rows(doc, rows)
		rows = [r for r in rows if r.name not in carried]
	if not rows:
		return []
	reached = {
		(r[0], r[1])
		for r in frappe.db.sql(
			"""SELECT DISTINCT parent, UPPER(bucket) FROM `tabPick List Item`
			WHERE parenttype = 'Order Pick List' AND parent IN %(opls)s AND UPPER(bucket) IN %(b)s
			  AND (IFNULL(shelved, 0) = 1 OR IFNULL(issued, 0) = 1
			       OR (IFNULL(custom_ready_for_packing, 0) = 1 AND IFNULL(awaiting_transfer, 0) = 0))""",
			{
				"opls": tuple({r.order_pick_list for r in rows}),
				"b": tuple({r.bucket.upper() for r in rows}),
			},
		)
	}
	return [r for r in rows if r.shelved or (r.order_pick_list, r.bucket.upper()) in reached]


def _settle_held_trip(doc, how, estimate=False):
	"""A trip still at the farms (Draft / Requested / Scheduled -- held at its last stop,
	or never dispatched by the farm) whose loaded buckets reached the hub: its truck has
	left every stop and arrived. Dispatch it and stamp it arrived, then tick off what is
	shelved (it ends once every bucket it carried is), as for a dispatched trip -- so the
	hub's shelving, auto-arrive / auto-receive, End Trip and the truck's next run all
	carry on instead of the trip staying "loading" for good (and its loads going back to
	the farm at midnight). estimate: the trip is settled late (scheduler sweep), so it
	left after its last load (or its last stop), not now. Returns True when settled."""
	if doc.status not in ACTIVE_TRIP_STATUSES or not doc.vehicle:
		return False
	reached = _held_loads_at_hub(doc)
	if not reached:
		return False
	now = frappe.utils.now()
	at = now
	if estimate:
		times = [
			str(r.loaded_at)
			for r in doc.get("trip_buckets") or []
			if r.loaded_at and not int(r.off_truck or 0)
		]
		times += [str(doc.last_departed_at)] if doc.get("last_departed_at") else []
		at = max(times) if times else now
	hub = transfer_hub(required=False) or ""
	was = doc.status
	departed = _departed(doc)
	departed += [f for f in _trip_stops(doc) if f not in departed]
	doc.departed_stops = ",".join(departed)
	doc.last_departed_at = doc.get("last_departed_at") or at
	doc.status = "Dispatched"
	doc.dispatched_at = at
	doc.heading_to = hub
	doc.auto_planned = 0  # its truck has gone: no longer the planner's to re-plan
	# Measured before the loads go in transit; the truck is at the hub already, so a
	# short load is only noted, never held.
	short = below_minimum(doc)
	_put_loads_in_transit(doc)
	doc.add_comment(
		"Info",
		"Dispatched on arrival at {0}: {1} — the trip was still {2}".format(hub or "the packhouse", how, was),
	)
	if short:
		doc.add_comment(
			"Info",
			"Arrived at {0} with {1} of {2} (min {3}) — sent without pressing Send".format(
				hub or "the packhouse", short["loaded"], short["capacity"], short["needed"]
			),
		)
	doc.save(ignore_permissions=True)
	_mark_arrived(doc, hub or "the packhouse", how)
	doc.reload()
	if _end_trip_if_shelved(doc):
		carry_over_to_next_run(doc.name)
	return True


def _settle_held_trips(bucket_id):
	"""The trips still at the farms that this bucket, just shelved at the hub, is loaded
	on: settled (_settle_held_trip). Returns their names."""
	bucket_id = (bucket_id or "").strip()
	if not bucket_id:
		return []
	settled = []
	for name in frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Bucket` b ON b.parent = t.name AND b.parenttype = 'Bucket Request Trip'
		WHERE UPPER(b.bucket) = UPPER(%(bucket)s) AND IFNULL(b.off_truck, 0) = 0
		  AND t.status IN %(active)s AND t.trip_date >= %(since)s""",
		{
			"bucket": bucket_id,
			"active": ACTIVE_TRIP_STATUSES,
			"since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS),
		},
	):
		doc = frappe.get_doc("Bucket Request Trip", name)
		if _settle_held_trip(doc, "bucket {0} shelved there".format(bucket_id)):
			settled.append(name)
	return settled


def _busy_trip_is_back(busy, doc):
	"""`doc` (every stop left, waiting) runs before `busy` or has no run, so the newer-run
	rule (_end_trip_for_newer_run) never ends `busy` for it. Its truck loaded `doc` after
	`busy` left, so the truck came back: `busy` is stamped arrived. Returns True then."""
	if doc.get("run") and busy.get("run") and int(doc.run) > int(busy.run):
		return False
	if busy.get("trip_date") and doc.get("trip_date") and str(doc.trip_date) > str(busy.trip_date):
		return False  # a later day: the newer-run rule covers it
	since = str(busy.dispatched_at or "")
	if not since or not any(
		str(r.loaded_at) >= since for r in doc.get("trip_buckets") or [] if r.loaded_at and not r.off_truck
	):
		return False
	_mark_arrived(
		busy,
		transfer_hub(required=False) or "the packhouse",
		"{0} loaded {1} after it left".format(busy.vehicle, doc.name),
	)
	return True


def _dispatch_waiting_trips():
	"""Trips that left every stop while their truck was still out on another trip
	(_dispatch_last_stop) are dispatched once that truck is free. Returns the trips."""
	out = []
	for name in frappe.get_all(
		"Bucket Request Trip",
		filters={
			"status": ["in", ACTIVE_TRIP_STATUSES],
			"trip_date": [">=", frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)],
			"departed_stops": ["is", "set"],
		},
		order_by="trip_date asc, run asc, creation asc",
		pluck="name",
	):
		doc = frappe.get_doc("Bucket Request Trip", name)
		stops = _trip_stops(doc)
		if not stops or not _trip_has_loads(doc) or any(f not in _departed(doc) for f in stops):
			continue
		busy = _vehicle_on_road(doc.vehicle, exclude_trip=name)
		if busy and not _busy_trip_is_back(frappe.get_doc("Bucket Request Trip", busy), doc):
			continue  # still out: said so once, when its last stop was left
		if _dispatch_last_stop(doc, "every stop left, {0} is back".format(doc.vehicle)):
			doc.save(ignore_permissions=True)
			out.append(name)
	return out


def _newer_run_started(doc):
	"""The truck's trip on a later run (that day, or a later day) that has started since
	this one left -- released, dispatched or buckets loaded on it -- else None. A truck
	is on one run at a time: once it is out on the next, this one is over."""
	if not doc.vehicle:
		return None
	since = doc.dispatched_at or doc.trip_date
	# A loaded bucket's time: loaded_at, else the row's last save (a child row's
	# creation is its trip's, which can be older than this run).
	rows = frappe.db.sql(
		"""SELECT t.name FROM `tabBucket Request Trip` t
		WHERE t.vehicle = %(v)s AND t.name != %(n)s
		  AND (t.trip_date > %(d)s OR (t.trip_date = %(d)s AND IFNULL(t.run, 0) > %(run)s))
		  AND (
		    (t.status IN ('Dispatched', 'Received') AND t.dispatched_at >= %(since)s)
		    OR (t.status = 'Scheduled' AND t.released_at >= %(since)s)
		    OR (t.status IN %(active)s AND EXISTS (
		        SELECT 1 FROM `tabBucket Request Trip Bucket` b
		        WHERE b.parent = t.name AND b.parenttype = 'Bucket Request Trip'
		          AND COALESCE(b.loaded_at, b.modified) >= %(since)s))
		  )
		ORDER BY t.trip_date, t.run LIMIT 1""",
		{
			"v": doc.vehicle,
			"n": doc.name,
			"d": doc.trip_date,
			"run": int(doc.run or 0),
			"since": since,
			"active": ACTIVE_TRIP_STATUSES,
		},
	)
	return rows[0][0] if rows else None


def _carry_over_unshelved():
	"""Production Settings > Carry Unshelved Buckets to the Truck's Next Trip (upande_quality's
	doctype -- off where it isn't installed)."""
	if not frappe.db.exists("DocType", "Production Settings"):
		return False
	return bool(
		frappe.utils.cint(frappe.db.get_single_value("Production Settings", "carry_over_unshelved_buckets"))
	)


def _carry_unshelved(doc, later, left):
	"""Move the buckets an ended trip carried but never shelved onto the truck's later
	trip `later`: they come off the old trip (off_truck) and ride on the new one."""
	new = frappe.get_doc("Bucket Request Trip", later)
	have = {((b.bucket or "").upper(), b.order_pick_list) for b in new.get("trip_buckets") or []}
	idx = len(new.get("trip_buckets") or [])
	for b in left:
		b.db_set("off_truck", 1, update_modified=False)
		if ((b.bucket or "").upper(), b.order_pick_list) in have:
			continue
		idx += 1
		# Child rows written directly: saving the whole trip collides with a loading scan
		# saving it at the same moment.
		frappe.get_doc(
			{
				"doctype": "Bucket Request Trip Bucket",
				"parent": later,
				"parenttype": "Bucket Request Trip",
				"parentfield": "trip_buckets",
				"idx": idx,
				"bucket": b.bucket,
				"order_pick_list": b.order_pick_list,
				"farm": b.farm,
				"loaded_at": b.loaded_at,
				"loaded_by": b.loaded_by,
			}
		).db_insert()
		log_transfer_event(
			b.bucket,
			"Carried to next trip",
			opl=b.order_pick_list,
			farm=b.farm,
			trip=later,
			vehicle=doc.vehicle,
			details="Not shelved off {0}; carried on {1}".format(doc.name, later),
		)
	if frappe.db.has_column("Bucket Request Trip", "loaded_buckets"):
		# One per bucket id: a bucket filling two orders has a row per order.
		loaded = len(
			{
				(b or "").upper()
				for b in frappe.get_all(
					"Bucket Request Trip Bucket",
					filters={
						"parent": later,
						"parenttype": "Bucket Request Trip",
						"loaded_at": ["is", "set"],
					},
					pluck="bucket",
				)
			}
		)
		frappe.db.set_value("Bucket Request Trip", later, "loaded_buckets", loaded, update_modified=False)
	doc.add_comment(
		"Info",
		"Ended: {0} left on a later run ({1}) — {2} unshelved bucket(s) carried onto it: {3}".format(
			doc.vehicle, later, len(left), ", ".join(b.bucket for b in left)
		),
	)


def _end_trip_for_newer_run(doc):
	"""End (Received) a dispatched trip whose truck has started a later run, once nothing
	it carried is still on the truck: every one of its buckets is shelved or has left the
	truck. A later run alone never ends it -- the farm loads the truck's next run while
	the truck is still unloading this one at the hub; its stragglers wait for their
	shelving scan, End Trip, or the midnight close. With Production Settings > Carry
	Unshelved Buckets to the Truck's Next Trip on, the stragglers ride the later trip
	instead and this one ends. Returns True when it ended the trip."""
	if doc.status != "Dispatched":
		return False
	left = [b for b in doc.get("trip_buckets") or [] if not b.shelved and not b.off_truck]
	carry = bool(left) and _carry_over_unshelved()
	if left and not carry:
		return False
	later = _newer_run_started(doc)
	if not later:
		return False
	_receive_trip(doc.name)
	if carry:
		_carry_unshelved(doc, later, left)
		return True
	doc.add_comment("Info", "Ended: {0} left on a later run ({1})".format(doc.vehicle, later))
	return True


def end_shelved_trips():
	"""Scheduler (every 5 minutes): end every dispatched trip whose run is over, so the
	truck isn't kept busy until someone presses End Trip. A run is over when

	  1. every bucket it carried is shelved at the hub, issued, or left the truck --
	     shelving a bucket already ends its trip, but only that one scan's trips, once:
	     a missed or failed scan would leave the trip open; or
	  2. the same truck has started a later run (released, dispatched, or loading on a
	     later run or day) and nothing it carried is still on the truck. A bucket still
	     in transit keeps it open, whatever its orders' other buckets did.

	First, a trip still at the farms whose loaded buckets reached the hub (a shelving
	scan's settlement missed or failed) is dispatched and arrived (_settle_held_trip),
	and one that left every stop while its truck was out on another trip is dispatched
	once the truck is free (_dispatch_waiting_trips)."""
	since = frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)
	for name in frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Bucket` b ON b.parent = t.name AND b.parenttype = 'Bucket Request Trip'
		WHERE t.status IN %(active)s AND t.trip_date >= %(since)s AND IFNULL(b.off_truck, 0) = 0
		ORDER BY t.trip_date, t.run, t.creation""",
		{"active": ACTIVE_TRIP_STATUSES, "since": since},
	):
		try:
			_settle_held_trip(
				frappe.get_doc("Bucket Request Trip", name), "its buckets shelved there", estimate=True
			)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one trip at a time
		except Exception:
			frappe.db.rollback()
			frappe.log_error("Settle held trip {0} failed".format(name), frappe.get_traceback())
	rows = frappe.db.sql(
		"""SELECT t.name FROM `tabBucket Request Trip` t
		WHERE t.status = 'Dispatched' AND t.trip_date >= %(since)s""",
		{"since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)},
		as_dict=True,
	)
	ended = []
	for r in rows:
		try:
			doc = frappe.get_doc("Bucket Request Trip", r.name)
			# A bucket of it shelved at the hub (by any path): the truck arrived.
			if ensure_arrived(doc):
				doc.reload()
			if _end_trip_if_shelved(doc) or _end_trip_for_newer_run(doc):
				ended.append(r.name)
				carry_over_to_next_run(r.name)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one trip at a time
		except Exception:
			frappe.db.rollback()
			frappe.log_error("End finished trip {0} failed".format(r.name), frappe.get_traceback())
	try:
		if _dispatch_waiting_trips():
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
	except Exception:
		frappe.db.rollback()
		frappe.log_error("Dispatch waiting trips failed", frappe.get_traceback())
	return ended


def close_previous_day_trips(before=None):
	"""End every trip planned for a day before `before` (default today) that is still
	open, so no trip runs into the next day (scheduler: every midnight; also the
	Production Settings button).

	  * Dispatched: ended (Received) like End Trip; buckets it carried that were never
	    shelved stay recorded on it as not shelved.
	  * Draft / Requested / Scheduled (the truck never left): closed. Buckets loaded on
	    it come off that truck and its trolley and are kept as anomalies
	    (keep_as_anomaly): not put back on a shelf, not waiting for a new trip.

	Then every bucket still in a trolley or on a truck from before `before` is kept as
	an anomaly the same way (clear_left_trolleys): each day starts on a clean slate."""
	before = before or frappe.utils.today()
	names = frappe.get_all(
		"Bucket Request Trip",
		filters={"trip_date": ["<", before], "status": ["!=", "Received"]},
		order_by="trip_date asc, run asc",
		pluck="name",
	)
	closed = []
	for name in names:
		try:
			doc = frappe.get_doc("Bucket Request Trip", name)
			# Its loads reached the hub: the truck went -- settled, not sent back to the farm.
			if _settle_held_trip(doc, "its buckets shelved there", estimate=True):
				doc.reload()
				if doc.status == "Received":
					frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one trip at a time
					closed.append(name)
					continue
			was = doc.status
			left = [b for b in doc.get("trip_buckets") or [] if not b.shelved and not b.off_truck]
			back = _return_loads_to_farm(doc, left, before) if was != "Dispatched" else {}
			_receive_trip(name)
			doc.add_comment("Info", _close_comment(doc, was, left, back))
			for b in left:
				r = back.get(b.bucket) or {}
				if was == "Dispatched":
					stage = "Not shelved when trip ended"
					details = "Trip {0} closed at the end of {1}".format(name, doc.trip_date)
				else:
					stage = LEFT_OVERNIGHT
					details = _anomaly_details(
						TRUCK_OVERNIGHT, doc.trip_date, r, trip=name, truck=doc.vehicle
					)
				log_transfer_event(
					b.bucket,
					stage,
					opl=b.order_pick_list,
					farm=b.farm,
					trip=name,
					vehicle=doc.vehicle,
					details=details,
				)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one trip at a time
			closed.append(name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error("Close previous day trip {0} failed".format(name), frappe.get_traceback())
	clear_left_trolleys(before)
	return closed


def clear_left_trolleys(before=None):
	"""The rest of the midnight clean slate (close_previous_day_trips): every bucket still
	in a trolley or flagged on a truck from before `before` (default today) -- scanned
	into a trolley but never loaded, or loaded on a truck that never left -- is kept as
	an anomaly (keep_as_anomaly): its trolley and truck flags are cleared, so the farm
	app's saved trolleys are gone and nothing of an earlier day counts on the new day's
	trips, but it is not put back on a shelf nor set waiting for a truck. Each is logged
	as LEFT_OVERNIGHT (Bucket Transfer Event) and listed on its order and on the trip
	that planned it there. Left alone: buckets on the road (in transit), shelved, packed
	or issued; buckets recorded on a trip not yet ended (a later day's); rows changed on
	`before` or later (today's scans). Only rows last changed in the TRIP_LOOKBACK_DAYS
	before `before`. One bucket at a time, committed; a failure is logged and the rest
	go on. Returns the buckets kept."""
	before = str(before or frappe.utils.today())
	day = str(frappe.utils.add_days(before, -1))
	has_lot = frappe.db.has_column("Pick List Item", "loaded_on_truck")
	lot = " OR IFNULL(pli.loaded_on_truck, 0) = 1" if has_lot else ""
	flagged = "(pli.loaded_in_trolley = 1 OR IFNULL(pli.trolley_id, '') != ''" + lot + ")"
	rows = frappe.db.sql(
		"""SELECT pli.name, pli.parent AS opl, pli.bucket, IFNULL(pli.trolley_id, '') AS trolley,
		       """
		+ ("IFNULL(pli.loaded_on_truck, 0)" if has_lot else "0")
		+ """ AS lot,
		       IFNULL("""
		+ FARM_EXPR
		+ """, '') AS farm,
		       IF("""
		+ TRANSFER_TRUCK_OK
		+ """, pli.transit_truck, '') AS truck
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` o ON o.name = pli.parent AND o.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND IFNULL(pli.bucket, '') != '' AND """
		+ flagged
		+ """
		  AND IFNULL(pli.in_transit, 0) = 0 AND IFNULL(pli.shelved, 0) = 0
		  AND IFNULL(pli.not_found, 0) = 0 AND NOT """
		+ PACKED_SQL
		+ """
		  AND pli.modified < %(before)s AND pli.modified >= %(since)s
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabBucket Request Trip Bucket` tb
		      JOIN `tabBucket Request Trip` t ON t.name = tb.parent
		      WHERE tb.parenttype = 'Bucket Request Trip' AND tb.order_pick_list = pli.parent
		        AND tb.bucket = pli.bucket AND t.status != 'Received' AND IFNULL(tb.off_truck, 0) = 0)
		ORDER BY pli.parent, pli.idx""",
		{"before": before, "since": str(frappe.utils.add_days(before, -TRIP_LOOKBACK_DAYS))},
		as_dict=True,
	)
	groups = {}  # opl -> {UPPER(bucket): [rows]}
	for r in rows:
		groups.setdefault(r.opl, {}).setdefault(r.bucket.upper(), []).append(r)
	kept = []
	on_trips = {}  # trip -> ["bucket (reason, order)"]
	for opl, by_bucket in groups.items():
		left, back = [], {}
		for bkt_rows in by_bucket.values():
			r = bkt_rows[0]
			try:
				# Kept already with another order's rows (a shared bucket on one truck).
				if not frappe.db.sql(
					"SELECT 1 FROM `tabPick List Item` pli WHERE pli.name IN %(n)s AND " + flagged,
					{"n": tuple(x.name for x in bkt_rows)},
				):
					continue
				truck = next((x.truck for x in bkt_rows if x.truck), "")
				on_truck = bool(truck or any(cint(x.lot) for x in bkt_rows))
				reason = TRUCK_OVERNIGHT if on_truck else TROLLEY_OVERNIGHT
				trip = _planning_trip(opl, r.farm, before)
				res = keep_as_anomaly(r.bucket, opl, r.farm, truck or None)
				res["trolley"] = res.get("trolley") or next((x.trolley for x in bkt_rows if x.trolley), "")
				res["reason"] = reason
				log_transfer_event(
					r.bucket,
					LEFT_OVERNIGHT,
					opl=opl,
					farm=r.farm,
					trip=trip,
					vehicle=truck or None,
					details=_anomaly_details(reason, day, res, trip=trip, truck=truck),
				)
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one bucket at a time
				left.append(frappe._dict(bucket=r.bucket))
				back[r.bucket] = res
				kept.append(r.bucket)
				if trip:
					on_trips.setdefault(trip, []).append(
						"{0} ({1}, {2}, order due {3})".format(r.bucket, reason, opl, res.get("due") or "?")
					)
			except Exception:
				frappe.db.rollback()
				frappe.log_error(
					"Clear left trolley bucket {0} ({1}) failed".format(r.bucket, opl), frappe.get_traceback()
				)
		if left:
			_comment(
				"Order Pick List",
				opl,
				"Left in a trolley or on a truck at the end of {0} — {1}".format(
					day, _anomaly_part(left, back, "bucket(s)")
				),
			)
	for trip, listed in on_trips.items():
		_comment(
			"Bucket Request Trip",
			trip,
			"Kept as anomaly at the end of {0} — {1} bucket(s) left in a trolley or on a truck: {2}".format(
				day, len(listed), "; ".join(listed)
			),
		)
	return kept


#: The transfer event of a bucket a new day found still in a trolley, or loaded on a
#: truck that never left: kept off any shelf and any trip, for someone to look into.
LEFT_OVERNIGHT = "Anomaly: left overnight"
TROLLEY_OVERNIGHT = "left in trolley overnight"
TRUCK_OVERNIGHT = "loaded on a truck that never left"


def keep_as_anomaly(bucket, opl, farm, vehicle=None):
	"""A new day's clean slate for a bucket left in a trolley or loaded on a truck that
	never left: its pick rows (_load_rows: this order's, plus the other orders' rows of
	the same bucket loaded on this truck at that farm) come off the trolley and truck --
	no trolley id, the truck's name cleared -- so it counts on no trip and in no trolley.
	It is NOT put back on a shelf and NOT set waiting for a truck: an anomaly, logged by
	the caller. Returns {"due": `opl`'s delivery date, "so": its sales order, "trolley":
	the trolley id it was in}."""
	lot = frappe.db.has_column("Pick List Item", "loaded_on_truck")
	trolley = frappe.db.has_column("Pick List Item", "trolley_id")
	rows = _load_rows(bucket, opl, farm, vehicle)
	out = {
		"due": str(_opl_delivery_dates({opl}).get(opl) or ""),
		"so": frappe.db.get_value("Order Pick List", opl, "sales_order") or "",
		"trolley": next((r.trolley_id for r in rows if r.get("trolley_id")), ""),
	}
	for r in rows:
		values = {"loaded_in_trolley": 0}
		if lot:
			values["loaded_on_truck"] = 0
		if trolley:
			values["trolley_id"] = None
		# Allocation pre-fills transit_truck with the order's delivery label: only this
		# truck's name is cleared.
		if vehicle and (r.transit_truck or "").strip().upper() == vehicle.strip().upper():
			values["transit_truck"] = None
		frappe.db.set_value("Pick List Item", r.name, values)
	return out


def _anomaly_details(reason, day, res, trip=None, truck=None):
	"""The LEFT_OVERNIGHT event's details: why, when, and where it was."""
	parts = [reason, "left at the end of {0}".format(day)]
	if res.get("trolley"):
		parts.append("trolley {0}".format(res["trolley"]))
	if trip:
		parts.append("trip {0}".format(trip))
	if truck:
		parts.append("truck {0}".format(truck))
	if res.get("so"):
		parts.append("sales order {0}".format(res["so"]))
	parts.append("delivery {0}".format(res.get("due") or "unknown"))
	return "; ".join(parts)


def _planning_trip(opl, farm, before):
	"""The latest trip before `before` that planned `opl` at `farm`, or None."""
	for t in frappe.db.sql(
		"""SELECT t.name, IFNULL(o.farm, '') AS farm
		FROM `tabBucket Request Trip Order` o
		JOIN `tabBucket Request Trip` t ON t.name = o.parent
		WHERE o.order_pick_list = %(opl)s AND t.trip_date < %(before)s
		ORDER BY t.trip_date DESC, t.run DESC, t.creation DESC LIMIT 10""",
		{"opl": opl, "before": before},
		as_dict=True,
	):
		if not farm or not t.farm or _same_farm(t.farm, farm):
			return t.name
	return None


def _comment(doctype, name, content):
	"""An Info comment, committed on its own; a failure is logged, never raised."""
	try:
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": doctype,
				"reference_name": name,
				"content": content,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one comment at a time
	except Exception:
		frappe.db.rollback()
		frappe.log_error("Comment on {0} {1} failed".format(doctype, name), frappe.get_traceback())


def _close_comment(doc, was, left, back):
	"""The trip comment of close_previous_day_trips: what happened to the buckets left on it."""
	head = "Closed at the end of {0} ({1})".format(doc.trip_date, was)
	if not left:
		return head
	if was == "Dispatched":
		return head + " — {0} bucket(s) never shelved: {1}".format(
			len(left), ", ".join(b.bucket for b in left)
		)
	return head + " — " + _anomaly_part(left, back)


def _anomaly_part(left, back, what="loaded bucket(s)"):
	"""Buckets kept as anomalies (keep_as_anomaly results `back`), with their orders' due dates."""
	return "{0} {1} kept as anomaly (left overnight, not carried over): {2}".format(
		len(left),
		what,
		", ".join(
			"{0} ({1}order due {2})".format(
				b.bucket,
				"{0}, ".format(back[b.bucket]["reason"]) if (back.get(b.bucket) or {}).get("reason") else "",
				(back.get(b.bucket) or {}).get("due") or "?",
			)
			for b in left
		),
	)


def _return_loads_to_farm(doc, left, before=None):
	"""A trip that never left at the end of its day: its loaded buckets come off this
	truck and are kept as anomalies (keep_as_anomaly) -- not put back on a shelf, not
	waiting for a new day's trip. Returns {bucket: keep_as_anomaly's result}."""
	out = {}
	for b in left:
		b.db_set("off_truck", 1, update_modified=False)
		out[b.bucket] = keep_as_anomaly(b.bucket, b.order_pick_list, b.farm, doc.vehicle)
	return out


def back_to_farm_shelf(bucket, opl, farm, vehicle=None, before=None):
	"""Undo a bucket's trolley scan and truck load at its farm (saveTrolleyData /
	setOfflineTrolleyFlags, then loadTrolleyInTruck): its pick rows come off the trolley
	and truck (no trolley id, the truck's name cleared) and the shelf rows the scan
	removed come back (_restore_farm_shelf). A row goes back to waiting for transfer
	(awaiting_transfer) only when its order delivers after `before` (default today): an
	order due by then is not carried over to a new day's trip. The rows are this
	order's, plus the other orders' rows of the same bucket loaded on this truck at that
	farm (a shared bucket is loaded once for all its orders). Rows already on the road,
	shelved or issued are left alone. No stock moves: neither the scan nor the load
	posted any. Returns {"shelf": the shelf or None, "carried": `opl`'s rows waiting
	again, "due": `opl`'s delivery date}."""
	before = str(before or frappe.utils.today())
	lot = frappe.db.has_column("Pick List Item", "loaded_on_truck")
	trolley = frappe.db.has_column("Pick List Item", "trolley_id")
	rows = _load_rows(bucket, opl, farm, vehicle)
	dds = _opl_delivery_dates({r.parent for r in rows} | {opl})
	out = {"shelf": None, "carried": False, "due": dds.get(opl) or ""}
	if not rows:
		return out
	out["shelf"] = shelf = _restore_farm_shelf(
		bucket, farm, next((r.shelf for r in rows if r.shelf), None), rows
	)
	for r in rows:
		values = {"loaded_in_trolley": 0}
		# Due on or before the new day: back on the shelf, never waiting for its trucks.
		if not dds.get(r.parent) or dds[r.parent] > before:
			values["awaiting_transfer"] = 1
			out["carried"] = out["carried"] or r.parent == opl
		if lot:
			values["loaded_on_truck"] = 0
		if trolley:
			values["trolley_id"] = None
		# Allocation pre-fills transit_truck with the order's delivery label: only this
		# truck's name is cleared.
		if vehicle and (r.transit_truck or "").strip().upper() == vehicle.strip().upper():
			values["transit_truck"] = None
		if shelf:
			values["shelf"] = shelf
		frappe.db.set_value("Pick List Item", r.name, values)
	return out


def _load_rows(bucket, opl, farm, vehicle=None):
	"""A bucket's pick rows at its farm that a trolley scan / truck load flagged: this
	order's, plus the other orders' rows of the same bucket loaded on `vehicle` at that
	farm. Never rows on the road, shelved, issued or not found."""
	return frappe.db.sql(
		"""SELECT pli.name, pli.parent, pli.shelf, pli.transit_truck, pli.trolley_id
		FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %(b)s
		  AND IFNULL(pli.in_transit, 0) = 0 AND IFNULL(pli.shelved, 0) = 0
		  AND IFNULL(pli.issued, 0) = 0 AND IFNULL(pli.not_found, 0) = 0
		  AND (pli.parent = %(opl)s OR (pli.loaded_in_trolley = 1 AND %(truck)s != ''
		       AND UPPER(TRIM(IFNULL(pli.transit_truck, ''))) = UPPER(TRIM(%(truck)s))
		       AND """
		+ FARM_EXPR
		+ """ = %(farm)s))""",
		{"b": bucket, "opl": opl or "", "truck": vehicle or "", "farm": farm or ""},
		as_dict=True,
	)


#: The Shelving Log reason the trolley scan writes on the shelf rows it removes.
TROLLEY_REMOVED = "Transferred (Trolley/Truck)"


def _restore_farm_shelf(bucket, farm, fallback_shelf=None, rows=None):
	"""Put a bucket taken off its farm shelf by its trolley scan back there. The scan
	deleted its Shelf Item rows and marked their Shelving Log rows "Transferred
	(Trolley/Truck)" (kept, with the shelf, variety, stems and dates): each such row of
	the bucket's last removal comes back as it was, on the same shelf, and its log row
	is "Shelved" again. Nothing when the bucket is on a shelf at the farm already, or
	shelved since. With no log row: onto `fallback_shelf` (the pick row's shelf) from
	the pick rows `rows`, with a new "Shelved" log row. Returns the shelf, or None when
	none is known (it then waits off any shelf)."""
	farm = farm or ""
	on_shelf = frappe.db.sql(
		"""SELECT si.parent FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		WHERE si.bucket_id = %(b)s AND (%(farm)s = '' OR s.farm = %(farm)s) LIMIT 1""",
		{"b": bucket, "farm": farm},
	)
	if on_shelf:
		return on_shelf[0][0]
	logs = frappe.db.sql(
		"""SELECT sl.* FROM `tabShelving Log` sl
		WHERE sl.bucket_id = %(b)s AND (%(farm)s = '' OR IFNULL(sl.farm, '') IN ('', %(farm)s))
		ORDER BY sl.shelved_on DESC, sl.creation DESC""",
		{"b": bucket, "farm": farm},
		as_dict=True,
	)
	if logs and logs[0].reason == TROLLEY_REMOVED and logs[0].shelf:
		last = [
			sl
			for sl in logs
			if sl.reason == TROLLEY_REMOVED and sl.removed_on == logs[0].removed_on and sl.shelf
		]
		shelf_name = frappe.db.get_value("Shelf", {"name": last[0].shelf}) or frappe.db.get_value(
			"Shelf", {"shelf_id": last[0].shelf}
		)
		if shelf_name:
			shelf = frappe.get_doc("Shelf", shelf_name)
			added = []
			for sl in last:
				row = shelf.append(
					"items",
					{
						"bucket_id": sl.bucket_id,
						"variety": sl.variety,
						"stem_qty": sl.stem_qty,
						"stem_length": sl.stem_length,
						"greenhouse": sl.greenhouse,
						"warehouse": sl.warehouse,
						"cut_stage": sl.cut_stage,
						"harvest_date": sl.harvest_date,
						"receiving_date": sl.receiving_date,
						"farm": sl.farm or shelf.farm,
						"harvester": sl.harvester,
						"graded_by": sl.graded_by,
						"grading_date": sl.grading_date,
						"date_added": sl.shelved_on,
					},
				)
				added.append((sl, row))
			shelf.save(ignore_permissions=True)
			for sl, row in added:
				frappe.db.set_value(
					"Shelving Log",
					sl.name,
					{"reason": "Shelved", "removed_on": None, "shelf_item": row.name},
					update_modified=False,
				)
			return shelf.name
	if not fallback_shelf or not rows or not frappe.db.exists("Shelf", fallback_shelf):
		return None
	shelf = frappe.get_doc("Shelf", fallback_shelf)
	if farm and shelf.farm and shelf.farm != farm:
		return None
	picks = frappe.get_all(
		"Pick List Item",
		filters={"name": ["in", [r.name for r in rows]]},
		fields=["item_code", "stock_qty", "stem_length", "source_warehouse", "warehouse"],
	)
	added = [
		shelf.append(
			"items",
			{
				"bucket_id": bucket,
				"variety": p.item_code,
				"stem_qty": cint(p.stock_qty),
				"stem_length": p.stem_length,
				"warehouse": p.source_warehouse or p.warehouse,
				"farm": farm or shelf.farm,
				"date_added": frappe.utils.now_datetime(),
			},
		)
		for p in picks
	]
	shelf.save(ignore_permissions=True)
	from upande_quality.mobile.api import _write_shelved_log

	for row in added:
		_write_shelved_log(row, shelf.name, farm or shelf.farm)
	return shelf.name


@frappe.whitelist(methods=["POST"])
def closePreviousDayTrips():
	"""Production Settings button: close every trip of an earlier day now."""
	if not frappe.has_permission("Production Settings", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	closed = close_previous_day_trips()
	frappe.response["message"] = {"status": "success", "closed": closed, "count": len(closed)}


def carry_over_to_next_run(name):
	"""Planned buckets a finished run didn't load move to the truck's next open run
	that visits their farm (as far as its room allows) — the plan keeps going on the
	same route instead of the buckets silently dropping back to "open"."""
	doc = frappe.get_doc("Bucket Request Trip", name)
	if doc.status != "Received":
		return []
	# The trip has ended either way: a failed carry-over must not roll that back (the
	# scheduler would retry, fail and leave the truck "on the road" for good).
	frappe.db.savepoint("carry_over")
	try:
		return _carry_over(doc)
	except Exception:
		frappe.db.rollback(save_point="carry_over")
		frappe.log_error("Carry over from trip {0} failed".format(name), frappe.get_traceback())
		return []


def _plan_lock():
	"""Serialise trip planning and loading: the free-bucket check and the write it
	guards must not interleave with another planner, the scheduler or a truck load.
	A named MariaDB lock: it lasts until this request's (or job's) DB connection
	closes, which Frappe does at the end of each; taking it again in the same session
	is a no-op."""
	got = frappe.db.sql("SELECT GET_LOCK('upande_bucket_trip_plan', 30)")[0][0]
	if not got:
		frappe.throw(_("Trip planning is busy — try again in a moment."))


def _carry_over(doc, farm=None, date=None, report=None):
	"""Move the trip's planned-but-not-loaded buckets (one farm's, or every farm's) to
	the truck's next open run. Idempotent: what another trip already claims is skipped
	-- and a trip already trimmed when it left the farm (trim_left_farms) has nothing
	left to move."""
	if not (doc.get("route") and doc.get("run")):
		return []
	rows = []
	for o in doc.orders:
		if farm and (o.farm or "") != farm:
			continue
		left = int(o.buckets or 0) - int(o.loaded_buckets or 0)
		if left > 0 and not int(o.unscheduled or 0):
			per = (int(o.stems or 0) / int(o.buckets)) if int(o.buckets or 0) else 0
			rows.append(_carry_row(o, left, round(per * left)))
	placed = _carry_rows(doc, rows, date, report)
	saved = list(dict.fromkeys(t for t, _r in placed))
	if saved:
		doc.add_comment("Info", "Not loaded on this trip, moved to {0}".format(", ".join(saved)))
	return saved


def _carry_row(o, buckets, stems):
	"""A plan row for `buckets` of trip order row `o`, to go on another run."""
	return {
		"order_pick_list": o.order_pick_list,
		"order_name": o.order_name,
		"customer": o.customer,
		"farm": o.farm,
		"varieties": o.varieties,
		"buckets": buckets,
		"stems": stems,
		"full_farm_buckets": buckets,
		"is_partial": 0,
	}


def _carry_rows(doc, rows, date=None, report=None):
	"""Put plan rows a trip left behind on the truck's next open run to their farm: its
	trip (made on the route's run if it has none yet), a new run when every run there
	is full, else another truck already collecting there. Only orders delivering the
	day after `date` or the day after that (drop_due_orders), and only what is still
	open and unclaimed: a next trip that already has them is never topped up twice.
	Returns [(trip, row placed)]."""
	if not rows:
		return []
	_plan_lock()
	# Always onto today's (or later) runs: a trip dated earlier would not count as a
	# claim and the same buckets would be planned again.
	date = max(str(date or frappe.utils.today()), str(frappe.utils.today()))
	last = str(frappe.utils.add_days(date, 2))
	dds = _opl_delivery_dates({r["order_pick_list"] for r in rows})
	rows = [r for r in rows if not dds.get(r["order_pick_list"]) or date < dds[r["order_pick_list"]] <= last]
	# Only what is still open (not shelved, not on another trip).
	opls = list({r["order_pick_list"] for r in rows})
	open_counts, claims = (_open_counts(opls), _bucket_claims(opls)) if opls else ({}, {})
	carried = []
	for r in rows:
		key = (r["order_pick_list"], r["farm"] or "")
		n = min(r["buckets"], max(0, open_counts.get(key, 0) - claims.get(key, 0)))
		claims[key] = claims.get(key, 0) + n
		if n > 0:
			per = (r["stems"] / r["buckets"]) if r["buckets"] else 0
			carried.append({**r, "buckets": n, "stems": round(per * n), "full_farm_buckets": n})
	if report is not None:
		report["open"] = sum(r["buckets"] for r in carried)
	if not carried:
		return []
	placed = []

	def write(plan):
		for slot in plan:
			name = _write_run_trip(slot, doc.vehicle, date, "Draft", "", 0)
			placed.extend((name, r) for r in slot["rows"])

	plan, unplaced = _split_into_runs(doc.vehicle, date, carried)
	write(plan)
	# Every run of the truck to that farm is full (or none is left today): it gets
	# another run, as automatic scheduling adds one.
	rest = []
	for row, n, _why in unplaced:
		per = (row["stems"] / row["buckets"]) if row["buckets"] else 0
		part = {**row, "buckets": n, "stems": round(per * n)}
		res = truck_routes.add_farm_run(doc.vehicle, row["farm"], date, enforce_room=True, while_on_road=True)
		if res.get("status") != "success":
			rest.append((part, n))
			continue
		more, left = _split_into_runs(doc.vehicle, date, [part], only=(res["route"], res["run"]))
		write(more)
		rest.extend((r, k) for r, k, _w in left)
	# No run of this truck takes them: another truck already going to that farm today
	# does -- left-behind buckets never just drop back to "open".
	holders = farm_holders(date, exclude_vehicle=doc.vehicle, with_room=True) if rest else {}
	for row, n in rest:
		for _v, trip, _w in holders.get(row["farm"], []):
			per = (row["stems"] / row["buckets"]) if row["buckets"] else 0
			added, left = _add_rows_to_trip(trip, [{**row, "buckets": n, "stems": round(per * n)}])
			placed.extend((trip, a) for a in added)
			n = sum(k for _r, k in left)
			if not n:
				break
	return placed


def _left_farms(doc):
	"""The farms the trip's truck has left, with nothing more to collect there: every
	farm once it is dispatched, else its departed stops."""
	if doc.status == "Dispatched":
		hub = transfer_hub(required=False)
		return {o.farm for o in doc.orders if o.farm and o.farm != hub}
	if doc.status in ACTIVE_TRIP_STATUSES:
		return set(_departed(doc))
	return set()


def trim_left_farms(doc):
	"""Before a trip is saved: at each farm its truck has left (_left_farms) its order
	rows are cut to what was loaded there -- the rest is no longer this trip's to
	collect, and claimed here it stayed on this trip while the next run planned the same
	buckets. A row left with nothing is dropped (sync_riding_orders adds a rider back).
	Returns {farm: {planned, loaded, rows}}: the planned and loaded counts there, and the
	plan rows cut off, for carry_left_behind once the trip is saved."""
	farms = _left_farms(doc)
	if not farms:
		return {}
	totals = {}
	for o in doc.orders:
		if (o.farm or "") in farms:
			t = totals.setdefault(o.farm, [0, 0])
			t[0] += int(o.buckets or 0)
			t[1] += int(o.loaded_buckets or 0)
	out, keep = {}, []
	for o in doc.orders:
		planned, loaded = int(o.buckets or 0), int(o.loaded_buckets or 0)
		if (o.farm or "") not in farms or loaded >= planned:
			keep.append(o)
			continue
		e = out.setdefault(o.farm, {"planned": totals[o.farm][0], "loaded": totals[o.farm][1], "rows": []})
		left = planned - loaded
		left_stems = round(int(o.stems or 0) * left / planned)
		if not int(o.unscheduled or 0):
			e["rows"].append(_carry_row(o, left, left_stems))
		o.full_farm_buckets = max(int(o.full_farm_buckets or 0), planned)
		o.buckets = loaded
		o.stems = int(o.stems or 0) - left_stems
		o.is_partial = 1 if loaded < o.full_farm_buckets else 0
		if loaded:
			keep.append(o)
	if not out:
		return {}
	if len(keep) != len(doc.orders):
		doc.set("orders", keep)
		for i, r in enumerate(doc.orders, 1):
			r.idx = i
	doc.total_buckets = sum(int(o.buckets or 0) for o in doc.orders)
	doc.total_stems = sum(int(o.stems or 0) for o in doc.orders)
	return out


def carry_left_behind(doc, left):
	"""After a trip that left farms is saved trimmed (trim_left_farms): what it left
	behind goes on the truck's next run there now (_carry_rows), not once the trip is
	received. A failed carry-over never fails the save: the buckets are open again and
	automatic scheduling plans them."""
	rows = [r for e in left.values() for r in e["rows"]]
	placed = []
	if rows:
		frappe.db.savepoint("carry_left_behind")
		try:
			placed = _carry_rows(doc, rows)
		except Exception:
			frappe.db.rollback(save_point="carry_left_behind")
			frappe.log_error("Carry over from trip {0} failed".format(doc.name), frappe.get_traceback())
			placed = []
	for farm, e in left.items():
		moved = [(t, r) for t, r in placed if (r["farm"] or "") == farm]
		msg = "Left {0} with {1} of {2} planned".format(farm, e["loaded"], e["planned"])
		if moved:
			msg += "; {0} moved to {1}".format(
				sum(r["buckets"] for _t, r in moved), ", ".join(dict.fromkeys(t for t, _r in moved))
			)
		doc.add_comment("Info", msg)
	return placed


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


def _hold_window():
	"""(today, window): the planner's delivery window (auto_transfer._plan): tomorrow."""
	today = frappe.utils.today()
	return str(today), str(frappe.utils.add_days(today, 1))


def _current_step(window):
	"""The sequence step the planner works on now (auto_transfer._distribute_steps): the
	lowest schedule # among the window's scheduled orders still at a remote farm or on
	the road. None when none is."""
	hub = transfer_hub(required=False) or ""
	opls = frappe.db.sql_list(
		"""SELECT DISTINCT opl.name FROM `tabOrder Pick List` opl
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
		WHERE opl.docstatus < 2 AND so.delivery_date = %(w)s
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.not_found, 0) = 0 AND NOT """
		+ PACKED_SQL
		+ " AND IFNULL("
		+ FARM_EXPR
		+ ", '') NOT IN ('', %(hub)s)",
		{"w": window, "hub": hub},
	)
	steps = [int(s.get("schedule") or 0) for s in _schedule_map(opls=opls).values()]
	steps = [s for s in steps if s > 0]
	return min(steps) if steps else None


#: The order _hold_for_waiting takes waiting buckets in -- the planner's (_distribute).
_HOLD_RANK = {"asap": 0, "step": 1}


def _waiting_at_farm(doc, farm):
	"""Buckets waiting on `farm`'s shelf (not on a trolley, truck or any trip) that no
	other open trip is planned to collect and that the planner would put on a trip there
	now (auto_transfer._plan): of orders the farm app shows (a draft order, or a
	replacement a submitted one asked for ASAP) and on a Packhouse Schedule, delivering
	tomorrow or later -- ASAP replacements, then the window's (tomorrow's) orders at the
	sequence step being planned (_current_step). Never an order delivering today or
	earlier (an ASAP replacement for one included), the day after's orders or later
	steps: those wait for the next trip. [(order row, buckets, whole)] in the planner's order; whole: all of it or
	none (the planner never splits a step's portion)."""
	_today, window = _hold_window()
	meta = {
		r.opl: r
		for r in frappe.db.sql(
			"""SELECT DISTINCT pli.parent AS opl, opl.docstatus, opl.order_name, so.customer,
			       so.delivery_date
			FROM `tabOrder Pick List` opl
			JOIN `tabSales Order` so ON so.name = opl.sales_order
			JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
			WHERE so.delivery_date >= %(w)s
			  AND (opl.docstatus = 0 OR (opl.docstatus = 1 AND IFNULL(pli.transfer_priority, '') = 'ASAP'))
			  AND (so.delivery_date <= %(w)s OR IFNULL(pli.transfer_priority, '') = 'ASAP')
			  AND pli.awaiting_transfer = 1 AND IFNULL(pli.loaded_in_trolley, 0) = 0
			  AND IFNULL(pli.in_transit, 0) = 0 AND IFNULL(pli.shelved, 0) = 0
			  AND IFNULL(pli.not_found, 0) = 0 AND NOT """
			+ PACKED_SQL
			+ " AND "
			+ FARM_EXPR
			+ " = %(farm)s",
			{"w": window, "farm": farm},
			as_dict=True,
		)
	}
	sched = _schedule_map(opls=list(meta))
	opls = [o for o in meta if o in sched]
	if not opls:
		return []
	step = _current_step(window)

	def kind(b):
		m = meta[b["opl"]]
		if str(m.delivery_date or "") < window:
			return None  # delivering today or earlier: never held for, ASAP or not
		if b["asap"]:
			return "asap"
		if int(m.docstatus or 0) != 0:
			return None  # a submitted order's buckets: only its ASAP replacements
		due = str(m.delivery_date or "")
		if due == window and step and int(sched[b["opl"]].get("schedule") or 0) == step:
			return "step"
		return None

	buckets = [
		b
		for b in _transfer_buckets(opls)
		if b["open"] and not b["loaded"] and b["farm"] == farm and b["bucket"] and kind(b)
	]
	# One physical bucket, one trip: never one already recorded on a trip (this one's
	# fresh loads are only in memory yet).
	on_trips = set(_bucket_trip_rows({(b["bucket"] or "").upper() for b in buckets}))
	on_trips |= {(r.bucket or "").upper() for r in doc.get("trip_buckets") or [] if not r.off_truck}
	counts, stems, varieties = {}, {}, {}
	for b in buckets:
		if (b["bucket"] or "").upper() in on_trips:
			continue
		key = (b["opl"], kind(b))
		counts[key] = counts.get(key, 0) + 1
		stems[key] = stems.get(key, 0.0) + b["stems"]
		if b["variety"]:
			varieties.setdefault(key, set()).add(b["variety"])
	if not counts:
		return []
	# Planned to be collected by another open trip, or still by this one (in memory:
	# its loads since the last save aren't in the database): taken off an order's
	# buckets that go last first, so what the planner puts first stays free.
	claims = _trip_claims(list({o for o, _k in counts}), exclude_trip=doc.name, unloaded=True)
	for o in doc.orders:
		if (o.farm or "") == farm:
			key = (o.order_pick_list, farm)
			claims[key] = claims.get(key, 0) + max(0, int(o.buckets or 0) - int(o.loaded_buckets or 0))
	free = {}
	for key in sorted(counts, key=lambda k: -_HOLD_RANK[k[1]]):
		taken = min(counts[key], claims.get((key[0], farm), 0))
		claims[(key[0], farm)] = claims.get((key[0], farm), 0) - taken
		free[key] = counts[key] - taken
	out = []
	for (opl, k), n in free.items():
		if n <= 0:
			continue
		m = meta[opl]
		out.append(
			(
				{
					"order_pick_list": opl,
					"order_name": m.order_name or opl,
					"customer": m.customer or "",
					"farm": farm,
					"varieties": ", ".join(sorted(varieties.get((opl, k)) or [])),
					"per_bucket": stems[(opl, k)] / counts[(opl, k)],
					"full_farm_buckets": n,
					"delivery_date": str(m.delivery_date or ""),
					"schedule": int(sched[opl].get("schedule") or 0),
					"team": sched[opl].get("team") or "",
					"kind": k,
				},
				n,
				k == "step",
			)
		)
	# ASAP first (earliest delivery, then schedule), then the step's
	# portions smallest first -- as _distribute takes them.
	out.sort(
		key=lambda x: (
			_HOLD_RANK[x[0]["kind"]],
			x[1] if x[0]["kind"] == "step" else 0,
			x[0]["delivery_date"],
			x[0]["schedule"],
			x[0]["order_pick_list"],
		)
	)
	return out


def _trip_room(doc):
	"""Buckets this trip can still take: capacity less what is planned (live orders
	only) or loaded on it, whichever is more. None when the truck's capacity is unknown."""
	cap = _trip_capacity(doc)
	if not cap:
		return None
	live = set(
		frappe.get_all(
			"Order Pick List",
			filters={"name": ["in", list({o.order_pick_list for o in doc.orders if o.order_pick_list})]},
			pluck="name",
		)
	)
	planned = sum(int(o.buckets or 0) for o in doc.orders if o.order_pick_list in live)
	return cap - max(planned, _trip_loaded_count(doc), int(doc.loaded_buckets or 0))


def _hold_for_waiting(doc, farm):
	"""The trip's last stop is loaded, but the truck isn't full and buckets the planner
	would put on it now are waiting at that farm for no other trip (_waiting_at_farm):
	plan them onto this trip (in memory -- the caller saves it), as far as the truck has
	room, and keep the truck loading there instead of sending it off part-full. Returns
	the buckets added (0 = nothing held, dispatch)."""
	room = _trip_room(doc)
	if not room or room <= 0:
		return 0
	# The free-bucket check and the plan rows it adds must not interleave with a planner
	# saving trips (auto_transfer._save_plan) or another load.
	_plan_lock()
	added, blocked = 0, set()
	for row, n, whole in _waiting_at_farm(doc, farm):
		left = room - added
		if left <= 0:
			break
		if whole:
			# A step's portion goes whole or waits; a team that can't fit one keeps its
			# queue -- its later portions wait too (as in _distribute_steps).
			if row["team"] in blocked:
				continue
			if n > left:
				blocked.add(row["team"])
				continue
			take = n
		else:
			take = min(n, left)
		stems = round(row["per_bucket"] * take)
		same = next(
			(o for o in doc.orders if o.order_pick_list == row["order_pick_list"] and (o.farm or "") == farm),
			None,
		)
		if same:
			same.buckets = int(same.buckets or 0) + take
			same.stems = int(same.stems or 0) + stems
		else:
			doc.append(
				"orders",
				{
					"order_pick_list": row["order_pick_list"],
					"order_name": row["order_name"],
					"customer": row["customer"],
					"farm": farm,
					"varieties": row["varieties"],
					"buckets": take,
					"stems": stems,
					"full_farm_buckets": row["full_farm_buckets"],
					"is_partial": 1 if take < row["full_farm_buckets"] else 0,
				},
			)
		added += take
	if not added:
		return 0
	doc.total_buckets = sum(int(o.buckets or 0) for o in doc.orders)
	doc.total_stems = sum(int(o.stems or 0) for o in doc.orders)
	doc.heading_to = farm
	doc.add_comment("Info", "Holding at {0}: {1} more scheduled buckets to load".format(farm, added))
	return added


def _advance_stops(doc, hold=True):
	"""After a load: a stop whose planned buckets are all on the truck is left behind —
	the truck heads to the next stop, and once the last stop is loaded the trip is
	dispatched to the packhouse. Stops are left in route order only (a later farm
	loading early doesn't skip an earlier one). Returns the farms left by this call.

	hold (a load): the truck moves on only once it is full; until then the farm sends
	it ("Truck leaving" / "Send to packhouse": closeTripStop, hold=False) or Dispatch
	does."""
	if doc.status not in ACTIVE_TRIP_STATUSES:
		return []
	stops = _trip_stops(doc)
	if not stops:
		return []
	departed = [f for f in (doc.get("departed_stops") or "").split(",") if f]
	# A load never sends the truck on by itself unless the truck is full: the farm
	# sends it ("Truck leaving" / "Send to packhouse"), or Dispatch does.
	if hold and not _truck_full(doc):
		return []
	newly = []
	for f in stops:
		if f in departed:
			continue
		if not _stop_complete(doc, f):
			break
		if hold and f == stops[-1] and _hold_for_waiting(doc, f):
			break  # the last stop stays open: its new buckets are still to load
		if f == stops[-1]:
			# Leaving the last stop sends the truck to the hub: never by itself under
			# its minimum load -- it waits there for Send with a reason, or more loads.
			short = below_minimum(doc)
			if short:
				doc.add_comment(
					"Info",
					"Held at {0}: {1} of {2} buckets on {3} (min {4}) — send it with a reason or load more".format(
						f, short["loaded"], short["capacity"], doc.vehicle, short["needed"]
					),
				)
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
		_dispatch_last_stop(doc, "every stop loaded")
	return newly


def _truck_out_on(vehicle, exclude_trip):
	"""The truck's other Dispatched trip not yet arrived, else None -- read only: unlike
	_vehicle_on_road it never settles (and commits) that trip, so it is safe inside a
	load, a stop close or a not-found scan. A finished trip it misses is settled by the
	sweep, which then dispatches the waiting one (_dispatch_waiting_trips)."""
	if not vehicle:
		return None
	out = frappe.get_all(
		"Bucket Request Trip",
		filters={
			"vehicle": vehicle,
			"status": "Dispatched",
			"arrived_at": ["is", "not set"],
			"name": ["!=", exclude_trip or ""],
		},
		pluck="name",
		order_by="dispatched_at desc",
		limit=1,
	)
	return out[0] if out else None


def _dispatch_last_stop(doc, why, at=None):
	"""The truck left the trip's last stop for the hub: Dispatched, its loads in transit
	(as on a Dispatch press -- left out, they stayed "loaded on truck" all the way to
	the hub). Not while the truck is still out on another trip: one truck is on one run
	at a time, so this one stays as it is (every stop left, heading to the hub) and is
	dispatched once that trip has arrived (end_shelved_trips) or as soon as one of its
	buckets is shelved at the hub (_settle_held_trips). Returns True when dispatched."""
	hub = transfer_hub(required=False) or ""
	doc.heading_to = hub
	busy = _truck_out_on(doc.vehicle, doc.name)
	if busy:
		doc.add_comment(
			"Info",
			"Every stop left ({0}), but {1} is still out on {2}: dispatched once that trip has arrived".format(
				why, doc.vehicle, busy
			),
		)
		return False
	doc.status = "Dispatched"
	doc.dispatched_at = at or frappe.utils.now()
	_put_loads_in_transit(doc)
	doc.add_comment("Info", "Dispatched to {0}: {1}".format(hub or "the packhouse", why))
	return True


def farm_departed(truck, farm):
	"""True once the truck carrying this farm's buckets has left the farm: its trip is
	dispatched, or the farm is one of the trip's departed stops."""
	if _vehicle_on_road(truck, include_arrived=True):
		return True
	return any(farm in _departed(d) for d in _truck_open_trips(truck) if _trip_has_loads(d))


def record_truck_load(pli_names):
	"""Put buckets the mobile app just loaded onto a truck on that truck's trip.

	Loading (loadTrolleyInTruck / setOfflineTrolleyFlags) only flags the Pick List
	Item rows; this puts each bucket on the trip (run) that planned its order at its
	farm — the earliest run with room — else the run that visits the farm, else a new
	unscheduled trip on the next free run. Every run's loads stay on that run's one
	trip (loads used to pile onto the truck's first trip while its later planned trips
	stayed empty). The trip is dispatched once its last stop is loaded and the truck is
	full or nothing more scheduled waits there for it (_advance_stops), from the
	dashboard, or when its first bucket is shelved at the packhouse
	(auto_receive_trucks_for_bucket), which also ends it.
	Rows are on the truck once loaded (loaded_in_trolley with the truck), not yet in
	transit: Dispatch does that. A bucket already on a trip is not added again.
	Returns the trips touched."""
	if not pli_names:
		return []
	rows = frappe.db.sql(
		"""
		SELECT pli.name AS name, pli.parent AS opl, pli.bucket AS bucket, pli.item_code AS variety,
		       pli.stock_qty AS stems, pli.transit_truck AS truck, pli.modified AS modified,
		       pli.loaded_in_trolley_at AS scanned_at, """
		+ FARM_EXPR
		+ """ AS farm
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE pli.name IN %(names)s AND pli.parenttype = 'Order Pick List'
		  AND (pli.in_transit = 1 OR pli.loaded_in_trolley = 1) AND IFNULL(pli.shelved, 0) = 0
		  -- An older order's row of a reused bucket id (delivery passed) is no load: it
		  -- was flagged with the bucket, and on a trip it counted the bucket twice.
		  AND (so.delivery_date IS NULL OR so.delivery_date >= %(today)s)
		  AND pli.transit_truck IS NOT NULL AND pli.transit_truck != '' AND """
		+ TRANSFER_TRUCK_OK,
		{"names": tuple(pli_names), "today": frappe.utils.today()},
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
		# A bucket loaded for an order its truck's trips plan rides for that order: its
		# rows of other orders nothing plans here (flagged with the bucket) are no load.
		planned = {(o.order_pick_list, o.farm or "") for d in trips for o in d.orders}
		groups0 = shared_buckets({b["bucket"] for b in buckets.values()})
		by_id = {}
		for key, b in buckets.items():
			by_id.setdefault(key[1], []).append(key)
		for bid, keys in by_id.items():
			mine = [k for k in keys if (k[0], buckets[k]["farm"]) in planned]
			if not mine or len(keys) < 2:
				continue
			for k in keys:
				g = shared_group(groups0, bid, buckets[k]["farm"])
				if k not in mine and not (g and k[0] in g["opls"]):
					buckets.pop(k)
		by_trip, docs = {}, {d.name: d for d in trips}
		chosen = {}  # bucket -> trip doc: a bucket split over two orders rides one trip
		# Earliest scanned first, as trip_bucket_shares counts them: an order split over
		# two runs fills the earlier run with the buckets the farm scanned for it first.
		for b in buckets.values():
			b["scanned_at"] = min((str(r.get("scanned_at") or "9999") for r in b["rows"]), default="9999")
		# A bucket several orders share goes on the trip once, for its lead order; the
		# others' rows of it ride along uncounted (they were flagged with it).
		groups = groups0
		for b in sorted(buckets.values(), key=lambda b: (b["scanned_at"], b["bucket"])):
			lead = shared_lead(groups, b["bucket"], b["farm"])
			if lead and lead != b["opl"] and (lead, b["bucket"]) in buckets:
				continue
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
						# Never onto the trip the truck is out on (in transit): a load with no
						# open trip starts the truck's next run.
						doc = _new_load_trip(truck, today, b["farm"])
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
		WHERE tb.parenttype = 'Bucket Request Trip' AND tb.bucket IN %(b)s
		  AND t.status != 'Received' AND IFNULL(tb.off_truck, 0) = 0""",
		{"b": tuple(buckets)},
		as_dict=True,
	):
		out.setdefault(r.bucket, []).append(r)
	return out


#: How far along a bucket is, furthest first: the order an order's buckets fill its trips in.
_SHARE_RANK = {"transit": 0, "loaded": 0, "trolley": 1, "awaiting": 2}


def _same_farm(a, b):
	a, b = (a or "").strip(), (b or "").strip()
	return bool(a and b and (a == b or a in b or b in a))


def trip_bucket_shares(opls):
	"""Which trip each bucket of these orders counts for: one bucket, one trip. The farm
	app, the Bucket Logistics dashboard and truck loads all count by this.

	A trip plans an order at a farm as a COUNT of buckets (Bucket Request Trip Order),
	not as named buckets, and one order's farm portion can be split over several runs.
	So, per (order, farm):
	- a bucket recorded on a trip (loaded there, still on the truck or shelved) is that
	  trip's;
	- the rest fill the trips planning that portion in the order the trucks drive them,
	  earliest first, each up to its planned count less what is recorded on it -- buckets
	  further along first (on a truck, then on a trolley, earliest scanned first), then
	  waiting ones. Only a trip still at the farm (not dispatched, stop not departed)
	  takes more; a bucket flagged on a truck only fills that truck's trips. A bucket on a
	  trolley or truck beyond every plan goes on the last trip still at the farm;
	- a bucket already shelved / packed and recorded on no trip reached the hub some
	  other way (an earlier run, a copied site): it is on none of these trips -- unless
	  a trip planning that portion left (dispatched) with none of its buckets recorded
	  on it (it ran before trips recorded buckets): those trips take them, in the order
	  they ran, each up to its plan, the last one the rest.

	Returns {trip: {(opl, farm): {"planned", "total", "awaiting", "trolley", "loaded",
	"transit", "shelved", "buckets": {UPPER(bucket): state}, "riding",
	"riding_buckets"}}} for the trips (from the lookback window on) planning or carrying
	these orders. `farm` is the trip order row's farm.

	A bucket several orders share (shared_buckets) is one bucket: it counts on its lead
	order's portion only, on the one trip that takes it; the other orders' portions on
	that trip list it under riding / riding_buckets, never in total or a state count."""
	opls = {o for o in (opls or []) if o}
	if not opls:
		return {}
	groups = shared_buckets(
		frappe.db.sql_list(
			"""SELECT DISTINCT pli.bucket FROM `tabPick List Item` pli
			WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(o)s
			  AND IFNULL(pli.bucket, '') != '' AND IFNULL(pli.shelved, 0) = 0
			  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)""",
			{"o": tuple(opls)},
		)
	)
	# The orders sharing their buckets too: where a shared bucket goes is its lead's call.
	opls = tuple(opls | {o for g in groups.values() for o in g["opls"]})
	since = frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)
	trips = {}
	plans = {}  # (opl, farm) -> {trip: planned}
	for r in frappe.db.sql(
		"""SELECT t.name, t.vehicle, t.status, t.trip_date, t.run, t.creation, t.departed_stops,
		       t.dispatched_at, o.order_pick_list AS opl, IFNULL(o.farm, '') AS farm,
		       GREATEST(IFNULL(o.buckets, 0), IFNULL(o.loaded_buckets, 0)) AS planned
		FROM `tabBucket Request Trip Order` o
		JOIN `tabBucket Request Trip` t ON t.name = o.parent
		WHERE o.order_pick_list IN %(opls)s AND t.trip_date >= %(since)s""",
		{"opls": opls, "since": since},
		as_dict=True,
	):
		trips.setdefault(r.name, r)
		p = plans.setdefault((r.opl, r.farm), {})
		p[r.name] = p.get(r.name, 0) + int(r.planned or 0)
	pinned = {}  # (opl, UPPER(bucket)) -> (trip, farm)
	for r in frappe.db.sql(
		"""SELECT tb.order_pick_list AS opl, UPPER(tb.bucket) AS bucket, IFNULL(tb.farm, '') AS farm,
		       t.name, t.vehicle, t.status, t.trip_date, t.run, t.creation, t.departed_stops,
		       t.dispatched_at
		FROM `tabBucket Request Trip Bucket` tb
		JOIN `tabBucket Request Trip` t ON t.name = tb.parent
		WHERE tb.parenttype = 'Bucket Request Trip' AND tb.order_pick_list IN %(opls)s
		  AND IFNULL(tb.bucket, '') != '' AND t.trip_date >= %(since)s
		  AND (IFNULL(tb.off_truck, 0) = 0 OR IFNULL(tb.shelved, 0) = 1)
		ORDER BY t.trip_date DESC, t.creation DESC""",
		{"opls": opls, "since": since},
		as_dict=True,
	):
		trips.setdefault(r.name, r)
		pinned.setdefault((r.opl, r.bucket), (r.name, r.farm))

	def drive_order(name):
		t = trips[name]
		return (str(t.trip_date), 0 if t.run else 1, int(t.run or 0), str(t.creation), name)

	def still_here(name, farm):
		t = trips[name]
		departed = [f for f in (t.departed_stops or "").split(",") if f]
		return t.status in ACTIVE_TRIP_STATUSES and not any(_same_farm(f, farm) for f in departed)

	# Each bucket of each order once, at its furthest state (a bucket spans a row per box).
	lot = (
		"IFNULL(pli.loaded_on_truck, 0) = 1"
		if frappe.db.has_column("Pick List Item", "loaded_on_truck")
		else "0"
	)
	buckets = {}  # (opl, UPPER(bucket)) -> dict
	for r in frappe.db.sql(
		"""SELECT pli.parent AS opl, UPPER(pli.bucket) AS bucket,
		       COALESCE(NULLIF(pli.farm, ''), """
		+ _WH_FARM
		+ """, '') AS farm,
		       MAX(GREATEST(IFNULL(pli.shelved, 0), IFNULL(pli.issued, 0),
		           IF(IFNULL(pli.custom_ready_for_packing, 0) = 1 AND IFNULL(pli.awaiting_transfer, 0) = 0, 1, 0))) AS shelved,
		       MAX(IFNULL(pli.in_transit, 0)) AS transit,
		       MAX(IF((IFNULL(pli.loaded_in_trolley, 0) = 1 OR """
		+ lot
		+ """) AND """
		+ TRANSFER_TRUCK_OK
		+ """, 1, 0)) AS on_truck,
		       -- In a trolley since today only: an earlier day's trolley is gone at
		       -- midnight (clear_left_trolleys), never counted on today's trips.
		       MAX(IF(IFNULL(pli.loaded_in_trolley, 0) = 1 AND pli.modified >= %(today)s, 1, 0)) AS trolley,
		       MAX(IF(IFNULL(pli.in_transit, 0) = 1 OR IFNULL(pli.loaded_in_trolley, 0) = 1 OR """
		+ lot
		+ """, pli.transit_truck, NULL)) AS truck,
		       MIN(pli.loaded_in_trolley_at) AS scanned_at
		FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s
		  AND IFNULL(pli.bucket, '') != '' AND IFNULL(pli.not_found, 0) = 0
		GROUP BY pli.parent, UPPER(pli.bucket), COALESCE(NULLIF(pli.farm, ''), """
		+ _WH_FARM
		+ """, '')""",
		{"opls": opls, "today": str(frappe.utils.today())},
		as_dict=True,
	):
		state = (
			"shelved"
			if r.shelved
			else "transit"
			if r.transit
			else "loaded"
			if r.on_truck
			else "trolley"
			if r.trolley
			else "awaiting"
		)
		buckets.setdefault((r.opl, r.bucket), frappe._dict(r, state=state))

	out = {}

	def portion(trip, opl, farm):
		return out.setdefault(trip, {}).setdefault(
			(opl, farm),
			{
				"planned": plans.get((opl, farm), {}).get(trip, 0),
				"total": 0,
				"awaiting": 0,
				"trolley": 0,
				"loaded": 0,
				"transit": 0,
				"shelved": 0,
				"buckets": {},
				# Shared buckets travelling for another order on this trip: listed under
				# this order too, but counted once (on the lead's portion).
				"riding": 0,
				"riding_buckets": {},
			},
		)

	def put(trip, opl, farm, bucket, state):
		s = portion(trip, opl, farm)
		s["total"] += 1
		s[state] += 1
		s["buckets"][bucket] = state

	# The plan farm a bucket's farm stands for on this order (a trip row names the farm short).
	def plan_farm(opl, farm):
		if (opl, farm) in plans:
			return farm
		return next((f for (o, f) in plans if o == opl and _same_farm(f, farm)), farm)

	placed = {}  # (opl, bucket) -> (trip, plan farm)
	riders = []  # a shared bucket's rows for the orders it does not travel for
	free = {}  # (opl, plan farm) -> [bucket]
	arrived = {}  # (opl, plan farm) -> [bucket shelved, recorded on no trip]
	for (opl, bkt), b in buckets.items():
		farm = plan_farm(opl, b.farm)
		lead = shared_lead(groups, bkt, b.farm)
		if lead and lead != opl and (lead, bkt) in buckets:
			riders.append((opl, bkt, b, farm, lead))
			continue
		pin = pinned.get((opl, bkt))
		if pin:
			placed[(opl, bkt)] = (pin[0], plan_farm(opl, pin[1] or farm))
			put(pin[0], opl, placed[(opl, bkt)][1], bkt, b.state)
		elif b.state != "shelved":
			free.setdefault((opl, farm), []).append(b)
		else:
			arrived.setdefault((opl, farm), []).append(b)
	for (opl, farm), bs in free.items():
		plan = plans.get((opl, farm)) or {}
		order = sorted(plan, key=drive_order)
		here = [t for t in order if still_here(t, farm)]
		room = {t: plan[t] - len((out.get(t, {}).get((opl, farm)) or {}).get("buckets", {})) for t in here}
		bs.sort(key=lambda b: (_SHARE_RANK[b.state], str(b.scanned_at or "9999"), b.bucket))
		for b in bs:
			on_truck = b.state in ("loaded", "transit") and b.truck
			fits = [t for t in here if not on_truck or (trips[t].vehicle or "") == b.truck]
			trip = next((t for t in fits if room[t] > 0), None)
			if trip is None and b.state != "awaiting" and fits:
				trip = fits[-1]  # on a trolley / truck beyond the plan: the last run still here
			if trip is None:
				continue
			room[trip] -= 1
			put(trip, opl, farm, b.bucket, b.state)
			placed[(opl, b.bucket)] = (trip, farm)
	# A shared bucket rides the one trip its lead's portion takes it on.
	for opl, bkt, b, farm, lead in riders:
		where = placed.get((lead, bkt))
		if where:
			s = portion(where[0], opl, farm)
			s["riding"] += 1
			s["riding_buckets"][bkt] = b.state
	# A trip that left (dispatched) with none of a portion's buckets recorded on it ran
	# before trips recorded their buckets: that portion's shelved buckets no trip took
	# came on it -- filling such trips in the order they ran, each up to its plan, the
	# last one taking the rest. A trip that never left carried none of them.
	for (opl, farm), bs in arrived.items():
		plan = plans.get((opl, farm)) or {}
		legs = [
			t
			for t in sorted(plan, key=drive_order)
			if trips[t].get("dispatched_at")
			and not (out.get(t, {}).get((opl, farm)) or {}).get("buckets")
			and not (out.get(t, {}).get((opl, farm)) or {}).get("riding")
		]
		bs.sort(key=lambda b: b.bucket)
		for k, t in enumerate(legs):
			take = bs if k == len(legs) - 1 else bs[: plan[t]]
			bs = bs[len(take) :]
			for b in take:
				put(t, opl, farm, b.bucket, b.state)
				placed[(opl, b.bucket)] = (t, farm)
	# One physical bucket counts once on a trip, also when shared_buckets doesn't group
	# it (an order past its delivery date still has a row of it): it stays on a live
	# order's portion -- one that plans it, else the earliest delivery -- and the others
	# list it as riding.
	past = _past_delivery_opls(opls)
	for _trip, by in out.items():
		where = {}
		for key, s in by.items():
			for bkt in s["buckets"]:
				where.setdefault(bkt, []).append(key)
		for bkt, keys in where.items():
			if len(keys) < 2:
				continue
			keep = min(keys, key=lambda k: (k[0] in past, not by[k]["planned"], k[0]))
			for k in keys:
				if k == keep:
					continue
				s = by[k]
				state = s["buckets"].pop(bkt)
				s["total"] -= 1
				s[state] -= 1
				s["riding"] += 1
				s["riding_buckets"][bkt] = state
	# Trips that plan a portion none of whose buckets count there still show it (0 of n).
	for (opl, farm), plan in plans.items():
		for trip in plan:
			portion(trip, opl, farm)
	return out


#: A trip's bucket counts by state (trip_numbers), furthest last.
TRIP_COUNT_KEYS = ("awaiting", "trolley", "loaded", "transit", "shelved")


def _blank_numbers():
	return {
		"planned": 0,
		"buckets": 0,
		"load": 0,
		"total": 0,
		"awaiting": 0,
		"trolley": 0,
		"loaded": 0,
		"transit": 0,
		"shelved": 0,
		"on_truck": 0,
		"riding": 0,
	}


def _add_numbers(into, part):
	for k in ("planned", "buckets", "load", "total", "on_truck", "riding", *TRIP_COUNT_KEYS):
		into[k] += part[k]


def trip_numbers(trips, shares=None, ids=False):
	"""THE bucket numbers of these trips. The Bucket Logistics dashboard, the farm app
	(Bucket Requests, Trips, Completed), the packhouse app and the Transfer Scheduling
	page all show these and never count a trip themselves. Built on trip_bucket_shares:
	one bucket, one trip; a bucket several orders share counts once (on its lead's
	order, the others list it as riding).

	Whole trip -- every order on it, whatever day it delivers:
	- capacity: the truck's buckets (an ended trip keeps the one it ran with);
	- planned: its plan rows, each max(planned, loaded), of live orders only -- a
	  deleted or cancelled pick list counts nowhere, nor, on a trip still at the farm,
	  an order past its delivery date with none of it on a trolley or truck;
	- total: the buckets counting on it, by state: awaiting (on the farm's shelf),
	  trolley, loaded (on the truck at the farm), transit (on the road), shelved
	  (arrived); on_truck = loaded + transit + shelved;
	- buckets = per order its total, else (none of its buckets counted yet) its plan --
	  0 for an order only riding on another's shared bucket: the trip's / stop's / order's bucket count every surface shows beside on_truck;
	  free = capacity - buckets;
	- load = per order max(planned, total): the most room it can take (the planner's);
	- riding: its orders' shared buckets travelling with another order (not in total).

	Returns {trip: {"trip", "vehicle", "status", "trip_date", "run", "capacity", "free",
	<the counts>, "farms": {farm: counts}, "orders": [counts + "opl", "farm",
	"order_name", "delivery_date", "counts" (0 = left out of the trip's totals)]}}.
	`shares`: a trip_bucket_shares result covering these trips' orders, when the caller
	has one; else it is read here. ids: each order entry also lists its buckets on the
	trip, {UPPER(bucket): state}, under "ids"."""
	names = [t for t in dict.fromkeys(trips or []) if t]
	if not names:
		return {}
	heads = {
		h.name: h
		for h in frappe.get_all(
			"Bucket Request Trip",
			filters={"name": ["in", names]},
			fields=["name", "vehicle", "status", "trip_date", "run", "capacity_buckets"],
		)
	}
	if not heads:
		return {}
	rows = frappe.get_all(
		"Bucket Request Trip Order",
		filters={"parenttype": "Bucket Request Trip", "parent": ["in", list(heads)]},
		fields=["parent", "order_pick_list", "order_name", "farm", "buckets", "loaded_buckets"],
		order_by="parent asc, idx asc",
		limit_page_length=0,
	)
	opls = {r.order_pick_list for r in rows if r.order_pick_list}
	if shares is None:
		shares = trip_bucket_shares(opls)
	for name in heads:
		opls |= {o for (o, _f) in (shares.get(name) or {}) if o}
	meta = {
		m.name: m
		for m in frappe.db.sql(
			"""SELECT opl.name, opl.order_name, so.delivery_date
			FROM `tabOrder Pick List` opl LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
			WHERE opl.name IN %(o)s AND opl.docstatus < 2""",
			{"o": tuple(opls) or ("",)},
			as_dict=True,
		)
	}
	today = str(frappe.utils.today())
	caps = {v: _vehicle_capacity(v) for v in {h.vehicle for h in heads.values() if h.vehicle}}
	plan = {}  # trip -> {(opl, farm): [planned, order_name]}
	for r in rows:
		p = plan.setdefault(r.parent, {}).setdefault(
			(r.order_pick_list or "", r.farm or ""), [0, r.order_name or ""]
		)
		p[0] += max(int(r.buckets or 0), int(r.loaded_buckets or 0))
	out = {}
	for name, h in heads.items():
		active = h.status in ACTIVE_TRIP_STATUSES
		mine = shares.get(name) or {}
		by = dict.fromkeys(list(plan.get(name) or {}) + list(mine))
		n = _blank_numbers()
		farms, orders = {}, []
		for opl, farm in by:
			sh = mine.get((opl, farm)) or {}
			m = meta.get(opl)
			planned, row_name = (plan.get(name) or {}).get((opl, farm)) or (0, "")
			p = {
				"opl": opl,
				"farm": farm,
				"order_name": (m.order_name if m else "") or row_name or opl,
				"delivery_date": str(m.delivery_date or "") if m else "",
				"planned": planned,
				"riding": int(sh.get("riding") or 0),
			}
			for k in TRIP_COUNT_KEYS:
				p[k] = int(sh.get(k) or 0)
			p["total"] = sum(p[k] for k in TRIP_COUNT_KEYS)
			if ids:
				p["ids"] = dict(sh.get("buckets") or {})
			p["on_truck"] = p["loaded"] + p["transit"] + p["shelved"]
			# A riding-only order (its shared bucket counts on its lead) adds no bucket.
			p["buckets"] = p["total"] or (0 if p["riding"] else p["planned"])
			p["load"] = max(p["planned"], p["total"])
			moved = p["total"] - p["awaiting"]
			# Counted on the trip: a live order, and not one the truck won't take any more.
			p["counts"] = (
				0
				if not m
				or (active and p["delivery_date"] and p["delivery_date"] < today and not moved)
				or not (p["load"] or p["riding"])
				else 1
			)
			orders.append(p)
			if p["counts"]:
				_add_numbers(n, p)
				_add_numbers(farms.setdefault(farm, _blank_numbers()), p)
		cap = int(h.capacity_buckets or 0)
		if h.status != "Received":
			cap = caps.get(h.vehicle) or cap
		out[name] = {
			"trip": name,
			"vehicle": h.vehicle,
			"status": h.status,
			"trip_date": str(h.trip_date or ""),
			"run": int(h.run or 0),
			"capacity": cap,
			**n,
			"free": cap - n["buckets"],
			"farms": farms,
			"orders": orders,
		}
	return out


def trip_order_numbers(numbers):
	"""(opl, farm) -> that order's counts on the trip (a trip_numbers entry)."""
	return {(o["opl"], o["farm"]): o for o in (numbers or {}).get("orders") or []}


def earlier_trip_with_room(opl, farm, trip):
	"""The earlier run still at `farm` whose share of (opl, farm) is not yet on trolleys
	or trucks, when `trip` plans that portion too -- its buckets go there first (a farm
	loads an order's runs in order). None when `trip` is the one to fill now."""
	shares = trip_bucket_shares([opl])
	mine = [
		(t, s) for t, by in shares.items() for (o, f), s in by.items() if o == opl and _same_farm(f, farm)
	]
	if not any(t == trip for t, _s in mine):
		return None
	meta = {
		t.name: t
		for t in frappe.get_all(
			"Bucket Request Trip",
			filters={"name": ["in", [t for t, _s in mine]]},
			fields=["name", "vehicle", "run", "trip_date", "creation", "status", "departed_stops"],
		)
	}

	def key(t):
		m = meta[t]
		return (str(m.trip_date), 0 if m.run else 1, int(m.run or 0), str(m.creation), t)

	for t, s in sorted(mine, key=lambda x: key(x[0])):
		if t == trip:
			return None
		m = meta[t]
		if m.status not in ACTIVE_TRIP_STATUSES or any(
			_same_farm(f, farm) for f in (m.departed_stops or "").split(",") if f
		):
			continue
		# Its share still has buckets waiting on the shelf (not-found / short ones don't hold it up).
		if s["awaiting"] > 0:
			return m
	return None


def _trip_loaded_count(doc):
	"""Buckets physically on the trip: one per bucket id, however many orders it fills
	(a bucket split over two orders is one bucket on the truck, not two). Falls back to
	the orders' count while no bucket is recorded on the trip yet."""
	ids = {(b.bucket or "").upper() for b in doc.get("trip_buckets") or [] if b.bucket and not b.off_truck}
	return len(ids) if ids else sum(int(o.loaded_buckets or 0) for o in doc.orders)


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
				(
					o
					for o in doc.orders
					if o.order_pick_list == r.order_pick_list
					and (o.farm or "") == (r.farm or "")
					and int(o.loaded_buckets or 0) > 0
				),
				None,
			)
			if o:
				o.loaded_buckets = int(o.loaded_buckets) - 1
		doc.loaded_buckets = _trip_loaded_count(doc)
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
	_add_trip_buckets(doc, [r for b in entries for r in b["rows"][:1]])
	doc.loaded_buckets = _trip_loaded_count(doc)
	_advance_stops(doc)
	doc.save(ignore_permissions=True)


def _truck_full(doc):
	"""Every place on the trip's truck is taken (False when its capacity is unknown)."""
	cap = _trip_capacity(doc)
	return bool(cap) and _trip_loaded_count(doc) >= cap


def _trip_capacity(doc):
	"""A trip still running follows its truck's current capacity (the Vehicle can be
	corrected after the trip was planned); an ended trip keeps the one it ran with."""
	if doc.status != "Received":
		return _vehicle_capacity(doc.vehicle) or int(doc.capacity_buckets or 0)
	return int(doc.capacity_buckets or 0)


def _vehicle_capacity(vehicle):
	v = frappe.db.get_value(
		"Vehicle", vehicle, ["custom_trolley_capacity", "custom_buckets_per_trolley"], as_dict=True
	)
	return int((v.custom_trolley_capacity or 0) * (v.custom_buckets_per_trolley or 0)) if v else 0


def _production_setting(fieldname):
	"""A Production Settings value, None while the field doesn't exist (before migrate)."""
	try:
		return frappe.db.get_single_value("Production Settings", fieldname)
	except Exception:
		return None


def min_load_needed(vehicle):
	"""The buckets a truck must carry before it may be sent to the hub without a reason:
	all of its capacity, or -- Production Settings > Allow Sending Partly Loaded Trucks --
	Minimum Truck Load (%) of it, rounded up. Returns (needed, capacity, pct); needed is
	0 (no check) while the truck's capacity is unknown."""
	cap = _vehicle_capacity(vehicle) if vehicle else 0
	if cap <= 0:
		return 0, 0, 0
	pct = 100.0
	if cint(_production_setting("allow_partial_truck_load")):
		pct = min(100.0, max(0.0, flt(_production_setting("min_truck_load_pct"))))
	needed = int(math.ceil(cap * pct / 100 - 1e-9))
	return needed, cap, int(pct) if pct == int(pct) else pct


def _trip_load_count(doc):
	"""The buckets this trip takes to the hub: those loaded on its truck, or -- nothing
	loaded through the app -- the ones it plans (Dispatch flags them in transit)."""
	return _trip_loaded_count(doc) if _trip_has_loads(doc) else int(doc.get("total_buckets") or 0)


def below_minimum(doc):
	"""The trip's truck is under its minimum load (min_load_needed): {loaded, capacity,
	needed, pct}, else None."""
	needed, cap, pct = min_load_needed(doc.vehicle)
	if not needed:
		return None
	loaded = _trip_load_count(doc)
	if loaded >= needed:
		return None
	return {"loaded": loaded, "capacity": cap, "needed": needed, "pct": pct}


def below_minimum_refusal(doc, short):
	"""The reply refusing to send a truck under its minimum load without a reason."""
	return {
		"status": "error",
		"reason": "below_minimum",
		"loaded": short["loaded"],
		"capacity": short["capacity"],
		"needed": short["needed"],
		"pct": short["pct"],
		"message": "{0} has {1} of {2} buckets — it leaves at {3} ({4}%). Load more or give a reason to send it now.".format(
			doc.vehicle, short["loaded"], short["capacity"], short["needed"], short["pct"]
		),
	}


def below_minimum_note(short, reason=None, hub=None):
	"""'Sent to <hub> with 12 of 144 (min 116): <reason>'."""
	return "Sent to {0} with {1} of {2} (min {3}){4}".format(
		hub or transfer_hub(required=False) or "the packhouse",
		short["loaded"],
		short["capacity"],
		short["needed"],
		": {0}".format(frappe.utils.escape_html(reason)) if reason else "",
	)


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
		frappe.response["message"] = _transfer_schedule_payload(
			from_date, to_date, route_date=fd.get("route_date") or None
		)
	except Exception as e:
		frappe.response["message"] = {"success": False, "error": str(e)}


def schedule_orders(from_date, to_date):
	"""Only the feed's orders (_transfer_schedule_payload()["orders"], without their
	location): what automatic scheduling reads for days whose trips, fleet and routes it
	already has -- building the whole feed for them each time was most of a run."""
	return _schedule_orders(from_date, to_date)[0]


def _schedule_orders(from_date, to_date, hub=None):
	"""(orders, left behind) of the Transfer Scheduling feed for these delivery dates."""
	today = frappe.utils.today()
	hub = hub or transfer_hub()
	sched = _schedule_map()

	opl_rows = frappe.db.sql(
		"""
		SELECT DISTINCT opl.name AS opl, opl.order_name AS order_name, opl.sales_order AS so,
		       so.customer AS customer, so.delivery_date AS delivery_date, opl.team AS opl_team
		FROM `tabOrder Pick List` opl
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
		WHERE opl.docstatus < 2
		  AND (so.delivery_date BETWEEN %(f)s AND %(t)s
		       -- A quality-issue replacement requested ASAP (packing_quality) is planned
		       -- whatever day the page shows: its order is being packed now.
		       OR (IFNULL(pli.transfer_priority, '') = 'ASAP' AND pli.awaiting_transfer = 1
		           AND so.delivery_date >= %(today)s))
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1
		       OR pli.shelved = 1)
		  AND NOT """
		+ PACKED_SQL
		+ """
		""",
		{"f": from_date, "t": to_date, "today": today},
		as_dict=True,
	)
	opl_names = [r["opl"] for r in opl_rows]
	mixed = _mixed_map(opl_names)
	behind = _left_behind(opl_names)

	# opl -> farm -> {open: variety -> {buckets, stems}, on_road}
	# Driven by the transfer FLAGS, not "farm != Kapkolia": a bucket at its own
	# sales farm (e.g. Karen for a Karen order) was never flagged and must not be
	# planned onto a truck.
	agg = {}
	# opl -> {arrived, trolley}: the order card's stage checks (on a truck / arrived).
	# An order whose buckets have all arrived stays listed so its last stage shows.
	stage = {}
	for b in _transfer_buckets(opl_names):
		if b["farm"] and not (hub and b["farm"].lower() == hub.lower()):
			st = stage.setdefault(b["opl"], {"arrived": 0, "trolley": 0})
			if b["shelved"]:
				st["arrived"] += 1
			elif b["loaded"] and not b["in_transit"]:
				st["trolley"] += 1
		if not (b["open"] or b["on_road"]) or not b["farm"]:
			continue
		# Stock already at the packhouse needs no truck: never plan it onto a trip.
		if b["open"] and hub and b["farm"].lower() == hub.lower():
			continue
		fmap = agg.setdefault(b["opl"], {})
		frow = fmap.setdefault(b["farm"], {"varieties": {}, "on_road": 0, "asap": 0})
		if b["on_road"]:
			frow["on_road"] += 1
			continue
		if b["asap"]:
			frow["asap"] += 1
		vrow = frow["varieties"].setdefault(b["variety"] or "", {"buckets": 0, "stems": 0})
		vrow["buckets"] += 1
		vrow["stems"] += b["stems"]

	orders = []
	for r in opl_rows:
		fmap = agg.get(r["opl"]) or {}
		farms_out = []
		total_b, total_s, total_road, total_asap = 0, 0.0, 0, 0
		for farm, frow in sorted(fmap.items()):
			vmap = frow["varieties"]
			fb = sum(v["buckets"] for v in vmap.values())
			fs = sum(v["stems"] for v in vmap.values())
			total_b += fb
			total_s += fs
			total_road += frow["on_road"]
			total_asap += frow["asap"]
			farms_out.append(
				{
					"farm": farm,
					"buckets": fb,
					"stems": fs,
					"on_road": frow["on_road"],
					# Left behind by a truck that already came — goes first on the next trip.
					"left_behind": behind.get((r["opl"], farm)),
					# Quality-issue replacements requested ASAP — first of all.
					"asap": frow["asap"],
					"varieties": [
						{"variety": k, "buckets": v["buckets"], "stems": v["stems"]} for k, v in vmap.items()
					],
				}
			)
		st = stage.get(r["opl"]) or {"arrived": 0, "trolley": 0}
		if not farms_out and not st["arrived"]:
			continue
		sc = sched.get(r["opl"])
		if not sc:
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
				"asap_buckets": total_asap,
				"trolley_buckets": st["trolley"],
				"arrived_buckets": st["arrived"],
				"farms": farms_out,
			}
		)
	return orders, behind


def _transfer_schedule_payload(from_date, to_date, trip_date=None, route_date=None):
	"""The Transfer Scheduling feed — shared by the page and automatic scheduling.
	`trip_date`: also every trip of that day, whatever its status (the app's day filter).
	`route_date`: whose routes to return (default today); each route carries its date."""
	today = frappe.utils.today()
	hub = transfer_hub()
	orders, behind = _schedule_orders(from_date, to_date, hub=hub)
	# The day whose routes (and each truck's runs on them) are returned: today unless asked.
	route_day = str(frappe.utils.getdate(route_date)) if route_date else str(today)

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
				"route_end": _route_end(v.name, route_day),
				# Each run is a full truck: what the planner may still fill today.
				"runs": [
					{k: r[k] for k in ("route", "run", "runs", "stops", "trip", "trip_status")}
					for r in _day_runs(v.name, route_day)
				],
			}
		)
		veh_out[-1]["open_runs"] = sum(1 for r in veh_out[-1]["runs"] if _run_open(r))

	# Trips: all of today's, plus earlier ones that were never received (a
	# dispatched truck still on the road, or a stale draft that never left).
	# Only loading today's trips hid both of those.
	trip_names = set(frappe.get_all("Bucket Request Trip", filters={"trip_date": today}, pluck="name"))
	if trip_date and str(trip_date) != str(today):
		trip_names |= set(
			frappe.get_all("Bucket Request Trip", filters={"trip_date": trip_date}, pluck="name")
		)
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
	numbers = trip_numbers(trip_names)
	trips_out = [
		_trip_dict(frappe.get_doc("Bucket Request Trip", tn), today, numbers.get(tn) or {})
		for tn in trip_names
	]
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

	# The day's routes (today unless the app asks for its trip date)
	route_docs = frappe.get_all(
		"Bucket Logistics Route",
		filters={"route_date": route_day},
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
				"route_date": str(doc.route_date or route_day),
				"from_datetime": _dt(doc.from_datetime),
				"to_datetime": _dt(doc.to_datetime),
				"total_km": doc.total_km,
				"auto_planned": int(doc.get("auto_planned") or 0),
				"legs": legs,
				"farms": _route_farms(doc.legs),
				"runs": [r["stops"] for r in route_runs(legs)],
			}
		)

	where = order_locations([o["opl"] for o in orders])
	for o in orders:
		o["location"] = where.get(o.get("opl"), "")
	return {
		"success": True,
		"orders": orders,
		"vehicles": veh_out,
		"trips": trips_out,
		"truck_status": _truck_status(today),
		"distances": distances,
		"farm_list": _hub_company_farms(hub),
		"routes": routes_out,
		"packhouse": hub,
		"auto_planning": _auto_planning_status(),
		"duplicate_trips": duplicate_trips(today),
		"distributions": _distributions(from_date, to_date),
		# Ready to issue at the hub (all its buckets on hub shelves): issue them first.
		"hub_ready": hub_ready_orders(from_date, to_date),
		"left_behind": [
			{
				**v,
				"opl": k[0],
				"farm": k[1],
				"order_name": next((o["order_name"] for o in orders if o["opl"] == k[0]), k[0]),
			}
			for k, v in behind.items()
		],
		"today": str(today),
		"window": {"from": str(from_date), "to": str(to_date)},
		"generated_at": str(frappe.utils.now()),
	}


DISTRIBUTION = "Bucket Distribution"


def log_distribution(delivery_date, loads, source="Manual"):
	"""One Bucket Distribution per Distribute run — never merged into an earlier one, so
	the page lists each run on its own. loads: [{vehicle, trip, run, farms, buckets,
	stems, orders: [order names]}], only the trucks that were actually saved."""
	loads = [l for l in loads or [] if l.get("trip")]
	# A trip the run emptied and deleted (an untouched draft re-planned away) is not logged:
	# its link would fail the whole record.
	kept = set(
		frappe.get_all(
			"Bucket Request Trip", filters={"name": ["in", [l["trip"] for l in loads]]}, pluck="name"
		)
		if loads
		else []
	)
	loads = [l for l in loads if l["trip"] in kept]
	if not loads:
		return None
	doc = frappe.new_doc(DISTRIBUTION)
	doc.delivery_date = delivery_date
	doc.source = source
	for l in loads:
		doc.append(
			"trucks",
			{
				"vehicle": l.get("vehicle"),
				"trip": l.get("trip"),
				"run": frappe.utils.cint(l.get("run")) or None,
				"farms": l.get("farms") or "",
				"buckets": frappe.utils.cint(l.get("buckets")),
				"stems": frappe.utils.cint(l.get("stems")),
				"orders": "\n".join(l.get("orders") or []),
			},
		)
	doc.total_trucks = len(loads)
	doc.total_buckets = sum(r.buckets for r in doc.trucks)
	doc.total_stems = sum(r.stems for r in doc.trucks)
	doc.insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist(methods=["POST"])
def logDistribution():
	# Called by the page once ⚡ Distribute has saved its trucks.
	fd = frappe.form_dict
	loads = frappe.parse_json(fd.get("loads") or "[]")
	name = log_distribution(fd.get("delivery_date") or frappe.utils.add_days(frappe.utils.today(), 1), loads)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


def _distributions(from_date, to_date):
	"""This delivery window's Distribute runs, newest first, each with its trucks."""
	if not frappe.db.table_exists(DISTRIBUTION):
		return []
	out = []
	for d in frappe.get_all(
		DISTRIBUTION,
		filters={"delivery_date": ["between", [from_date, to_date]]},
		fields=[
			"name",
			"creation",
			"owner",
			"source",
			"delivery_date",
			"total_trucks",
			"total_buckets",
			"total_stems",
		],
		order_by="creation desc",
		limit=50,
	):
		trucks = frappe.get_all(
			"Bucket Distribution Truck",
			filters={"parent": d.name, "parenttype": DISTRIBUTION},
			fields=["vehicle", "trip", "run", "farms", "buckets", "stems", "orders"],
			order_by="idx",
		)
		out.append(
			{
				"name": d.name,
				"at": str(d.creation),
				"by": frappe.utils.get_fullname(d.owner) if d.owner != "Administrator" else "Administrator",
				"source": d.source,
				"delivery_date": str(d.delivery_date),
				"trucks": [{**t, "orders": [o for o in (t.orders or "").split("\n") if o]} for t in trucks],
				"total_trucks": d.total_trucks,
				"total_buckets": d.total_buckets,
				"total_stems": d.total_stems,
			}
		)
	return out


@frappe.whitelist(methods=["POST"])
def saveBucketTrip():
	# Upsert a Bucket Request Trip. orders = rows joined by \x1e, fields within a row
	# by \x1f: [order_pick_list, order_name, customer, farm, varieties, buckets, stems,
	# full_farm_buckets(optional)]. A trip saved here is a person's: it is never
	# auto-planned, and automatic scheduling leaves it alone from now on.
	fd = frappe.form_dict
	# A trip leaves the hub (Scheduled) only through release_trip, which needs it full.
	status = fd.get("status") or "Draft"
	if status == "Scheduled" and not (
		fd.get("name") and frappe.db.get_value("Bucket Request Trip", fd.get("name"), "status") == "Scheduled"
	):
		status = "Draft"
	res = _save_trip(
		name=fd.get("name"),
		vehicle=fd.get("vehicle"),
		trip_date=fd.get("trip_date") or frappe.utils.today(),
		status=status,
		notes=fd.get("notes") or "",
		collection_order=fd.get("collection_order") or "",
		farm=fd.get("farm") or "",
		rows=_parse_trip_rows(fd.get("orders") or ""),
		route=fd.get("route") or None,
		run=frappe.utils.cint(fd.get("run")) or None,
	)
	if res.get("status") == "success" and dispatch_full_trips([res["name"]]):
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		res["dispatched"] = 1
	frappe.response["message"] = res


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


# ── One truck per farm at a time ──────────────────────────────────────────────
# A farm is collected by ONE vehicle at a time: once a truck is planned (or loading,
# or on its way) at a farm, every order there goes on that truck — a second truck is
# only allowed at a time that doesn't overlap (another run window). _save_trip
# enforces it for every planner (Distribute, add-to-trip, automatic scheduling).


def _day_span(date):
	"""The whole of `date` as (from, to) datetimes — for comparing run windows."""
	d = str(frappe.utils.getdate(date))
	return (frappe.utils.get_datetime(d + " 00:00:00"), frappe.utils.get_datetime(d + " 23:59:59"))


def _trip_window(route, date):
	"""When a run on `route` is out: the route's From–To. Without a route, all day."""
	if route:
		w = frappe.db.get_value("Bucket Logistics Route", route, ["from_datetime", "to_datetime"])
		if w and w[0] and w[1]:
			return (frappe.utils.get_datetime(w[0]), frappe.utils.get_datetime(w[1]))
	return _day_span(date)


def trip_room(name):
	"""Buckets trip `name` can still take: its truck's capacity less the most of what is
	planned on it (live orders), loaded on it, and its bucket count (trip_numbers) --
	whichever is fullest. None when the truck's capacity is unknown."""
	doc = frappe.get_doc("Bucket Request Trip", name)
	cap = _trip_capacity(doc)
	if not cap:
		return None
	n = trip_numbers([name]).get(name) or {}
	return cap - max(_live_planned(name), int(doc.loaded_buckets or 0), int(n.get("buckets") or 0))


def farm_holders(date, exclude_vehicle=None, exclude_trip=None, skip_auto_drafts=False, with_room=False):
	"""farm -> [(vehicle, trip, (from, to))]: the farms a truck is collecting from on
	`date` — every trip not ended, except at a farm its truck has already left.
	with_room: only trips that still have room (trip_room). One that is full no longer
	holds its farm: a second truck (or run) may collect there once the first is full --
	never while it still has room."""
	hub = transfer_hub(required=False)
	out = {}
	rooms = {}
	for r in frappe.db.sql(
		"""SELECT DISTINCT t.name, t.vehicle, t.route, t.status, t.auto_planned, t.departed_stops, o.farm
		FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
		WHERE t.trip_date = %(d)s AND t.status != 'Received' AND IFNULL(o.farm, '') != ''""",
		{"d": str(date)},
		as_dict=True,
	):
		if r.farm == hub or r.vehicle == exclude_vehicle or r.name == exclude_trip:
			continue
		if skip_auto_drafts and r.auto_planned and r.status == "Draft":
			continue  # the automatic scheduler replaces its own drafts
		if r.status == "Dispatched":
			continue  # the truck has left every farm and is heading to the hub
		if r.farm in [f for f in (r.departed_stops or "").split(",") if f]:
			continue
		if with_room:
			if r.name not in rooms:
				rooms[r.name] = trip_room(r.name)
			if rooms[r.name] is not None and rooms[r.name] <= 0:
				continue
		out.setdefault(r.farm, []).append((r.vehicle, r.name, _trip_window(r.route, date)))
	return out


def _farm_taken(vehicle, date, farm_windows, exclude_trip=None):
	"""(farm, other vehicle, its trip) for the first farm another truck already collects
	from at an overlapping time with room left, else None. farm_windows: [(farm, (from, to))]."""
	holders = farm_holders(date, exclude_vehicle=vehicle, exclude_trip=exclude_trip, with_room=True)
	for farm, (a0, a1) in farm_windows:
		for v, trip, (b0, b1) in holders.get(farm, []):
			if a0 < b1 and b0 < a1:
				return farm, v, trip
	return None


def _add_rows_to_trip(name, rows):
	"""Put plan rows on an existing trip (planned, loading or on its way — the truck is
	still collecting from their farm): merged into its row for the same order and farm,
	as far as the truck has room. Returns (added rows, [(row, buckets not added)])."""
	doc = frappe.get_doc("Bucket Request Trip", name)
	room = trip_room(name)
	room = 10**9 if room is None else max(0, room)
	added, left = [], []
	for row in rows:
		take = max(0, min(int(row["buckets"]), room))
		if take < int(row["buckets"]):
			left.append((row, int(row["buckets"]) - take))
		if not take:
			continue
		per = (row["stems"] / row["buckets"]) if row["buckets"] else 0
		stems = round(per * take)
		same = next(
			(
				o
				for o in doc.orders
				if o.order_pick_list == row["order_pick_list"] and (o.farm or "") == row["farm"]
			),
			None,
		)
		if same:
			same.buckets = int(same.buckets or 0) + take
			same.stems = int(same.stems or 0) + stems
		else:
			doc.append(
				"orders",
				{
					**row,
					"buckets": take,
					"stems": stems,
					"is_partial": 1 if take < int(row.get("full_farm_buckets") or take) else 0,
				},
			)
		room -= take
		added.append({**row, "buckets": take, "stems": stems})
	if added:
		doc.total_buckets = sum(int(o.buckets or 0) for o in doc.orders)
		doc.total_stems = sum(int(o.stems or 0) for o in doc.orders)
		doc.add_comment(
			"Info",
			"Added {0} bkt at {1}: this truck is already collecting there.".format(
				sum(a["buckets"] for a in added), ", ".join(sorted({a["farm"] for a in added}))
			),
		)
		doc.save(ignore_permissions=True)
	return added, left


def _redirect_to_holders(vehicle, date, rows, runs, route=None, only=None):
	"""Rows for a farm another truck is already collecting from at an overlapping time
	go on THAT truck's trip as far as it has room — a second truck collects there only
	once the first is full. Returns (rows left for `vehicle`: what no such trip had room
	for, [{trip, vehicle, farm, buckets}] redirected, [(row, n)] that didn't fit on the
	other trucks -- also in the rows left)."""
	hub = transfer_hub(required=False)
	holders = farm_holders(date, exclude_vehicle=vehicle, with_room=True)
	if not holders:
		return rows, [], []

	def mine(farm):
		"""When this truck would be at `farm`: its open runs that visit it, else all day."""
		if runs:
			ws = [
				_trip_window(r["route"], date)
				for r in runs
				if farm in r["stops"] and _run_open(r) and (not only or (r["route"], r["run"]) == only)
			]
			return ws or [_day_span(date)]
		return [_trip_window(route, date)]

	keep, redirected, short = [], [], []
	for row in rows:
		farm = row["farm"]
		hits = []
		if farm and farm != hub:
			hits = [
				(v, trip)
				for v, trip, (b0, b1) in holders.get(farm, [])
				if any(a0 < b1 and b0 < a1 for a0, a1 in mine(farm))
			]
		for v, trip in hits:
			added, left = _add_rows_to_trip(trip, [row])
			if added:
				redirected.append({"trip": trip, "vehicle": v, "farm": farm, "buckets": added[0]["buckets"]})
			if not left:
				row = None
				break
			n = left[0][1]
			per = (row["stems"] / row["buckets"]) if row["buckets"] else 0
			row = {**row, "buckets": n, "stems": round(per * n)}
		if row is None:
			continue
		if hits:
			short.append((row, row["buckets"]))
		keep.append(row)
	return keep, redirected, short


def _top_up_open_trip(vehicle, date, rows):
	"""Rows onto the truck's own open trip that day (not yet left their farm), as far as
	it has room. Returns (rows left over, [{trip, vehicle, farm, buckets}] added)."""
	trips = frappe.get_all(
		"Bucket Request Trip",
		filters={"vehicle": vehicle, "trip_date": date, "status": ["in", ACTIVE_TRIP_STATUSES]},
		pluck="name",
		order_by="creation asc",
	)
	added_to = []
	for trip in trips:
		gone = _departed(frappe.get_doc("Bucket Request Trip", trip))
		fit = [r for r in rows if r["farm"] not in gone]
		if not fit:
			continue
		added, left = _add_rows_to_trip(trip, fit)
		short = {id(r): n for r, n in left}
		fit_ids = {id(r) for r in fit}
		rest = []
		for r in rows:
			if id(r) not in fit_ids:
				rest.append(r)
			elif id(r) in short:
				n = short[id(r)]
				per = (r["stems"] / r["buckets"]) if r["buckets"] else 0
				rest.append({**r, "buckets": n, "stems": round(per * n)})
		rows = rest
		for farm in sorted({a["farm"] for a in added}):
			added_to.append(
				{
					"trip": trip,
					"vehicle": vehicle,
					"farm": farm,
					"buckets": sum(a["buckets"] for a in added if a["farm"] == farm),
				}
			)
		if not rows:
			break
	return rows, added_to


def _redirect_note(redirected, short):
	parts = [
		"{0} bkt at {1} went on {2}'s trip {3} — it is already collecting there".format(
			r["buckets"], r["farm"], r["vehicle"], r["trip"]
		)
		for r in redirected
	]
	if short:
		parts.append(
			"{0} bkt didn't fit on that truck (full) and go on this one".format(sum(n for _r, n in short))
		)
	return ". ".join(parts)


def _farm_taken_refusal(hit):
	farm, other, trip = hit
	return {
		"status": "error",
		"reason": "farm_taken",
		"farm": farm,
		"vehicle": other,
		"trip": trip,
		"message": "{0} is already collecting from {1} at that time (trip {2}). Put these buckets on {0} — "
		"one truck picks up all of {1}'s orders.".format(other, farm, trip),
	}


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
	commit=True,
):
	"""Validate and upsert a Bucket Request Trip; returns the response message.

	Capacity is recomputed here and is authoritative — over-capacity refuses the whole
	save, no partial write. Also refuses to claim more buckets of an (order, farm) than
	are still open once every other planned trip today is counted. That is what stops a
	double-click (or a second planner, or the automatic scheduler) from putting the
	same buckets on two trucks. commit=False leaves committing to the caller (the
	automatic scheduler saves a truck's runs together or not at all)."""
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
	wanted = {}
	for r in rows:
		key = (r["order_pick_list"], r["farm"])
		wanted[key] = wanted.get(key, 0) + r["buckets"]
	if existing:
		# Buckets loaded for this trip (truck, trip record or trolley) are open and its
		# own: it claims its new plan or those, whichever is more -- never both.
		for key, n in _trip_loads(opls).get(name, {}).items():
			if key in wanted:
				wanted[key] = max(wanted[key], n)
	# By physical bucket: a shared bucket another order's trip already carries is not open.
	claims = _bucket_claims(opls, exclude_trip=name if existing else None, planning=wanted)
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

	# One truck per farm at a time: a farm another truck is already collecting from
	# (planned, loading or on its way) — those rows go on that truck's trip instead.
	redirected, short = [], []
	if not existing:
		rows, redirected, short = _redirect_to_holders(vehicle, trip_date, rows, runs, route=route, only=only)
		if rows and not runs:
			# An unrouted truck drives one trip at a time: its open trip that day is filled
			# before a second one is made.
			rows, topped = _top_up_open_trip(vehicle, trip_date, rows)
			redirected += topped
		if not rows:
			if not redirected:
				return {
					"status": "error",
					"reason": "farm_taken",
					"message": _redirect_note(redirected, short),
				}
			if commit:
				frappe.db.commit()  # nosemgrep: frappe-manual-commit
			return {
				"status": "success",
				"name": redirected[0]["trip"],
				"names": sorted({r["trip"] for r in redirected}),
				"redirected": redirected,
				"message": _redirect_note(redirected, short),
				"total_buckets": sum(r["buckets"] for r in redirected),
				"total_stems": 0,
				"capacity_buckets": _vehicle_capacity(vehicle),
			}

	if runs:
		plan, unplaced = _split_into_runs(vehicle, trip_date, rows, only=only)
		hub = transfer_hub(required=False)
		hit = _farm_taken(
			vehicle,
			trip_date,
			[
				(r["farm"], _trip_window(sl["route"], trip_date))
				for sl in plan
				for r in sl["rows"]
				if r["farm"] != hub
			],
		)
		if hit:
			return _farm_taken_refusal(hit)
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
		if commit:
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return {
			"status": "success",
			"name": names[0],
			"names": names,
			"runs": [
				{"trip": nm, "run": slot["run"], "buckets": sum(r["buckets"] for r in slot["rows"])}
				for nm, slot in zip(names, plan, strict=True)
			],
			"redirected": redirected,
			"message": _redirect_note(redirected, short),
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

	hub = transfer_hub(required=False)
	window = _trip_window(
		frappe.db.get_value("Bucket Request Trip", name, "route") if existing else route, trip_date
	)
	hit = _farm_taken(
		vehicle,
		trip_date,
		[(r["farm"], window) for r in rows if r["farm"] and r["farm"] != hub],
		exclude_trip=name if existing else None,
	)
	if hit:
		return _farm_taken_refusal(hit)

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
	if commit:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

	return {
		"status": "success",
		"name": doc.name,
		"redirected": redirected,
		"message": _redirect_note(redirected, short),
		"total_buckets": total_buckets,
		"total_stems": total_stems,
		"capacity_buckets": cap,
	}


def _live_planned(trip):
	"""Buckets planned on a trip, counting only orders whose pick list still exists. A
	deleted (re-allocated) pick list's rows are dropped the next time the trip is saved
	(drop_deleted_orders), so counting them left that much room empty on the first trip
	while the overflow opened a second one. Distinct buckets: a shared bucket planned
	under two of its orders on this trip counts once (_shared_overlap)."""
	rows = frappe.db.sql(
		"""SELECT o.order_pick_list AS opl, IFNULL(o.farm, '') AS farm, SUM(o.buckets) AS buckets
		FROM `tabBucket Request Trip Order` o
		JOIN `tabOrder Pick List` opl ON opl.name = o.order_pick_list
		WHERE o.parent = %s AND o.parenttype = 'Bucket Request Trip'
		GROUP BY o.order_pick_list, IFNULL(o.farm, '')""",
		trip,
		as_dict=True,
	)
	planned = {(r.opl, r.farm): int(r.buckets or 0) for r in rows if int(r.buckets or 0) > 0}
	return sum(planned.values()) - _shared_overlap(planned)


def _shared_overlap(planned):
	"""How many buckets of one trip's plan ({(opl, farm): buckets}) are counted twice: a
	shared bucket planned under an order beyond that order's own buckets (it moved to
	another lead) while its lead is planned on the same trip too."""
	if len(planned) < 2:
		return 0
	own, shared = {}, {}
	for e in _transfer_buckets(list({k[0] for k in planned})):
		if e["shelved"]:
			continue
		key = (e["opl"], e["farm"])
		if not e["rides"]:
			own[key] = own.get(key, 0) + 1
		elif (e["rides"], e["farm"]) in planned:
			shared.setdefault(key, []).append((e["rides"], e["farm"]))
	overlap, lead_left = 0, dict(planned)
	for key, leads in shared.items():
		extra = max(0, planned.get(key, 0) - own.get(key, 0))
		for lead in leads:
			if extra <= 0:
				break
			if lead_left.get(lead, 0) > 0:
				lead_left[lead] -= 1
				extra -= 1
				overlap += 1
	return overlap


def _open_run_room(vehicle, date, farm):
	"""[(run, room)] for each of the truck's runs on `date` that visit `farm` and can
	still collect there (no trip yet, or its trip hasn't left and hasn't left the farm),
	in driving order. room = capacity less what it takes (_trip_used)."""
	cap = _vehicle_capacity(vehicle)
	out = []
	for run in _day_runs(vehicle, date):
		if not _run_open(run) or farm not in run["stops"]:
			continue
		used = 0
		if run["trip"]:
			t = frappe.get_doc("Bucket Request Trip", run["trip"])
			if farm in _departed(t):
				continue
			used = _trip_used(t.name)
		out.append((run, max(0, cap - used) if cap else 10**9))
	return out


def _split_into_runs(vehicle, date, rows, skip_full=False, only=None):
	"""Spread plan rows over the truck's still-open runs that day, earliest first: a
	row goes to the first run that visits its farm (and hasn't left it) with room, and
	is split across runs when one run can't hold it all. Each run is a full truck.
	`only` = (route, run): rows go on that run -- or an earlier open run to the same
	farm that still has room, which fills first (a later run never gets buckets while
	an earlier one to that farm is short). Returns (slots with their rows, unplaced
	[(row, buckets, reason)])."""
	cap = _vehicle_capacity(vehicle)
	slots = []
	for run in _day_runs(vehicle, date):
		if not _run_open(run):
			continue
		used, departed = 0, []
		if run["trip"]:
			t = frappe.get_doc("Bucket Request Trip", run["trip"])
			used = _trip_used(t.name)
			departed = _departed(t)
		slots.append({**run, "room": (cap - used) if cap else 10**9, "departed": departed, "rows": []})
		if only and (run["route"], run["run"]) == only:
			break
	if only and not any((sl["route"], sl["run"]) == only for sl in slots):
		slots = []
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
		for k, ws in _bucket_trip_rows(
			{(b["bucket"] or "").upper() for b in all_buckets if b["bucket"]}
		).items()
		if any(w.trip != doc.name for w in ws)
	}
	groups = shared_buckets({b["bucket"] for b in all_buckets if b["open"] and b["bucket"]})
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
			# A shared bucket's rows on the orders riding with it go too.
			g = shared_group(groups, b["bucket"], b["farm"]) or {}
			riding = [n for o, ns in (g.get("rows") or {}).items() if o != b["opl"] for n in ns]
			for row_name in b["names"] + riding:
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
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %(b)s
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1
		       OR pli.shelved = 1 OR IFNULL(pli.not_found, 0) = 1
		       OR EXISTS (SELECT 1 FROM `tabBucket Request Trip Bucket` tb
		                  WHERE tb.order_pick_list = pli.parent AND tb.bucket = pli.bucket))
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
		remote_shelf = (
			times.get("remote_shelf") or allocated[2] or (r.shelf if not int(r.shelved or 0) else "")
		)
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
			  AND tb.bucket = %(b)s
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
					"detail": "At {0}{1}".format(
						hub or "the packhouse", " · box {0}".format(r.box_id) if r.box_id else ""
					),
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
			at = (
				(log[0].removed_on, log[0].modified_by, log[0].shelf)
				if log
				else (times.get("issued") or ("", "", None))
			)
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
				fields=[
					"stage",
					"outcome",
					"event_time",
					"user",
					"farm",
					"shelf",
					"trip",
					"vehicle",
					"details",
				],
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
			"Awaiting transfer",
			"Not found at farm",
			"Left remote shelf",
			"On trolley",
			"Load refused",
			"Loaded on truck",
			"Left the farm",
			"In transit",
			"Arrived at packhouse",
			"Off the truck",
			"Shelving farm corrected",
			"Shelving refused",
			"Shelved at remote farm",
			"Removed from wrong shelf",
			"Shelved at sales farm",
			"Stock moved",
			"Ready for packing",
			"Issued",
		]
		events.sort(
			key=lambda e: (order.index(e["stage"]) if e["stage"] in order else 99, e["datetime"] or "9999")
		)
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
		WHERE se.custom_bucket_id = %(b)s AND se.docstatus = 1
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
		WHERE se.custom_bucket_id = %(b)s AND se.docstatus = 1
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
			WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %(b)s
			  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1)
			  AND IFNULL(pli.in_transit, 0) = 0 AND IFNULL(pli.shelved, 0) = 0 AND NOT """
			+ PACKED_SQL,
			{"b": row.bucket},
			as_dict=True,
		)
	else:
		waiting = [frappe._dict(name=row.name, opl=row.opl, farm=row.farm)]
	# A shared bucket was planned once, for its lead order: only that plan expects one fewer.
	shared = shared_group(shared_buckets({row.bucket}), row.bucket, row.farm) if row.bucket else None
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
		if shared and opl in shared["opls"] and opl != shared["lead"]:
			continue
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
	# complete: the farm app's send button -- only once every bucket planned here is on.
	if cint(fd.get("complete")) and not _stop_complete(doc, farm):
		frappe.response["message"] = {
			"status": "error",
			"reason": "incomplete",
			"loaded": loaded,
			"planned": planned,
			"message": "{0} of {1} buckets are on {2} — load the rest or mark them not found.".format(
				loaded, planned, doc.vehicle
			),
		}
		return
	# Closing the last stop sends the truck to the hub: under its minimum load only with
	# a reason (the same reason covers a short stop too). An earlier stop just moves on.
	short_load = None if remaining else below_minimum(doc)
	if short_load and not reason:
		frappe.response["message"] = below_minimum_refusal(doc, short_load)
		return
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
		_advance_stops(doc, hold=False)
		remaining = [f for f in stops if f not in _departed(doc)]
	else:
		_dispatch_last_stop(doc, "truck leaving {0}".format(farm), at=now)
	doc.add_comment(
		"Info",
		"Stop {0} closed by {1}: {2} of {3} planned buckets loaded{4}{5}".format(
			farm,
			frappe.session.user,
			loaded,
			planned,
			" — reason: {0}".format(frappe.utils.escape_html(reason)) if reason else "",
			"; dispatched" if doc.status == "Dispatched" else "",
		),
	)
	if short_load:
		doc.add_comment("Info", below_minimum_note(short_load, reason))
	doc.save(ignore_permissions=True)
	# Traceability: each bucket of this farm on the truck left the farm now.
	for b in doc.get("trip_buckets") or []:
		if (b.farm or "") == farm:
			log_transfer_event(
				b.bucket,
				"Left the farm",
				opl=b.order_pick_list,
				farm=farm,
				trip=name,
				vehicle=doc.vehicle,
				details="Truck leaving {0}{1}".format(
					farm,
					" — below minimum load ({0} of {1}, min {2}): {3}".format(
						short_load["loaded"], short_load["capacity"], short_load["needed"], reason
					)
					if short_load
					else " — short: {0}".format(reason)
					if reason
					else "",
				),
			)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": name,
		"trip_status": doc.status,
		"heading_to": doc.heading_to,
		"loaded": loaded,
		"left_behind": max(0, planned - loaded),
		"below_minimum": 1 if short_load else 0,
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
		keep.loaded_buckets = _trip_loaded_count(keep)
		if rest:
			keep.add_comment(
				"Info",
				"Merged {0} into this trip (same truck, same trip number)".format(
					", ".join(d.name for d in rest)
				),
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
	if doc.status in ("Dispatched", "Received"):
		# Loading its last stop already sent it (_advance_stops): nothing to do.
		frappe.response["message"] = {
			"status": "success",
			"already": 1,
			"name": name,
			"trip_status": doc.status,
			"message": "Already dispatched." if doc.status == "Dispatched" else "Already received.",
		}
		return
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
	# Under the truck's minimum load it goes only with a reason, recorded on the trip.
	reason = (fd.get("reason") or "").strip()[:500]
	short_load = below_minimum(doc)
	if short_load and not reason:
		frappe.response["message"] = below_minimum_refusal(doc, short_load)
		return
	if _trip_has_loads(doc):
		# Loaded at the farms; they go in transit now, with the truck.
		_put_loads_in_transit(doc)
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
	if short_load:
		doc.add_comment("Info", below_minimum_note(short_load, reason, hub=doc.heading_to))
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": name,
		"trip_status": "Dispatched",
		"buckets_in_transit": marked,
		"short": short,
		"below_minimum": 1 if short_load else 0,
	}


def _put_loads_in_transit(doc):
	"""Dispatch: every bucket loaded on this trip's truck (its trip buckets, still on the
	truck) is in transit now -- for every order a shared bucket fills (shared_buckets),
	not only the one it is recorded for."""
	loads = [b for b in doc.get("trip_buckets") or [] if not (b.shelved or b.off_truck or not b.bucket)]
	groups = shared_buckets({b.bucket for b in loads})
	for b in loads:
		g = shared_group(groups, b.bucket, b.farm)
		opls = {b.order_pick_list} | set((g or {}).get("opls") or [])
		frappe.db.sql(
			"""UPDATE `tabPick List Item` SET in_transit = 1, transit_truck = %(v)s
			WHERE parenttype = 'Order Pick List' AND parent IN %(opls)s AND UPPER(bucket) = %(b)s
			  AND IFNULL(shelved, 0) = 0 AND IFNULL(issued, 0) = 0 AND IFNULL(not_found, 0) = 0""",
			{"v": doc.vehicle, "opls": tuple(opls), "b": (b.bucket or "").upper()},
		)


def trip_is_full(doc):
	"""A trip may leave the hub only once its plan fills the truck."""
	cap = _trip_capacity(doc)
	return cap > 0 and int(doc.total_buckets or 0) >= cap


def request_trip(doc):
	"""The scheduler asks for the truck to go to the trip's farms (Draft -> Requested).
	Returns None, or why it can't."""
	if doc.status != "Draft":
		return "Trip is already {0}.".format(doc.status)
	if str(doc.trip_date) < str(frappe.utils.today()):
		return "Trip was planned for an earlier day."
	doc.status = "Requested"
	doc.requested_by = frappe.session.user
	doc.requested_at = frappe.utils.now()
	# A new request: the last rejection is answered.
	doc.rejected_by, doc.rejected_at, doc.reject_reason = None, None, ""

	# A person asked for it: automatic scheduling no longer re-plans it.
	doc.auto_planned = 0
	doc.save(ignore_permissions=True)
	doc.add_comment(
		"Info",
		"Truck requested for {0}".format(
			", ".join(sorted({o.farm for o in doc.orders if o.farm})) or "the farms"
		),
	)
	# Production Settings > Release Trips Without Request and Confirmation: the request
	# releases the truck at once, no confirmation in the app.
	if frappe.utils.cint(
		frappe.get_cached_doc("Production Settings").get("release_trips_without_confirmation")
	):
		return release_trip(doc, how="without confirmation")
	return None


def reject_trip(doc, reason):
	"""Turn down a truck request (Requested -> Draft) with the reason, so the scheduler
	sees why and can fix the trip and request it again. Returns None, or why not."""
	reason = (reason or "").strip()
	if doc.status != "Requested":
		return "Trip is {0}, not requested.".format(doc.status)
	if not reason:
		return "Give a reason for rejecting the request."
	doc.status = "Draft"
	doc.rejected_by = frappe.session.user
	doc.rejected_at = frappe.utils.now()
	doc.reject_reason = reason
	doc.save(ignore_permissions=True)
	doc.add_comment("Info", "Truck request rejected: {0}".format(reason))
	return None


def release_trip(doc, how="manual"):
	"""Confirm the truck has left the hub for the trip's farms (Requested -> Scheduled).
	A Draft is requested on the way, by whoever releases it: automatic scheduling, and
	the packhouse app builds (1.1.8) that release a planned trip directly. Returns None,
	or why it can't go."""
	if doc.status not in ("Draft", "Requested"):
		return "Trip is already {0}.".format(doc.status)
	if not doc.vehicle:
		return "Trip has no truck."
	if str(doc.trip_date) < str(frappe.utils.today()):
		return "Trip was planned for an earlier day."
	cap = _trip_capacity(doc)
	out = _vehicle_on_road(doc.vehicle, exclude_trip=doc.name) or frappe.db.get_value(
		"Bucket Request Trip",
		{"vehicle": doc.vehicle, "status": "Scheduled", "name": ["!=", doc.name]},
		"name",
	)
	if out:
		return "{0} is still out on {1}.".format(doc.vehicle, out)
	now = frappe.utils.now()
	if doc.status == "Draft":
		# The request step, done by the release: the last rejection is answered.
		doc.requested_by, doc.requested_at = frappe.session.user, now
		doc.rejected_by, doc.rejected_at, doc.reject_reason = None, None, ""
	doc.status = "Scheduled"
	doc.released_at = now
	doc.released_by = frappe.session.user
	# Its truck has left: automatic scheduling no longer re-plans it.
	doc.auto_planned = 0
	doc.save(ignore_permissions=True)
	doc.add_comment(
		"Info",
		"Released to {0} ({1}), {2} / {3} buckets".format(
			", ".join(sorted({o.farm for o in doc.orders if o.farm})) or "the farms",
			how,
			int(doc.total_buckets or 0),
			cap,
		),
	)
	return None


def auto_dispatch_enabled():
	"""Full trips request and release themselves only under automatic scheduling;
	planned by hand, every trip goes through Request truck -> Confirm."""
	ps = frappe.get_cached_doc("Production Settings")
	return bool(
		frappe.utils.cint(ps.get("auto_dispatch_full_trips"))
		and frappe.utils.cint(ps.get("auto_remote_transfer_scheduling"))
	)


def dispatch_full_trips(names=None):
	"""Production Settings > Dispatch Full Trips Automatically: every full Draft trip
	of today (or just `names`) is requested and released. One that isn't full waits
	for a person to request the truck and confirm its release. Returns the trips released."""
	if not auto_dispatch_enabled():
		return []
	filters = {"status": "Draft", "trip_date": frappe.utils.today()}
	if names is not None:
		if not names:
			return []
		filters["name"] = ["in", list(names)]
	out = []
	for name in frappe.get_all(
		"Bucket Request Trip", filters=filters, order_by="run asc, creation asc", pluck="name"
	):
		doc = frappe.get_doc("Bucket Request Trip", name)
		if trip_is_full(doc) and not release_trip(doc, how="automatic"):
			out.append(name)
	if out:
		_rank_after_dispatch()
	return out


def _rank_after_dispatch():
	"""A truck left: its farm's orders take the next block of each team's sequence."""
	from upande_packhouse.api.remote_transfer.scheduler import rank_schedules

	rank_schedules()


@frappe.whitelist(methods=["POST"])
def requestBucketTrip():
	# The dashboard's Request truck: ask for the truck to go to the trip's farms.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	why = request_trip(frappe.get_doc("Bucket Request Trip", name))
	if why:
		frappe.response["message"] = {"status": "error", "message": why}
		return
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	status = frappe.db.get_value("Bucket Request Trip", name, "status")
	if status == "Scheduled":
		_rank_after_dispatch()
	frappe.response["message"] = {"status": "success", "name": name, "trip_status": status}


@frappe.whitelist(methods=["POST"])
def rejectBucketTrip():
	# The app's Reject on a truck request: back to the scheduler with the reason.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	why = reject_trip(frappe.get_doc("Bucket Request Trip", name), frappe.form_dict.get("reason"))
	if why:
		frappe.response["message"] = {"status": "error", "message": why}
		return
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name, "trip_status": "Draft"}


@frappe.whitelist(methods=["POST"])
def releaseBucketTrip():
	# The dashboard's Confirm release: the requested truck has left the hub for its farms.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	why = release_trip(frappe.get_doc("Bucket Request Trip", name))
	if why:
		frappe.response["message"] = {"status": "error", "message": why}
		return
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	_rank_after_dispatch()
	frappe.response["message"] = {"status": "success", "name": name, "trip_status": "Scheduled"}


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
	now = frappe.utils.now()
	values = {"status": "Received", "received_at": now}
	# Ending the trip says the truck is back: it arrived at the hub, if nothing said so yet.
	if not doc.get("arrived_at"):
		values["arrived_at"] = now
	frappe.db.set_value("Bucket Request Trip", name, values)
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
			"""SELECT parent, UPPER(bucket) AS bucket,
			       MAX(GREATEST(IFNULL(shelved, 0), IFNULL(custom_ready_for_packing, 0), IFNULL(issued, 0))) AS shelved,
			       MAX(GREATEST(IFNULL(in_transit, 0), IFNULL(loaded_in_trolley, 0))) AS in_transit
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
	# One entry per physical bucket: a bucket recorded for two orders is one on the truck.
	out, seen = [], {}
	for r in rows:
		st = states.get((r.order_pick_list, (r.bucket or "").upper()))
		shelved = bool(r.shelved or (st and int(st.shelved)))
		entry = {
			"bucket": r.bucket,
			"opl": r.order_pick_list,
			"farm": r.farm or "",
			"shelved": shelved,
			# Left the truck without being shelved (unloaded / swapped): not waited on.
			"off_truck": not shelved and not (st and int(st.in_transit)),
		}
		key = (r.bucket or "").upper() or r.name
		had = seen.get(key)
		if had is None:
			seen[key] = entry
			out.append(entry)
		else:
			had["shelved"] = had["shelved"] or entry["shelved"]
			had["off_truck"] = not had["shelved"] and had["off_truck"] and entry["off_truck"]
	return out


def _mark_arrived(doc, hub, how):
	"""Stamp a dispatched trip as arrived at the hub, and log every bucket still on it."""
	# Only these two fields: a full save collides with a shelving scan saving the same
	# trip (TimestampMismatchError) and the arrival is rolled back.
	doc.db_set({"arrived_at": frappe.utils.now(), "arrived_by": frappe.session.user})
	# Traceability: every bucket still on this truck reached the packhouse now.
	for b in doc.get("trip_buckets") or []:
		if not b.get("off_truck") and not b.get("shelved"):
			log_transfer_event(
				b.bucket,
				"Arrived at packhouse",
				opl=b.order_pick_list,
				farm=b.farm,
				trip=doc.name,
				vehicle=doc.vehicle,
				details="Truck arrived at {0}".format(hub or "the packhouse"),
			)
	doc.add_comment("Info", "Truck {0} arrived at {1} — {2}".format(doc.vehicle, hub, how))


def ensure_arrived(doc, hub=None):
	"""Any bucket a dispatched trip carried shelved at the hub means the truck got there:
	stamp it arrived (once). Returns True when it stamped now."""
	if doc.get("arrived_at") or doc.status != "Dispatched" or not doc.get("trip_buckets"):
		return False
	first = next((b for b in _trip_shelf_state(doc) if b["shelved"]), None)
	if not first:
		return False
	_mark_arrived(
		doc,
		hub or transfer_hub(required=False) or "the packhouse",
		"bucket {0} shelved there".format(first["bucket"]),
	)
	return True


def auto_arrive_for_bucket(bucket):
	"""A bucket was shelved at the hub. Its dispatched trip, at once (not at the next
	5-minute sweep):
	  * the first bucket shelved means the truck has arrived — stamped then, so nobody
	    has to press "arrived";
	  * the bucket is ticked shelved on the trip's bucket list, so the trip, the
	    dashboard and the farm app count it straight away; the trip ends once every
	    bucket it carried is shelved (or left the truck).
	A trip not dispatched yet that has the bucket LOADED on it (held at its last stop,
	or never dispatched at the farm) is dispatched and arrived now: the bucket came on
	its truck (_settle_held_trips). One that only planned it is left alone -- the bucket
	may have been at the hub already."""
	bucket = (bucket or "").strip().upper()
	if not bucket:
		return
	try:
		_settle_held_trips(bucket)
		trips = frappe.db.sql(
			"""SELECT DISTINCT t.name FROM `tabBucket Request Trip Bucket` tb
			JOIN `tabBucket Request Trip` t ON t.name = tb.parent
			WHERE tb.parenttype = 'Bucket Request Trip' AND tb.bucket = %s
			  AND t.status = 'Dispatched' AND t.trip_date >= %s
			  AND IFNULL(tb.off_truck, 0) = 0 AND IFNULL(tb.shelved, 0) = 0""",
			(bucket, frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)),
			pluck=True,
		)
		if not trips:
			return
		hub = transfer_hub(required=False) or "the packhouse"
		for name in trips:
			doc = frappe.get_doc("Bucket Request Trip", name)
			if not doc.get("arrived_at"):
				_mark_arrived(doc, hub, "first bucket ({0}) shelved there".format(bucket))
				doc.reload()
			ended = False
			for attempt in range(3):
				try:
					ended = _end_trip_if_shelved(doc)
					break
				except frappe.TimestampMismatchError:
					# Another scan saved the trip a moment ago: tick it off on the fresh copy.
					# Scans in quick succession keep clashing -- end_shelved_trips catches up.
					if attempt == 2:
						break
					doc.reload()
			if ended:
				carry_over_to_next_run(name)
				doc.add_comment("Info", "Trip completed at {0}: every bucket shelved".format(hub))
	except Exception:
		# Arrival is a convenience on top of shelving; never let it fail the shelve.
		frappe.log_error(title="Auto trip arrival failed", message=frappe.get_traceback())


@frappe.whitelist(methods=["POST"])
def tripArrival(name: str | None = None, farm: str | None = None, action: str = "status"):
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
		_mark_arrived(doc, hub, "confirmed by {0}".format(frappe.session.user))
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

	if ensure_arrived(doc, hub):
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	# The buckets that went on it, as every surface counts them (trip_numbers): one
	# bucket, one trip; a deleted pick list's, and one taken off the truck, are not.
	n = trip_numbers([name], ids=True).get(name) or {}
	buckets = [
		{"bucket": b, "opl": o["opl"], "farm": o["farm"], "shelved": st == "shelved", "off_truck": False}
		for o in n.get("orders") or []
		if o["counts"]
		for b, st in sorted((o.get("ids") or {}).items())
		if st in ("loaded", "transit", "shelved")
	]
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
def getFarmShelvedBuckets(farm: str | None = None, days: int = 3, delivery_date: str | None = None):
	"""Farm app "Shelved": every bucket this farm sent on a dispatched / received trip,
	per trip, and whether it is shelved at the hub yet (with the shelf and when) — so the
	remote QC sees what reached the sales farm.

	`delivery_date` (YYYY-MM-DD) keeps only buckets of orders delivering that day;
	without it, orders delivering today or later -- the ones the app lists. Past days'
	trips are not this farm's current work."""
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
		day_opls = set(
			frappe.db.sql_list(
				"""SELECT opl.name FROM `tabOrder Pick List` opl
				JOIN `tabSales Order` so ON so.name = opl.sales_order
				WHERE so.delivery_date >= %s AND opl.docstatus < 2""",
				getdate(frappe.utils.today()),
			)
		)
		if not day_opls:
			return {"status": "success", "farm": farm, "hub": hub, "trips": []}
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
		# What each bucket holds for its order: variety, stem length, stems, and the
		# variety's Variety Tree colour -- the farm app lists the trip by variety.
		held = {}
		for r in frappe.db.sql(
			"""SELECT pli.parent AS opl, UPPER(pli.bucket) AS bucket, pli.item_code, pli.stem_length,
			       SUM(pli.stock_qty) AS stems, c.color AS color
			FROM `tabPick List Item` pli
			LEFT JOIN `tabItem` i ON i.name = pli.item_code
			LEFT JOIN `tabColor` c ON c.name = i.custom_color
			WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s AND pli.bucket IN %(ids)s
			GROUP BY pli.parent, UPPER(pli.bucket), pli.item_code, pli.stem_length, c.color""",
			{"opls": tuple({b["opl"] for b in mine}), "ids": tuple(ids)},
			as_dict=True,
		):
			held.setdefault((r.opl, r.bucket), r)
		orders = {o.order_pick_list: o.order_name or o.order_pick_list for o in doc.orders}
		customers = {o.order_pick_list: o.customer or "" for o in doc.orders}
		buckets = []
		for b in mine:
			key = (b["bucket"] or "").upper()
			shelf = on_shelf.get(key)
			at = (stamped.get(key) or (shelf.at if shelf else "")) if b["shelved"] else ""
			h = held.get((b["opl"], key)) or {}
			buckets.append(
				{
					"bucket": b["bucket"],
					"opl": b["opl"],
					"variety": h.get("item_code") or "",
					"stem_length": h.get("stem_length") or "",
					"stems": flt(h.get("stems")),
					"variety_color": h.get("color") or "",
					"order_name": orders.get(b["opl"]) or b["opl"],
					"customer": customers.get(b["opl"]) or "",
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

	bench --site kaitet execute upande_packhouse.api.remote_transfer.transfer_scheduling.repair_remote_source_warehouse \\
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
			fixed["pick_rows"].append(
				{"row": r.name, "opl": r.parent, "bucket": r.bucket, "from": r.source_warehouse, "to": wh}
			)
			if not cint(dry_run):
				frappe.db.set_value("Pick List Item", r.name, "source_warehouse", wh, update_modified=False)
	for r in shelf_items:
		wh = own_wh(r.farm)
		# Only where the ledger agrees the stems are at the farm: a row whose stems really
		# moved (e.g. shelved at Kapkolia) must keep pointing at where they are.
		if wh and wh != r.warehouse and stock_movement.bucket_balance(r.bucket_id, r.variety, wh) > 0:
			fixed["shelf_items"].append(
				{"row": r.name, "shelf": r.parent, "bucket": r.bucket_id, "from": r.warehouse, "to": wh}
			)
			if not cint(dry_run):
				frappe.db.set_value("Shelf Item", r.name, "warehouse", wh, update_modified=False)
	if not cint(dry_run):
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- repair run: keep what it fixed
	return {
		"dry_run": bool(cint(dry_run)),
		"pick_row_count": len(fixed["pick_rows"]),
		"shelf_item_count": len(fixed["shelf_items"]),
		**fixed,
	}


def _farm_rows(doc, farm):
	return [o for o in doc.orders if (o.farm or "") == farm]


@frappe.whitelist(methods=["POST"])
def getFarmCompletedTrips(farm: str | None = None, days: int = 3):
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
	# The trips' numbers as every surface shows them (trip_numbers).
	numbers = trip_numbers(names)
	out = []
	for name in names:
		doc = frappe.get_doc("Bucket Request Trip", name)
		# Any bucket it carried shelved at the hub: the truck arrived (stamped once).
		if ensure_arrived(doc):
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
		shelved_any = any(b["shelved"] for b in _trip_shelf_state(doc))
		rows = _farm_rows(doc, farm)
		mine = {
			k: p for k, p in trip_order_numbers(numbers.get(name)).items() if k[1] == farm and p["counts"]
		}
		fn = ((numbers.get(name) or {}).get("farms") or {}).get(farm) or _blank_numbers()
		planned, loaded = fn["planned"], fn["on_truck"]
		# Per order: an order loaded over its plan must not hide another that went short.
		left = sum(max(0, p["planned"] - p["on_truck"]) for p in mine.values())
		carried = []
		if left:
			opls = [k[0] for k, p in mine.items() if p["planned"] > p["on_truck"]]
			carried = frappe.db.sql_list(
				"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
				JOIN `tabBucket Request Trip Order` o ON o.parent = t.name
				WHERE t.name != %(name)s AND t.status IN %(active)s AND o.farm = %(farm)s
				  AND o.order_pick_list IN %(opls)s""",
				{
					"name": name,
					"active": tuple(ACTIVE_TRIP_STATUSES),
					"farm": farm,
					"opls": tuple(opls) or ("",),
				},
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
				"arrived_at": str(doc.get("arrived_at") or ""),
				# At least one of its buckets is shelved at the hub: it has arrived.
				"arrived": 1 if (doc.get("arrived_at") or shelved_any) else 0,
				"planned": planned,
				"loaded": loaded,
				"shelved": fn["shelved"],
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
						"buckets": mine[(o.order_pick_list, farm)]["planned"],
						"loaded": mine[(o.order_pick_list, farm)]["on_truck"],
						"shelved": mine[(o.order_pick_list, farm)]["shelved"],
					}
					for o in {(o.order_pick_list, farm): o for o in rows}.values()
					if (o.order_pick_list, farm) in mine
				],
			}
		)
	return {"status": "success", "farm": farm, "trips": out}


@frappe.whitelist(methods=["POST"])
def reopenTripStop(name: str | None = None, farm: str | None = None):
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
		doc.add_comment(
			"Info", "Stop {0} reopened by {1} ({2} left to load)".format(farm, frappe.session.user, left)
		)
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
			msg = "The {0} bucket(s) left behind are already on another trip or no longer waiting.".format(
				left
			)
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

	bench --site kaitet execute upande_packhouse.api.remote_transfer.transfer_scheduling.return_early_arrivals \\
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
			  AND pli.bucket = %(b)s AND pli.item_code = %(item)s
			  AND pli.modified >= %(since)s""",
			{"b": si.bucket_id, "item": si.variety, "since": si.date_added},
			as_dict=True,
		)
		if any(cint(r.issued) or cint(r.loaded_in_trolley) or cint(r.in_transit) for r in rows):
			skipped.append(
				{
					"shelf_item": si.name,
					"bucket": si.bucket_id,
					"reason": "left the shelf (trolley/truck/issued)",
				}
			)
			continue
		plan = {
			"shelf_item": si.name,
			"shelf": si.parent,
			"bucket": si.bucket_id,
			"item": si.variety,
			"qty": qty,
		}
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
	return {
		"dry_run": bool(cint(dry_run)),
		"done": len(done),
		"skipped": len(skipped),
		"rows": done,
		"skips": skipped,
	}


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
				if cint(r.loaded_in_trolley)
				and cint(r.in_transit)
				and not cint(r.shelved)
				and not cint(r.issued)
				and (r.farm or "") == si.farm
			]
			if gone and str(si.date_added) < str(max(r.modified for r in gone)):
				why = "Transferred (Trolley/Truck)"
		elif si.farm == hub:
			mine = [r for r in rows if r.item_code == si.variety]
			if (
				mine
				and all(cint(r.issued) for r in mine)
				and str(si.date_added) < str(max(r.modified for r in mine))
			):
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
	for row in frappe.get_doc(
		sm.MAPPING_DT, frappe.db.get_value(sm.MAPPING_DT, {"business_unit": business_unit})
	).items:
		route = (
			sm.resolve_route(row.source_warehouse, business_unit, upto=sm.ARRIVAL_STAGE)
			if row.source_warehouse
			else []
		)
		if route:
			pairs.append((row.source_warehouse, route[-1]["to"]))
	fixed = []
	for farm_wh, arrival in pairs:
		bucket = sm._bucket_expr()  # line bucket where the site has it, else the entry's
		# nosemgrep: frappe-sql-format-injection -- `bucket` is a fixed column expression; values are bound
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
					fixed.append(
						{"bucket": ln.bucket, "item": ln.item_code, "cancel": d.name, "qty": flt(d.qty)}
					)
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


def hub_ready_orders(since, until):
	"""Orders ready to issue at the hub: every bucket still to issue (delivering in the
	selected days, `since`..`until`) is on a hub shelf. Issuing them first frees that many
	shelf spaces for the next transfers. Per order: team (its Packhouse Schedule's,
	else the OPL's), schedule #, delivery date and buckets on the hub's shelves."""
	hub = transfer_hub(required=False) or ""
	if not hub:
		return []
	rows = frappe.db.sql(
		"""SELECT opl.name AS opl, opl.order_name, IFNULL(opl.team, '') AS opl_team,
		       so.delivery_date, opl.docstatus,
		       COUNT(DISTINCT CASE WHEN IFNULL(pli.issued, 0) = 0 THEN UPPER(pli.bucket) END) AS open_b,
		       COUNT(DISTINCT CASE WHEN IFNULL(pli.issued, 0) = 0 AND hs.bucket IS NOT NULL
		                           THEN UPPER(pli.bucket) END) AS at_hub,
		       COUNT(DISTINCT CASE WHEN pli.issued = 1 THEN UPPER(pli.bucket) END) AS issued_b
		FROM `tabOrder Pick List` opl
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
		LEFT JOIN (
			SELECT DISTINCT UPPER(si.bucket_id) AS bucket FROM `tabShelf Item` si
			JOIN `tabShelf` s ON s.name = si.parent WHERE s.farm = %(hub)s
		) hs ON hs.bucket = UPPER(pli.bucket)
		WHERE opl.docstatus < 2 AND so.delivery_date BETWEEN %(since)s AND %(until)s
		  AND COALESCE(pli.bucket, '') != '' AND IFNULL(pli.not_found, 0) = 0
		GROUP BY opl.name, opl.order_name, opl.team, so.delivery_date, opl.docstatus
		HAVING open_b > 0 AND open_b = at_hub""",
		{"hub": hub, "since": since, "until": until},
		as_dict=True,
	)
	if not rows:
		return []
	# Its latest schedule wins (an order can sit on more than one day's schedule).
	sched = {
		r.order_pick_list: r
		for r in frappe.db.sql(
			"""SELECT pso.order_pick_list, ps.team, pso.sequence FROM `tabPackhouse Schedule Order` pso
			JOIN `tabPackhouse Schedule` ps ON ps.name = pso.parent
			WHERE pso.order_pick_list IN %(o)s ORDER BY ps.schedule_date ASC, ps.modified ASC""",
			{"o": tuple(r.opl for r in rows)},
			as_dict=True,
		)
	}
	out = []
	for r in rows:
		sc = sched.get(r.opl)
		out.append(
			{
				"opl": r.opl,
				"order_name": r.order_name or r.opl,
				"team": (sc.team if sc else "") or r.opl_team or "",
				"schedule": int(sc.sequence or 0) if sc else 0,
				"delivery_date": str(r.delivery_date),
				"buckets": int(r.at_hub or 0),
				"issued": int(r.issued_b or 0),
				"submitted": int(r.docstatus or 0) == 1,
			}
		)
	# Per team: its sequence first (#1, #2 …), the unscheduled after, oldest delivery first.
	out.sort(key=lambda x: (x["team"] or "~", x["schedule"] or 9999, x["delivery_date"], x["order_name"]))
	return out


def allow_shelving_in_transit():
	"""Production Settings > "Allow Shelving Buckets In Transit"."""
	return bool(
		frappe.utils.cint(frappe.get_cached_doc("Production Settings").get("allow_shelving_in_transit"))
	)


def return_to_farm(bucket_id, farm):
	"""With "Allow Shelving Buckets In Transit": a bucket flagged on a trolley / truck
	for the hub, shelved back at its remote `farm`, never left -- it goes back to
	waiting there: off the trolley / truck on its open orders (still awaiting the
	transfer) and off the load of the trips that recorded it. Returns the OPLs touched."""
	hub = transfer_hub(required=False) or ""
	if not bucket_id or not farm or (hub and farm.lower() == hub.lower()) or not allow_shelving_in_transit():
		return []
	rows = frappe.db.sql(
		"""SELECT pli.name, pli.parent FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %(b)s
		  AND (pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0
		  AND COALESCE(NULLIF(pli.farm, ''), SUBSTRING_INDEX(pli.source_warehouse, ' ', 1)) = %(f)s""",
		{"b": bucket_id, "f": farm},
		as_dict=True,
	)
	if not rows:
		return []
	# Back as before its trolley scan (as back_to_farm_shelf; the shelving just put it
	# on the shelf): off the trolley and truck too.
	values = {"loaded_in_trolley": 0, "in_transit": 0, "transit_truck": None, "awaiting_transfer": 1}
	if frappe.db.has_column("Pick List Item", "loaded_on_truck"):
		values["loaded_on_truck"] = 0
	if frappe.db.has_column("Pick List Item", "trolley_id"):
		values["trolley_id"] = None
	for r in rows:
		frappe.db.set_value("Pick List Item", r.name, values)
	key = bucket_id.upper()
	where = [w for w in _bucket_trip_rows([key]).get(key, []) if w.opl in {r.parent for r in rows}]
	if where:
		_drop_bucket_from_trips(key, where, "shelved back at {0}".format(farm))
	opls = sorted({r.parent for r in rows})
	log_transfer_event(
		bucket_id, "Back on farm shelf", farm=farm, opl=opls[0], details="Shelved again at {0}".format(farm)
	)
	return opls


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
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %(b)s
		  AND (pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0
		GROUP BY pli.parent LIMIT 1""",
		{"b": bucket_id},
		as_dict=True,
	)
	# "Allow Shelving Buckets In Transit": it goes back on the farm's shelf (return_to_farm).
	if moving and not allow_shelving_in_transit():
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
			WHERE se.custom_bucket_id = %(b)s AND se.docstatus = 1
			  AND se.stock_entry_type IN ('Receiving', 'Late Receipt')""",
			{"b": bucket_id},
		)[0][0]
		if received:
			left = frappe.db.sql(
				"""SELECT se.name FROM `tabStock Entry` se JOIN `tabStock Entry Detail` d ON d.parent = se.name
				WHERE se.custom_bucket_id = %(b)s AND se.docstatus = 1
				  AND se.stock_entry_type = %(t)s AND d.s_warehouse = %(wh)s
				  AND TIMESTAMP(se.posting_date, se.posting_time) >= %(r)s LIMIT 1""",
				{"b": bucket_id, "t": sm.TYPE_REMOTE_TRANSFER, "wh": farm_wh, "r": received},
			)
			if left:
				return "Bucket {0} was already transferred to {1} ({2}) — it can't go back on a {3} shelf.".format(
					bucket_id, hub or "the packhouse", left[0][0], farm
				)
	return None


def hub_shelving_block(bucket_id, farm):
	"""Why a bucket may NOT be shelved at the hub (`farm`), or None: an order is still
	waiting for it at a remote farm and it never went on a trolley or truck there, nor
	on a trip — it never left that farm, so a hub shelf is a mis-scan.

	Production Settings > "Allow Shelving Buckets Not Transferred" lifts this: the
	bucket is shelved at the hub and its transfer carries on from there."""
	hub = transfer_hub(required=False) or ""
	if not bucket_id or not farm or not hub or farm.lower() != hub.lower():
		return None
	if frappe.utils.cint(
		frappe.db.get_single_value("Production Settings", "allow_hub_shelving_without_transfer")
	):
		return None
	rows = frappe.db.sql(
		"""SELECT pli.parent, pli.source_warehouse,
		       MAX(GREATEST(IFNULL(pli.loaded_in_trolley, 0), IFNULL(pli.in_transit, 0))) AS moved
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %(b)s
		  AND pli.awaiting_transfer = 1
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0
		GROUP BY pli.parent, pli.source_warehouse""",
		{"b": bucket_id},
		as_dict=True,
	)
	if not rows or any(int(r.moved or 0) for r in rows):
		return None
	on_trip = frappe.db.sql(
		"""SELECT 1 FROM `tabBucket Request Trip Bucket` tb
		JOIN `tabBucket Request Trip` t ON t.name = tb.parent
		WHERE tb.parenttype = 'Bucket Request Trip' AND tb.bucket = %(b)s
		  AND tb.order_pick_list IN %(opls)s AND IFNULL(tb.off_truck, 0) = 0 LIMIT 1""",
		{"b": bucket_id, "opls": tuple(r.parent for r in rows)},
	)
	if on_trip:
		return None
	r = rows[0]
	src = (r.source_warehouse or "").split(" ", 1)[0] or "its farm"
	shelf = frappe.db.sql(
		"""SELECT si.parent FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		WHERE si.bucket_id = %(b)s AND IFNULL(s.farm, '') != %(hub)s LIMIT 1""",
		{"b": bucket_id, "hub": hub},
	)
	return "Bucket {0} was never transferred — it is still waiting at {1}{2} for {3}, not loaded on a truck. Load it there and send it to {4} first.".format(
		bucket_id, src, " (shelf {0})".format(shelf[0][0]) if shelf else "", r.parent, hub
	)


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
				"stock_entry": stock_entry
				if stock_entry and frappe.db.exists("Stock Entry", stock_entry)
				else None,
				"details": details or "",
			}
		).insert(ignore_permissions=True)
		if outcome == "Done":
			# The step's tick, time and user on the order's own rows -- in a savepoint, so
			# a failure here never takes the caller's own writes with it.
			from upande_packhouse.api.remote_transfer import transfer_steps

			frappe.db.savepoint("transfer_steps")
			try:
				transfer_steps.on_event(bucket, stage, opl=opl)
			except Exception:
				frappe.db.rollback(save_point="transfer_steps")
				raise
	except Exception:
		frappe.log_error(
			"Bucket transfer event not logged: {0} {1}".format(bucket, stage), frappe.get_traceback()
		)


def open_transfer_opls(bucket):
	"""Order Pick Lists (not cancelled) on which this bucket is in an open transfer."""
	if not bucket:
		return []
	return frappe.db.sql_list(
		"""SELECT DISTINCT pli.parent FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0""",
		bucket,
	)


def opl_delete_blockers(opl):
	"""Why this Order Pick List must not be deleted while its buckets move: rows loaded on
	a trolley or truck, in transit, or awaiting transfer at a farm, and active trips it
	is planned on. Deleting it then drops the request from the farm app and its trips
	for good. [] when it can go: a cancelled pick list, or one whose delivery date has
	passed (its rows are stale, as in shared_buckets) on no current trip."""
	head = frappe.db.sql(
		"""SELECT opl.docstatus, so.delivery_date FROM `tabOrder Pick List` opl
		LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order WHERE opl.name = %s""",
		opl,
	)
	if not head or int(head[0][0] or 0) == 2:
		return []
	today = frappe.utils.getdate(frappe.utils.today())
	current = head[0][1] is None or frappe.utils.getdate(head[0][1]) >= today
	out = []
	if current:
		out = _opl_moving_rows(opl)
	trips = frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip Order` o
		JOIN `tabBucket Request Trip` t ON t.name = o.parent
		WHERE o.order_pick_list = %(opl)s AND o.parenttype = 'Bucket Request Trip'
		  AND t.status IN ('Draft', 'Requested', 'Scheduled', 'Dispatched')
		  AND t.trip_date >= %(since)s
		  AND (IFNULL(o.buckets, 0) > 0 OR IFNULL(o.loaded_buckets, 0) > 0)""",
		{"opl": opl, "since": frappe.utils.add_days(today, -TRIP_LOOKBACK_DAYS)},
	)
	if trips:
		out.append(_("planned on trip(s) {0}").format(", ".join(trips)))
	return out


def _opl_moving_rows(opl):
	"""opl_delete_blockers' row counts: loaded, in transit, awaiting transfer."""
	loaded = "pli.loaded_in_trolley = 1"
	if frappe.db.has_column("Pick List Item", "loaded_on_truck"):
		loaded += " OR pli.loaded_on_truck = 1"
	row = frappe.db.sql(
		"SELECT SUM(CASE WHEN ("
		+ loaded
		+ """) AND IFNULL(pli.in_transit, 0) = 0 THEN 1 ELSE 0 END),
		     SUM(CASE WHEN pli.in_transit = 1 THEN 1 ELSE 0 END),
		     SUM(CASE WHEN pli.awaiting_transfer = 1 AND IFNULL(pli.in_transit, 0) = 0 AND NOT ("""
		+ loaded
		+ """) THEN 1 ELSE 0 END)
		FROM `tabPick List Item` pli
		WHERE pli.parent = %s AND pli.parenttype = 'Order Pick List'
		  AND IFNULL(pli.bucket, '') != '' AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0""",
		opl,
	)[0]
	out = []
	for n, what in zip(
		row,
		(
			_("{0} row(s) loaded on a truck"),
			_("{0} row(s) in transit"),
			_("{0} row(s) awaiting transfer at a farm"),
		),
		strict=True,
	):
		if int(n or 0):
			out.append(what.format(int(n)))
	return out


# Truck Routes moved to truck_routes.py. Imported last (it imports helpers from this
# module), and every old upande_packhouse.api.transfer_control.<name> of a Truck Routes
# function — the page's calls, auto_transfer, the mobile apps — still resolves here.
from upande_packhouse.api.remote_transfer import truck_routes


def __getattr__(name):
	try:
		return getattr(truck_routes, name)
	except AttributeError:
		raise AttributeError("module {0!r} has no attribute {1!r}".format(__name__, name)) from None
