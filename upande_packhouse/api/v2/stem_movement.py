# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Stem Movement v2 (www/stem-movement-v2.html).

How many stems moved through each step of the packhouse in a date range, and
where the stems on the shelf are now. Every figure is in STEMS (boxes only where
the column says boxes) and every figure follows every filter.

Each step is counted on the date of its own event, from one source:

  harvested   Stock Entry "Harvesting", transfer_qty, posting_date        core/harvest.py
  received    Stock Entry "Receiving" + "Late Receipt", transfer_qty, posting_date
  shelved     Shelving Log stem_qty by shelved_on (shelf-to-shelf moves excluded)
  allocated   Bucket Allocations.quantity_allocated (not cancelled) by creation date
  issued      Pick List Item.stock_qty with issued / issued_offline, by pick list date_created
  packed      Farm Packlist Item.stock_qty on live Farm Pack Lists, by FPL creation date
  staged      stems of Box Labels flagged staged (or loaded/delivered), by label date
  loaded      stems of Box Labels flagged loaded (or delivered), by label date
  dispatched  Delivery Note Item.stock_qty, submitted non-return notes, by posting_date
  discarded   Discard Request Bucket.stem_qty of Approved requests,
              by COALESCE(approval_date, requested_date)   (README rule 4)

Which farm each step uses (README rule 6, amended 2026-10-07):

  stock steps  - harvested, received, shelved, discarded, shelf now: the PHYSICAL
                 farm (Stock Entry.farm / Shelf.farm / Discard Request Bucket.farm).
  order steps  - allocated, issued, packed, staged, loaded, dispatched: the ORDER's
                 farm, region.order_farm_sql(): COALESCE(Sales Order.farm,
                 Sales Order.custom_farm, Order Pick List.farm).  Orders with no
                 farm anywhere drop out while a farm / region filter is on.

Filters: from_date / to_date, region, farm, variety, location (greenhouse).
Greenhouse is only recorded on harvest, receiving, the shelf and discards; the
order steps cannot honour it and the response says so per step (`location_ok`).

Why a step can legitimately exceed the one before it in a window: steps are
counted on their own event date, so a window can pack stems shelved before the
window opened (and received-before-harvested across midnight). Within a window
the SHELF LEDGER is exact by construction: opening + shelved in - removed =
closing. Stems that sit on a shelf but were never written to the Shelving Log
(MV-7: all remote farms) are reported separately as `unlogged_now`.

