# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Journey (www/bucket-tracker.html) in ONE call, the way the mobile
# getTraceability does it. The page used to assemble the journey in the browser
# from ~15 frappe.client calls, then fetched every Receiving entry and one entry per
# harvest day as a full document (10 at a time, in sequence) just to total its stems.
# A recycled bucket carries hundreds of entries, so that took minutes. Here every
# table is read once, by indexed bucket / parent columns, and stems and variety come
# from a single Stock Entry Detail aggregate.

import frappe
from frappe import _

from upande_packhouse.api import bucket_replacement
from upande_packhouse.api.remote_transfer import transfer_scheduling

#: Same caps as the page used.
MAX_ENTRIES = 2000
MAX_ROWS = 800

SE_FIELDS = (
	"name, stock_entry_type, posting_date, posting_time, farm, custom_greenhouse, "
	"custom_stem_length, custom_bucket_id, custom_harvester, custom_grading_entry, "
	"custom_receiving_entry, to_warehouse, from_warehouse, docstatus"
)
QUARANTINE_TYPES = ("Receiving Quarantined", "Quarantine Rejects", "Quarantine Accept", "Material Transfer")
REJECT_TYPES = ("Packhouse Rejects", "Airport rejects")


def _variants(bucket_id):
	return tuple({bucket_id, bucket_id.upper(), bucket_id.lower()})


def _has(doctype, column):
	return frappe.db.has_column(doctype, column)


