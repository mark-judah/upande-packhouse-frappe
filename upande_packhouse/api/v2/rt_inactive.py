# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Remote Transfers v2 -> Bucket Journey -> Inactive buckets.

INACTIVE = a bucket that is still IN THE PROCESS whose last ACTIVITY is at least N days old.

IN THE PROCESS (current stage, first match wins):
  1 In transit          Pick List Item in_transit / BAS in_transit, not yet shelved or issued
  2 On trolley          Pick List Item loaded_in_trolley, not yet shelved or issued
  3 Awaiting transfer   Pick List Item awaiting_transfer, not yet shelved or issued
  4 On shelf            a Shelf Item with stems
  5 Open shelving log   a Shelving Log with no removed_on and no Shelf Item
  6 Allocated           a submitted order's open pick row (not issued, not marked not found) or a live
                        Bucket Allocations line, bucket on no shelf
  7 Received, not shelved  latest Receiving entry (last max(30, N+14) days: older ones need a stock-count, not this list), no shelving since, not harvested again,
                        not discarded
  'allocated' is also shown as a flag on shelf buckets. NOT in the process: buckets fully issued /
  discarded / cleared (idle in the field), and 'not found' pick rows (those belong on the Watchlist).

ACTIVITY = the newest event of core/bucket_activity.py (harvest, grading, receiving, quarantine /
release, shelving, shelf move, allocation, issue, transfer step, replacement, discard, count).
N = the 'Idle for at least' box; its default is Production Settings > Inactive Bucket Days (4 if unset).