Reads only; nothing is written or committed (README rule 1).
"""

from collections import defaultdict

import frappe
from frappe.utils import add_days, cint, getdate, now_datetime, today

from upande_packhouse.api.v2.core import harvest as harvest_core
from upande_packhouse.api.v2.core import region as region_core
from upande_packhouse.api.v2.core import stock as stock_core

STAGES = [
	# key, label, farm meaning, event date, unit note
	("harvested", "Harvested", "stock", "Stock Entry posting date"),
	("received", "Received", "stock", "Stock Entry posting date"),
	("shelved", "Shelved", "stock", "Shelving Log shelved_on"),
	("allocated", "Allocated", "order", "allocation created"),
	("issued", "Issued", "order", "pick list date"),
	("packed", "Packed", "order", "Farm Pack List created"),
	("staged", "Staged", "order", "box label date"),
	("loaded", "Loaded", "order", "box label date"),
	("dispatched", "Dispatched", "order", "Delivery Note posting date"),
	("discarded", "Discarded", "stock", "approval date"),
]
KEYS = [s[0] for s in STAGES]
FARM_NAMES = region_core.REGIONS["Ravine"] + region_core.REGIONS["Karen"]
ISSUE_REASONS = ("Issued to Sales Order", "Offline Issuing")
TRANSFER_REASON = "Shelf-to-Shelf"
AGE_BANDS = [("< 24 h", 0, 24, "ok"), ("24–48 h", 24, 48, "warn"), ("48–72 h", 48, 72, "bad"), ("> 72 h", 72, None, "bad")]
THRESHOLD_H = 48

# Orders carry the farm; a pick list is the last fallback (one farm per order).
_OPL_BY_ORDER = (
	"(SELECT sales_order, MIN(NULLIF(farm, '')) AS farm FROM `tabOrder Pick List` "
	"WHERE docstatus < 2 GROUP BY sales_order)"
)


def _args(from_date, to_date, region, farm, variety, location):
	to_d = getdate(to_date) if to_date else getdate(today())
	from_d = getdate(from_date) if from_date else to_d
	if from_d > to_d:
		from_d, to_d = to_d, from_d
	farms = region_core.farms_for(region=region, farm=farm)
	return frappe._dict(
		f=from_d,
		t=to_d,
		farms=farms,  # None = no restriction; [] = match nothing
		variety=(variety or "").strip(),
		location=(location or "").strip(),
		region=region_core.normalize(region) or "",
		farm=(farm or "").strip(),
	)


def _num(v):
	return float(v or 0)


def _day(v):
	return str(v)[:10]


# ----------------------------------------------------------- stock steps


def _stock_entry_days(a, types):
	"""{date: stems} for Stock Entries of these types, physical farm."""
	if a.farms is not None and not a.farms:
		return {}
	if types == ("Harvesting",) and not a.location:
		rows = harvest_core.harvest(
			date_from=a.f,
			date_to=a.t,
			group_by=("date",),
			farms=a.farms,
			item_codes=[a.variety] if a.variety else None,
		)
		return {_day(r["date"]): _num(r["stems"]) for r in rows}
	params = {"f": a.f, "t": a.t, "types": tuple(types)}
	cond = ""
	if a.farms is not None:
		cond += " AND se.farm IN %(farms)s"
		params["farms"] = region_core.sql_tuple(a.farms)
	if a.variety:
		cond += " AND sed.item_code = %(var)s"
		params["var"] = a.variety
	if a.location:
		cond += " AND se.custom_greenhouse = %(gh)s"
		params["gh"] = a.location
	rows = frappe.db.sql(
		f"""
		SELECT STRAIGHT_JOIN se.posting_date AS d, SUM(sed.transfer_qty) AS s
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.stock_entry_type IN %(types)s AND se.docstatus = 1
		  AND se.posting_date BETWEEN %(f)s AND %(t)s {cond}
		GROUP BY se.posting_date
		""",
		params,
		as_dict=True,
	)  # nosemgrep: the f-string holes are fixed SQL, values are bound
	return {_day(r.d): _num(r.s) for r in rows}


def _log_cond(a, params):
	"""Shelving Log condition on the SHELF's farm, variety and greenhouse."""
	cond = ""
	if a.farms is not None:
		cond += " AND s.farm IN %(farms)s"
		params["farms"] = region_core.sql_tuple(a.farms)
	if a.variety:
		cond += " AND sl.variety = %(var)s"
		params["var"] = a.variety
	if a.location:
		cond += " AND sl.greenhouse = %(gh)s"
		params["gh"] = a.location
	return cond


_NOT_MOVE = f"AND IFNULL(sl.reason, '') NOT LIKE '%%{TRANSFER_REASON}%%'"
_LOG_FROM = "FROM `tabShelving Log` sl INNER JOIN `tabShelf` s ON s.name = sl.shelf"