@frappe.whitelist()
def getBucketJourney(bucket_id: str | None = None):
	bucket_id = (bucket_id or frappe.form_dict.get("bucket_id") or "").strip()
	if not bucket_id:
		frappe.throw(_("Bucket ID is required"))
	ids = _variants(bucket_id)

	# Every Stock Entry of the bucket in one indexed read, split by type below.
	ses = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- only fixed column lists are interpolated; values are bound
		f"""SELECT {SE_FIELDS} FROM `tabStock Entry`
		WHERE custom_bucket_id IN %(ids)s AND docstatus < 2
		ORDER BY posting_date, posting_time LIMIT {MAX_ENTRIES * 2}""",
		{"ids": ids},
		as_dict=True,
	)
	harvest = [s for s in ses if s.stock_entry_type == "Harvesting"]
	receiving = [s for s in ses if s.stock_entry_type == "Receiving"]
	rejects = [s for s in ses if s.stock_entry_type in REJECT_TYPES and s.docstatus == 1]
	quarantine = [s for s in ses if s.stock_entry_type in QUARANTINE_TYPES and s.docstatus == 1]

	grading_names = sorted({s.custom_grading_entry for s in harvest if s.custom_grading_entry})
	grading = []
	if grading_names:
		extra = [
			c
			for c in (
				"custom_graded_by",
				"custom_bunch_id",
				"custom_harvest_entry",
				"custom_opl_scanned",
				"custom_scanned_grading",
				"custom_scanned_packing",
				"custom_bunched_by",
			)
			if _has("Stock Entry", c)
		]
		grading = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- only fixed column lists are interpolated; values are bound
			f"""SELECT {SE_FIELDS}{"".join(", " + c for c in extra)} FROM `tabStock Entry`
			WHERE name IN %(names)s ORDER BY posting_date, posting_time""",
			{"names": tuple(grading_names)},
			as_dict=True,
		)

	# Variety (first item) and stems (sum of transfer_qty) per entry, in one pass.
	se_item, se_qty = {}, {}
	names = [s.name for s in harvest + grading + receiving]
	if names:
		for r in frappe.db.sql(
			"""SELECT parent, item_code, COALESCE(NULLIF(transfer_qty, 0), qty) AS stems
			FROM `tabStock Entry Detail` WHERE parent IN %(names)s ORDER BY parent, idx""",
			{"names": tuple(names)},
			as_dict=True,
		):
			se_item.setdefault(r.parent, r.item_code or "")
			se_qty[r.parent] = se_qty.get(r.parent, 0.0) + float(r.stems or 0)

	shelf_log = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- only fixed column lists are interpolated; values are bound
		f"""SELECT name, shelf, farm, variety, stem_qty, stem_length, greenhouse, shelved_on,
		       removed_on, reason, shelved_by
		FROM `tabShelving Log` WHERE bucket_id IN %(ids)s ORDER BY removed_on LIMIT {MAX_ROWS}""",
		{"ids": ids},
		as_dict=True,
	)
	shelf_items = frappe.db.sql(
		"""SELECT si.name, si.parent, COALESCE(NULLIF(si.farm, ''), sh.farm) AS farm, si.variety,
		       si.stem_qty, si.stem_length, si.greenhouse, si.date_added
		FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
		WHERE si.bucket_id IN %(ids)s ORDER BY si.date_added LIMIT 200"""
		if _has("Shelf Item", "farm")
		else """SELECT si.name, si.parent, sh.farm AS farm, si.variety, si.stem_qty, si.stem_length,
		       si.greenhouse, si.date_added
		FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
		WHERE si.bucket_id IN %(ids)s ORDER BY si.date_added LIMIT 200""",
		{"ids": ids},
		as_dict=True,
	)

	alloc_name = frappe.db.get_value("Bucket Allocation Status", {"bucket_id": bucket_id}, "name")
	alloc = frappe.get_doc("Bucket Allocation Status", alloc_name).as_dict() if alloc_name else None

	discards = []
	if frappe.db.exists("DocType", "Discard Request Bucket"):
		discards = frappe.db.sql(
			"""SELECT name, parent, farm, greenhouse, variety, stem_qty, stem_length, harvest_date, creation
			FROM `tabDiscard Request Bucket` WHERE bucket_id IN %(ids)s AND discarded = 1
			ORDER BY creation LIMIT 200""",
			{"ids": ids},
			as_dict=True,
		)

	# Pick-list rows straight from the child table (v16: `table_ytkc`, bare field names),
	# with the order header joined in — no per-OPL document fetch.
	opl_cols = {
		"customer": "opl_customer",
		"order_name": "opl_order_name",
		"team": "opl_team",
		"date_created": "opl_date",
		"sales_order": "opl_sales_order",
		"status": "opl_status",
		"issuing_percentage": "opl_issuing_pct",
		"consignee": "opl_consignee",
	}
	header = ", ".join(f"opl.`{c}` AS {alias}" for c, alias in opl_cols.items() if _has("Order Pick List", c))
	opl_rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- only fixed column lists are interpolated; values are bound
		f"""SELECT pli.*, opl.name AS parent{", " + header if header else ""}
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket IN %(ids)s
		ORDER BY pli.creation LIMIT 500""",
		{"ids": ids},
		as_dict=True,
	)
	# The page still reads the v15 names for these.
	for r in opl_rows:
		r.setdefault("custom_issued", r.get("issued"))
		r.setdefault("custom_shelf", r.get("shelf"))

	return {
		"id": bucket_id,
		"harvest": harvest,
		"grading": grading,
		"receiving": receiving,
		"shelf_log": shelf_log,
		"shelf_items": shelf_items,
		"alloc": alloc,
		"opl_rows": opl_rows,
		"discards": discards,
		"rejects": rejects,
		"quarantine": quarantine,
		"se_item": se_item,
		"se_qty": se_qty,
		"replacements": bucket_replacement.for_bucket(bucket_id),
		# Remote transfers: farm → packhouse by truck, run and trip, with each event.
		"remote_transfers": transfer_scheduling.bucket_transfer_trace(bucket_id, after_packhouse=True),
	}


# ── Bucket & shelf overview (tiles above the journey search) ───────────────────
#
# A bucket is IN USE while it holds flowers: harvested and not yet received (Bucket
# QR Code status "In Use" — receiving sets it back to "Available"), on a shelf
# (Shelf Item), or on a transfer truck (pick row in transit, not yet shelved). Every
# other bucket is empty and can be used. A shelf is free when no Shelf Item sits on it.