Read only.
"""

import time
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from upande_packhouse.api.v2.core import bucket_activity as act
from upande_packhouse.api.v2.core import region as rgn

DEFAULT_DAYS = 4
RECEIVED_LOOKBACK_DAYS = 30
ROW_CAP = 1000
RANK = {"In transit": 1, "On trolley": 2, "Awaiting transfer": 3, "On shelf": 4, "Open shelving log": 5, "Allocated": 6, "Received, not shelved": 7}


def inactive_days():
	"""Production Settings > Inactive Bucket Days; 4 when unset, zero or unreadable."""
	try:
		v = cint(frappe.db.get_single_value("Production Settings", "inactive_bucket_days"))
	except Exception:
		v = 0
	return v if v > 0 else DEFAULT_DAYS


def candidates(now=None, days=DEFAULT_DAYS):
	"""{BUCKET: {...}} every bucket still in the process, with its stage, place, variety and stems."""
	now = now or now_datetime()
	C = {}

	def offer(b, stage, farm="", loc="", variety="", stems=0.0):
		b = act.up(b)
		if not b:
			return
		cur = C.get(b)
		if cur is None or RANK[stage] < RANK[cur["stage"]]:
			C[b] = {"bucket": b, "stage": stage, "farm": farm or "", "loc": loc or "", "variety": variety or "", "stems": float(stems or 0), "allocated": bool(cur and cur["allocated"])}
		elif cur and not cur["variety"] and variety:
			cur["variety"] = variety

	farm_col = "COALESCE(NULLIF(MAX(si.farm), ''), MAX(sh.farm))" if frappe.db.has_column("Shelf Item", "farm") else "MAX(sh.farm)"
	for r in frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- fixed expression
		f"""SELECT UPPER(si.bucket_id) AS b, GROUP_CONCAT(DISTINCT si.parent ORDER BY si.parent SEPARATOR ', ') AS shelves,
		       SUM(si.stem_qty) AS stems, MAX(si.variety) AS variety, {farm_col} AS farm
		FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
		WHERE IFNULL(si.bucket_id, '') != '' AND IFNULL(si.stem_qty, 0) > 0 GROUP BY UPPER(si.bucket_id)""", as_dict=True):
		offer(r.b, "On shelf", r.farm, r.shelves, r.variety, r.stems)
	for r in frappe.db.sql(
		"""SELECT UPPER(bucket_id) AS b, GROUP_CONCAT(DISTINCT shelf ORDER BY shelf SEPARATOR ', ') AS shelves, SUM(stem_qty) AS stems,
		       MAX(variety) AS variety, MAX(farm) AS farm
		FROM `tabShelving Log` WHERE removed_on IS NULL AND IFNULL(bucket_id, '') != '' GROUP BY UPPER(bucket_id)""", as_dict=True):
		offer(r.b, "Open shelving log", r.farm, r.shelves, r.variety, r.stems)
	for r in frappe.db.sql(
		"""SELECT UPPER(pli.bucket) AS b, MAX(pli.farm) AS farm, MAX(pli.shelf) AS shelf, SUM(pli.stock_qty) AS stems, MAX(pli.item_code) AS variety,
		       MAX(IFNULL(pli.in_transit, 0)) AS tr, MAX(IFNULL(pli.loaded_in_trolley, 0)) AS tro, MAX(IFNULL(pli.awaiting_transfer, 0)) AS aw
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND o.docstatus < 2 AND IFNULL(pli.bucket, '') != ''
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0 AND IFNULL(pli.not_found, 0) = 0
		  AND (pli.in_transit = 1 OR pli.loaded_in_trolley = 1 OR pli.awaiting_transfer = 1)
		GROUP BY UPPER(pli.bucket)""", as_dict=True):
		offer(r.b, "In transit" if r.tr else "On trolley" if r.tro else "Awaiting transfer", r.farm, r.shelf, r.variety, r.stems)
	for r in frappe.db.sql(
		"""SELECT UPPER(bucket_id) AS b, MAX(shelf_farm) AS farm, MAX(shelf_location) AS loc, MAX(item_code) AS variety, SUM(total_quantity) AS stems
		FROM `tabBucket Allocation Status` WHERE in_transit = 1 AND IFNULL(bucket_id, '') != '' GROUP BY UPPER(bucket_id)""", as_dict=True):
		offer(r.b, "In transit", r.farm, r.loc, r.variety, r.stems)
	alloc = {}
	for r in frappe.db.sql(
		"""SELECT UPPER(pli.bucket) AS b, MAX(pli.farm) AS farm, MAX(pli.shelf) AS shelf, SUM(pli.stock_qty) AS stems, MAX(pli.item_code) AS variety
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND o.docstatus = 1 AND IFNULL(pli.bucket, '') != ''
		  AND IFNULL(pli.issued, 0) = 0 AND IFNULL(pli.not_found, 0) = 0 GROUP BY UPPER(pli.bucket)""", as_dict=True):
		alloc[r.b] = r
	for r in frappe.db.sql(
		"""SELECT UPPER(b.bucket_id) AS b, MAX(b.shelf_farm) AS farm, MAX(b.shelf_location) AS shelf, MAX(b.item_code) AS variety, SUM(a.quantity_allocated) AS stems
		FROM `tabBucket Allocations` a JOIN `tabBucket Allocation Status` b ON b.name = a.parent
		WHERE IFNULL(a.cancelled, 0) = 0 AND IFNULL(a.issued, 0) = 0 AND IFNULL(b.bucket_id, '') != '' GROUP BY UPPER(b.bucket_id)""", as_dict=True):
		alloc.setdefault(r.b, r)
	for b, r in alloc.items():
		offer(b, "Allocated", r.farm, r.shelf, r.variety, r.stems)
		C[b]["allocated"] = True
	# received, not shelved
	d = (now - timedelta(days=max(RECEIVED_LOOKBACK_DAYS, cint(days) + 14))).date()
	rec = {}
	for r in frappe.db.sql(
		"""SELECT se.name, UPPER(se.custom_bucket_id) AS b, se.farm, TIMESTAMP(se.posting_date, se.posting_time) AS ts
		FROM `tabStock Entry` se WHERE se.stock_entry_type = 'Receiving' AND se.docstatus = 1 AND se.posting_date >= %(d)s
		  AND IFNULL(se.custom_bucket_id, '') != '' ORDER BY ts""", {"d": d}, as_dict=True):
		rec[r.b] = r  # newest wins
	rest = tuple(b for b in rec if b not in C)
	if rest:
		later = {}
		for r in frappe.db.sql(
			"""SELECT UPPER(custom_bucket_id) AS b, MAX(creation) AS ts FROM `tabStock Entry`
			WHERE stock_entry_type = 'Discard' AND docstatus < 2 AND creation >= %(d)s GROUP BY custom_bucket_id""", {"d": d}, as_dict=True):
			later[r.b] = r.ts
		shelved = {}
		for r in frappe.db.sql(
			"SELECT UPPER(bucket_id) AS b, MAX(shelved_on) AS ts FROM `tabShelving Log` WHERE bucket_id IN %(ids)s GROUP BY bucket_id", {"ids": rest}, as_dict=True):
			shelved[r.b] = r.ts
		names = []
		for b in rest:
			ts = rec[b].ts
			if (later.get(b) and later[b] > ts) or (shelved.get(b) and shelved[b] >= ts):
				continue
			names.append(rec[b].name)
		det = {}
		if names:
			for r in frappe.db.sql(
				"""SELECT parent, MAX(item_code) AS variety, SUM(COALESCE(NULLIF(transfer_qty, 0), qty)) AS stems FROM `tabStock Entry Detail`
				WHERE parent IN %(n)s GROUP BY parent""", {"n": tuple(names)}, as_dict=True):
				det[r.parent] = r
			for b in rest:
				if rec[b].name in det:
					x = det[rec[b].name]
					offer(b, "Received, not shelved", rec[b].farm, "", x.variety, x.stems)
	return C


def _filter(rows, search, farms):
	if farms is not None:
		fl = {f.lower() for f in farms}
		rows = [r for r in rows if (r["farm"] or "").lower() in fl]
	if search:
		q = str(search).strip().upper()
		rows = [r for r in rows if q in r["bucket"] or q in (r["variety"] or "").upper() or q in (r["loc"] or "").upper()]
	return rows


def compute(days=None, search=None, farm=None, region=None, now=None):
	"""Pure read: the inactive list (all rows, sorted by days idle desc) + the settings default."""
	now = now or now_datetime()
	n = cint(days) if days not in (None, "") and cint(days) > 0 else inactive_days()
	cutoff = now - timedelta(days=n)
	C = candidates(now, n)
	rows = _filter(list(C.values()), search, rgn.farms_for(region=region, farm=farm))
	last = act.last_activity([r["bucket"] for r in rows], cutoff)
	watch = set(frappe.db.sql_list("SELECT UPPER(bucket_id) FROM `tabBucket Watch` WHERE status IN ('Watching', 'Reappeared')"))
	out = []
	for r in rows:
		ts, label = last.get(r["bucket"], (None, ""))
		if ts is not None and ts >= cutoff:
			continue  # active
		out.append({**r, "stems": round(r["stems"], 1), "last_at": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "", "last_event": label or "no event on record",
			"days_idle": round((now - ts).total_seconds() / 86400.0, 1) if ts else None, "watched": r["bucket"] in watch})
	out.sort(key=lambda r: (r["days_idle"] is None, -(r["days_idle"] or 0), r["bucket"]))
	return n, out, len(rows)


@frappe.whitelist()
def get_inactive_buckets(days: int | str | None = None, search: str | None = None, farm: str | None = None, region: str | None = None, limit: int = ROW_CAP):
	"""Buckets still in the process with no activity for at least `days` days (default: Production Settings)."""
	frappe.has_permission("Bucket Watch", "read", throw=True)
	t0 = time.time()
	n, rows, in_process = compute(days, search, farm, region)
	limit = cint(limit)
	shown = rows[:limit] if limit > 0 else rows
	stages = {}
	for r in rows:
		stages[r["stage"]] = stages.get(r["stage"], 0) + 1
	return {
		"success": True, "days": n, "default_days": inactive_days(), "rows": shown, "truncated": len(shown) < len(rows), "count": len(rows),
		"in_process": in_process, "stages": stages,
		"kpi": {"buckets": len(rows), "stems": round(sum(r["stems"] for r in rows), 1),
			"oldest_days": max([r["days_idle"] for r in rows if r["days_idle"] is not None] or [0]), "watched": sum(1 for r in rows if r["watched"])},
		"now_ts": now_datetime().strftime("%Y-%m-%d %H:%M:%S"), "ms": int((time.time() - t0) * 1000),
		"definition": _("Idle = no event for this long: harvest, grading, receiving, shelving, shelf move, quarantine, allocation, issue, transfer step, replacement, discard or count."),
	}
