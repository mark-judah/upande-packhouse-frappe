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

import frappe

FARM_EXPR = "SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1)"
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
		"Set <b>Remote Transfer Hub Farm</b> in Production Settings — the sales farm "
		"remote buckets are trucked to."
	)


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
		       pli.in_transit AS in_transit, pli.shelved AS shelved, pli.transit_truck AS truck
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
	"""(opl, farm) -> buckets claimed by TODAY's still-planned trips. Stale drafts
	from earlier days don't count — their truck never went, so the buckets are
	open again (and the dashboard flags the stale trip for cleanup)."""
	if not opl_names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT o.order_pick_list AS opl, o.farm AS farm, SUM(o.buckets) AS buckets
		FROM `tabBucket Request Trip Order` o
		JOIN `tabBucket Request Trip` t ON t.name = o.parent
		WHERE t.status IN %(active)s AND t.trip_date = %(today)s
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
		phase = ""
		if int(r.get("sh") or 0):
			phase = "shelved"
		elif int(r.get("tr") or 0):
			phase = "in_transit"
		elif int(r.get("ld") or 0):
			phase = "loaded"
		elif int(r.get("aw") or 0):
			phase = "awaiting"
		if phase:
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


def _trip_dict(doc, today):
	return {
		"name": doc.name,
		"vehicle": doc.vehicle,
		"trip_date": str(doc.trip_date),
		"status": doc.status,
		# A Draft/Scheduled trip from an earlier day never left: it claims nothing
		# any more and should be deleted or re-planned.
		"stale": 1 if (str(doc.trip_date) < str(today) and doc.status in ACTIVE_TRIP_STATUSES) else 0,
		"notes": doc.notes,
		"collection_order": doc.collection_order,
		"farm": doc.farm,
		"total_buckets": doc.total_buckets,
		"total_stems": doc.total_stems,
		"capacity_buckets": doc.capacity_buckets,
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
			}
			for o in doc.orders
		],
	}


def _route_farms(legs):
	# Parenthesised on purpose: `a | b - {hub}` binds as `a | (b - ...)` and
	# kept the hub (every route's first from_farm) in the list.
	return sorted(({l.from_farm for l in legs} | {l.to_farm for l in legs}) - {transfer_hub(), None, ""})


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
	return frappe.db.get_value("Bucket Request Trip", filters, "name", order_by="dispatched_at desc")


def _route_end(vehicle, date):
	"""Final destination of a truck's route that day (last leg's to_farm), else the hub."""
	name = "BLR-" + str(date) + "-" + str(vehicle)
	if frappe.db.exists("Bucket Logistics Route", name):
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


