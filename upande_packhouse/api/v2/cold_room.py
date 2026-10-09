# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Cold Room v2 (www/cold-room-v2.html).

Filters (every endpoint, every number):
  region    Ravine / Karen (core/region.py), intersected with `farm`
  farm      the SHELF's farm for stock (README rule 6); for flows, the farm the
            event was posted at (Receiving / Discard Stock Entry farm) or the
            pick list's farm (Order Pick List.farm, else Pick List Item.farm)
  from_date / to_date   the page's one date meaning: WHEN THE STOCK CAME IN
            - stock on shelf: the live shelf position (Shelf Item, the source
              of truth -- audit stock.md) restricted to stems whose age anchor
              (harvest date, else shelving date; stock.bucket_rows) falls in
              the range. A historical "position as at a date" cannot be rebuilt:
              the Shelving Log is incomplete and the Bin ledger unusable (ST, MV-7).
            - flows: the event's own date (Receiving / Discard posting_date,
              Order Pick List.date_created).

Stock numbers come only from core/stock.py (bucket_rows -> summarize/totals), so
shelved = held + allocated + free everywhere and buckets are counted distinct.

"Received, not shelved" has one definition (audit ST-7 / ST-23): the bucket's
latest receipt in the range whose journey is still open -- no later receipt or
harvest of the same bucket id (reuse), no live Shelf Item or Shelving Log row
shelved on/after the receipt, no Discard, issue or onward movement on/after it,
no issued pick-list line on a pick list created on/after it.
"""

from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import add_days, getdate

from upande_packhouse.api.v2.core import region as region_mod
from upande_packhouse.api.v2.core import stock

RECEIPT_TYPES = ("Receiving", "Late Receipt")
# Stock Entry types that move a bucket on after receipt (it left the cold room
# some other way than via a shelf).
ONWARD_TYPES = (
	"Discard",
	"Issuing From Cold Store",
	"Issue From The Cold Store",
	"Offline Issuing",
	"Move To Graded Sold",
	"Packing",
	"Dispatch",
)
AGE_EDGES = (1, 2, 3, 5, 7, 14, 30)
FLOW_DAYS = 3  # flow window when the page has no date range


# ── argument handling ──────────────────────────────────────────────────


def _scope(region=None, farm=None, from_date=None, to_date=None):
	farms = region_mod.farms_for(region=region, farm=farm)
	f = getdate(from_date) if from_date else None
	t = getdate(to_date) if to_date else None
	if f and t and f > t:
		f, t = t, f
	return frappe._dict(
		farms=farms,
		from_date=f,
		to_date=t,
		region=region_mod.normalize(region),
		farm=(farm or "").strip() or None,
	)


def _farm_cond(col, sc, params, key="farms"):
	if sc.farms is None:
		return ""
	params[key] = region_mod.sql_tuple(sc.farms)
	return f" AND {col} IN %({key})s"


def _date_cond(col, sc, params):
	out = ""
	if sc.from_date:
		out += f" AND {col} >= %(from_date)s"
		params["from_date"] = sc.from_date
	if sc.to_date:
		out += f" AND {col} <= %(to_date)s"
		params["to_date"] = sc.to_date
	return out


def _flow_scope(sc):
	"""Flows (received / not shelved / requested / discarded) are about recent
	events; with no date range set they cover the last FLOW_DAYS days instead of
	all history (an all-time "not shelved" would list every old receipt)."""
	fs = frappe._dict(sc)
	if not fs.from_date and not fs.to_date:
		fs.from_date = add_days(getdate(), -FLOW_DAYS)
		fs.defaulted = True
	return fs


def _in_range(d, sc):
	if d is None:
		return not (sc.from_date or sc.to_date)
	d = getdate(d)
	return (not sc.from_date or d >= sc.from_date) and (not sc.to_date or d <= sc.to_date)


# ── stock on shelf ─────────────────────────────────────────────────────


def _stock(sc):
	"""(rows in range, rows of the same farm scope outside the range)."""
	if sc.farms == []:
		return [], []
	rows = stock.bucket_rows(farms=sc.farms)
	inside, outside = [], []
	for r in rows:
		(inside if _in_range(r.age_anchor, sc) else outside).append(r)
	return inside, outside


def _shelf_names(sc):
	"""(bucket, variety, length) -> set of shelves holding it."""
	params = {}
	cond = _farm_cond("s.farm", sc, params)
	out = defaultdict(set)
	for bucket_id, variety, length, shelf in frappe.db.sql(
		f"""SELECT si.bucket_id, si.variety, si.stem_length, s.name
		    FROM `tabShelf Item` si INNER JOIN `tabShelf` s ON s.name = si.parent
		    WHERE si.stem_qty > 0 AND IFNULL(si.bucket_id, '') != '' AND IFNULL(si.variety, '') != ''{cond}""",
		params,
	):  # nosemgrep: fixed SQL fragments, bound values
		out[(bucket_id, variety, length)].add(shelf)
	return out


# ── flows ──────────────────────────────────────────────────────────────


def _not_shelved(sc):
	"""Buckets received in range and still waiting for a shelf (one row per
	bucket / variety / length of its latest receipt)."""
	if sc.farms == []:
		return []
	params = {"rt": RECEIPT_TYPES, "cycle": RECEIPT_TYPES + ("Harvesting",), "onward": ONWARD_TYPES}
	cond = _farm_cond("se.farm", sc, params) + _date_cond("se.posting_date", sc, params)
	rows = frappe.db.sql(
		f"""
		SELECT se.custom_bucket_id AS bucket_id, se.farm, sed.item_code AS variety,
		       se.custom_stem_length AS stem_length, se.posting_date AS received_on,
		       se.name AS stock_entry, SUM(sed.qty) AS stems
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1 AND se.stock_entry_type IN %(rt)s
		  AND IFNULL(se.custom_bucket_id, '') != ''{cond}
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabStock Entry` n
		      WHERE n.custom_bucket_id = se.custom_bucket_id AND n.docstatus = 1
		        AND n.stock_entry_type IN %(cycle)s AND n.name != se.name
		        AND (n.posting_date > se.posting_date
		             OR (n.posting_date = se.posting_date AND n.stock_entry_type != 'Harvesting'
		                 AND n.creation > se.creation)))
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabShelf Item` si
		      WHERE si.bucket_id = se.custom_bucket_id AND si.stem_qty > 0
		        AND DATE(si.date_added) >= se.posting_date)
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabShelving Log` sl
		      WHERE sl.bucket_id = se.custom_bucket_id AND DATE(sl.shelved_on) >= se.posting_date)
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabStock Entry` d
		      WHERE d.custom_bucket_id = se.custom_bucket_id AND d.docstatus = 1
		        AND d.stock_entry_type IN %(onward)s AND d.posting_date >= se.posting_date)
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabPick List Item` p
		      INNER JOIN `tabOrder Pick List` o ON o.name = p.parent
		      WHERE p.bucket = se.custom_bucket_id AND p.issued = 1 AND o.docstatus < 2
		        AND o.date_created >= se.posting_date)
		GROUP BY se.custom_bucket_id, se.farm, sed.item_code, se.custom_stem_length, se.posting_date, se.name
		ORDER BY se.farm, sed.item_code
		""",
		params,
		as_dict=True,
	)  # nosemgrep: fixed SQL fragments, bound values
	for r in rows:
		r.stems = float(r.stems or 0)
		r.age_days = (getdate() - getdate(r.received_on)).days if r.received_on else None
	return rows


def _received(sc):
	if sc.farms == []:
		return frappe._dict(stems=0.0, buckets=0)
	params = {"rt": RECEIPT_TYPES}
	cond = _farm_cond("se.farm", sc, params) + _date_cond("se.posting_date", sc, params)
	r = frappe.db.sql(
		f"""SELECT IFNULL(SUM(sed.qty), 0) AS stems, COUNT(DISTINCT se.custom_bucket_id) AS buckets
		    FROM `tabStock Entry` se INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		    WHERE se.docstatus = 1 AND se.stock_entry_type IN %(rt)s
		      AND IFNULL(se.custom_bucket_id, '') != ''{cond}""",
		params,
		as_dict=True,
	)[0]  # nosemgrep
	return frappe._dict(stems=float(r.stems or 0), buckets=int(r.buckets or 0))


def _discarded(sc):
	if sc.farms == []:
		return frappe._dict(stems=0.0, buckets=0, entries=0)
	params = {}
	cond = _farm_cond("se.farm", sc, params) + _date_cond("se.posting_date", sc, params)
	r = frappe.db.sql(
		f"""SELECT IFNULL(SUM(sed.qty), 0) AS stems, COUNT(DISTINCT se.name) AS entries,
		           COUNT(DISTINCT NULLIF(se.custom_bucket_id, '')) AS buckets
		    FROM `tabStock Entry` se INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		    WHERE se.docstatus = 1 AND se.stock_entry_type = 'Discard'{cond}""",
		params,
		as_dict=True,
	)[0]  # nosemgrep
	return frappe._dict(stems=float(r.stems or 0), entries=int(r.entries or 0), buckets=int(r.buckets or 0))


PL_FARM = "COALESCE(NULLIF(opl.farm, ''), pli.farm)"


def _requested(sc):
	"""Pick-list lines (submitted pick lists created in range) not yet issued."""
	if sc.farms == []:
		return []
	params = {}
	cond = _farm_cond(PL_FARM, sc, params) + _date_cond("opl.date_created", sc, params)
	rows = frappe.db.sql(
		f"""SELECT pli.bucket AS bucket_id, pli.item_code AS variety, pli.stem_length,
		           {PL_FARM} AS farm, pli.stock_qty AS stems, opl.name AS opl,
		           opl.customer, opl.date_created
		    FROM `tabOrder Pick List` opl
		    INNER JOIN `tabPick List Item` pli ON pli.parent = opl.name
		    WHERE opl.docstatus = 1 AND IFNULL(pli.issued, 0) = 0{cond}
		    ORDER BY farm, pli.item_code""",
		params,
		as_dict=True,
	)  # nosemgrep
	for r in rows:
		r.stems = float(r.stems or 0)
	return rows


def _capacity(sc):
	if not frappe.db.table_exists("Cold Store Capacity"):
		return []
	params = {}
	cond = _farm_cond("farm", sc, params)
	return frappe.db.sql(
		f"""SELECT farm, sensor_name, max_shelves, max_buckets, max_stems,
		           temp_min, temp_max, humidity_min, humidity_max
		    FROM `tabCold Store Capacity` WHERE 1=1{cond} ORDER BY farm""",
		params,
		as_dict=True,
	)  # nosemgrep


def _flow_total(rows):
	return frappe._dict(
		stems=sum(r.stems for r in rows), buckets=len({r.bucket_id for r in rows if r.bucket_id})
	)


def _farm_options():
	return [
		r[0]
		for r in frappe.db.sql(
			"SELECT DISTINCT farm FROM `tabShelf` WHERE IFNULL(farm, '') != '' ORDER BY farm"
		)
	]


def _meta(sc):
	return {
		"region": sc.region,
		"farm": sc.farm,
		"farms": sc.farms,
		"from_date": str(sc.from_date) if sc.from_date else None,
		"to_date": str(sc.to_date) if sc.to_date else None,
		"date_meaning": "Stock on shelf: harvest date (else shelving date). Flows: receipt / discard posting date, pick list creation date.",
	}


def _clean(d):
	"""frappe._dict -> plain dict with JSON-safe values (sets, dates)."""
	return {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in d.items()}


# ── endpoints ──────────────────────────────────────────────────────────


@frappe.whitelist()
def get_cold_room(region: Any = None, farm: Any = None, from_date: Any = None, to_date: Any = None):
	"""Dashboard + shelf-contents data. Every number honours every filter."""
	try:
		sc = _scope(region, farm, from_date, to_date)
		rows, outside = _stock(sc)
		t = stock.totals(rows)
		fs = _flow_scope(sc)
		ns = _not_shelved(fs)
		req = _requested(fs)

		# per shelf farm (pressure bars, farm chart)
		by_farm = {g.shelf_farm: g for g in stock.summarize(rows, by=("shelf_farm",))}
		ns_farm = defaultdict(lambda: {"stems": 0.0, "buckets": set()})
		for r in ns:
			ns_farm[r.farm or "Unknown"]["stems"] += r.stems
			ns_farm[r.farm or "Unknown"]["buckets"].add(r.bucket_id)
		farms_out = []
		for f in sorted(set(by_farm) | set(ns_farm)):
			g = by_farm.get(f) or {}
			n = ns_farm.get(f) or {"stems": 0.0, "buckets": set()}
			farms_out.append(
				{
					"farm": f,
					"stems": g.get("stems", 0.0),
					"held": g.get("held", 0.0),
					"allocated": g.get("allocated", 0.0),
					"free": g.get("free", 0.0),
					"buckets": g.get("buckets", 0),
					"not_shelved_stems": n["stems"],
					"not_shelved_buckets": len(n["buckets"]),
				}
			)

		# shelf contents: variety x length x farm, shelved + not shelved
		names = _shelf_names(sc)
		shelf_map = {}
		for g in stock.summarize(rows, by=("variety", "stem_length", "shelf_farm")):
			shelf_map[(g.variety, g.stem_length or "", g.shelf_farm)] = {
				"variety": g.variety,
				"stem_length": g.stem_length,
				"farm": g.shelf_farm,
				"shelved": g.stems,
				"held": g.held,
				"allocated": g.allocated,
				"free": g.free,
				"buckets": g.buckets,
				"avg_age_days": g.avg_age_days,
				"oldest_days": g.oldest_days,
				"not_shelved": 0.0,
				"_shelves": set(),
			}
		for r in rows:
			k = (r.variety, r.stem_length or "", r.shelf_farm)
			shelf_map[k]["_shelves"] |= names.get((r.bucket_id, r.variety, r.stem_length), {r.shelf})
		for r in ns:
			k = (r.variety, r.stem_length or "", r.farm)
			m = shelf_map.get(k)
			if m is None:
				m = shelf_map[k] = {
					"variety": r.variety,
					"stem_length": r.stem_length,
					"farm": r.farm,
					"shelved": 0.0,
					"held": 0.0,
					"allocated": 0.0,
					"free": 0.0,
					"buckets": 0,
					"avg_age_days": None,
					"oldest_days": None,
					"not_shelved": 0.0,
					"_shelves": set(),
				}
			m["not_shelved"] += r.stems
		shelf_rows = []
		for m in shelf_map.values():
			m["total"] = m["shelved"] + m["not_shelved"]
			m["shelves"] = ", ".join(sorted(m.pop("_shelves")))
			shelf_rows.append(m)
		shelf_rows.sort(key=lambda m: (m["variety"] or "", str(m["stem_length"] or ""), m["farm"] or ""))

		variety = sorted(
			({"variety": g.variety, "stems": g.stems} for g in stock.summarize(rows, by=("variety",))),
			key=lambda x: -x["stems"],
		)
		length = sorted(
			(
				{"stem_length": g.stem_length, "stems": g.stems}
				for g in stock.summarize(rows, by=("stem_length",))
			),
			key=lambda x: (
				float(x["stem_length"]) if str(x["stem_length"] or "").replace(".", "", 1).isdigit() else 1e9
			),
		)

		ot = stock.totals(outside)
		anchors = [r.age_anchor for r in outside if r.age_anchor]
		ns_t, req_t = _flow_total(ns), _flow_total(req)
		return {
			"success": True,
			"scope": dict(
				_meta(sc),
				flow_from=str(fs.from_date),
				flow_to=str(fs.to_date) if fs.to_date else None,
				flow_defaulted=bool(fs.get("defaulted")),
			),
			"totals": {
				**_clean(t),
				"not_shelved_stems": ns_t.stems,
				"not_shelved_buckets": ns_t.buckets,
			},
			"flows": {
				"received": _received(fs),
				"not_shelved": ns_t,
				"requested": req_t,
				"discarded": _discarded(fs),
			},
			"outside_range": {
				"stems": ot.stems,
				"buckets": ot.buckets,
				"oldest_anchor": str(min(anchors)) if anchors else None,
				"newest_anchor": str(max(anchors)) if anchors else None,
			},
			"by_farm": farms_out,
			"by_variety": variety,
			"by_length": length,
			"age_bands": stock.age_bands(rows, edges=AGE_EDGES),
			"shelf_rows": shelf_rows,
			"capacity": _capacity(sc),
			"farm_options": _farm_options(),
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Cold Room v2: get_cold_room")
		return {"success": False, "error": "Could not load cold room data. The error was logged."}


def _open_requests(bucket_ids):
	"""bucket -> earliest open Discard Request holding it (availability.reserved_bucket_ids rule)."""
	if not bucket_ids:
		return {}
	return dict(
		frappe.db.sql(
			"""SELECT drb.bucket_id, MIN(drb.parent)
			   FROM `tabDiscard Request Bucket` drb
			   INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
			   WHERE COALESCE(dr.workflow_state, '') != 'Rejected' AND COALESCE(drb.discarded, 0) = 0
			     AND drb.bucket_id IN %(b)s
			   GROUP BY drb.bucket_id""",
			{"b": tuple(bucket_ids)},
		)
	)


@frappe.whitelist()
def get_cold_room_buckets(region: Any = None, farm: Any = None, from_date: Any = None, to_date: Any = None):
	"""Bucket-level lists for the Buckets tab, same filters as get_cold_room.
	`age` holds one row per (bucket, variety, length) on the shelf; its stems
	add up to the dashboard's "Stems on shelf"."""
	try:
		sc = _scope(region, farm, from_date, to_date)
		rows, _ = _stock(sc)
		names = _shelf_names(sc)
		held_ids = {r.bucket_id for r in rows if r.in_discard_request}
		reqs = _open_requests(held_ids)
		age, disc = [], []
		for r in rows:
			shelves = sorted(names.get((r.bucket_id, r.variety, r.stem_length), {r.shelf}))
			row = {
				"bucket_id": r.bucket_id,
				"variety": r.variety,
				"stem_length": r.stem_length,
				"farm": r.shelf_farm,
				"shelf": ", ".join(shelves),
				"stems": r.stems,
				"allocated": r.allocated,
				"held": r.held,
				"free": r.free,
				"age_days": r.age_days,
				"anchor": str(r.age_anchor) if r.age_anchor else None,
				"state": "held" if r.in_discard_request else ("allocated" if r.allocated else "free"),
			}
			age.append(row)
			if r.in_discard_request:
				disc.append(dict(row, request=reqs.get(r.bucket_id), allocated_in_held=r.allocated_in_held))
		age.sort(key=lambda x: (x["age_days"] if x["age_days"] is not None else 1e9, x["variety"] or ""))
		disc.sort(key=lambda x: -(x["age_days"] or 0))
		fs = _flow_scope(sc)
		ns = _not_shelved(fs)
		req = _requested(fs)
		t = stock.totals(rows)
		return {
			"success": True,
			"scope": _meta(sc),
			"age": age,
			"discard_shelved": disc,
			"received_not_shelved": [_clean(r) for r in ns],
			"requested_not_issued": [_clean(r) for r in req],
			"summary": {
				"stems": t.stems,
				"buckets": t.buckets,
				"rows": len(age),
				"held_stems": t.held,
				"held_buckets": len(held_ids),
				"allocated_in_held": t.allocated_in_held,
				"not_shelved": _flow_total(ns),
				"requested": _flow_total(req),
			},
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Cold Room v2: get_cold_room_buckets")
		return {"success": False, "error": "Could not load cold room buckets. The error was logged."}