#: Pick rows flagged in transit longer ago than this are stale, not on a truck.
TRUCK_LOOKBACK_DAYS = 7


def _natural(name):
	import re

	return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name or "")]


def _in_use_sets():
	harvesting = set(
		frappe.db.sql_list("SELECT UPPER(name) FROM `tabBucket QR Code` WHERE status = 'In Use'")
	)
	on_shelf = set(
		frappe.db.sql_list(
			"SELECT DISTINCT UPPER(bucket_id) FROM `tabShelf Item` WHERE IFNULL(bucket_id, '') != ''"
		)
	)
	on_truck = set(
		frappe.db.sql_list(
			"""SELECT DISTINCT UPPER(bucket) FROM `tabPick List Item`
			WHERE parenttype = 'Order Pick List' AND in_transit = 1 AND IFNULL(shelved, 0) = 0
			  AND IFNULL(bucket, '') != '' AND modified >= %s""",
			frappe.utils.add_days(frappe.utils.today(), -TRUCK_LOOKBACK_DAYS),
		)
	)
	# Most advanced place wins, so each bucket is counted once.
	harvesting -= on_shelf | on_truck
	on_shelf -= on_truck
	return harvesting, on_shelf, on_truck


@frappe.whitelist()
def getBucketShelfOverview():
	total = frappe.db.count("Bucket QR Code")
	harvesting, on_shelf, on_truck = _in_use_sets()
	in_use = harvesting | on_shelf | on_truck
	known = 0
	if in_use:
		known = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabBucket QR Code` WHERE name IN %(ids)s", {"ids": tuple(in_use)}
		)[0][0]

	farms = frappe.db.sql(
		"""SELECT COALESCE(NULLIF(sh.farm, ''), '') AS farm, COUNT(*) AS shelves,
		       SUM(CASE WHEN x.buckets > 0 THEN 1 ELSE 0 END) AS with_buckets,
		       COALESCE(SUM(x.buckets), 0) AS buckets
		FROM `tabShelf` sh
		LEFT JOIN (
			SELECT parent, COUNT(DISTINCT UPPER(bucket_id)) AS buckets
			FROM `tabShelf Item` WHERE IFNULL(bucket_id, '') != '' GROUP BY parent
		) x ON x.parent = sh.name
		GROUP BY COALESCE(NULLIF(sh.farm, ''), '')
		ORDER BY farm""",
		as_dict=True,
	)
	for f in farms:
		f["with_buckets"] = int(f.with_buckets or 0)
		f["buckets"] = int(f.buckets or 0)
		f["free"] = int(f.shelves) - f["with_buckets"]
	return {
		"buckets": {
			"total": total,
			"in_use": len(in_use),
			"empty": max(0, total - known),
			"harvesting": len(harvesting),
			"on_shelf": len(on_shelf),
			"on_truck": len(on_truck),
		},
		"shelves": {
			"total": sum(int(f.shelves) for f in farms),
			"with_buckets": sum(f["with_buckets"] for f in farms),
			"free": sum(f["free"] for f in farms),
		},
		"farms": farms,
		"missing": frappe.db.count("Bucket Replacement", {"status": "Open"}),
	}


@frappe.whitelist()
def getFarmShelves(farm: str | None = None, view: str = "all"):
	"""Shelves of one farm ('' = shelves with no farm, None = every farm) with the buckets
	on each and what each bucket holds. view: all | occupied | free."""
	cond, params = "", {}
	if farm is not None:
		cond = "WHERE COALESCE(NULLIF(sh.farm, ''), '') = %(farm)s"
		params["farm"] = farm
	shelves = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- only fixed column lists are interpolated; values are bound
		f"SELECT sh.name, COALESCE(sh.farm, '') AS farm FROM `tabShelf` sh {cond}", params, as_dict=True
	)
	items = {}
	if shelves:
		for r in frappe.db.sql(
			"""SELECT parent, bucket_id, variety, stem_length, stem_qty, greenhouse, harvest_date,
			       receiving_date, grading_date, date_added, cut_stage, harvester, warehouse
			FROM `tabShelf Item` WHERE parent IN %(names)s AND IFNULL(bucket_id, '') != ''
			ORDER BY date_added""",
			{"names": tuple(s.name for s in shelves)},
			as_dict=True,
		):
			items.setdefault(r.parent, []).append(r)
	out = []
	for s in sorted(shelves, key=lambda s: _natural(s.name)):
		rows = items.get(s.name, [])
		if (view == "occupied" and not rows) or (view == "free" and rows):
			continue
		buckets = {}
		for r in rows:
			b = buckets.setdefault(
				(r.bucket_id or "").upper(),
				{"bucket": r.bucket_id, "date_added": r.date_added, "stems": 0, "contents": []},
			)
			b["stems"] += int(r.stem_qty or 0)
			b["contents"].append(r)
		out.append({"shelf": s.name, "farm": s.farm, "buckets": list(buckets.values())})
	return out


@frappe.whitelist()
def getBucketsInUse(kind: str = "all"):
	"""Buckets in use and where they are. kind: all | harvesting | shelf | truck."""
	harvesting, on_shelf, on_truck = _in_use_sets()
	out = []
	if kind in ("all", "shelf") and on_shelf:
		for r in frappe.db.sql(
			"""SELECT si.bucket_id AS bucket, si.parent AS shelf, sh.farm, si.variety, si.stem_length,
			       si.stem_qty AS stems, si.date_added AS since
			FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
			WHERE UPPER(si.bucket_id) IN %(ids)s ORDER BY si.date_added""",
			{"ids": tuple(on_shelf)},
			as_dict=True,
		):
			out.append({**r, "where": "shelf"})
	if kind in ("all", "truck") and on_truck:
		for r in frappe.db.sql(
			"""SELECT pli.bucket, pli.transit_truck AS truck, pli.parent AS opl, pli.item_code AS variety,
			       pli.stock_qty AS stems, pli.modified AS since
			FROM `tabPick List Item` pli
			WHERE pli.parenttype = 'Order Pick List' AND pli.in_transit = 1 AND IFNULL(pli.shelved, 0) = 0
			  AND UPPER(pli.bucket) IN %(ids)s AND pli.modified >= %(since)s ORDER BY pli.modified""",
			{
				"ids": tuple(on_truck),
				"since": frappe.utils.add_days(frappe.utils.today(), -TRUCK_LOOKBACK_DAYS),
			},
			as_dict=True,
		):
			out.append({**r, "where": "truck"})
	if kind in ("all", "harvesting") and harvesting:
		for r in frappe.db.sql(
			"""SELECT q.name AS bucket, se.farm, se.custom_greenhouse AS greenhouse, se.custom_stem_length AS stem_length,
			       (SELECT d.item_code FROM `tabStock Entry Detail` d WHERE d.parent = se.name ORDER BY d.idx LIMIT 1) AS variety,
			       (SELECT SUM(d.qty) FROM `tabStock Entry Detail` d WHERE d.parent = se.name) AS stems,
			       se.posting_date AS since
			FROM `tabBucket QR Code` q LEFT JOIN `tabStock Entry` se ON se.name = q.last_stock_entry
			WHERE UPPER(q.name) IN %(ids)s ORDER BY se.posting_date""",
			{"ids": tuple(harvesting)},
			as_dict=True,
		):
			out.append({**r, "where": "harvesting"})
	# One row per bucket: a bucket holding several varieties has a Shelf Item / pick row each.
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
	return list(merged.values())[:1000]


@frappe.whitelist()
def getMissingBuckets():
	"""Buckets replaced on a pick list and not accounted for since (Bucket Replacement, Open)."""
	return bucket_replacement.open_replacements()
