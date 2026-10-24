# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Cold room capacity: how full is each cold store, in buckets.

  occupied buckets   DISTINCT buckets on a shelf, taken from the rows of
                     core/stock.bucket_rows (the page's one stock definition;
                     held and allocated buckets still occupy a shelf). Never a
                     second query of its own for "what is on the shelf".
  capacity (buckets) per cold store (= the shelf's farm), first that exists:
                       1. Cold Store Capacity.max_buckets          (explicit)
                       2. Cold Store Capacity.max_shelves x BUCKETS_PER_SHELF
                       3. registered Shelf rows x BUCKETS_PER_SHELF
                     `capacity_source` says which one was used.

BUCKETS_PER_SHELF = 2 is not stored on any doctype: it is the rule the shelving
API enforces (mobile/api.py shelveBucket, "two_buckets_per_shelf": a third
distinct bucket on a shelf is refused). Legacy data holds shelves with 3-4
buckets; they show as "over a shelf's capacity" and are counted, not hidden.

Alert levels are one constant (CAPACITY_LEVELS). Capacity is a LIVE position, so
the page's date range does not apply here; region and farm do.
"""

from collections import defaultdict
from typing import Any

import frappe

from upande_packhouse.api.v2.core import region as region_mod
from upande_packhouse.api.v2.core import stock

BUCKETS_PER_SHELF = 2

# (minimum % full, key, label), highest first. Change the numbers here only.
CAPACITY_LEVELS = (
	(100.0, "full", "Full / over capacity"),
	(90.0, "almost", "Almost full"),
	(80.0, "getting", "Getting full"),
)
LEVEL_NORMAL = ("normal", "Normal")
SHELF_LIST_LIMIT = 5000  # drilldowns page client-side; keeps their rows adding up to the gauge


def level_for(pct):
	"""(key, label) for a % full; unknown when there is no capacity to compare to."""
	if pct is None:
		return "unknown", "No capacity set"
	for floor, key, label in CAPACITY_LEVELS:
		if pct >= floor:
			return key, label
	return LEVEL_NORMAL


def gauge(occupied, capacity):
	"""One gauge's numbers. pct is not rounded here, so a 89.96% store is not
	promoted to "Almost full" by display rounding."""
	occupied = int(occupied or 0)
	capacity = int(capacity or 0)
	pct = (occupied / capacity * 100.0) if capacity > 0 else None
	key, label = level_for(pct)
	return {
		"occupied": occupied,
		"capacity": capacity,
		"free": max(0, capacity - occupied),
		"over_by": max(0, occupied - capacity) if capacity > 0 else 0,
		"pct": None if pct is None else round(pct, 1),
		"level": key,
		"level_label": label,
	}


def _store_config(farms):
	"""farm -> Cold Store Capacity row (the doctype is not in every site)."""
	if not frappe.db.table_exists("Cold Store Capacity"):
		return {}
	params = {}
	cond = ""
	if farms is not None:
		cond = " AND farm IN %(farms)s"
		params["farms"] = region_mod.sql_tuple(farms)
	rows = frappe.db.sql(
		f"SELECT farm, max_shelves, max_buckets FROM `tabCold Store Capacity` WHERE 1=1{cond}",
		params,
		as_dict=True,
	)  # nosemgrep: fixed SQL fragment, bound values
	return {r.farm: r for r in rows}


def _shelf_counts(farms):
	"""farm -> registered shelves (None farm = shelves with no farm)."""
	params = {}
	cond = ""
	if farms is not None:
		cond = " AND farm IN %(farms)s"
		params["farms"] = region_mod.sql_tuple(farms)
	return {
		(f or None): int(n)
		for f, n in frappe.db.sql(
			f"SELECT NULLIF(farm, ''), COUNT(*) FROM `tabShelf` WHERE 1=1{cond} GROUP BY NULLIF(farm, '')",
			params,
		)
	}  # nosemgrep: fixed SQL fragment, bound values


def capacity_for(farm, shelves, cfg):
	"""(capacity in buckets, source, shelves that capacity is based on)."""
	c = cfg.get(farm)
	if c and int(c.max_buckets or 0) > 0:
		return int(c.max_buckets), "Cold Store Capacity (max buckets)", shelves
	if c and int(c.max_shelves or 0) > 0:
		return int(c.max_shelves) * BUCKETS_PER_SHELF, "Cold Store Capacity (max shelves)", int(c.max_shelves)
	return shelves * BUCKETS_PER_SHELF, "Registered shelves", shelves


def shelf_states(occ, registered):
	"""Shelves by state for one cold store. `occ` = shelf_occupancy entries of the
	store, `registered` = its registered shelves. full = BUCKETS_PER_SHELF buckets,
	over = more than that (legacy data), partial = fewer, empty = nothing on it.
	This is the "pressure" of a store: how many of its shelves have no room left."""
	n = [len(v["buckets"]) for v in occ]
	full = sum(1 for x in n if x == BUCKETS_PER_SHELF)
	over = sum(1 for x in n if x > BUCKETS_PER_SHELF)
	partial = sum(1 for x in n if 0 < x < BUCKETS_PER_SHELF)
	empty = max(0, registered - len(n))
	at_cap = full + over
	return {
		"shelves_full": full,
		"shelves_over": over,
		"shelves_partial": partial,
		"shelves_empty": empty,
		"shelves_in_use": len(n),
		"shelves_full_pct": round(at_cap / registered * 100.0, 1) if registered else None,
	}


def build(rows, farms, incoming=None):
	"""Capacity block for get_cold_room. `rows` = the live stock rows of the
	farm scope (core/stock.bucket_rows, NOT date-limited). `incoming` = farm ->
	buckets received but not yet shelved (the page's one "Received, not shelved"
	definition), compared with each store's free bucket slots."""
	incoming = incoming or {}
	occ_by_farm = defaultdict(list)
	for sh in shelf_occupancy(rows, farms).values():
		occ_by_farm[sh["farm"] or None].append(sh)
	by_farm = {g.shelf_farm: g for g in stock.summarize(rows, by=("shelf_farm",))}
	shelf_counts = _shelf_counts(farms)
	cfg = _store_config(farms)
	names = set(by_farm) | set(shelf_counts) | set(cfg)
	stores = []
	for f in sorted(names, key=lambda x: (x is None, x or "")):
		shelves = shelf_counts.get(f, 0)
		cap, source, cap_shelves = capacity_for(f, shelves, cfg)
		g = by_farm.get(f)
		row = gauge(g.buckets if g else 0, cap)
		row.update(
			{
				"farm": f or "No farm",
				"farm_key": f or "",
				"shelves": shelves,
				"capacity_shelves": cap_shelves,
				"capacity_source": source,
				**shelf_states(occ_by_farm.get(f, []), shelves),
				"incoming_buckets": int(incoming.get(f, 0)),
			}
		)
		row["incoming_exceeds_free"] = row["incoming_buckets"] > row["free"]
		stores.append(row)
	# Total occupied = the page's distinct-bucket count (stock.totals), so the gauge
	# agrees with every "buckets" figure. A bucket id live in two farms (id reuse)
	# is one bucket here but counted in each store above; `shared_buckets` says how many.
	distinct = stock.totals(rows).buckets
	total = gauge(distinct, sum(s["capacity"] for s in stores))
	total["shared_buckets"] = sum(s["occupied"] for s in stores) - distinct
	total["shelves"] = sum(s["shelves"] for s in stores)
	for k in (
		"shelves_full",
		"shelves_over",
		"shelves_partial",
		"shelves_empty",
		"shelves_in_use",
		"incoming_buckets",
	):
		total[k] = sum(s[k] for s in stores)
	at_cap = total["shelves_full"] + total["shelves_over"]
	total["shelves_full_pct"] = round(at_cap / total["shelves"] * 100.0, 1) if total["shelves"] else None
	total["incoming_exceeds_free"] = total["incoming_buckets"] > total["free"]
	worst = max((s for s in stores if s["pct"] is not None), key=lambda s: s["pct"], default=None)
	return {
		"buckets_per_shelf": BUCKETS_PER_SHELF,
		"levels": [{"from_pct": a, "key": b, "label": c} for a, b, c in CAPACITY_LEVELS],
		"stores": stores,
		"total": total,
		"worst": worst["farm"] if worst else None,
		"alerts": [s["farm"] for s in stores if s["level"] in ("getting", "almost", "full")],
	}


def shelf_occupancy(rows, farms):
	"""shelf -> {farm, buckets: {bucket_id: {stems, varieties}}} for the buckets
	in `rows` (the stock definition's buckets), by live Shelf Item position."""
	bucket_ids = {r.bucket_id for r in rows}
	if not bucket_ids:
		return {}
	params = {"b": tuple(bucket_ids)}
	cond = ""
	if farms is not None:
		cond = " AND s.farm IN %(farms)s"
		params["farms"] = region_mod.sql_tuple(farms)
	out = {}
	for shelf, farm, bucket, variety, stems in frappe.db.sql(
		f"""SELECT s.name, s.farm, si.bucket_id, si.variety, SUM(si.stem_qty)
		    FROM `tabShelf Item` si INNER JOIN `tabShelf` s ON s.name = si.parent
		    WHERE si.stem_qty > 0 AND IFNULL(si.bucket_id, '') != '' AND IFNULL(si.variety, '') != ''
		      AND si.bucket_id IN %(b)s{cond}
		    GROUP BY s.name, s.farm, si.bucket_id, si.variety""",
		params,
	):  # nosemgrep: fixed SQL fragment, bound values
		sh = out.setdefault(shelf, {"farm": farm, "buckets": {}})
		b = sh["buckets"].setdefault(bucket, {"stems": 0.0, "varieties": set()})
		b["stems"] += float(stems or 0)
		b["varieties"].add(variety)
	return out


def _registered_shelves(farms, only_farm=None):
	params = {}
	cond = ""
	if farms is not None:
		cond = " AND farm IN %(farms)s"
		params["farms"] = region_mod.sql_tuple(farms)
	if only_farm is not None:
		cond += " AND IFNULL(farm, '') = %(only)s"
		params["only"] = only_farm
	return frappe.db.sql(
		f"SELECT name, NULLIF(farm, '') AS farm FROM `tabShelf` WHERE 1=1{cond}", params, as_dict=True
	)  # nosemgrep: fixed SQL fragment, bound values


def shelf_list(rows, farms, limit=SHELF_LIST_LIMIT, only_farm=None, include_empty=False):
	"""Fullest shelves first. Counts cover every shelf in scope; only the list is
	cut. `only_farm` narrows to one cold store ("" = shelves with no farm).
	`include_empty` adds every registered shelf with nothing on it (0 buckets), so
	the list adds up to the "Total shelves available" figure."""
	occ = shelf_occupancy(rows, farms)
	if only_farm is not None:
		occ = {k: v for k, v in occ.items() if (v["farm"] or "") == only_farm}
	items = []
	for name, sh in occ.items():
		n = len(sh["buckets"])
		g = gauge(n, BUCKETS_PER_SHELF)
		items.append(
			{
				"shelf": name,
				"farm": sh["farm"],
				"buckets": n,
				"capacity": BUCKETS_PER_SHELF,
				"free": g["free"],
				"over_by": g["over_by"],
				"pct": g["pct"],
				"level": g["level"],
				"stems": sum(b["stems"] for b in sh["buckets"].values()),
				"bucket_list": [
					{"bucket_id": bid, "stems": b["stems"], "varieties": ", ".join(sorted(b["varieties"]))}
					for bid, b in sorted(sh["buckets"].items())
				],
			}
		)
	if include_empty:
		used = {i["shelf"] for i in items}
		for sh in _registered_shelves(farms, only_farm):
			if sh.name not in used:
				items.append(
					{"shelf": sh.name, "farm": sh.farm, "buckets": 0, "capacity": BUCKETS_PER_SHELF,
					 "free": BUCKETS_PER_SHELF, "over_by": 0, "pct": 0.0, "level": "normal", "stems": 0.0,
					 "bucket_list": []}
				)  # fmt: skip
	items.sort(key=lambda x: (-x["buckets"], -x["stems"], x["shelf"]))
	counts = _shelf_counts(farms)
	registered = sum(counts.values()) if only_farm is None else counts.get(only_farm or None, 0)
	occupied = sum(1 for i in items if i["buckets"] > 0)
	if include_empty:
		limit = max(limit, len(items))
	return {
		"shelves": items[:limit],
		"truncated": len(items) > limit,
		"occupied_shelves": occupied,
		"registered_shelves": registered,
		"empty_shelves": max(0, registered - occupied),
		"full_shelves": sum(1 for i in items if i["buckets"] == BUCKETS_PER_SHELF),
		"over_shelves": sum(1 for i in items if i["buckets"] > BUCKETS_PER_SHELF),
		"slots_used": sum(i["buckets"] for i in items),
	}


@frappe.whitelist()
def get_cold_room_shelves(
	region: Any = None,
	farm: Any = None,
	from_date: Any = None,
	to_date: Any = None,
	shelf_farm: Any = None,
	include_empty: Any = None,
):
	"""Shelf-level view behind the gauges (fullest first). Region / farm follow
	the page filters; dates do not apply to a live capacity position. `shelf_farm`
	narrows the list (and its counts) to one cold store, so a small store is not
	crowded out of the fullest-N list by a bigger one. `include_empty` also lists
	the registered shelves with nothing on them (the "Total shelves" drilldown)."""
	from upande_packhouse.api.v2.cold_room import _scope

	try:
		sc = _scope(region, farm, from_date, to_date)
		only = None if shelf_farm is None or shelf_farm == "null" else str(shelf_farm)
		inc = str(include_empty).lower() in ("1", "true", "yes")
		if sc.farms == []:
			return {"success": True, **shelf_list([], sc.farms, only_farm=only, include_empty=inc)}
		rows = stock.bucket_rows(farms=sc.farms)
		return {"success": True, **shelf_list(rows, sc.farms, only_farm=only, include_empty=inc)}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Cold Room v2: get_cold_room_shelves")
		return {"success": False, "error": "Could not load shelf capacity. The error was logged."}
