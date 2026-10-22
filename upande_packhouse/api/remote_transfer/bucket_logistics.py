# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `bucket-logistics` page API — ported verbatim from kaitet-group v15 LIVE
# Server Scripts (these were never migrated to the v16 bench). Bodies keep
# frappe.form_dict / frappe.response exactly as the live scripts set them.

import frappe

from upande_packhouse.api.remote_transfer.transfer_scheduling import (
	ACTIVE_TRIP_STATUSES,
	TRANSFER_TRUCK_OK,
	TRIP_LOOKBACK_DAYS,
	_same_farm,
	_schedule_map,
	_trip_dict,
	shared_buckets,
	trip_bucket_shares,
	trip_numbers,
	trip_order_numbers,
)

#: The one section for orders without a trip: a scheduled order always has one.
NOT_SCHEDULED = "Not scheduled"

# source-farm expression (reused in SELECT + WHERE)
FARM_EXPR = "COALESCE(NULLIF(pli.farm, ''), NULLIF(SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1), ''))"
# Ready / issued keep a remote bucket in view after Kapkolia: issuing can clear
# its shelved flag, and an order that reached Kapkolia must not drop off the page.
TRANSFER = (
	"(pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1 OR pli.shelved = 1"
	" OR pli.not_found = 1 OR pli.custom_ready_for_packing = 1 OR pli.issued = 1)"
)

# Each stage counts a bucket once it has PASSED it, as getBucketLogisticsDetail
# ticks it: shelving clears awaiting / trolley / in transit on the row, but the
# bucket did go through them -- counting only the flags a row still carries made a
# shelved order read "Awaiting 0/2 · Trolley 0/2 · In Transit 0/2 · Shelved 2/2",
# as if it had skipped the truck.
_ready = "IFNULL(pli.custom_ready_for_packing, 0) = 1 OR IFNULL(pli.issued, 0) = 1"
_shelved = "IFNULL(pli.shelved, 0) = 1 OR " + _ready
_transit = "IFNULL(pli.in_transit, 0) = 1 OR " + _shelved
_truck = "IFNULL(pli.loaded_on_truck, 0) = 1 OR " + _transit
_trolley = "IFNULL(pli.loaded_in_trolley, 0) = 1 OR IFNULL(pli.trolley_id, '') != '' OR " + _truck
REACHED_READY = "(" + _ready + ")"
REACHED_SHELVED = "(" + _shelved + ")"
REACHED_TRANSIT = "(" + _transit + ")"
REACHED_TRUCK = "(" + _truck + ")"
REACHED_TROLLEY = "(" + _trolley + ")"
REACHED_AWAITING = "(IFNULL(pli.awaiting_transfer, 0) = 1 OR " + _trolley + ")"

# A bucket's identity on the pick list (case-insensitive; a row without a bucket counts alone).
BKT = "COALESCE(NULLIF(UPPER(pli.bucket), ''), pli.name)"


def no_trip_section(order):
	"""Where an order with no trip is listed: "Not scheduled" when it is on no team's
	schedule, else nowhere -- every scheduled order is planned onto a trip (Distribute
	and automatic scheduling place all of them)."""
	return None if order.get("scheduled") else NOT_SCHEDULED


