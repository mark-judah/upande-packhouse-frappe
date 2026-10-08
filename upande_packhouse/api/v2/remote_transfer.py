# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Remote Transfers v2 (www/remote-transfer-v2.html).

Read endpoints for the tabs whose numbers must follow every filter. The v1
modules (api/remote_transfer/*, api/transfer_control) are untouched and keep
serving the v1 pages and the mobile apps; their write endpoints (trips,
dispatch, receive) are still called directly by the page.

Farm here is the bucket's ORIGIN farm: the farm the bucket was harvested /
shelved at before it is trucked to the hub. Source of truth, in order:

  1. Pick List Item.farm, when it is not a sales farm (the hub or Karen). After a
     bucket arrives, shelving rewrites pli.farm to the hub, so
  2. the farm of Pick List Item.source_warehouse (else warehouse), which keeps
     the remote farm's cold store, and last
  3. Pick List Item.farm as it is.

Never Shelf Item.warehouse and never source_warehouse alone as the packing farm
(it can say Kapkolia for buckets still at a remote farm).

Date meaning (README rule 4): the Sales Order's delivery_date.

Counts are per physical bucket per Order Pick List (COUNT DISTINCT on the
upper-cased bucket id; a bucket packed into several boxes has several rows).
Stages are exclusive in the numbers the KPIs add up:

    total = at_farm + in_trolley + on_road + shelved + other
    at_farm     awaiting transfer, not yet on a trolley
    in_trolley  on a trolley, truck not gone
    on_road     on a dispatched truck, not shelved
    shelved     shelved at the hub (or ready / issued)
    other       flagged for transfer but with no stage yet (e.g. not found)
"""

import frappe
from frappe.utils import add_days, get_datetime, getdate, today

from upande_packhouse.api.remote_transfer.transfer_scheduling import (
	TRANSFER_TRUCK_OK,
	TRIP_LOOKBACK_DAYS,
	_schedule_map,
	_trip_dict,
	transfer_hub,
)
from upande_packhouse.api.v2.core import region as region_core

TRANSFER = (
	"(pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1 OR pli.shelved = 1"
	" OR pli.not_found = 1 OR pli.custom_ready_for_packing = 1 OR pli.issued = 1)"
)
_ready = "IFNULL(pli.custom_ready_for_packing, 0) = 1 OR IFNULL(pli.issued, 0) = 1"
_shelved = "IFNULL(pli.shelved, 0) = 1 OR " + _ready
_transit = "IFNULL(pli.in_transit, 0) = 1 OR " + _shelved
_trolley = "IFNULL(pli.loaded_in_trolley, 0) = 1 OR IFNULL(pli.trolley_id, '') != '' OR " + _transit
REACHED_READY = "(" + _ready + ")"
REACHED_SHELVED = "(" + _shelved + ")"
REACHED_TRANSIT = "(" + _transit + ")"
REACHED_TROLLEY = "(" + _trolley + ")"
REACHED_AWAITING = "(IFNULL(pli.awaiting_transfer, 0) = 1 OR " + _trolley + ")"
BKT = "COALESCE(NULLIF(UPPER(pli.bucket), ''), pli.name)"
_SRC_FARM = "NULLIF(SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1), '')"
ORIGIN = (
	"COALESCE(NULLIF(CASE WHEN pli.farm IN %(sales_farms)s THEN '' ELSE pli.farm END, ''), "
	+ _SRC_FARM
	+ ", NULLIF(pli.farm, ''))"
)


def _sales_farms():
	hub = transfer_hub(required=False) or ""
	farms = set(
		frappe.get_all(
			"Shelf Locations",
			filters={"parent": "Production Settings", "sales_shelf": 1, "enabled": 1},
			pluck="farm",
		)
	)
	farms.add(hub)
	return hub, {f for f in farms if f}


def _like(q):
	return "%" + (q or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _dates(from_date, to_date):
	f = str(getdate(from_date)) if from_date else str(getdate(add_days(today(), 1)))
	t = str(getdate(to_date)) if to_date else f
	return (t, f) if f > t else (f, t)


def _counts_sql(prefix=""):
	"""The per (opl, origin farm) bucket counts, one definition."""
	parts = {
		"total": BKT,
		"awaiting": "CASE WHEN " + REACHED_AWAITING + " THEN " + BKT + " END",
		"trolley": "CASE WHEN " + REACHED_TROLLEY + " THEN " + BKT + " END",
		"transit": "CASE WHEN " + REACHED_TRANSIT + " THEN " + BKT + " END",
		"shelved": "CASE WHEN " + REACHED_SHELVED + " THEN " + BKT + " END",
		"ready": "CASE WHEN " + REACHED_READY + " THEN " + BKT + " END",
		"issued": "CASE WHEN pli.issued = 1 THEN " + BKT + " END",
		"issued_offline": "CASE WHEN pli.issued_offline = 1 THEN " + BKT + " END",
		"not_found": "CASE WHEN pli.not_found = 1 THEN " + BKT + " END",
		"flag_shelved": "CASE WHEN pli.shelved = 1 THEN " + BKT + " END",
		"asap": "CASE WHEN IFNULL(pli.transfer_priority, '') = 'ASAP' AND IFNULL(pli.shelved, 0) = 0"
		" AND IFNULL(pli.issued, 0) = 0 AND IFNULL(pli.custom_ready_for_packing, 0) = 0 THEN " + BKT + " END",
	}
	return ", ".join("COUNT(DISTINCT {0}) AS {1}".format(v, k) for k, v in parts.items())


COUNT_KEYS = (
	"total",
	"awaiting",
	"trolley",
	"transit",
	"shelved",
	"ready",
	"issued",
	"issued_offline",
	"not_found",
	"flag_shelved",
	"asap",
)


def _stage_split(r):
	"""Exclusive stages from the cumulative 'reached' counts."""
	r["at_farm"] = max(0, r["awaiting"] - r["trolley"])
	r["in_trolley"] = max(0, r["trolley"] - r["transit"])
	r["on_road"] = max(0, r["transit"] - r["shelved"])
	r["other"] = max(0, r["total"] - r["awaiting"])


@frappe.whitelist()
def get_logistics(from_date=None, to_date=None, region=None, farm=None, team=None, q=None):
	"""Bucket Logistics: every order with buckets being transferred for a delivery-date
	range, per origin farm. KPIs are computed here over exactly the rows returned."""
	f, t = _dates(from_date, to_date)
	hub, sales = _sales_farms()
	sales_t = tuple(sales) or ("",)
	region_farms = region_core.farms_for(region=region, farm=farm)
	base = {"f": f, "t": t, "sales_farms": sales_t}

	conds = [
		"opl.docstatus < 2",
		"pli.parenttype = 'Order Pick List'",
		"so.delivery_date BETWEEN %(f)s AND %(t)s",
		TRANSFER,
		"COALESCE(" + ORIGIN + ", '') NOT IN %(sales_farms)s",
	]
	params = dict(base)
	if region_farms is not None:
		conds.append(ORIGIN + " IN %(farms)s")
		params["farms"] = region_core.sql_tuple(region_farms)
	if q:
		conds.append(
			"CONCAT_WS(' ', opl.order_name, so.customer, opl.sales_order, opl.name, pli.item_code) LIKE %(q)s"
		)
		params["q"] = _like(q)

	farm_rows = frappe.db.sql(
		"""
		SELECT opl.name AS opl, opl.order_name AS order_name, opl.sales_order AS sales_order,
		       so.customer AS customer, so.delivery_date AS delivery_date, opl.creation AS initiated,
		       opl.team AS opl_team, """
		+ ORIGIN
		+ """ AS farm,
		       GROUP_CONCAT(DISTINCT pli.item_code ORDER BY pli.item_code SEPARATOR ', ') AS varieties,
		       MAX(CASE WHEN """
		+ TRANSFER_TRUCK_OK
		+ """ THEN pli.transit_truck END) AS truck,
		       """
		+ _counts_sql()
		+ """
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE """
		+ " AND ".join(conds)
		+ """
		GROUP BY opl.name, opl.order_name, opl.sales_order, so.customer, so.delivery_date, opl.creation,
		         opl.team, """
		+ ORIGIN
		+ """
		ORDER BY opl.order_name, farm""",
		params,
		as_dict=True,
	)

	# Merge the (opl, farm) rows into one row per order.
	orders, index = [], {}
	for r in farm_rows:
		for k in COUNT_KEYS:
			r[k] = int(r.get(k) or 0)
		_stage_split(r)
		o = index.get(r.opl)
		if o is None:
			o = index[r.opl] = {
				"opl": r.opl,
				"order_name": r.order_name,
				"sales_order": r.sales_order,
				"customer": r.customer,
				"delivery_date": str(r.delivery_date),
				"_initiated": r.initiated,
				"initiated": str(r.initiated) if r.initiated else "",
				"opl_team": r.opl_team,
				"truck": None,
				"varieties": [],
				"by_farm": [],
				**{k: 0 for k in COUNT_KEYS},
				"at_farm": 0,
				"in_trolley": 0,
				"on_road": 0,
				"other": 0,
			}
			orders.append(o)
		o["truck"] = o["truck"] or r.truck
		for v in (r.varieties or "").split(", "):
			if v and v not in o["varieties"]:
				o["varieties"].append(v)
		for k in COUNT_KEYS + ("at_farm", "in_trolley", "on_road", "other"):
			o[k] += r[k]
		o["by_farm"].append(
			{
				"farm": r.farm or "",
				"varieties": r.varieties or "",
				**{k: r[k] for k in COUNT_KEYS + ("at_farm", "in_trolley", "on_road", "other")},
			}
		)
	for o in orders:
		o["varieties"] = ", ".join(o["varieties"])
		o["farm"] = ", ".join(x["farm"] for x in o["by_farm"])

	opl_names = tuple(o["opl"] for o in orders)
	sched = _schedule_map()
	trips, at_hub, arrivals = {}, {}, {}
	if opl_names:
		for tr in frappe.db.sql(
			"""
			SELECT o.order_pick_list AS opl, t.name AS trip, t.vehicle AS vehicle, t.status AS status,
			       t.trip_date AS trip_date, SUM(o.buckets) AS buckets, SUM(o.loaded_buckets) AS loaded_buckets,
			       t.run AS run, t.route AS route, t.arrived_at AS arrived_at, IFNULL(o.farm, '') AS farm
			FROM `tabBucket Request Trip Order` o
			JOIN `tabBucket Request Trip` t ON t.name = o.parent
			WHERE o.order_pick_list IN %(opls)s
			  AND (t.trip_date = %(today)s OR (t.trip_date >= %(since)s AND t.status != 'Received'))
			GROUP BY o.order_pick_list, t.name, IFNULL(o.farm, '')
			ORDER BY t.trip_date DESC, t.vehicle, t.run, IFNULL(o.farm, ''), t.name""",
			{"opls": opl_names, "today": today(), "since": add_days(today(), -TRIP_LOOKBACK_DAYS)},
			as_dict=True,
		):
			trips.setdefault(tr.opl, []).append(
				{
					"trip": tr.trip,
					"vehicle": tr.vehicle,
					"status": tr.status,
					"trip_date": str(tr.trip_date),
					"buckets": int(tr.buckets or 0),
					"loaded_buckets": int(tr.loaded_buckets or 0),
					"run": int(tr.run or 0),
					"route": tr.route or "",
					"farm": tr.farm,
					"arrived_at": str(tr.arrived_at or ""),
				}
			)
		for x in frappe.db.sql(
			"""
			SELECT pli.parent AS opl, """
			+ ORIGIN
			+ """ AS src, COUNT(DISTINCT """
			+ BKT
			+ """) AS n
			FROM `tabBucket Request Trip Bucket` tb
			JOIN `tabBucket Request Trip` t ON t.name = tb.parent
			JOIN `tabPick List Item` pli ON pli.parent = tb.order_pick_list AND pli.parenttype = 'Order Pick List'
			     AND pli.bucket = tb.bucket
			WHERE tb.parenttype = 'Bucket Request Trip' AND tb.order_pick_list IN %(opls)s
			  AND t.arrived_at IS NOT NULL AND IFNULL(tb.off_truck, 0) = 0 AND IFNULL(pli.shelved, 0) = 0
			GROUP BY pli.parent, src""",
			{"opls": opl_names, "sales_farms": sales_t},
			as_dict=True,
		):
			at_hub[(x.opl, x.src or "")] = int(x.n or 0)
		# Arrival = the first shelving of the order's buckets at the order's own farm,
		# from the Shelving Log (a bucket's latest entry only, after the OPL existed).
		for a in frappe.db.sql(
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
			WHERE pli.parenttype = 'Order Pick List' AND sl.farm = o2.farm
			  AND COALESCE(sl.shelved_on, sl.creation) >= o2.creation AND pli.parent IN %(opls)s
			GROUP BY pli.parent""",
			{"opls": opl_names},
			as_dict=True,
		):
			arrivals[a["opl"]] = a["arrival"]

	for o in orders:
		sc = sched.get(o["opl"]) or {}
		o["team"] = sc.get("team") or o["opl_team"] or ""
		o["schedule"] = sc.get("schedule")
		o["scheduled"] = 1 if sc else 0
		o["no_team"] = 0 if o["team"] else 1
		o["hub"] = hub
		o["trips"] = trips.get(o["opl"], [])
		for b in o["by_farm"]:
			b["arrived_hub"] = at_hub.get((o["opl"], b["farm"]), 0) + b["shelved"]
		o["arrived_hub"] = sum(b["arrived_hub"] for b in o["by_farm"])
		for tr in o["trips"]:
			tr["shelved"] = next((b["flag_shelved"] for b in o["by_farm"] if b["farm"] == tr["farm"]), 0)
		a = arrivals.get(o["opl"])
		o["arrived"] = str(a) if a else ""
		o["_arrived"] = a
	if team:
		orders = [o for o in orders if o["team"] == team]

	# KPIs: one pass over exactly the orders returned.
	k = {
		"orders": len(orders),
		"buckets": 0,
		"at_farm": 0,
		"in_trolley": 0,
		"on_road": 0,
		"shelved": 0,
		"other": 0,
		"issued": 0,
	}
	unsched = no_trip = 0
	durs = []
	for o in orders:
		for key in ("at_farm", "in_trolley", "on_road", "shelved", "other", "issued"):
			k[key] += o[key]
		k["buckets"] += o["total"]
		if o["at_farm"] > 0:
			if not o["scheduled"]:
				unsched += 1
			elif not any(x["status"] != "Received" for x in o["trips"]):
				no_trip += 1
		if o["_arrived"] and o["_initiated"]:
			durs.append(
				max(0, (get_datetime(o["_arrived"]) - get_datetime(o["_initiated"])).total_seconds() / 60)
			)
	k["not_scheduled"] = unsched
	k["no_trip"] = unsched + no_trip
	k["without_trip"] = no_trip
	k["avg_transit_minutes"] = round(sum(durs) / len(durs), 1) if durs else None
	k["arrived_orders"] = len(durs)
	for o in orders:
		o.pop("_initiated", None)
		o.pop("_arrived", None)

	# Origin farms available (same range + region, not narrowed by the farm / team / search).
	farm_conds = [c for c in conds if "IN %(farms)s" not in c and "%(q)s" not in c]
	farm_params = {k_: v for k_, v in params.items() if k_ in ("f", "t", "sales_farms")}
	if region_farms is not None and not farm:
		farm_conds.append(ORIGIN + " IN %(farms)s")
		farm_params["farms"] = region_core.sql_tuple(region_farms)
	farms = [
		r[0]
		for r in frappe.db.sql(
			"SELECT DISTINCT "
			+ ORIGIN
			+ " AS f FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name = pli.parent"
			" JOIN `tabSales Order` so ON so.name = opl.sales_order WHERE "
			+ " AND ".join(farm_conds)
			+ " ORDER BY 1",
			farm_params,
		)
		if r[0]
	]

	# Every run the trucks drive today (truck cards). Trucks are today's, not date-ranged.
	run_trips = set(frappe.get_all("Bucket Request Trip", filters={"trip_date": today()}, pluck="name"))
	run_trips |= set(
		frappe.get_all(
			"Bucket Request Trip",
			filters={
				"trip_date": [">=", add_days(today(), -TRIP_LOOKBACK_DAYS)],
				"status": ["!=", "Received"],
			},
			pluck="name",
		)
	)
	keep = (
		"name vehicle trip_date status loading stale run runs run_chain window total_buckets loaded_buckets"
		" tracked_buckets shelved_buckets departed_stops heading_to dispatched_at received_at"
	).split()
	runs = []
	for name in run_trips:
		td = _trip_dict(frappe.get_doc("Bucket Request Trip", name), today())
		runs.append({x: td[x] for x in keep})
	runs.sort(key=lambda x: (x["vehicle"] or "", x["trip_date"], x["run"] or 99, x["name"]))

	note = ""
	if region_farms is not None and not region_farms:
		note = "That region and farm do not overlap, so nothing matches."
	elif region_farms is not None and not (set(region_farms) - sales):
		note = (
			"No remote transfers: "
			+ ", ".join(region_farms)
			+ " packs its own orders, so nothing is trucked."
		)
	return {
		"success": True,
		"from_date": f,
		"to_date": t,
		"orders": orders,
		"farms": farms,
		"kpis": k,
		"runs": runs,
		"note": note,
	}


@frappe.whitelist()
def get_bucket_overview(region=None, farm=None):
	"""Bucket Journey overview (current state, no date): buckets in use and shelves,
	scoped to a region / farm. A bucket's farm is its shelf's farm (on a shelf), its
	origin farm (on a truck) or the harvest farm (still being harvested). Empty
	buckets belong to no farm and are shown for all farms."""
	from upande_packhouse.api.remote_transfer.bucket_journey import TRUCK_LOOKBACK_DAYS

	farms = region_core.farms_for(region=region, farm=farm)
	if farms is not None and not farms:
		return {
			"buckets": {
				"total": frappe.db.count("Bucket QR Code"),
				"in_use": 0,
				"empty": 0,
				"harvesting": 0,
				"on_shelf": 0,
				"on_truck": 0,
			},
			"shelves": {"total": 0, "with_buckets": 0, "free": 0},
			"farms": [],
			"missing": 0,
			"scoped": True,
		}
	_hub, sales = _sales_farms()
	sales_t = tuple(sales) or ("",)
	shelf = {}
	for r in frappe.db.sql(
		"""SELECT UPPER(si.bucket_id) AS b, MIN(sh.farm) AS farm FROM `tabShelf Item` si
		JOIN `tabShelf` sh ON sh.name = si.parent WHERE IFNULL(si.bucket_id, '') != '' GROUP BY UPPER(si.bucket_id)""",
		as_dict=True,
	):
		shelf[r.b] = r.farm or ""
	truck = {}
	for r in frappe.db.sql(
		"""SELECT UPPER(pli.bucket) AS b, MIN("""
		+ ORIGIN
		+ """) AS farm FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.in_transit = 1 AND IFNULL(pli.shelved, 0) = 0
		  AND IFNULL(pli.bucket, '') != '' AND pli.modified >= %(since)s GROUP BY UPPER(pli.bucket)""",
		{"since": add_days(today(), -TRUCK_LOOKBACK_DAYS), "sales_farms": sales_t},
		as_dict=True,
	):
		truck[r.b] = r.farm or ""
	harvest = {}
	for r in frappe.db.sql(
		"""SELECT UPPER(q.name) AS b, se.farm FROM `tabBucket QR Code` q
		LEFT JOIN `tabStock Entry` se ON se.name = q.last_stock_entry WHERE q.status = 'In Use'""",
		as_dict=True,
	):
		harvest[r.b] = r.farm or ""
	on_truck = set(truck)
	on_shelf = set(shelf) - on_truck
	harvesting = set(harvest) - set(shelf) - on_truck

	def keep(f):
		return farms is None or f in farms

	n_truck = sum(1 for b in on_truck if keep(truck[b]))
	n_shelf = sum(1 for b in on_shelf if keep(shelf[b]))
	n_harv = sum(1 for b in harvesting if keep(harvest[b]))
	total = frappe.db.count("Bucket QR Code")
	known = len(on_truck | on_shelf | harvesting)

	shelf_conds, sp = "", {}
	if farms is not None:
		shelf_conds = "WHERE COALESCE(NULLIF(sh.farm, ''), '') IN %(farms)s"
		sp["farms"] = region_core.sql_tuple(farms)
	farm_rows = frappe.db.sql(
		"""SELECT COALESCE(NULLIF(sh.farm, ''), '') AS farm, COUNT(*) AS shelves,
		       SUM(CASE WHEN x.buckets > 0 THEN 1 ELSE 0 END) AS with_buckets, COALESCE(SUM(x.buckets), 0) AS buckets
		FROM `tabShelf` sh
		LEFT JOIN (SELECT parent, COUNT(DISTINCT UPPER(bucket_id)) AS buckets FROM `tabShelf Item`
		           WHERE IFNULL(bucket_id, '') != '' GROUP BY parent) x ON x.parent = sh.name
		"""
		+ shelf_conds
		+ " GROUP BY COALESCE(NULLIF(sh.farm, ''), '') ORDER BY farm",
		sp,
		as_dict=True,
	)
	for fr in farm_rows:
		fr["with_buckets"] = int(fr.with_buckets or 0)
		fr["buckets"] = int(fr.buckets or 0)
		fr["free"] = int(fr.shelves) - fr["with_buckets"]
	mf = {"status": "Open"}
	if farms is not None:
		mf["farm"] = ["in", list(farms)]
	return {
		"buckets": {
			"total": total,
			"in_use": n_truck + n_shelf + n_harv,
			"empty": max(0, total - known),
			"harvesting": n_harv,
			"on_shelf": n_shelf,
			"on_truck": n_truck,
		},
		"shelves": {
			"total": sum(int(x.shelves) for x in farm_rows),
			"with_buckets": sum(x["with_buckets"] for x in farm_rows),
			"free": sum(x["free"] for x in farm_rows),
		},
		"farms": farm_rows,
		"missing": frappe.db.count("Bucket Replacement", mf),
		"scoped": farms is not None,
	}


@frappe.whitelist()
def get_opl_stems(opls=None):
	"""Stems on each pick list, split by origin farm: {opl: {farm: stems}}.

	Order Pick List.custom_total_stems is stale (README rule 8), so the Scheduler's
	stem totals come from the pick list rows themselves (Pick List Item.stock_qty)."""
	if isinstance(opls, str):
		opls = frappe.parse_json(opls)
	if not opls:
		return {}
	_hub, sales = _sales_farms()
	out = {}
	for r in frappe.db.sql(
		"""SELECT pli.parent AS opl, COALESCE("""
		+ ORIGIN
		+ """, '') AS farm, SUM(pli.stock_qty) AS stems
		FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s
		GROUP BY pli.parent, farm""",
		{"opls": tuple(opls), "sales_farms": tuple(sales) or ("",)},
		as_dict=True,
	):
		out.setdefault(r.opl, {})[r.farm] = float(r.stems or 0)
	return out


@frappe.whitelist()
def get_buckets_in_use(region=None, farm=None, kind="all"):
	"""Buckets in use and where they are (the Bucket Journey drawer), scoped to a region /
	farm, one row per bucket and place, NOT capped (v1 cut the list at 1,000 so it
	disagreed with the KPI). The count equals get_bucket_overview's in_use."""
	from upande_packhouse.api.remote_transfer.bucket_journey import TRUCK_LOOKBACK_DAYS, _in_use_sets

	farms = region_core.farms_for(region=region, farm=farm)
	if farms is not None and not farms:
		return []
	_hub, sales = _sales_farms()
	harvesting, on_shelf, on_truck = _in_use_sets()
	fp = {"farms": region_core.sql_tuple(farms)} if farms is not None else {}
	out = []
	if kind in ("all", "shelf") and on_shelf:
		out += [
			{**r, "where": "shelf"}
			for r in frappe.db.sql(
				"""SELECT si.bucket_id AS bucket, si.parent AS shelf, sh.farm, si.variety, si.stem_length,
				       si.stem_qty AS stems, si.date_added AS since
				FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
				WHERE UPPER(si.bucket_id) IN %(ids)s"""
				+ (" AND sh.farm IN %(farms)s" if farms is not None else "")
				+ " ORDER BY si.date_added",
				{"ids": tuple(on_shelf), **fp},
				as_dict=True,
			)
		]
	if kind in ("all", "truck") and on_truck:
		out += [
			{**r, "where": "truck"}
			for r in frappe.db.sql(
				"""SELECT pli.bucket, pli.transit_truck AS truck, pli.parent AS opl, pli.item_code AS variety,
				       pli.stock_qty AS stems, pli.modified AS since, COALESCE("""
				+ ORIGIN
				+ """, '') AS farm
				FROM `tabPick List Item` pli
				WHERE pli.parenttype = 'Order Pick List' AND pli.in_transit = 1 AND IFNULL(pli.shelved, 0) = 0
				  AND UPPER(pli.bucket) IN %(ids)s AND pli.modified >= %(since)s"""
				+ (" AND " + ORIGIN + " IN %(farms)s" if farms is not None else "")
				+ " ORDER BY pli.modified",
				{
					"ids": tuple(on_truck),
					"since": add_days(today(), -TRUCK_LOOKBACK_DAYS),
					"sales_farms": tuple(sales) or ("",),
					**fp,
				},
				as_dict=True,
			)
		]
	if kind in ("all", "harvesting") and harvesting:
		out += [
			{**r, "where": "harvesting"}
			for r in frappe.db.sql(
				"""SELECT q.name AS bucket, se.farm, se.custom_greenhouse AS greenhouse, se.custom_stem_length AS stem_length,
				       (SELECT d.item_code FROM `tabStock Entry Detail` d WHERE d.parent = se.name ORDER BY d.idx LIMIT 1) AS variety,
				       (SELECT SUM(d.qty) FROM `tabStock Entry Detail` d WHERE d.parent = se.name) AS stems,
				       se.posting_date AS since
				FROM `tabBucket QR Code` q LEFT JOIN `tabStock Entry` se ON se.name = q.last_stock_entry
				WHERE UPPER(q.name) IN %(ids)s"""
				+ (" AND se.farm IN %(farms)s" if farms is not None else "")
				+ " ORDER BY se.posting_date",
				{"ids": tuple(harvesting), **fp},
				as_dict=True,
			)
		]
	merged = {}
	for r in out:
		key = (r["where"], (r.get("bucket") or "").upper())
		m = merged.get(key)
		if m is None:
			merged[key] = {**r, "variety": r.get("variety") or "", "stems": float(r.get("stems") or 0)}
			continue
		m["stems"] += float(r.get("stems") or 0)
		if r.get("variety") and r["variety"] not in m["variety"].split(", "):
			m["variety"] = ", ".join(v for v in (m["variety"], r["variety"]) if v)
	return list(merged.values())