def _shelf_ledger(a):
	"""Shelf ledger over the window from the Shelving Log, plus per-day shelved-in."""
	empty = {
		"opening": 0.0, "shelved_in": 0.0, "issued_out": 0.0, "discarded_out": 0.0,
		"replaced_out": 0.0, "other_out": 0.0, "closing": 0.0, "buckets_in": 0, "days": {},
	}
	if a.farms is not None and not a.farms:
		return empty
	start = f"{a.f} 00:00:00"
	end = f"{add_days(a.t, 1)} 00:00:00"
	p = {"start": start, "end": end}
	cond = _log_cond(a, p)

	opening = _num(
		frappe.db.sql(
			f"""SELECT SUM(sl.stem_qty) {_LOG_FROM}
			WHERE sl.shelved_on < %(start)s AND (sl.removed_on IS NULL OR sl.removed_on >= %(start)s)
			{_NOT_MOVE} {cond}""",
			p,
		)[0][0]
	)  # nosemgrep: fixed SQL, values bound
	days = {}
	for r in frappe.db.sql(
		f"""SELECT DATE(sl.shelved_on) AS d, SUM(sl.stem_qty) AS s, COUNT(DISTINCT sl.bucket_id) AS b
		{_LOG_FROM}
		WHERE sl.shelved_on >= %(start)s AND sl.shelved_on < %(end)s {_NOT_MOVE} {cond}
		GROUP BY DATE(sl.shelved_on)""",
		p,
		as_dict=True,
	):
		days[_day(r.d)] = _num(r.s)
	buckets_in = cint(
		frappe.db.sql(
			f"""SELECT COUNT(DISTINCT sl.bucket_id) {_LOG_FROM}
			WHERE sl.shelved_on >= %(start)s AND sl.shelved_on < %(end)s {_NOT_MOVE} {cond}""",
			p,
		)[0][0]
	)
	out = {"issue": 0.0, "discard": 0.0, "replaced": 0.0, "other": 0.0}
	for r in frappe.db.sql(
		f"""SELECT CASE
		        WHEN sl.reason IN %(issue)s THEN 'issue'
		        WHEN sl.reason LIKE '%%Discard%%' THEN 'discard'
		        WHEN sl.reason LIKE '%%Replace%%' THEN 'replaced'
		        ELSE 'other' END AS k, SUM(sl.stem_qty) AS s
		{_LOG_FROM}
		WHERE sl.removed_on >= %(start)s AND sl.removed_on < %(end)s {_NOT_MOVE} {cond}
		GROUP BY k""",
		{**p, "issue": ISSUE_REASONS},
		as_dict=True,
	):
		out[r.k] = _num(r.s)
	shelved_in = sum(days.values())
	removed = sum(out.values())
	return {
		"opening": opening,
		"shelved_in": shelved_in,
		"issued_out": out["issue"],
		"discarded_out": out["discard"],
		"replaced_out": out["replaced"],
		"other_out": out["other"],
		"closing": opening + shelved_in - removed,
		"buckets_in": buckets_in,
		"days": days,
	}


def _unlogged_now(a, live_stems):
	"""Stems on the shelf right now (same filters) that no Shelving Log row covers."""
	if a.farms is not None and not a.farms:
		return 0.0
	p = {}
	cond = _log_cond(a, p)
	logged = _num(
		frappe.db.sql(
			f"""SELECT SUM(sl.stem_qty) {_LOG_FROM}
			WHERE sl.shelved_on IS NOT NULL AND sl.removed_on IS NULL {_NOT_MOVE} {cond}""",
			p,
		)[0][0]
	)  # nosemgrep: fixed SQL, values bound
	return live_stems - logged


def _discard_days(a):
	if a.farms is not None and not a.farms:
		return {}, 0.0
	p = {"f": a.f, "t": a.t}
	cond = ""
	if a.farms is not None:
		cond += " AND drb.farm IN %(farms)s"
		p["farms"] = region_core.sql_tuple(a.farms)
	if a.variety:
		cond += " AND drb.variety = %(var)s"
		p["var"] = a.variety
	if a.location:
		cond += " AND drb.greenhouse = %(gh)s"
		p["gh"] = a.location
	days, actioned = {}, 0.0
	for r in frappe.db.sql(
		f"""SELECT COALESCE(dr.approval_date, dr.requested_date) AS d,
		       SUM(COALESCE(drb.stem_qty, 0)) AS s,
		       SUM(CASE WHEN IFNULL(drb.discarded, 0) = 1 THEN COALESCE(drb.stem_qty, 0) ELSE 0 END) AS done
		FROM `tabDiscard Request Bucket` drb
		INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
		WHERE COALESCE(dr.workflow_state, '') = 'Approved' AND dr.docstatus < 2
		  AND COALESCE(dr.approval_date, dr.requested_date) BETWEEN %(f)s AND %(t)s {cond}
		GROUP BY COALESCE(dr.approval_date, dr.requested_date)""",
		p,
		as_dict=True,
	):
		days[_day(r.d)] = _num(r.s)
		actioned += _num(r.done)
	return days, actioned


# ----------------------------------------------------------- order steps


def _order_cond(a, params, item_col, so_alias="so", opl_alias="opl"):
	"""Order-farm + variety condition shared by every order-side step."""
	cond = ""
	if a.farms is not None:
		cond += f" AND {region_core.order_farm_sql(so_alias, opl_alias)} IN %(farms)s"
		params["farms"] = region_core.sql_tuple(a.farms)
	if a.variety:
		cond += f" AND {item_col} = %(var)s"
		params["var"] = a.variety
	return cond