def auto_receive_trucks_for_bucket(bucket_id):
	"""Called after a bucket is shelved at the sales farm. Any transfer truck that carried
	it and now has nothing left in transit is back at its final destination: receive its
	trip so the truck becomes available to route and plan again. Returns the trips received."""
	trucks = frappe.db.sql_list(
		"""SELECT DISTINCT pli.transit_truck FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s AND """
		+ TRANSFER_TRUCK_OK,
		bucket_id,
	)
	received = []
	for truck in trucks:
		trip = _vehicle_on_road(truck)
		if not trip:
			continue
		left = frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabPick List Item`
			WHERE parenttype = 'Order Pick List' AND in_transit = 1 AND IFNULL(shelved, 0) = 0
			  AND transit_truck = %s""",
			truck,
		)[0][0]
		if not left:
			_receive_trip(trip)
			received.append(trip)
	return received


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
		  AND COALESCE(pli.shelved, 0) = 0
		""",
		{"f": from_date, "t": to_date},
		as_dict=True,
	)
	opl_names = [r["opl"] for r in opl_rows]

	# opl -> farm -> {open: variety -> {buckets, stems}, on_road}
	# Driven by the transfer FLAGS, not "farm != Kapkolia": a bucket at its own
	# sales farm (e.g. Karen for a Karen order) was never flagged and must not be
	# planned onto a truck.
	agg = {}
	for b in _transfer_buckets(opl_names):
		if not (b["open"] or b["on_road"]) or not b["farm"]:
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
			if total_b:
				unscheduled.append(
					{
						"opl": r["opl"],
						"order_name": r.get("order_name") or r["opl"],
						"customer": r.get("customer"),
						"delivery_date": str(r.get("delivery_date") or ""),
						"team": r.get("opl_team") or "",
						"reason": "not_scheduled" if r.get("opl_team") else "no_team",
						"buckets": total_b,
						"farms": sorted(f["farm"] for f in farms_out if f["buckets"]),
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
				"mixed": None,
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
			}
		)

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
	route_docs = frappe.get_all("Bucket Logistics Route", filters={"route_date": today}, pluck="name")
	routes_out = []
	for rn in route_docs:
		doc = frappe.get_doc("Bucket Logistics Route", rn)
		legs = [
			{"leg": l.leg, "from_farm": l.from_farm, "to_farm": l.to_farm, "distance_km": l.distance_km}
			for l in doc.legs
		]
		routes_out.append(
			{
				"name": doc.name,
				"vehicle": doc.vehicle,
				"total_km": doc.total_km,
				"auto_planned": int(doc.get("auto_planned") or 0),
				"legs": legs,
				"farms": _route_farms(doc.legs),
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
		"farm_list": frappe.get_all("Farm", pluck="name", order_by="name"),
		"routes": routes_out,
		"packhouse": transfer_hub(),
		"auto_planning": _auto_planning_status(),
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


def _save_trip(name, vehicle, trip_date, status, notes, collection_order, farm, rows, auto_planned=0):
	"""Validate and upsert a Bucket Request Trip; returns the response message.

	Capacity is recomputed here and is authoritative — over-capacity refuses the whole
	save, no partial write. Also refuses to claim more buckets of an (order, farm) than
	are still open once every other planned trip today is counted. That is what stops a
	double-click (or a second planner, or the automatic scheduler) from putting the
	same buckets on two trucks."""
	if not vehicle:
		return {"status": "error", "message": "A vehicle is required."}
	busy = _vehicle_on_road(vehicle)
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

	existing = name and frappe.db.exists("Bucket Request Trip", name)
	if existing:
		current = frappe.db.get_value("Bucket Request Trip", name, "status")
		if current not in ACTIVE_TRIP_STATUSES:
			return {
				"status": "error",
				"message": "Trip {0} is already {1} — it can't be edited.".format(name, current),
			}

	if not rows:
		return {"status": "error", "message": "The trip has no buckets on it."}

	# Claim check — only meaningful for a trip that runs today (claims are per day).
	if str(trip_date) == str(frappe.utils.today()):
		opls = list({r["order_pick_list"] for r in rows})
		open_counts = _open_counts(opls)
		claims = _trip_claims(opls, exclude_trip=name if existing else None)
		wanted = {}
		for r in rows:
			key = (r["order_pick_list"], r["farm"])
			wanted[key] = wanted.get(key, 0) + r["buckets"]
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
	fd = frappe.form_dict
	frappe.response["message"] = _save_route(
		date=fd.get("date") or frappe.utils.today(),
		vehicle=fd.get("vehicle"),
		leg_names=(fd.get("legs") or "").split("|~|"),
	)


def _save_route(date, vehicle, leg_names, auto_planned=0):
	"""Validate and upsert a truck's Bucket Logistics Route; returns the response message.
	A route saved by hand (auto_planned=0) is never re-routed by automatic scheduling."""
	if not vehicle:
		return {"status": "error", "message": "A vehicle is required."}
	busy = _vehicle_on_road(vehicle)
	if busy and str(date) == str(frappe.utils.today()):
		return {
			"status": "error",
			"reason": "on_road",
			"message": _on_road_message(vehicle, busy, "re-routed"),
		}

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
		return {
			"status": "error",
			"message": "Unknown road leg(s): {0}. Refresh the page and try again.".format(", ".join(unknown)),
		}

	hub = transfer_hub()
	path, here = [], hub
	for ln in leg_names:
		d = by_name[ln]
		if d.from_farm == here:
			start, end = d.from_farm, d.to_farm
		elif d.to_farm == here:
			start, end = d.to_farm, d.from_farm
		else:
			return {
				"status": "error",
				"message": "Route is broken at {0}: leg {1} ({2}–{3}) doesn't start there.".format(
					here, ln, d.from_farm, d.to_farm
				),
			}
		path.append({"leg": d.name, "from_farm": start, "to_farm": end, "distance_km": d.distance_km})
		here = end
	if path and here != hub:
		return {
			"status": "error",
			"message": "Route ends at {0} — it must return to {1}.".format(here, hub),
		}

	name = "BLR-" + str(date) + "-" + vehicle
	if frappe.db.exists("Bucket Logistics Route", name):
		doc = frappe.get_doc("Bucket Logistics Route", name)
	else:
		doc = frappe.new_doc("Bucket Logistics Route")
		doc.route_date = date
		doc.vehicle = vehicle

	doc.set("legs", [])
	total_km = 0.0
	for leg in path:
		doc.append("legs", leg)
		total_km += float(leg["distance_km"] or 0)
	doc.total_km = total_km
	doc.auto_planned = 1 if auto_planned else 0
	doc.save(ignore_permissions=True)
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
	out = []
	for name in frappe.get_all(
		"Bucket Logistics Route", filters={"route_date": date}, pluck="name", order_by="vehicle"
	):
		doc = frappe.get_doc("Bucket Logistics Route", name)
		legs = [
			{"leg": l.leg, "from_farm": l.from_farm, "to_farm": l.to_farm, "distance_km": l.distance_km}
			for l in doc.legs
		]
		stops = [legs[0]["from_farm"]] + [l["to_farm"] for l in legs] if legs else []
		out.append(
			{
				"name": doc.name,
				"vehicle": doc.vehicle,
				"route_date": str(doc.route_date),
				"total_km": doc.total_km,
				"auto_planned": int(doc.get("auto_planned") or 0),
				"legs": legs,
				"stops": stops,
				"farms": _route_farms(doc.legs),
				"modified": str(doc.modified),
			}
		)
	frappe.response["message"] = {"status": "success", "date": str(date), "routes": out}


@frappe.whitelist(methods=["POST"])
def deleteBucketLogisticsRoute():
	# Remove a truck's route for a day. The truck is then not used by ⚡ Distribute
	# for that day until it is routed again; trips already planned are untouched.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Bucket Logistics Route", name):
		frappe.response["message"] = {"status": "error", "message": "Route not found."}
		return
	frappe.delete_doc("Bucket Logistics Route", name, ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


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
	pool = {}
	for b in _transfer_buckets(opls):
		if b["open"]:
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
def dispatchBucketTrip():
	# Dispatch = the truck leaves. Besides the trip status, the buckets it carries are
	# now flagged in transit — previously nothing ever set in_transit, so every
	# "in transit" count on Bucket Logistics / Transfer Scheduling stayed at zero.
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
	marked, short = _mark_trip_in_transit(doc)
	frappe.db.set_value(
		"Bucket Request Trip", name, {"status": "Dispatched", "dispatched_at": frappe.utils.now()}
	)
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
	}