@frappe.whitelist()
def getBucketLogistics():
	# Bucket Logistics — orders with buckets being transferred, for a delivery date.
	# A bucket is "being transferred" iff it was ever flagged for transfer (awaiting,
	# loaded, in transit) or has been shelved. Dispatch keeps awaiting_transfer=1 while
	# it sets in_transit, but in_transit/loaded are included so nothing drops out.
	# Source farm = the pick row's farm (the shelf the bucket was allocated from), else
	# the first word of its source warehouse — "<Farm> Receiving Cold Store - KR".
	# Not the warehouse first: shelving at Kapkolia re-points it to Kapkolia's cold
	# store, and the shelved bucket then read as a hub bucket and dropped off the page.
	# Default delivery_date = TOMORROW.
	fd = frappe.form_dict
	delivery_date = fd.get("delivery_date") or frappe.utils.add_days(frappe.utils.today(), 1)

	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	# Transfers are from the remote farms only: a bucket whose stock is already at the
	# packhouse (hub) is not coming from anywhere, whatever its flags say.
	hub = transfer_hub(required=False) or ""
	# Nor from any other sales farm (Production Settings > Shelf Locations, Sales Shelf):
	# Karen packs and issues its own orders, which never transfer, and their issued
	# buckets used to show here as if they had come in on a truck.
	sales_farms = set(
		frappe.get_all(
			"Shelf Locations",
			filters={"parent": "Production Settings", "sales_shelf": 1, "enabled": 1},
			pluck="farm",
		)
	)
	sales_farms.add(hub)
	NOT_HUB = "COALESCE(" + FARM_EXPR + ", '') NOT IN %(sales_farms)s"
	params = {"d": delivery_date, "hub": hub, "sales_farms": tuple(f for f in sales_farms if f) or ("",)}
	# onward=1: every order delivering on delivery_date or later (the page's default view:
	# tomorrow onwards), not that one day only.
	onward = frappe.utils.cint(fd.get("onward"))
	date_cond = "so.delivery_date >= %(d)s" if onward else "so.delivery_date = %(d)s"
	conds = [
		"opl.docstatus < 2",
		"pli.parenttype = 'Order Pick List'",
		date_cond,
		TRANSFER,
		NOT_HUB,
	]
	if fd.get("farm"):
		conds.append(FARM_EXPR + " = %(farm)s")
		params["farm"] = fd.get("farm")
	# Team is filtered after the query: the effective team is the one on the order's
	# Packhouse Schedule (what the transfer planner uses), falling back to OPL.team.
	team_filter = fd.get("team") or ""
	where = " AND ".join(conds)

	rows = frappe.db.sql(
		"""
        SELECT
            opl.name                  AS opl,
            opl.order_name     AS order_name,
            opl.sales_order           AS sales_order,
            so.customer               AS customer,
            so.delivery_date          AS delivery_date,
            opl.creation              AS initiated,
            opl.team                  AS opl_team,
            GROUP_CONCAT(DISTINCT """
		+ FARM_EXPR
		+ """ ORDER BY 1 SEPARATOR ', ') AS farm,
            -- Only the varieties of the buckets being transferred (same WHERE as the counts).
            GROUP_CONCAT(DISTINCT pli.item_code ORDER BY pli.item_code SEPARATOR ', ') AS varieties,
            MAX(CASE WHEN """
		+ TRANSFER_TRUCK_OK
		+ """ THEN pli.transit_truck END) AS truck,
            -- Counted per BUCKET: a pick list keeps one row per box, so a bucket
            -- packed into two boxes has two rows (and was counted twice).
            COUNT(DISTINCT """
		+ BKT
		+ """)                  AS total,
            COUNT(DISTINCT CASE WHEN """
		+ REACHED_AWAITING
		+ """ THEN """
		+ BKT
		+ """ END) AS awaiting,
            COUNT(DISTINCT CASE WHEN """
		+ REACHED_TROLLEY
		+ """ THEN """
		+ BKT
		+ """ END) AS trolley,
            COUNT(DISTINCT CASE WHEN """
		+ REACHED_TRUCK
		+ """ THEN """
		+ BKT
		+ """ END) AS on_truck,
            COUNT(DISTINCT CASE WHEN """
		+ REACHED_TRANSIT
		+ """ THEN """
		+ BKT
		+ """ END) AS transit,
            COUNT(DISTINCT CASE WHEN """
		+ REACHED_SHELVED
		+ """ THEN """
		+ BKT
		+ """ END) AS shelved,
            COUNT(DISTINCT CASE WHEN """
		+ REACHED_READY
		+ """ THEN """
		+ BKT
		+ """ END) AS ready,
            COUNT(DISTINCT CASE WHEN pli.issued = 1 THEN """
		+ BKT
		+ """ END) AS issued,
            COUNT(DISTINCT CASE WHEN pli.issued_offline = 1 THEN """
		+ BKT
		+ """ END) AS issued_offline,
            -- Quality-issue replacements requested ASAP (packing) and not here yet.
            COUNT(DISTINCT CASE WHEN IFNULL(pli.transfer_priority, '') = 'ASAP'
                AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0
                AND IFNULL(pli.custom_ready_for_packing, 0) = 0 THEN """
		+ BKT
		+ """ END) AS asap,
            -- Not in the farm's cold room and nothing to replace it: left out of the transfer.
            COUNT(DISTINCT CASE WHEN pli.not_found = 1 THEN """
		+ BKT
		+ """ END) AS not_found
        FROM `tabPick List Item` pli
        JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
        LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
        WHERE """
		+ where
		+ """
        GROUP BY opl.name
        ORDER BY opl.order_name
    """,
		params,
		as_dict=True,
	)

	sched = _schedule_map()
	for r in rows:
		for k in [
			"total",
			"awaiting",
			"trolley",
			"transit",
			"shelved",
			"ready",
			"issued",
			"issued_offline",
			"not_found",
			"asap",
		]:
			r[k] = int(r.get(k) or 0)
		# Transfer initiation time = OPL creation datetime (full timestamp).
		r["initiated"] = str(r.get("initiated")) if r.get("initiated") else ""
		sc = sched.get(r["opl"]) or {}
		r["team"] = sc.get("team") or r.get("opl_team") or ""
		r["schedule"] = sc.get("schedule")
		# Why an order with buckets still out at a remote farm isn't moving: it has to
		# be on a team's schedule before Transfer Scheduling will put it on a truck.
		r["scheduled"] = 1 if sc else 0
		r["no_trip_section"] = no_trip_section(r)
		r["no_team"] = 0 if r["team"] else 1
	if team_filter:
		rows = [r for r in rows if r["team"] == team_filter]

	# Trips carrying each order, whatever their status: an order delivered tomorrow is
	# trucked in today or the day before, so a trip dated before today that has been
	# received still carried it (it is not "Not Scheduled"). One order can ride several
	# trucks.
	trips = {}
	if rows:
		for t in frappe.db.sql(
			"""
            SELECT o.order_pick_list AS opl, t.name AS trip, t.vehicle AS vehicle, t.status AS status,
                   t.trip_date AS trip_date, SUM(o.buckets) AS buckets,
                   SUM(o.loaded_buckets) AS loaded_buckets, t.run AS run, t.route AS route,
                   t.arrived_at AS arrived_at, t.dispatched_at AS dispatched_at,
                   IFNULL(o.farm, '') AS farm
            FROM `tabBucket Request Trip Order` o
            JOIN `tabBucket Request Trip` t ON t.name = o.parent
            WHERE o.order_pick_list IN %(opls)s
              AND t.trip_date >= %(since)s
            -- One entry per trip per farm: each visit to a farm is one trip (run); the
            -- next pickup from the same farm is the truck's next run, its own entry.
            GROUP BY o.order_pick_list, t.name, IFNULL(o.farm, '')
            ORDER BY t.trip_date DESC, t.vehicle, t.run, IFNULL(o.farm, ''), t.name
        """,
			{
				"opls": tuple(r["opl"] for r in rows),
				"since": frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS),
			},
			as_dict=True,
		):
			trips.setdefault(t.opl, []).append(
				{
					"trip": t.trip,
					"vehicle": t.vehicle,
					"status": t.status,
					"trip_date": str(t.trip_date),
					"buckets": int(t.buckets or 0),
					# The trip order rows' own plan (buckets is the counted figure below).
					"row_buckets": int(t.buckets or 0),
					"loaded_buckets": int(t.loaded_buckets or 0),
					"run": int(t.run or 0),
					"route": t.route or "",
					"farm": t.farm,
					# The farm app's "Truck arrived at <hub>": at the packhouse, not shelved yet.
					"arrived_at": str(t.arrived_at or ""),
					"dispatched_at": str(t.dispatched_at or ""),
				}
			)
	# Per source farm counts of each order (the page groups rows by farm): an order
	# collecting from two farms shows under both, with that farm's own numbers.
	by_farm = {}
	if rows:
		for x in frappe.db.sql(
			"""SELECT pli.parent AS opl, """
			+ FARM_EXPR
			+ """ AS farm,
			    COUNT(DISTINCT """
			+ BKT
			+ """) AS total,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_AWAITING
			+ """ THEN """
			+ BKT
			+ """ END) AS awaiting,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_TROLLEY
			+ """ THEN """
			+ BKT
			+ """ END) AS trolley,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_TRUCK
			+ """ THEN """
			+ BKT
			+ """ END) AS on_truck,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_TRANSIT
			+ """ THEN """
			+ BKT
			+ """ END) AS transit,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_SHELVED
			+ """ THEN """
			+ BKT
			+ """ END) AS shelved,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_READY
			+ """ THEN """
			+ BKT
			+ """ END) AS ready,
			    COUNT(DISTINCT CASE WHEN pli.issued = 1 THEN """
			+ BKT
			+ """ END) AS issued,
			    COUNT(DISTINCT CASE WHEN pli.issued_offline = 1 THEN """
			+ BKT
			+ """ END) AS issued_offline,
			    COUNT(DISTINCT CASE WHEN pli.not_found = 1 THEN """
			+ BKT
			+ """ END) AS not_found,
			    -- Stems: a bucket split over boxes has a row per box, each with its share.
			    SUM(pli.stock_qty) AS stems,
			    SUM(CASE WHEN """
			+ REACHED_SHELVED
			+ """ THEN pli.stock_qty ELSE 0 END) AS arrived_stems,
			    GROUP_CONCAT(DISTINCT pli.item_code ORDER BY pli.item_code SEPARATOR ', ') AS varieties
			FROM `tabPick List Item` pli
			WHERE pli.parent IN %(opls)s AND pli.parenttype = 'Order Pick List' AND """
			+ TRANSFER
			+ " AND "
			+ NOT_HUB
			+ """
			GROUP BY pli.parent, """
			+ FARM_EXPR,
			{"opls": tuple(r["opl"] for r in rows), "hub": hub, "sales_farms": params["sales_farms"]},
			as_dict=True,
		):
			by_farm.setdefault(x.opl, []).append(
				{
					k: (int(x[k] or 0) if k not in ("farm", "varieties") else (x[k] or ""))
					for k in x
					if k != "opl"
				}
			)
	# On a truck that has arrived at the packhouse (the farm app's "Truck arrived"), not
	# shelved yet — per order and source farm.
	at_hub = {}
	if rows:
		for x in frappe.db.sql(
			"""SELECT pli.parent AS opl, """
			+ FARM_EXPR
			+ """ AS src, COUNT(DISTINCT """
			+ BKT
			+ """) AS n
			FROM `tabBucket Request Trip Bucket` tb
			JOIN `tabBucket Request Trip` t ON t.name = tb.parent
			JOIN `tabPick List Item` pli ON pli.parent = tb.order_pick_list AND pli.parenttype = 'Order Pick List'
			     AND pli.bucket = tb.bucket
			WHERE tb.parenttype = 'Bucket Request Trip' AND tb.order_pick_list IN %(opls)s
			  -- Any status: a Received trip's unshelved buckets still reached Kapkolia.
			  AND t.arrived_at IS NOT NULL
			  AND IFNULL(tb.off_truck, 0) = 0 AND IFNULL(pli.shelved, 0) = 0
			GROUP BY pli.parent, """
			+ FARM_EXPR,
			{"opls": tuple(r["opl"] for r in rows)},
			as_dict=True,
		):
			at_hub[(x.opl, x.src or "")] = int(x.n or 0)
	for r in rows:
		r["hub"] = hub
		# Reached the hub = on an arrived truck (not shelved yet) + already shelved,
		# which counts ready / issued too.
		r["arrived_hub"] = sum(n for (o, _f), n in at_hub.items() if o == r["opl"]) + r["shelved"]
		for f in by_farm.get(r["opl"], []):
			f["arrived_hub"] = at_hub.get((r["opl"], f["farm"]), 0) + f["shelved"]
	# Per trip, the buckets that count on it: trip_numbers, the one count the farm app,
	# the packhouse app and truck loads show too (one trip per bucket; an order split
	# over two runs fills the earlier one first; a shared bucket counts once). The
	# trips' other orders are counted too, for each trip's own totals.
	today = frappe.utils.today()
	current = _current_trips(today)
	run_trips = current | {t["trip"] for ts in trips.values() for t in ts}
	shares = trip_bucket_shares({r["opl"] for r in rows} | _trip_order_opls(run_trips))
	rides = _riding_trips(shares, {r["opl"] for r in rows})
	numbers = trip_numbers(run_trips | {t["trip"] for ts in rides.values() for t in ts}, shares)
	for r in rows:
		r["by_farm"] = by_farm.get(r["opl"], [])
		mine = trips.get(r["opl"], [])
		# A shared bucket rides with its lead order: that trip carries this order too.
		have = {(t["trip"], t["farm"]) for t in mine}
		ride = {(t["trip"], t["farm"]): t for t in rides.get(r["opl"], [])}
		mine += [t for k, t in ride.items() if k not in have]
		for t in mine:
			p = trip_order_numbers(numbers.get(t["trip"])).get((r["opl"], t["farm"])) or {}
			t["buckets"] = int(p.get("buckets") or 0)
			t["planned"] = int(p.get("planned") or 0)
			t["trolley"] = int(p.get("trolley") or 0)
			# On the truck or past it: what the farm app counts as loaded.
			t["loaded_buckets"] = int(p.get("on_truck") or 0)
			t["shelved"] = int(p.get("shelved") or 0)
			t["riding"] = int(p.get("riding") or 0)
			t["counted"] = int(p.get("total") or 0) + t["riding"]
			# Its own trip entry with buckets riding along too: name the order they ride with.
			lead = ride.get((t["trip"], t["farm"]))
			if t["riding"] and lead and not t.get("lead_name"):
				t["lead"] = lead["lead"]
				t["lead_name"] = lead["lead_name"]
		# A trip closed without ever leaving that took none of this order's buckets is
		# not one of its trips.
		r["trips"] = [t for t in mine if t["counted"] or t["dispatched_at"] or t["status"] != "Received"]
		# Buckets riding with another order on a trip still out.
		r["riding"] = sum(t["riding"] for t in r["trips"] if t["status"] != "Received")
	# Under a trip, each order's stages count only its buckets on that trip.
	trip_stages(rows)

	# Arrival time per OPL = when the FIRST bucket of the order was shelved at the
	# sales (destination) farm. Source of truth = the CONTINUOUS `Shelving Log`
	# (NOT `Shelf Item`): issuing a bucket to the sales order clears its Shelf Item,
	# so a fully-shelved-then-issued order would otherwise look like it never
	# arrived. The log keeps every shelving forever, so we take, per bucket, its
	# LATEST log entry (max creation) and keep it only if that entry is at the
	# OPL's own farm (`o2.farm`) — i.e. the bucket's current/most-recent
	# shelving is at the destination. Arrival for the OPL = the earliest such
	# `shelved_on`. Buckets are reused across orders, so we also require the
	# shelving to have happened at/after the OPL was created (>= o2.creation) —
	# otherwise a stale Kapkolia entry from a PREVIOUS cycle would falsely mark an
	# order as arrived (and yield a negative transit time). bucket_id case is
	# inconsistent between tables → compare UPPER().
	# Computed in a SEPARATE query so it can't inflate the status counts above.
	opl_names = [r["opl"] for r in rows]
	arrivals = {}
	if opl_names:
		# In steps rather than one join: UPPER() on both sides of the bucket join kept
		# MySQL off the bucket_id index and scanned the whole Shelving Log (the collation
		# is case-insensitive, so a plain IN matches any case).
		opl_meta = {
			o.name: o
			for o in frappe.get_all(
				"Order Pick List", filters={"name": ["in", opl_names]}, fields=["name", "farm", "creation"]
			)
		}
		opl_buckets = {}
		for p in frappe.get_all(
			"Pick List Item",
			filters={"parenttype": "Order Pick List", "parent": ["in", opl_names], "bucket": ["is", "set"]},
			fields=["parent", "bucket"],
		):
			opl_buckets.setdefault(p.parent, set()).add(p.bucket.upper())
		all_buckets = sorted(set().union(*opl_buckets.values())) if opl_buckets else []
		# The latest Shelving Log row of each bucket.
		latest = {}
		if all_buckets:
			for sl in frappe.get_all(
				"Shelving Log",
				filters={"bucket_id": ["in", all_buckets]},
				fields=["bucket_id", "farm", "shelved_on", "creation"],
				order_by="creation asc",
			):
				latest[(sl.bucket_id or "").upper()] = sl
		for opl, buckets in opl_buckets.items():
			o = opl_meta.get(opl)
			if not o:
				continue
			times = [
				sl.shelved_on or sl.creation
				for sl in (latest.get(b) for b in buckets)
				if sl and sl.farm == o.farm and (sl.shelved_on or sl.creation) >= o.creation
			]
			if times:
				arrivals[opl] = min(times)
	for r in rows:
		a = arrivals.get(r["opl"])
		r["arrived"] = str(a) if a else ""

	# Distinct source farms for the date (unfiltered by the farm dropdown)
	farm_rows = frappe.db.sql(
		"""
        SELECT DISTINCT """
		+ FARM_EXPR
		+ """ AS f
        FROM `tabPick List Item` pli
        JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
        LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
        WHERE opl.docstatus < 2 AND pli.parenttype = 'Order Pick List'
          AND """
		+ date_cond
		+ """ AND """
		+ TRANSFER
		+ """
        ORDER BY 1
    """,
		{"d": delivery_date},
		as_dict=True,
	)
	# Remote farms only: the sales farms (Kapkolia, Karen) send nothing.
	farms = [r["f"] for r in farm_rows if r.get("f") and r["f"] not in sales_farms]

	# Every run the trucks drive today (one trip per run), for the truck cards (current),
	# and the earlier trips the orders rode, for their trip cards: which run each truck is
	# on, where it goes and how far loading / shelving has got -- the trip's own totals,
	# whichever day its orders deliver.
	run_trips = current | {t["trip"] for r in rows for t in r["trips"]}
	keep = (
		"name",
		"vehicle",
		"trip_date",
		"status",
		"loading",
		"stale",
		"run",
		"runs",
		"run_chain",
		"window",
		"total_buckets",
		"capacity_buckets",
		"loaded_buckets",
		"tracked_buckets",
		"shelved_buckets",
		"departed_stops",
		"heading_to",
		"dispatched_at",
		"received_at",
	)
	numbers.update(trip_numbers(run_trips - set(numbers), shares))
	runs = []
	for name in run_trips:
		n = numbers.get(name) or {}
		t = _trip_dict(frappe.get_doc("Bucket Request Trip", name), today, n)
		run = {k: t[k] for k in keep}
		run["current"] = 1 if name in current else 0
		# The trip's own numbers (trip_numbers): whole trip, every order on it.
		run["numbers"] = {k: v for k, v in n.items() if k != "orders"}
		run["total_buckets"] = n.get("buckets") or 0
		run["capacity_buckets"] = n.get("capacity") or 0
		run["loaded_buckets"] = n.get("on_truck") or 0
		run["trolley_buckets"] = n.get("trolley") or 0
		run["shelved_buckets"] = n.get("shelved") or 0
		runs.append(run)
	runs.sort(key=lambda t: (t["vehicle"] or "", t["trip_date"], t["run"] or 99, t["name"]))
	frappe.response["runs"] = runs

	# The page's tiles: every stage counted once per physical bucket -- a bucket split
	# over two orders is one bucket, though each order's own counts include it.
	totals = {}
	if rows:
		bt = frappe.db.sql(
			"""SELECT COUNT(DISTINCT """
			+ BKT
			+ """) AS total,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_AWAITING
			+ """ THEN """
			+ BKT
			+ """ END) AS awaiting,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_TROLLEY
			+ """ THEN """
			+ BKT
			+ """ END) AS trolley,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_TRANSIT
			+ """ THEN """
			+ BKT
			+ """ END) AS transit,
			    COUNT(DISTINCT CASE WHEN """
			+ REACHED_SHELVED
			+ """ THEN """
			+ BKT
			+ """ END) AS shelved,
			    COUNT(DISTINCT CASE WHEN pli.issued = 1 THEN """
			+ BKT
			+ """ END) AS issued,
			    COUNT(DISTINCT CASE WHEN pli.not_found = 1 THEN """
			+ BKT
			+ """ END) AS not_found
			FROM `tabPick List Item` pli
			WHERE pli.parent IN %(opls)s AND pli.parenttype = 'Order Pick List' AND """
			+ TRANSFER
			+ " AND "
			+ NOT_HUB,
			{"opls": tuple(r["opl"] for r in rows), "hub": hub, "sales_farms": params["sales_farms"]},
			as_dict=True,
		)
		totals = {k: int(v or 0) for k, v in (bt[0] if bt else {}).items()}
	frappe.response["bucket_totals"] = totals

	frappe.response["delivery_date"] = str(delivery_date)
	frappe.response["onward"] = onward
	frappe.response["orders"] = rows
	frappe.response["farms"] = farms
	frappe.response["total_orders"] = len(rows)


def _current_trips(today):
	"""Trips driving today, and earlier ones (lookback) not received yet."""
	names = set(frappe.get_all("Bucket Request Trip", filters={"trip_date": today}, pluck="name"))
	return names | set(
		frappe.get_all(
			"Bucket Request Trip",
			filters={
				"trip_date": [">=", frappe.utils.add_days(today, -TRIP_LOOKBACK_DAYS)],
				"status": ["!=", "Received"],
			},
			pluck="name",
		)
	)


#: The page's stage chips (OV_FLAGS) a trip entry carries under "stages".
STAGE_KEYS = (
	"awaiting",
	"trolley",
	"on_truck",
	"transit",
	"arrived_hub",
	"shelved",
	"ready",
	"issued",
	"issued_offline",
	"not_found",
)


def trip_stages(rows):
	"""Each order's stage counts on each of its trips (r["trips"][i]["stages"]), so a trip
	card shows only the buckets on that trip, not the whole order's at that farm:
	- buckets recorded on the trip (Bucket Request Trip Bucket) by their pick rows'
	  flags, with the same REACHED_* stages as the order's counts; arrived at the hub =
	  on the trip once it arrived (still on the truck) or shelved;
	- the rest of the trip's plan for the order, while the trip is still to leave
	  (Draft / Requested / Scheduled): awaiting, and in a trolley for as many of the
	  order's buckets as sit in one recorded on no trip (earliest run first)."""
	entries = [(r["opl"], t) for r in rows for t in r.get("trips") or []]
	for _opl, t in entries:
		t["stages"] = dict.fromkeys(STAGE_KEYS, 0)
	if not entries:
		return
	opls = tuple({o for o, _t in entries})
	since = frappe.utils.add_days(frappe.utils.today(), -TRIP_LOOKBACK_DAYS)
	recorded = frappe.db.sql(
		"""SELECT tb.parent AS trip, tb.order_pick_list AS opl,
		    COALESCE(NULLIF(tb.farm, ''), """
		+ FARM_EXPR
		+ """, '') AS farm,
		    COUNT(DISTINCT """
		+ BKT
		+ """) AS total,
		    COUNT(DISTINCT CASE WHEN """
		+ REACHED_AWAITING
		+ """ THEN """
		+ BKT
		+ """ END) AS awaiting,
		    COUNT(DISTINCT CASE WHEN """
		+ REACHED_TROLLEY
		+ """ THEN """
		+ BKT
		+ """ END) AS trolley,
		    COUNT(DISTINCT CASE WHEN """
		+ REACHED_TRUCK
		+ """ THEN """
		+ BKT
		+ """ END) AS on_truck,
		    COUNT(DISTINCT CASE WHEN """
		+ REACHED_TRANSIT
		+ """ THEN """
		+ BKT
		+ """ END) AS transit,
		    COUNT(DISTINCT CASE WHEN """
		+ REACHED_SHELVED
		+ """ OR (t.arrived_at IS NOT NULL AND IFNULL(tb.off_truck, 0) = 0) THEN """
		+ BKT
		+ """ END) AS arrived_hub,
		    COUNT(DISTINCT CASE WHEN """
		+ REACHED_SHELVED
		+ """ THEN """
		+ BKT
		+ """ END) AS shelved,
		    COUNT(DISTINCT CASE WHEN """
		+ REACHED_READY
		+ """ THEN """
		+ BKT
		+ """ END) AS ready,
		    COUNT(DISTINCT CASE WHEN pli.issued = 1 THEN """
		+ BKT
		+ """ END) AS issued,
		    COUNT(DISTINCT CASE WHEN pli.issued_offline = 1 THEN """
		+ BKT
		+ """ END) AS issued_offline,
		    COUNT(DISTINCT CASE WHEN pli.not_found = 1 THEN """
		+ BKT
		+ """ END) AS not_found
		FROM `tabBucket Request Trip Bucket` tb
		JOIN `tabBucket Request Trip` t ON t.name = tb.parent
		JOIN `tabPick List Item` pli ON pli.parent = tb.order_pick_list AND pli.parenttype = 'Order Pick List'
		     AND pli.bucket = tb.bucket
		WHERE tb.parenttype = 'Bucket Request Trip' AND tb.parent IN %(trips)s
		  AND tb.order_pick_list IN %(opls)s
		  -- Taken back off the truck at the farm: no longer on this trip.
		  AND (IFNULL(tb.off_truck, 0) = 0 OR IFNULL(tb.shelved, 0) = 1)
		GROUP BY tb.parent, tb.order_pick_list, 3""",
		{"trips": tuple({t["trip"] for _o, t in entries}), "opls": opls},
		as_dict=True,
	)
	# The orders' buckets sitting in a trolley, not on the truck and recorded on no trip.
	in_trolley = frappe.db.sql(
		"""SELECT pli.parent AS opl, IFNULL("""
		+ FARM_EXPR
		+ """, '') AS farm, COUNT(DISTINCT """
		+ BKT
		+ """) AS n
		FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s
		  AND IFNULL(pli.not_found, 0) = 0 AND """
		+ REACHED_TROLLEY
		+ """ AND NOT """
		+ REACHED_TRUCK
		+ """
		  -- An earlier day's trolley is gone at midnight (clear_left_trolleys).
		  AND pli.modified >= %(today)s
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabBucket Request Trip Bucket` tb
		      JOIN `tabBucket Request Trip` t ON t.name = tb.parent
		      WHERE tb.parenttype = 'Bucket Request Trip' AND tb.order_pick_list = pli.parent
		        AND tb.bucket = pli.bucket AND t.trip_date >= %(since)s
		        AND (IFNULL(tb.off_truck, 0) = 0 OR IFNULL(tb.shelved, 0) = 1))
		GROUP BY pli.parent, 2""",
		{"opls": opls, "since": since, "today": frappe.utils.today()},
		as_dict=True,
	)
	scope_trip_stages(entries, recorded, in_trolley)