def _by_day(sql, params):
	return {_day(r.d): (_num(r.s), cint(r.b)) for r in frappe.db.sql(sql, params, as_dict=True)}


def _order_days(a):
	"""{stage: {date: (stems, boxes)}} for allocated .. dispatched."""
	if a.farms is not None and not a.farms:
		return {k: {} for k in ("allocated", "issued", "packed", "staged", "loaded", "dispatched")}
	out = {}
	base = {"f": a.f, "t": a.t}

	p = dict(base)
	c = _order_cond(a, p, "soi.item_code")
	out["allocated"] = _by_day(
		f"""SELECT DATE(ba.creation) AS d, SUM(ba.quantity_allocated) AS s, 0 AS b
		FROM `tabBucket Allocations` ba
		INNER JOIN `tabSales Order Item` soi ON soi.name = ba.sales_order_item
		INNER JOIN `tabSales Order` so ON so.name = soi.parent AND so.docstatus = 1
		LEFT JOIN {_OPL_BY_ORDER} opl ON opl.sales_order = so.name
		WHERE IFNULL(ba.cancelled, 0) = 0 AND DATE(ba.creation) BETWEEN %(f)s AND %(t)s {c}
		GROUP BY DATE(ba.creation)""",
		p,
	)

	p = dict(base)
	c = _order_cond(a, p, "pli.item_code")
	out["issued"] = _by_day(
		f"""SELECT opl.date_created AS d, SUM(pli.stock_qty) AS s, 0 AS b
		FROM `tabPick List Item` pli
		INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND pli.parenttype = 'Order Pick List'
		LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE opl.docstatus < 2 AND (pli.issued = 1 OR pli.issued_offline = 1)
		  AND opl.date_created BETWEEN %(f)s AND %(t)s {c}
		GROUP BY opl.date_created""",
		p,
	)

	p = dict(base)
	c = _order_cond(a, p, "fpi.item_code")
	out["packed"] = _by_day(
		f"""SELECT DATE(fpl.creation) AS d, SUM(fpi.stock_qty) AS s,
		       COUNT(DISTINCT CONCAT(fpl.name, '/', fpi.box_id)) AS b
		FROM `tabFarm Packlist Item` fpi
		INNER JOIN `tabFarm Pack List` fpl ON fpl.name = fpi.parent AND fpl.docstatus < 2
		INNER JOIN `tabOrder Pick List` opl ON opl.name = fpl.order_pick_list AND opl.docstatus < 2
		LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE DATE(fpl.creation) BETWEEN %(f)s AND %(t)s {c}
		GROUP BY DATE(fpl.creation)""",
		p,
	)

	for key, flag in (
		("staged", "(IFNULL(bl.staged, 0) = 1 OR IFNULL(bl.loaded, 0) = 1 OR IFNULL(bl.delivered, 0) = 1)"),
		("loaded", "(IFNULL(bl.loaded, 0) = 1 OR IFNULL(bl.delivered, 0) = 1)"),
	):
		p = dict(base)
		c = _order_cond(a, p, "fpi.item_code")
		out[key] = _by_day(
			f"""SELECT bl.date AS d, SUM(fpi.stock_qty) AS s, COUNT(DISTINCT bl.name) AS b
			FROM `tabBox Label` bl
			INNER JOIN `tabFarm Packlist Item` fpi
			        ON fpi.parent = bl.farm_pack_lis AND fpi.box_id = CAST(bl.box_number AS UNSIGNED)
			INNER JOIN `tabOrder Pick List` opl ON opl.name = bl.order_pick_list AND opl.docstatus < 2
			LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
			WHERE IFNULL(bl.farm_pack_lis, '') != '' AND {flag}
			  AND bl.date BETWEEN %(f)s AND %(t)s {c}
			GROUP BY bl.date""",
			p,
		)

	p = dict(base)
	c = _order_cond(a, p, "dni.item_code")
	out["dispatched"] = _by_day(
		f"""SELECT dn.posting_date AS d, SUM(dni.stock_qty) AS s, 0 AS b
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		LEFT JOIN `tabSales Order` so ON so.name = dni.against_sales_order
		LEFT JOIN {_OPL_BY_ORDER} opl ON opl.sales_order = so.name
		WHERE dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0
		  AND dn.posting_date BETWEEN %(f)s AND %(t)s {c}
		GROUP BY dn.posting_date""",
		p,
	)
	return out


