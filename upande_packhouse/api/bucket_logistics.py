# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `bucket-logistics` page API — ported verbatim from kaitet-group v15 LIVE
# Server Scripts (these were never migrated to the v16 bench). Bodies keep
# frappe.form_dict / frappe.response exactly as the live scripts set them.

import frappe

from upande_packhouse.api.transfer_control import TRANSFER_TRUCK_OK, TRIP_LOOKBACK_DAYS, _schedule_map


@frappe.whitelist()
def getBucketLogistics():
	# Bucket Logistics — orders with buckets being transferred, for a delivery date.
	# A bucket is "being transferred" iff it was ever flagged for transfer (awaiting,
	# loaded, in transit) or has been shelved. Dispatch keeps awaiting_transfer=1 while
	# it sets in_transit, but in_transit/loaded are included so nothing drops out.
	# Source farm = first word of the source warehouse (custom_source_warehouse, else
	# warehouse) — warehouses are named "<Farm> Receiving Cold Store - KR".
	# Default delivery_date = TOMORROW.
	fd = frappe.form_dict
	delivery_date = fd.get("delivery_date") or frappe.utils.add_days(frappe.utils.today(), 1)

	# source-farm expression (reused in SELECT + WHERE)
	FARM_EXPR = "SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1)"
	TRANSFER = (
		"(pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1 OR pli.shelved = 1)"
	)

	# A bucket's identity on the pick list (case-insensitive; a row without a bucket counts alone).
	BKT = "COALESCE(NULLIF(UPPER(pli.bucket), ''), pli.name)"

	params = {"d": delivery_date}
	conds = ["opl.docstatus < 2", "pli.parenttype = 'Order Pick List'", "so.delivery_date = %(d)s", TRANSFER]
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
            MAX(CASE WHEN """
		+ TRANSFER_TRUCK_OK
		+ """ THEN pli.transit_truck END) AS truck,
            -- Counted per BUCKET: a pick list keeps one row per box, so a bucket
            -- packed into two boxes has two rows (and was counted twice).
            COUNT(DISTINCT """
		+ BKT
		+ """)                  AS total,
            COUNT(DISTINCT CASE WHEN pli.awaiting_transfer = 1 THEN """
		+ BKT
		+ """ END) AS awaiting,
            COUNT(DISTINCT CASE WHEN pli.loaded_in_trolley = 1 THEN """
		+ BKT
		+ """ END) AS trolley,
            COUNT(DISTINCT CASE WHEN pli.in_transit = 1 THEN """
		+ BKT
		+ """ END) AS transit,
            COUNT(DISTINCT CASE WHEN pli.shelved = 1 THEN """
		+ BKT
		+ """ END) AS shelved,
            COUNT(DISTINCT CASE WHEN pli.custom_ready_for_packing = 1 THEN """
		+ BKT
		+ """ END) AS ready,
            COUNT(DISTINCT CASE WHEN pli.issued = 1 THEN """
		+ BKT
		+ """ END) AS issued
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
		for k in ["total", "awaiting", "trolley", "transit", "shelved", "ready", "issued"]:
			r[k] = int(r.get(k) or 0)
		# Transfer initiation time = OPL creation datetime (full timestamp).
		r["initiated"] = str(r.get("initiated")) if r.get("initiated") else ""
		sc = sched.get(r["opl"]) or {}
		r["team"] = sc.get("team") or r.get("opl_team") or ""
		r["schedule"] = sc.get("schedule")
		# Why an order with buckets still out at a remote farm isn't moving: it has to
		# be on a team's schedule before Transfer Scheduling will put it on a truck.
		r["scheduled"] = 1 if sc else 0
		r["no_team"] = 0 if r["team"] else 1
	if team_filter:
		rows = [r for r in rows if r["team"] == team_filter]

	# Trips carrying each order: today's plus any not yet received (still on the road
	# or a stale draft). One order can ride several trucks.
	trips = {}
	if rows:
		for t in frappe.db.sql(
			"""
            SELECT o.order_pick_list AS opl, t.name AS trip, t.vehicle AS vehicle, t.status AS status,
                   t.trip_date AS trip_date, SUM(o.buckets) AS buckets,
                   SUM(o.loaded_buckets) AS loaded_buckets
            FROM `tabBucket Request Trip Order` o
            JOIN `tabBucket Request Trip` t ON t.name = o.parent
            WHERE o.order_pick_list IN %(opls)s
              AND (t.trip_date = %(today)s
                   OR (t.trip_date >= %(since)s AND t.status != 'Received'))
            GROUP BY o.order_pick_list, t.name
            ORDER BY t.trip_date DESC, t.name
        """,
			{
				"opls": tuple(r["opl"] for r in rows),
				"today": frappe.utils.today(),
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
					"loaded_buckets": int(t.loaded_buckets or 0),
				}
			)
	for r in rows:
		r["trips"] = trips.get(r["opl"], [])

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
		ar_rows = frappe.db.sql(
			"""
            SELECT pli.parent AS opl, MIN(COALESCE(sl.shelved_on, sl.creation)) AS arrival
            FROM `tabPick List Item` pli
            JOIN `tabOrder Pick List` o2 ON o2.name = pli.parent
            JOIN `tabShelving Log` sl ON UPPER(sl.bucket_id) = UPPER(pli.bucket)
            JOIN (
                SELECT UPPER(sl2.bucket_id) AS bid, MAX(sl2.creation) AS mx
                FROM `tabShelving Log` sl2
                JOIN `tabPick List Item` p2 ON UPPER(p2.bucket) = UPPER(sl2.bucket_id)
                WHERE p2.parenttype = 'Order Pick List' AND p2.parent IN %(opls)s
                GROUP BY UPPER(sl2.bucket_id)
            ) latest ON latest.bid = UPPER(sl.bucket_id) AND latest.mx = sl.creation
            WHERE pli.parenttype = 'Order Pick List'
              AND sl.farm = o2.farm
              AND COALESCE(sl.shelved_on, sl.creation) >= o2.creation
              AND pli.parent IN %(opls)s
            GROUP BY pli.parent
        """,
			{"opls": tuple(opl_names)},
			as_dict=True,
		)
		for a in ar_rows:
			arrivals[a["opl"]] = a["arrival"]
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
          AND so.delivery_date = %(d)s AND """
		+ TRANSFER
		+ """
        ORDER BY 1
    """,
		{"d": delivery_date},
		as_dict=True,
	)
	farms = [r["f"] for r in farm_rows if r.get("f")]

	frappe.response["delivery_date"] = str(delivery_date)
	frappe.response["orders"] = rows
	frappe.response["farms"] = farms
	frappe.response["total_orders"] = len(rows)


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
		FARM_EXPR = "SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1)"
		params = {"opl": opl}
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
                CASE WHEN """
			+ TRANSFER_TRUCK_OK
			+ """ THEN pli.transit_truck END AS truck,
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
                MAX(GREATEST(IFNULL(pli.in_transit, 0), IFNULL(pli.shelved, 0),
                    IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS transit,
                MAX(GREATEST(IFNULL(pli.shelved, 0), IFNULL(pli.custom_ready_for_packing, 0),
                    IFNULL(pli.issued, 0))) AS shelved,
                MAX(GREATEST(IFNULL(pli.custom_ready_for_packing, 0), IFNULL(pli.issued, 0))) AS ready,
                MAX(IFNULL(pli.issued, 0))   AS issued
            FROM `tabPick List Item` pli
            JOIN `tabOrder Pick List` o ON o.name = pli.parent
            WHERE pli.parenttype = 'Order Pick List' AND o.name = %(opl)s
              AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1
                   OR pli.in_transit = 1 OR pli.shelved = 1)"""
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