def scope_trip_stages(entries, recorded, in_trolley):
	"""trip_stages' counting, on its query rows: entries = [(opl, trip entry)]."""

	def mine(t, opl, x):
		return x.opl == opl and (not t["farm"] or not x.farm or _same_farm(t["farm"], x.farm))

	pool = {(x.opl, x.farm): int(x.n or 0) for x in in_trolley}
	waiting = []
	for opl, t in entries:
		s = t["stages"]
		done = 0
		for x in recorded:
			if x.trip == t["trip"] and mine(t, opl, x):
				done += int(x.total or 0)
				for k in STAGE_KEYS:
					s[k] += int(x.get(k) or 0)
		rest = max(0, int(t.get("row_buckets") or 0) - done)
		if rest and t["status"] in ACTIVE_TRIP_STATUSES:
			s["awaiting"] += rest
			waiting.append((opl, t, rest))
	waiting.sort(key=lambda w: (w[1]["trip_date"], w[1]["run"] or 99, w[1]["trip"]))
	for opl, t, rest in waiting:
		for key in pool:
			if not pool[key] or not mine(t, opl, frappe._dict(opl=key[0], farm=key[1])):
				continue
			take = min(rest, pool[key])
			pool[key] -= take
			rest -= take
			t["stages"]["trolley"] += take