# ------------------------------------------------------------ shelf now


def _shelf_now(a):
	empty = {
		"stems": 0.0, "buckets": 0, "held": 0.0, "allocated": 0.0, "free": 0.0,
		"past_threshold": 0.0, "age_bands": [{"label": b[0], "qty": 0.0, "tone": b[3]} for b in AGE_BANDS],
		"oldest_h": None,
	}
	if a.farms is not None and not a.farms:
		return empty
	rows = stock_core.bucket_rows(farms=a.farms, varieties=[a.variety] if a.variety else None)
	if a.location:
		rows = [r for r in rows if (r.greenhouse or "") == a.location]
	if not rows:
		return empty
	t = stock_core.totals(rows)
	bands = []
	for label, lo, hi, tone in AGE_BANDS:
		qty = sum(
			r.stems for r in rows if (r.hours_on_shelf or 0) >= lo and (hi is None or (r.hours_on_shelf or 0) < hi)
		)
		bands.append({"label": label, "qty": qty, "tone": tone})
	return {
		"stems": t.stems,
		"buckets": t.buckets,
		"held": t.held,
		"allocated": t.allocated,
		"free": t.free,
		"past_threshold": sum(b["qty"] for b in bands if b["tone"] == "bad"),
		"age_bands": bands,
		"oldest_h": max((r.hours_on_shelf or 0) for r in rows),
	}


# ---------------------------------------------------------------- options


def _options():
	items = []
	try:
		from upande_packhouse.api.v2.core import rose as rose_core

		groups = rose_core.rose_groups("spray") + rose_core.rose_groups("standard")
		if groups:
			items = frappe.get_all(
				"Item", filters={"item_group": ["in", groups], "disabled": 0}, pluck="name", order_by="name"
			)
	except Exception:
		items = []
	gh = frappe.get_all("Greenhouse", fields=["name", "farm"], order_by="name", limit_page_length=0)
	return {
		"farms": list(FARM_NAMES),
		"varieties": items,
		"locations": [{"name": g.name, "farm": g.farm or ""} for g in gh],
	}


# ---------------------------------------------------------------- endpoint


@frappe.whitelist()
def get_flow(from_date=None, to_date=None, region=None, farm=None, variety=None, location=None):
	"""Stage totals, per-day matrix, shelf ledger and the shelf right now."""
	a = _args(from_date, to_date, region, farm, variety, location)

	by = {
		"harvested": {k: (v, 0) for k, v in _stock_entry_days(a, ("Harvesting",)).items()},
		"received": {k: (v, 0) for k, v in _stock_entry_days(a, ("Receiving", "Late Receipt")).items()},
	}
	ledger = _shelf_ledger(a)
	by["shelved"] = {k: (v, 0) for k, v in ledger["days"].items()}
	disc, disc_actioned = _discard_days(a)
	by["discarded"] = {k: (v, 0) for k, v in disc.items()}
	by.update(_order_days(a))

	days = defaultdict(lambda: {k: 0.0 for k in KEYS})
	totals = {k: 0.0 for k in KEYS}
	boxes = {k: 0 for k in KEYS}
	for k in KEYS:
		for d, (stems, bx) in by.get(k, {}).items():
			days[d][k] = stems
			totals[k] += stems
			boxes[k] += bx
	day_rows = [dict(date=d, **vals) for d, vals in sorted(days.items(), reverse=True)]

	shelf = _shelf_now(a)
	ledger.pop("days")
	ledger["now_live"] = shelf["stems"]
	ledger["unlogged_now"] = _unlogged_now(a, shelf["stems"])

	stages = []
	prev = None
	for key, label, farm_kind, date_note in STAGES:
		v = totals[key]
		stages.append(
			{
				"key": key,
				"label": label,
				"stems": v,
				"boxes": boxes[key] if key in ("packed", "staged", "loaded") else None,
				"farm_basis": (
					"Order farm (Sales Order farm, else pick list farm)"
					if farm_kind == "order"
					else "Physical farm (stock entry / shelf / discard farm)"
				),
				"farm_kind": farm_kind,
				"date_basis": date_note,
				"location_ok": farm_kind == "stock",
				"prev": prev,
				"vs_prev": (v / totals[prev]) if prev and totals[prev] else None,
			}
		)
		if key not in ("discarded",):
			prev = key

	return {
		"success": True,
		"from_date": str(a.f),
		"to_date": str(a.t),
		"as_of": now_datetime().strftime("%a %d %b %Y, %H:%M"),
		"filters": {"region": a.region, "farm": a.farm, "variety": a.variety, "location": a.location},
		"stages": stages,
		"totals": totals,
		"days": day_rows,
		"shelf_ledger": ledger,
		"discard_actioned": disc_actioned,
		"shelf": shelf,
		"threshold_h": THRESHOLD_H,
		"options": _options(),
	}


