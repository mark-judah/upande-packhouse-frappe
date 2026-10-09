# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Bucket Count v2 - the empty-bucket count (Bucket Count / Bucket Count Bucket / Bucket
Count Location, all in upande_quality; written by the app's syncBucketCountScans).

One Bucket Count per farm per day; one row per distinct bucket, carrying the place it was
LAST seen that day. This page answers: how many empty buckets did each farm count, where
were they, and how many of the buckets in the system have not been counted yet.

Numbers
    Counted        distinct buckets in each farm's LATEST count inside the date range
    Marked in use  counted buckets the system currently shows as In Use (not empty)
    In the system  every Bucket QR Code
    Not yet counted  buckets in the system never counted in the range. System-wide only:
                   a bucket has no home farm, so the figure is not given when a farm,
                   region or location filter is set.

Filters: from/to over count_date, region / farm (Bucket Count.farm), location (the row's).
"""

import re
from collections import defaultdict

import frappe
from frappe.utils import cint

from upande_packhouse.api.v2.core import region as region_core

ROW_LIMIT = 3000
_ID = re.compile(r'"([^"]+)"')


def _short(name):
	"""Bucket QR Code names look like { "0052da": "bucket" }; show the id."""
	m = _ID.search(name or "")
	return m.group(1) if m else (name or "")


def _args(kw):
	g = lambda k: str(kw.get(k) or "").strip()  # noqa: E731
	return {
		"from_date": g("from_date") or None,
		"to_date": g("to_date") or None,
		"region": g("region"),
		"farm": g("farm"),
		"location": g("location"),
	}


def _scans(a):
	cond = ["bc.docstatus < 2"]
	p = {}
	if a["from_date"]:
		cond.append("bc.count_date >= %(f)s")
		p["f"] = a["from_date"]
	if a["to_date"]:
		cond.append("bc.count_date <= %(t)s")
		p["t"] = a["to_date"]
	farms = region_core.farms_for(region=a["region"], farm=a["farm"])
	if farms is not None:
		cond.append("bc.farm IN %(farms)s")
		p["farms"] = region_core.sql_tuple(farms)
	if a["location"]:
		cond.append("b.location = %(loc)s")
		p["loc"] = a["location"]
	return frappe.db.sql(
		f"""SELECT bc.name AS count_name, bc.farm, bc.count_date, b.bucket_id, b.location, b.scanned_at,
		       q.status AS system_status
		FROM `tabBucket Count` bc
		JOIN `tabBucket Count Bucket` b ON b.parent = bc.name
		LEFT JOIN `tabBucket QR Code` q ON q.name = b.bucket_id
		WHERE {" AND ".join(cond)}
		ORDER BY bc.count_date DESC, bc.farm, b.scanned_at DESC""",
		p,
		as_dict=True,
	)  # nosemgrep: fixed fragments, values bound


@frappe.whitelist()
def get_bucket_count(**kw):
	"""Everything the page shows for the filters."""
	a = _args(kw)
	rows = _scans(a)

	latest = {}  # farm -> its most recent count date in range
	for r in rows:
		if r.farm not in latest or r.count_date > latest[r.farm]:
			latest[r.farm] = r.count_date
	current = [r for r in rows if r.count_date == latest[r.farm]]

	by_loc = defaultdict(int)
	for r in current:
		by_loc[r.location] += 1
	locations = [r.name for r in frappe.get_all("Bucket Count Location", fields=["name"], order_by="creation asc, name asc")]
	by_location = [{"location": loc, "buckets": by_loc.get(loc, 0)} for loc in locations]
	by_location += [{"location": loc, "buckets": n} for loc, n in by_loc.items() if loc not in locations]

	farm_rows = {}
	for r in current:
		f = farm_rows.setdefault(
			r.farm, {"farm": r.farm, "date": str(r.count_date), "buckets": 0, "in_use": 0, "locations": defaultdict(int)}
		)
		f["buckets"] += 1
		f["in_use"] += 1 if r.system_status == "In Use" else 0
		f["locations"][r.location] += 1
	by_farm = sorted(
		({**f, "locations": dict(f["locations"])} for f in farm_rows.values()), key=lambda x: (-x["buckets"], x["farm"])
	)

	per_day = defaultdict(int)  # buckets counted per day, all farms in scope
	for r in rows:
		per_day[str(r.count_date)] += 1
	trend = [{"date": d, "buckets": per_day[d]} for d in sorted(per_day)]

	distinct = {r.bucket_id for r in rows}
	fleet = cint(frappe.db.count("Bucket QR Code"))
	unfiltered = not (a["region"] or a["farm"] or a["location"])
	in_use = sum(1 for r in current if r.system_status == "In Use")

	listing = [
		{
			"bucket": _short(r.bucket_id),
			"farm": r.farm,
			"date": str(r.count_date),
			"location": r.location,
			"scanned_at": str(r.scanned_at) if r.scanned_at else "",
			"in_use": r.system_status == "In Use",
		}
		for r in current[:ROW_LIMIT]
	]
	return {
		"success": True,
		"counted": len(current),
		"n_counts": len({r.count_name for r in rows}),
		"n_farms": len(latest),
		"in_use": in_use,
		"fleet": fleet,
		"not_yet_counted": max(fleet - len(distinct), 0) if unfiltered else None,
		"by_location": by_location,
		"by_farm": by_farm,
		"trend": trend,
		"locations": locations,
		"farms": frappe.get_all("Bucket Count", filters={"docstatus": ["<", 2]}, pluck="farm", distinct=True, order_by="farm"),
		"buckets": listing,
		"truncated": len(current) > ROW_LIMIT,
		"row_limit": ROW_LIMIT,
	}