def _trip_order_opls(trips):
	"""Every order planned on these trips."""
	if not trips:
		return set()
	return set(
		frappe.get_all(
			"Bucket Request Trip Order",
			filters={"parenttype": "Bucket Request Trip", "parent": ["in", list(trips)]},
			pluck="order_pick_list",
		)
	) - {None, ""}


def _riding_trips(shares, opls):
	"""opl -> trip entries (as getBucketLogistics lists them) for the trips its shared
	buckets ride on with their lead order (trip_bucket_shares' riding portions)."""
	found = {}
	for trip, by in shares.items():
		for (opl, farm), sh in by.items():
			if opl in opls and sh["riding"]:
				lead = next(
					(
						o
						for (o, _f), x in by.items()
						if o != opl and any(b in x["buckets"] for b in sh["riding_buckets"])
					),
					"",
				)
				found.setdefault(trip, []).append((opl, farm, lead))
	# An order whose only buckets here ride with another order (shared buckets) has no
	# row of its own on that trip: it rides there with the lead that carries them.
	groups = shared_buckets({b for by in shares.values() for x in by.values() for b in x["buckets"]})
	for trip, by in shares.items():
		for (lead, farm), x in by.items():
			for b in x["buckets"]:
				for opl in (groups.get((b, farm)) or {}).get("opls") or []:
					if opl in opls and opl != lead and (opl, farm) not in by:
						entry = (opl, farm, lead)
						if entry not in found.get(trip, []):
							found.setdefault(trip, []).append(entry)
	if not found:
		return {}
	meta = {
		t.name: t
		for t in frappe.get_all(
			"Bucket Request Trip",
			filters={"name": ["in", list(found)]},
			fields=["name", "vehicle", "status", "trip_date", "run", "route", "arrived_at", "dispatched_at"],
		)
	}
	leads = {l for v in found.values() for (_o, _f, l) in v if l}
	names = (
		dict(
			frappe.get_all(
				"Order Pick List",
				filters={"name": ["in", list(leads)]},
				fields=["name", "order_name"],
				as_list=True,
			)
		)
		if leads
		else {}
	)
	out = {}
	for trip, entries in found.items():
		t = meta.get(trip)
		if not t:
			continue
		for opl, farm, lead in entries:
			out.setdefault(opl, []).append(
				{
					"trip": trip,
					"vehicle": t.vehicle,
					"status": t.status,
					"trip_date": str(t.trip_date),
					"buckets": 0,
					"loaded_buckets": 0,
					"run": int(t.run or 0),
					"route": t.route or "",
					"farm": farm,
					"arrived_at": str(t.arrived_at or ""),
					"dispatched_at": str(t.dispatched_at or ""),
					"lead": lead,
					"lead_name": names.get(lead) or lead,
				}
			)
	return out