# ------------------------------------------------------------ box trace


@frappe.whitelist()
def search_box_labels(q=None):
	rows = frappe.get_all(
		"Box Label",
		filters={"name": ["like", "%" + (q or "").strip() + "%"]} if (q or "").strip() else {},
		fields=["name", "customer", "date"],
		order_by="modified desc",
		limit_page_length=15,
	)
	return {"success": True, "results": rows}


@frappe.whitelist()
def get_box_trace(box=None):
	"""One box end to end. Planned buckets come from the Pick List Items of the
	box's pick list (custom_box_id = box number); packed stems from the Farm Pack
	List rows of THIS box; each bucket's stages are read inside its current cycle
	(the latest harvest on or before the pick list date)."""
	box = (box or "").strip()
	if not box:
		return {"success": False, "error": "No box label provided"}
	bl = frappe.db.get_value(
		"Box Label",
		box,
		[
			"name", "farm", "customer", "date", "box_number", "box_total_count", "delivery_point",
			"customer_purchase_order", "pack_rate", "order_pick_list", "farm_pack_lis", "team",
			"consignee", "owner",
		],
		as_dict=True,
	)
	if not bl:
		return {"success": False, "error": "Box Label not found: " + box}
	opl = (
		frappe.db.get_value(
			"Order Pick List", bl.order_pick_list, ["team", "farm", "date_created", "sales_order"], as_dict=True
		)
		if bl.order_pick_list
		else None
	) or frappe._dict()
	consignee = bl.consignee
	if not consignee and opl.get("sales_order"):
		consignee = frappe.db.get_value("Sales Order", opl.sales_order, "custom_consignee")

	items = frappe.get_all(
		"Box Label Item",
		filters={"parent": box},
		fields=["variety", "length", "qty"],
		order_by="idx",
		limit_page_length=0,
	)

	packed_rows = []
	if bl.farm_pack_lis and bl.box_number:
		packed_rows = frappe.db.sql(
			"""SELECT item_code, SUM(stock_qty) AS stems, SUM(bunch_qty) AS bunches
			   FROM `tabFarm Packlist Item` WHERE parent = %(p)s AND box_id = %(b)s GROUP BY item_code""",
			{"p": bl.farm_pack_lis, "b": cint(bl.box_number)},
			as_dict=True,
		)
	packed_stems = sum(_num(r.stems) for r in packed_rows)
	packed_bunches = sum(_num(r.bunches) for r in packed_rows)

	buckets = []
	if bl.order_pick_list and bl.box_number:
		buckets = frappe.db.sql(
			"""SELECT bucket, item_code, SUM(stock_qty) AS stems, MAX(issued) AS issued, MIN(shelf) AS shelf
			   FROM `tabPick List Item`
			   WHERE parent = %(o)s AND parenttype = 'Order Pick List' AND IFNULL(bucket, '') != ''
			     AND custom_box_id = %(b)s
			   GROUP BY bucket, item_code""",
			{"o": bl.order_pick_list, "b": str(cint(bl.box_number))},
			as_dict=True,
		)
	planned_stems = sum(_num(r.stems) for r in buckets)
	cutoff = opl.get("date_created") or bl.date or today()

	ids = tuple({r.bucket for r in buckets}) or ("",)
	harvest, received, graded, shelved, discarded = {}, {}, {}, {}, {}
	greens = defaultdict(lambda: {"stems": 0.0, "buckets": set(), "varieties": defaultdict(float)})
	if buckets:
		for r in frappe.db.sql(
			"""SELECT se.custom_bucket_id AS b, se.stock_entry_type AS t, se.posting_date AS d, se.name,
			          se.custom_greenhouse AS gh, se.custom_harvester AS harvester, se.to_warehouse AS wh
			   FROM `tabStock Entry` se
			   WHERE se.custom_bucket_id IN %(b)s AND se.docstatus = 1 AND se.posting_date <= %(c)s
			     AND se.stock_entry_type IN ('Harvesting','Receiving','Late Receipt','Receiving Quarantined',
			                                 'Grading','Grading Forecast','Discard')
			   ORDER BY se.posting_date, se.creation""",
			{"b": ids, "c": cutoff},
			as_dict=True,
		):
			if r.t == "Harvesting":
				harvest[r.b] = r  # latest wins: the bucket's current cycle
				received.pop(r.b, None)
				graded.pop(r.b, None)
				discarded.pop(r.b, None)
			elif r.t in ("Receiving", "Late Receipt", "Receiving Quarantined"):
				received[r.b] = r
			elif r.t in ("Grading", "Grading Forecast"):
				graded[r.b] = r
			elif r.t == "Discard":
				discarded[r.b] = r
		for r in frappe.db.sql(
			"""SELECT bucket_id, shelf, stem_qty, shelved_on FROM `tabShelving Log`
			   WHERE bucket_id IN %(b)s AND shelved_on IS NOT NULL AND shelved_on <= %(c)s
			   ORDER BY shelved_on""",
			{"b": ids, "c": f"{cutoff} 23:59:59"},
			as_dict=True,
		):
			shelved[r.bucket_id] = r
		# origin greenhouses: the harvest entries of THESE buckets' current cycle
		cyc = {b: h for b, h in harvest.items()}
		if cyc:
			for r in frappe.db.sql(
				"""SELECT se.custom_bucket_id AS b, se.custom_greenhouse AS gh, sed.item_code AS item,
				          SUM(sed.transfer_qty) AS stems
				   FROM `tabStock Entry` se
				   INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
				   WHERE se.name IN %(n)s GROUP BY se.custom_bucket_id, se.custom_greenhouse, sed.item_code""",
				{"n": tuple(h.name for h in cyc.values())},
				as_dict=True,
			):
				g = greens[r.gh or "Unassigned"]
				g["stems"] += _num(r.stems)
				g["buckets"].add(r.b)
				g["varieties"][r.item] += _num(r.stems)

	journey = []
	for r in buckets:
		b = r.bucket
		h, rc, gr, sh, ds = harvest.get(b), received.get(b), graded.get(b), shelved.get(b), discarded.get(b)
		journey.append(
			{
				"bucket": b,
				"group": r.item_code,
				"harvested": h and {"date": str(h.d), "greenhouse": h.gh or "", "harvester": h.harvester or ""},
				"received": rc and {"date": str(rc.d), "action": "Quarantined" if "Quarantined" in rc.t else "Received"},
				"graded": gr and {"date": str(gr.d), "to": gr.wh or ""},
				"shelved": sh and {"shelf": sh.shelf, "qty": sh.stem_qty, "date": str(sh.shelved_on)},
				"packed": {"stems": _num(r.stems), "bunches": 0} if r.issued else None,
				"discarded": ds and {"date": str(ds.d), "reason": ""},
			}
		)

	return {
		"success": True,
		"box": {
			"name": bl.name,
			"farm": bl.farm,
			"customer": bl.customer,
			"date": str(bl.date or ""),
			"box_number": bl.box_number,
			"box_total_count": bl.box_total_count,
			"delivery_point": bl.delivery_point,
			"po": bl.customer_purchase_order,
			"pack_rate": bl.pack_rate,
			"team": bl.team or opl.get("team") or "",
			"consignee": consignee or "",
			"packed_by": bl.owner,
			"packed_stems": packed_stems,
			"packed_bunches": packed_bunches,
			"label_bunches": sum(_num(i.qty) for i in items),
			"planned_stems": planned_stems,
		},
		"box_items": items,
		"packed_by_variety": packed_rows,
		"journey": journey,
		"greenhouses": [
			{
				"name": n,
				"total_stems": g["stems"],
				"bucket_count": len(g["buckets"]),
				"varieties": [
					{"variety": v, "length": "", "qty": q} for v, q in sorted(g["varieties"].items(), key=lambda x: -x[1])
				],
			}
			for n, g in sorted(greens.items())
		],
	}
