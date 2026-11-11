# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Shelving Performance v2 - how much of what each cold room receives gets shelved, how fast,
by whom, and how often a bucket called from a shelf cannot be found.

Sources
    Received   submitted Stock Entries of type Receiving (greenhouse -> farm cold room) and
               Remote Transfers (farm cold room -> Kapkolia) whose target is a
               "<Farm> Receiving Cold Store" warehouse. One receipt per bucket, cold room and
               day: a bucket re-scanned the same day counts once, at its first scan.
    Shelved    Shelving Log rows (one per stint on a shelf: shelved_on, shelved_by, farm).
    Missing    Bucket Replacement with reason Missing (the bucket was not on its shelf when
               called), by the farm of the shelf it was expected on.

Numbers
    Shelved %        receipts whose bucket was put on a shelf of the same cold room after the
                     receipt (from 30 minutes before, allowing for scan order) and before the
                     bucket's next receipt there, at most 3 days later.
    On time %        receipts shelved within the target (default 2 hours).
    Time to shelve   receipt -> first shelving, in minutes (median and 90th percentile).
    Waiting          receipts older than the target with no shelving yet.
    Missing %        missing reports / buckets called from that farm's shelves (issued, issued
                     offline, loaded on a trolley or truck, plus the missing ones).

Filters: from/to over the receipt date, region / farm (the cold room's farm), target minutes.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime

from upande_packhouse.api.v2.core import region as region_core

EARLY = timedelta(minutes=30)
WINDOW = timedelta(days=3)
ROW_LIMIT = 3000
CALLED = ("Issued to Sales Order", "Offline Issuing", "Transferred (Trolley/Truck)")
COLD_STORE = " Receiving Cold Store"


def _args(kw):
	def g(k):
		return str(kw.get(k) or "").strip()

	today = getdate()
	return {
		"from_date": getdate(g("from_date") or today - timedelta(days=6)),
		"to_date": getdate(g("to_date") or today),
		"region": g("region"),
		"farm": g("farm"),
		"target": max(cint(g("target_minutes")) or 120, 5),
	}


def _receipts(a, farms):
	rows = frappe.db.sql(
		"""SELECT se.custom_bucket_id AS bucket,
		       TRIM(SUBSTRING_INDEX(sed.t_warehouse, %(cs)s, 1)) AS farm,
		       se.stock_entry_type AS source, se.owner AS received_by,
		       TIMESTAMP(se.posting_date, se.posting_time) AS at,
		       sed.item_code AS variety, sed.transfer_qty AS stems
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1 AND se.stock_entry_type IN ('Receiving', 'Remote Transfers')
		  AND se.posting_date BETWEEN %(f)s AND %(t)s
		  AND sed.t_warehouse LIKE %(like)s AND IFNULL(se.custom_bucket_id, '') != ''
		ORDER BY at""",
		{"f": a["from_date"], "t": a["to_date"], "cs": COLD_STORE, "like": "%" + COLD_STORE + "%"},
		as_dict=True,
	)
	out = {}
	for r in rows:
		if farms is not None and r.farm not in farms:
			continue
		key = (r.bucket.upper(), r.farm, r.at.date())
		if key in out:
			out[key]["stems"] += flt(r.stems)
			continue
		out[key] = {**r, "bucket": r.bucket, "stems": flt(r.stems)}
	return list(out.values())


def _shelvings(a, farms):
	rows = frappe.db.sql(
		"""SELECT bucket_id, farm, shelf, shelved_on, shelved_by, stem_qty, variety
		FROM `tabShelving Log`
		WHERE shelved_on BETWEEN %(f)s AND %(t)s""",
		{
			"f": get_datetime(a["from_date"]) - EARLY,
			"t": get_datetime(a["to_date"]) + timedelta(days=1) + WINDOW,
		},
		as_dict=True,
	)
	return [r for r in rows if r.bucket_id and (farms is None or r.farm in farms)]


def _missing(a, farms):
	rows = frappe.db.sql(
		"""SELECT name, farm, old_bucket AS bucket, expected_shelf, item_code AS variety, stems,
		       reported_by, reported_at, status
		FROM `tabBucket Replacement`
		WHERE reason = 'Missing' AND DATE(reported_at) BETWEEN %(f)s AND %(t)s
		ORDER BY reported_at DESC""",
		{"f": a["from_date"], "t": a["to_date"]},
		as_dict=True,
	)
	return [r for r in rows if farms is None or r.farm in farms]


def _called(a, farms):
	rows = frappe.db.sql(
		"""SELECT farm, COUNT(*) AS n FROM `tabShelving Log`
		WHERE reason IN %(r)s AND DATE(removed_on) BETWEEN %(f)s AND %(t)s
		GROUP BY farm""",
		{"r": CALLED, "f": a["from_date"], "t": a["to_date"]},
		as_dict=True,
	)
	return {r.farm: cint(r.n) for r in rows if farms is None or r.farm in farms}


def _match(receipts, shelvings):
	"""Each receipt's first shelving in the same cold room, before the bucket's next receipt there."""
	logs = defaultdict(list)
	for s in shelvings:
		logs[(s.bucket_id.upper(), s.farm)].append(s)
	for v in logs.values():
		v.sort(key=lambda s: s.shelved_on)
	nxt = {}
	by_key = defaultdict(list)
	for r in receipts:
		by_key[(r["bucket"].upper(), r["farm"])].append(r)
	for v in by_key.values():
		v.sort(key=lambda r: r["at"])
		for i, r in enumerate(v):
			nxt[id(r)] = v[i + 1]["at"] if i + 1 < len(v) else None
	for r in receipts:
		end = r["at"] + WINDOW
		if nxt[id(r)] and nxt[id(r)] < end:
			end = nxt[id(r)]
		r["shelving"] = next(
			(
				s
				for s in logs.get((r["bucket"].upper(), r["farm"]), [])
				if r["at"] - EARLY <= s.shelved_on < end
			),
			None,
		)
		r["minutes"] = (
			max(int((r["shelving"].shelved_on - r["at"]).total_seconds() // 60), 0) if r["shelving"] else None
		)


def _pct(n, d):
	return round(100.0 * n / d, 1) if d else None


def _quantile(values, q):
	if not values:
		return None
	v = sorted(values)
	return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def _summary(rs, target):
	done = [r["minutes"] for r in rs if r["minutes"] is not None]
	return {
		"received": len(rs),
		"shelved": len(done),
		"shelved_pct": _pct(len(done), len(rs)),
		"on_time": sum(1 for m in done if m <= target),
		"on_time_pct": _pct(sum(1 for m in done if m <= target), len(rs)),
		"median_minutes": _quantile(done, 0.5),
		"p90_minutes": _quantile(done, 0.9),
	}


@frappe.whitelist()
def get_shelving_performance(**kw):
	"""Everything the page shows for the filters."""
	a = _args(kw)
	farms = region_core.farms_for(region=a["region"], farm=a["farm"])
	target, now = a["target"], now_datetime()

	receipts = _receipts(a, farms)
	shelvings = _shelvings(a, farms)
	_match(receipts, shelvings)
	missing = _missing(a, farms)
	called = _called(a, farms)

	waiting = [r for r in receipts if not r["shelving"] and (now - r["at"]).total_seconds() > target * 60]

	by_room = defaultdict(list)
	for r in receipts:
		by_room[r["farm"]].append(r)
	miss_by_farm = defaultdict(int)
	for m in missing:
		miss_by_farm[m.farm] += 1
	rooms = []
	for farm in sorted(set(by_room) | set(miss_by_farm) | set(called)):
		rs = by_room.get(farm, [])
		calls = called.get(farm, 0) + miss_by_farm.get(farm, 0)
		rooms.append(
			{
				"farm": farm,
				**_summary(rs, target),
				"from_harvest": sum(1 for r in rs if r["source"] == "Receiving"),
				"from_farms": sum(1 for r in rs if r["source"] == "Remote Transfers"),
				"waiting": sum(1 for r in waiting if r["farm"] == farm),
				"called": calls,
				"missing": miss_by_farm.get(farm, 0),
				"missing_pct": _pct(miss_by_farm.get(farm, 0), calls),
			}
		)

	in_range = [s for s in shelvings if a["from_date"] <= s.shelved_on.date() <= a["to_date"]]
	people = {}
	for s in in_range:
		p = people.setdefault(
			s.shelved_by or "",
			{"user": s.shelved_by or "", "farms": set(), "buckets": 0, "stems": 0, "timed": []},
		)
		p["farms"].add(s.farm)
		p["buckets"] += 1
		p["stems"] += cint(s.stem_qty)
	for r in receipts:
		if r["shelving"] and r["shelving"].shelved_by in people:
			people[r["shelving"].shelved_by]["timed"].append(r["minutes"])
	names = _names(
		{u for u in people} | {r["received_by"] for r in waiting} | {m.reported_by for m in missing}
	)
	shelvers = sorted(
		(
			{
				"user": u,
				"name": names.get(u) or u or "Unknown",
				"farms": ", ".join(sorted(f for f in p["farms"] if f)),
				"buckets": p["buckets"],
				"stems": p["stems"],
				"timed": len(p["timed"]),
				"median_minutes": _quantile(p["timed"], 0.5),
				"on_time_pct": _pct(sum(1 for m in p["timed"] if m <= target), len(p["timed"])),
			}
			for u, p in people.items()
		),
		key=lambda x: -x["buckets"],
	)

	days = defaultdict(lambda: defaultdict(list))
	for r in receipts:
		days[str(r["at"].date())][r["farm"]].append(r)
	trend = [
		{"date": d, "rooms": {f: _summary(rs, target) for f, rs in v.items()}}
		for d, v in sorted(days.items())
	]

	return {
		"success": True,
		"target_minutes": target,
		"totals": {
			**_summary(receipts, target),
			"waiting": len(waiting),
			"called": sum(x["called"] for x in rooms),
			"missing": len(missing),
			"missing_pct": _pct(len(missing), sum(x["called"] for x in rooms)),
			"shelvers": len([s for s in shelvers if s["user"]]),
		},
		"rooms": rooms,
		"shelvers": shelvers,
		"trend": trend,
		"waiting": [
			{
				"bucket": r["bucket"],
				"farm": r["farm"],
				"variety": r["variety"],
				"stems": r["stems"],
				"source": "From farm" if r["source"] == "Remote Transfers" else "From harvest",
				"received_at": str(r["at"]),
				"received_by": names.get(r["received_by"]) or r["received_by"],
				"hours": round((now - r["at"]).total_seconds() / 3600, 1),
			}
			for r in sorted(waiting, key=lambda r: r["at"])[:ROW_LIMIT]
		],
		"missing": [
			{
				"bucket": m.bucket,
				"farm": m.farm,
				"shelf": m.expected_shelf,
				"variety": m.variety,
				"stems": flt(m.stems),
				"reported_by": names.get(m.reported_by) or m.reported_by,
				"reported_at": str(m.reported_at) if m.reported_at else "",
				"status": m.status,
			}
			for m in missing[:ROW_LIMIT]
		],
		"farms": sorted({r["farm"] for r in receipts} | set(called) | {m.farm for m in missing}),
	}


def _names(users):
	users = [u for u in users if u]
	if not users:
		return {}
	return dict(
		frappe.get_all("User", filters={"name": ["in", users]}, fields=["name", "full_name"], as_list=True)
	)