@frappe.whitelist()
def getTripsStamp(date: str | None = None):
	"""When today's trips last changed -- a trip, its orders or the buckets put on it --
	so the page re-reads as soon as Distribute, a farm or a truck adds to them."""
	date = date or frappe.utils.today()
	row = frappe.db.sql(
		"""SELECT COUNT(DISTINCT t.name), MAX(t.modified), MAX(o.modified), MAX(b.modified),
		       COALESCE(SUM(DISTINCT t.total_buckets), 0)
		FROM `tabBucket Request Trip` t
		LEFT JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
		LEFT JOIN `tabBucket Request Trip Bucket` b ON b.parent = t.name AND b.parenttype = 'Bucket Request Trip'
		WHERE t.trip_date = %s""",
		date,
	)[0]
	return "|".join(str(v or "") for v in row)


@frappe.whitelist()
def getBucketLogisticsDetail():
	# Per-bucket detail for one Order Pick List — raw Pick List Item flags as
	# checkboxes. Only buckets being transferred (any transfer flag, or shelved).
	# Source farm = first word of source_warehouse (else warehouse). Pick List
	# Item's native field is `source_warehouse`, no "custom_" prefix -- this
	# used to reference a "custom_source_warehouse" that doesn't exist on this
	# doctype at all (a real "Unknown column" bug, just never hit because
	# every call so far happened to pass a farm filter that took a different
	# path, or hit an empty result set first).
	fd = frappe.form_dict
	opl = fd.get("opl")
	if not opl:
		frappe.response["buckets"] = []
	else:
		FARM_EXPR = "COALESCE(NULLIF(pli.farm, ''), NULLIF(SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1), ''))"
		from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

		params = {"opl": opl, "hub": transfer_hub(required=False) or ""}
		extra = ""
		if fd.get("farm"):
			extra = " AND " + FARM_EXPR + " = %(farm)s"
			params["farm"] = fd.get("farm")
		frappe.response["buckets"] = frappe.db.sql(
			"""
            SELECT
                pli.bucket                   AS bucket,
                pli.item_code                AS variety,
                pli.stem_length               AS length,
                pli.shelf                    AS shelf,
                COALESCE(MAX(CASE WHEN """
			+ TRANSFER_TRUCK_OK
			+ """ THEN pli.transit_truck END), MAX(tr.vehicle)) AS truck,
                -- The trip (run) that carried it: shelving can leave no truck on the row.
                MAX(tr.trip) AS trip, MAX(tr.run) AS run,
                """
			+ FARM_EXPR
			+ """        AS farm,
                GROUP_CONCAT(DISTINCT pli.custom_box_id ORDER BY pli.custom_box_id SEPARATOR ', ') AS box_id,
                COUNT(*)                     AS boxes,
                SUM(pli.stock_qty)           AS stems,
                -- Each stage ticks once the bucket has PASSED it: shelving clears the
                -- earlier flags (awaiting / trolley / in transit) on the row, but the
                -- bucket did go through them, so a later stage implies the earlier ones.
                MAX(GREATEST(IFNULL(pli.awaiting_transfer, 0), IFNULL(pli.loaded_in_trolley, 0),
                    IFNULL(pli.in_transit, 0), IFNULL(pli.shelved, 0),
                    IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS awaiting,
                MAX(GREATEST(IFNULL(pli.loaded_in_trolley, 0), IF(IFNULL(pli.trolley_id, '') != '', 1, 0),
                    IFNULL(pli.in_transit, 0), IFNULL(pli.shelved, 0),
                    IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS trolley,
                MAX(GREATEST(IFNULL(pli.loaded_on_truck, 0), IFNULL(pli.in_transit, 0), IFNULL(pli.shelved, 0),
                    IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS on_truck,
                MAX(GREATEST(IFNULL(pli.in_transit, 0), IFNULL(pli.shelved, 0),
                    IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS transit,
                -- Reached the hub: its truck arrived there, or it is already shelved.
                MAX(GREATEST(IF(tr.arrived_at IS NOT NULL, 1, 0), IFNULL(pli.shelved, 0),
                    IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS arrived_hub,
                MAX(GREATEST(IFNULL(pli.shelved, 0), IFNULL(pli.custom_ready_for_packing, 0),
                    IFNULL(pli.issued, 0))) AS shelved,
                MAX(GREATEST(IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS ready,
                MAX(IFNULL(pli.issued, 0))   AS issued,
                MAX(IFNULL(pli.issued_offline, 0)) AS issued_offline,
                MAX(IFNULL(pli.not_found, 0)) AS not_found,
                -- When, and by whom, each step was done (first time on any of its rows).
                MIN(pli.loaded_in_trolley_at) AS trolley_at, MAX(pli.loaded_in_trolley_by) AS trolley_by,
                MIN(pli.loaded_on_truck_at) AS on_truck_at, MAX(pli.loaded_on_truck_by) AS on_truck_by,
                MIN(pli.left_farm_at) AS transit_at, MAX(pli.left_farm_by) AS transit_by,
                MIN(pli.arrived_at) AS arrived_hub_at, MAX(pli.arrived_by) AS arrived_hub_by,
                MIN(pli.shelved_at) AS shelved_at, MAX(pli.shelved_by) AS shelved_by,
                MIN(pli.issued_at) AS issued_at, MAX(pli.issued_by) AS issued_by,
                MIN(pli.not_found_at) AS not_found_at, MAX(pli.not_found_by) AS not_found_by
            FROM `tabPick List Item` pli
            JOIN `tabOrder Pick List` o ON o.name = pli.parent
            LEFT JOIN (
                SELECT UPPER(b.bucket) AS bucket, MAX(t.name) AS trip, MAX(t.vehicle) AS vehicle,
                       MAX(t.run) AS run,
                       MAX(CASE WHEN IFNULL(b.off_truck, 0) = 0 THEN t.arrived_at END) AS arrived_at
                FROM `tabBucket Request Trip Bucket` b
                JOIN `tabBucket Request Trip` t ON t.name = b.parent
                WHERE b.parenttype = 'Bucket Request Trip' AND b.order_pick_list = %(opl)s
                GROUP BY UPPER(b.bucket)
            ) tr ON tr.bucket = UPPER(pli.bucket)
            WHERE pli.parenttype = 'Order Pick List' AND o.name = %(opl)s
              AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1
                   OR pli.in_transit = 1 OR pli.shelved = 1 OR pli.not_found = 1
                   OR ((pli.custom_ready_for_packing = 1 OR pli.issued = 1)
                       AND COALESCE("""
			+ FARM_EXPR
			+ """, '') != %(hub)s))"""
			+ extra
			+ """
            -- One row per bucket (the pick list keeps a row per box); a bucket holding
            -- more than one variety or stem length gets a row for each of them.
            GROUP BY COALESCE(NULLIF(UPPER(pli.bucket), ''), pli.name), pli.item_code, pli.stem_length
            ORDER BY MIN(pli.idx)
            LIMIT 2000
        """,
			params,
			as_dict=True,
		)
		# Users by name on the dashboard, not by login.
		users = {u for b in frappe.response["buckets"] for k, u in b.items() if k.endswith("_by") and u}
		names = {
			u.name: u.full_name
			for u in (
				frappe.get_all("User", filters={"name": ["in", list(users)]}, fields=["name", "full_name"])
				if users
				else []
			)
		}
		for b in frappe.response["buckets"]:
			for k in [k for k in b if k.endswith("_by")]:
				b[k] = names.get(b[k]) or b[k]
		# Each variety's colour (Item colour, as on the Variety Tree), for the apps'
		# per-variety bucket lists.
		colors = _variety_colors({b.get("variety") for b in frappe.response["buckets"]})
		for b in frappe.response["buckets"]:
			b["variety_color"] = colors.get(b.get("variety")) or ""


def _variety_colors(item_codes):
	"""Item code -> its Variety Tree colour hex (Item.custom_color -> Color.color)."""
	item_codes = [i for i in item_codes if i]
	if not item_codes or not frappe.get_meta("Item").has_field("custom_color"):
		return {}
	item_colour = {
		i.name: i.custom_color
		for i in frappe.get_all("Item", filters={"name": ["in", item_codes]}, fields=["name", "custom_color"])
		if i.custom_color
	}
	if not item_colour:
		return {}
	hexes = {
		c.name: c.color
		for c in frappe.get_all(
			"Color", filters={"name": ["in", list(set(item_colour.values()))]}, fields=["name", "color"]
		)
	}
	return {k: hexes.get(v) or "" for k, v in item_colour.items()}
