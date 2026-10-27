# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Stock Visibility v2 - "where is every stem, and why can't I sell it".

Built for the sales team, who confirm orders against this page. Every stem the
system knows about is in exactly ONE state, so the numbers always add up, and
every withheld stem names its reason, since when, and who has to act.

States (mutually exclusive, in the order a stem is tested)

  sellable            on a shelf, free, young enough, cooled          -> Sellable now
  cooling             on a shelf, free, still inside cooling hours    -> Ready soon (ready_at)
  coldroom            received, not yet put on a shelf                -> Ready soon
  awaiting            harvested < 36 h ago, not yet received          -> Ready soon
  committed_live      allocated to a live order                       -> Committed
  committed_transit   allocated and on a trolley / truck to the hub   -> Committed
  reclaimable         allocated to an order that is cancelled, already delivered or past
                      its delivery date: it holds the stems for nobody -> Committed (stale)
  discard             bucket on an open discard request               -> Withheld
  quarantine          net held in quarantine pending a decision       -> Withheld
  too_old             free stems past the farm's allocation age        -> Withheld
  unreceived          harvested > 36 h ago and never received          -> Withheld
  farm_disabled       shelf at a farm not enabled for sales            -> Withheld

Rules mirror the allocator (sales_allocation.get_sales_order_items_with_buckets,
catalogued in core/stock.py) so "sellable" is what allocation will accept:
discard hold is per BUCKET; age anchor is harvest date else date added; a stem
older than the farm's max allocation age, or at the discard age, is too old.
Remote farms (not a sales shelf) are sellable but flagged `remote` - the allocator
offers them and moves them to the hub on demand. Longer stems are offered by the
allocator as an approved downgrade; `check` reports that pool separately.

Allocation health is a SEPARATE view of the allocation ledger (Bucket Allocations
not cancelled, not issued): each outstanding allocation is tagged with why it may
be stale (order cancelled, delivered, past delivery date) or at risk (bucket on a
discard request, missing from every shelf, marked not found). Reads only.
"""

import re
import time
from collections import defaultdict
from datetime import timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime, today

from upande_packhouse.api.v2 import stock_visibility as sv
from upande_packhouse.api.v2.core import pipeline
from upande_packhouse.api.v2.core import region as region_core
from upande_packhouse.api.v2.core import stock
from upande_packhouse.api.v2.core.rose import rose_sql, rose_type

COLDROOM_DAYS = 5
AWAITING_HOURS = 36  # harvested but not received: normal in-transit window
AWAITING_DAYS = 7  # older unclaimed harvests are shown as "never received" up to here
TRANSIT_DAYS = 5

# key: (label, group, why, who acts)
STATE_META = {
	"sellable": ("Sellable now", "sellable", "On a shelf, free, fresh enough and cooled.", "Sales: confirm it"),
	"cooling": ("Still cooling", "soon", "On a shelf but inside the farm's cooling hours.", "Nobody - it frees up by itself"),
	"coldroom": ("Coldroom floor, not shelved", "soon", "Received but not on a shelf yet, so allocation cannot see it.", "Cold room: shelve the bucket"),
	"awaiting": ("Harvested, not yet received", "soon", "Cut recently and on its way to be booked in.", "Receiving: book it in"),
	"committed_live": ("Allocated to a live order", "committed", "Promised to an order that is still going out.", "-"),
	"committed_transit": ("On the move for an order", "committed", "Allocated and on a trolley or truck to the packhouse.", "-"),
	"reclaimable": ("Allocated, but stale", "committed", "Held by an order that is cancelled, delivered or past its delivery date.", "Sales: release it in Sales Allocation"),
	"discard": ("On a discard request", "withheld", "The whole bucket is queued for discard, so allocation refuses it.", "Quality: discard it, or reject the request"),
	"quarantine": ("In quarantine", "withheld", "Held pending a quality decision; it is not on any shelf.", "Quality: accept or reject the batch"),
	"too_old": ("Too old to allocate", "withheld", "Older than the farm's maximum allocation age.", "Sales: sell as a downgrade; Quality: discard"),
	"unreceived": ("Harvested, never received", "withheld", "Harvested more than 36 hours ago and never booked in.", "Receiving: book it in or find the bucket"),
	"farm_disabled": ("Farm not enabled for sales", "withheld", "On a shelf at a farm that Production Settings does not enable.", "Admin: enable the farm in Shelf Locations"),
}
STATE_ORDER = list(STATE_META)
PHYSICAL = tuple(s for s in STATE_ORDER)  # every state is stems that exist
RECLAIM_REASONS = ("order_cancelled", "dispatched", "delivery_passed")
STALE_LABEL = {
	"order_cancelled": "Order cancelled",
	"dispatched": "Order already delivered",
	"delivery_passed": "Delivery date has passed",
	"discard_held": "Bucket is on a discard request",
	"bucket_gone": "Bucket is on no shelf",
	"not_found": "Bucket marked not found",
}
RISK_REASONS = ("discard_held", "bucket_gone", "not_found")


def _guard():
	"""The board shows every order's demand and every bucket: same audience as Sales Orders."""
	frappe.has_permission("Sales Order", "read", throw=True)


def _f(v):
	return float(v or 0)


def _ln(v):
	return (v or "").strip() or "--"


def _cm(v):
	m = re.match(r"\s*(\d+)", v or "")
	return int(m.group(1)) if m else 0


def _up(v):
	return str(v or "").strip().upper()


def _ts(v):
	return str(v) if v else None


# ------------------------------------------------------------------ pure classification


def classify_shelf_row(r, *, held_by, farm_cfg, farm_known, stale_qty, now=None):
	"""One bucket/variety/length shelf row -> [(state, stems, extra)].

	`r` has stems, allocated, free, in_discard_request, too_old, cooling,
	hours_on_shelf, shelf_farm, age_days. `held_by` is the discard request for
	the bucket (or None). `stale_qty` is how much of the allocation is stale."""
	out = []
	if r["stems"] <= 0:
		return out
	if r["in_discard_request"]:
		return [("discard", r["stems"], {"ref": held_by})]
	if not farm_known:
		return [("farm_disabled", r["stems"], {})]
	alloc = min(_f(r["allocated"]), r["stems"])
	stale = min(max(0.0, stale_qty), alloc)
	if stale > 0:
		after = "too_old" if r["too_old"] else "cooling" if r["cooling"] else "sellable"
		ready = None
		if after == "cooling" and r.get("date_added") is not None and farm_cfg.get("cooling_hours"):
			ready = get_datetime(r["date_added"]) + timedelta(hours=farm_cfg["cooling_hours"])
		out.append(("reclaimable", stale, {"after": after, "ready_at": ready, "remote": not farm_cfg.get("sales_shelf")}))
	if alloc - stale > 0:
		out.append(("committed_live", alloc - stale, {}))
	free = r["stems"] - alloc
	if free > 0:
		if r["too_old"]:
			out.append(("too_old", free, {}))
		elif r["cooling"]:
			ready = None
			if r.get("date_added") is not None and farm_cfg.get("cooling_hours"):
				ready = get_datetime(r["date_added"]) + timedelta(hours=farm_cfg["cooling_hours"])
			out.append(("cooling", free, {"ready_at": ready}))
		else:
			out.append(("sellable", free, {"remote": not farm_cfg.get("sales_shelf")}))
	return out


def verdict(qty, safe, soon, downgrade):
	"""Can `qty` be confirmed? -> (code, label)."""
	if qty <= safe:
		return "safe", "Safe to confirm"
	if qty <= safe + soon:
		return "possible", "Possible - part of it is not ready yet"
	if qty <= safe + soon + downgrade:
		return "downgrade", "Only with longer stems (downgrade)"
	return "short", "Not enough stock"


# ------------------------------------------------------------------ sources


def _args(kw):
	a = frappe._dict(kw or {})
	farm_pick = sv._list(a.farm)
	a.farms = sv._farms(a.get("region"), farm_pick)
	a.lengths = sv._list(a.length)
	a.rose = a.rose or "all"
	a.q = (a.q or "").strip().lower()
	t = getdate(today())
	a.today = t
	a.through = getdate(a.through) if a.through else getdate(add_days(t, 7))
	if a.through < t:
		a.through = t
	return a


def _farm_ok(a, farm):
	return a.farms is None or (farm or "") in a.farms


def _discards():
	"""bucket -> (request name, workflow state, since date) for buckets that are held."""
	out = {}
	for r in frappe.db.sql(
		"""
		SELECT drb.bucket_id, dr.name, dr.workflow_state,
		       COALESCE(dr.approval_date, dr.requested_date) AS since
		FROM `tabDiscard Request Bucket` drb
		INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
		WHERE COALESCE(dr.workflow_state, '') != 'Rejected'
		  AND COALESCE(drb.discarded, 0) = 0 AND COALESCE(drb.bucket_id, '') != ''
		ORDER BY since DESC
		""",
		as_dict=True,
	):
		out[r.bucket_id] = {"name": r.name, "state": r.workflow_state, "since": r.since}
	return out


def _allocations(shelf_keys, held_ids):
	"""Outstanding allocation rows, each tagged with a stale / risk reason (or None)."""
	rows = frappe.db.sql(
		"""
		SELECT ba.name AS ba, bas.bucket_id, bas.item_code AS variety,
		       IFNULL(bas.stem_length, '') AS length, bas.shelf_farm AS farm, bas.in_transit,
		       ba.sales_order, ba.sales_order_item, ba.quantity_allocated AS qty, ba.creation,
		       so.docstatus AS so_docstatus, so.status AS so_status, so.delivery_date,
		       so.per_delivered, so.customer_name, so.customer
		FROM `tabBucket Allocations` ba
		INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		LEFT JOIN `tabSales Order` so ON so.name = ba.sales_order
		WHERE ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0 AND ba.quantity_allocated > 0
		""",
		as_dict=True,
	)
	if not rows:
		return []
	items = tuple({r.sales_order_item for r in rows if r.sales_order_item})
	delivered = set()
	if items:
		delivered = set(
			frappe.db.sql_list(
				"""SELECT DISTINCT dni.so_detail FROM `tabDelivery Note Item` dni
				   INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
				   WHERE dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0 AND dni.so_detail IN %(i)s""",
				{"i": items},
			)
		)
	not_found = {
		_up(b)
		for b in frappe.db.sql_list(
			"""SELECT DISTINCT p.bucket FROM `tabPick List Item` p
			   INNER JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2
			   WHERE p.not_found = 1 AND IFNULL(p.issued, 0) = 0 AND IFNULL(p.bucket, '') != ''"""
		)
	}
	t = getdate(today())
	for r in rows:
		r.qty = _f(r.qty)
		r.key = (_up(r.bucket_id), r.variety, r.length)
		if r.so_docstatus is None or r.so_docstatus == 2 or r.so_status == "Cancelled":
			r.reason = "order_cancelled"
		elif r.sales_order_item in delivered or _f(r.per_delivered) >= 100:
			r.reason = "dispatched"
		elif r.delivery_date and getdate(r.delivery_date) < t:
			r.reason = "delivery_passed"
		elif r.bucket_id in held_ids:
			r.reason = "discard_held"
		elif r.key not in shelf_keys:
			r.reason = "bucket_gone"
		elif _up(r.bucket_id) in not_found:
			r.reason = "not_found"
		else:
			r.reason = None
		r.held = r.bucket_id in held_ids
		r.age_days = (t - getdate(r.creation)).days if r.creation else None
	return rows


def _coldroom_rows(a):
	since = add_days(a.today, -COLDROOM_DAYS)
	rows = frappe.db.sql(
		"""
		SELECT se.custom_bucket_id AS bucket_id, se.farm, sed.item_code AS variety, i.item_name, i.item_group,
		       IFNULL(NULLIF(TRIM(se.custom_stem_length), ''), '') AS length,
		       se.creation AS since, se.name AS ref, SUM(sed.qty) AS stems
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		LEFT JOIN `tabItem` i ON i.name = sed.item_code
		WHERE se.docstatus = 1 AND se.stock_entry_type IN ('Receiving', 'Late Receipt')
		  AND se.posting_date >= %(since)s AND IFNULL(se.custom_bucket_id, '') != ''
		  AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` n
		        WHERE n.custom_bucket_id = se.custom_bucket_id AND n.docstatus = 1 AND n.name != se.name
		          AND n.stock_entry_type IN ('Receiving', 'Late Receipt', 'Harvesting') AND n.creation > se.creation)
		  AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = se.custom_bucket_id
		        AND si.stem_qty > 0 AND DATE(si.date_added) >= se.posting_date)
		  AND NOT EXISTS (SELECT 1 FROM `tabShelving Log` sl WHERE sl.bucket_id = se.custom_bucket_id
		        AND DATE(sl.shelved_on) >= se.posting_date)
		  AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` d WHERE d.custom_bucket_id = se.custom_bucket_id
		        AND d.docstatus = 1 AND d.posting_date >= se.posting_date
		        AND d.stock_entry_type IN ('Discard', 'Issuing From Cold Store', 'Issue From The Cold Store',
		            'Offline Issuing', 'Move To Graded Sold', 'Packing', 'Dispatch'))
		  AND NOT EXISTS (SELECT 1 FROM `tabPick List Item` p INNER JOIN `tabOrder Pick List` o ON o.name = p.parent
		        WHERE p.bucket = se.custom_bucket_id AND p.issued = 1 AND o.docstatus < 2
		          AND o.date_created >= se.posting_date)
		GROUP BY se.custom_bucket_id, se.farm, sed.item_code, se.custom_stem_length, se.name, se.creation
		""",
		{"since": since},
		as_dict=True,
	)
	return rows


def _harvest_rows(a):
	"""Harvested stems nobody has booked in yet (latest cycle of the bucket only)."""
	since = add_days(a.today, -AWAITING_DAYS)
	return frappe.db.sql(
		"""
		SELECT se.custom_bucket_id AS bucket_id, se.farm, sed.item_code AS variety, i.item_name, i.item_group,
		       IFNULL(NULLIF(TRIM(COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length)), ''), '') AS length,
		       se.creation AS since, se.name AS ref, SUM(sed.qty) AS stems
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		LEFT JOIN `tabItem` i ON i.name = sed.item_code
		WHERE se.docstatus = 1 AND se.stock_entry_type = 'Harvesting' AND se.posting_date >= %(since)s
		  AND IFNULL(se.custom_receiving_entry, '') = '' AND IFNULL(se.custom_bucket_id, '') != ''
		  AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` n
		        WHERE n.custom_bucket_id = se.custom_bucket_id AND n.docstatus = 1 AND n.name != se.name
		          AND n.stock_entry_type IN ('Receiving', 'Late Receipt', 'Harvesting', 'Discard')
		          AND n.creation > se.creation)
		  AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = se.custom_bucket_id
		        AND si.stem_qty > 0 AND si.date_added >= se.creation)
		GROUP BY se.custom_bucket_id, se.farm, sed.item_code, sed.custom_stem_length, se.custom_stem_length, se.name, se.creation
		""",
		{"since": since},
		as_dict=True,
	)


def _quarantine_rows():
	"""Net stems held in quarantine per bucket / variety / length."""
	return frappe.db.sql(
		"""
		SELECT UPPER(TRIM(se.custom_bucket_id)) AS bucket_id, sed.item_code AS variety, i.item_name, i.item_group,
		       IFNULL(NULLIF(TRIM(COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length)), ''), '') AS length,
		       SUM(CASE WHEN se.stock_entry_type = 'Receiving Quarantined' THEN sed.qty ELSE -sed.qty END) AS stems,
		       MIN(se.creation) AS since,
		       IFNULL(NULLIF(MAX(se.farm), ''),
		              REPLACE(MAX(CASE WHEN se.stock_entry_type = 'Receiving Quarantined' THEN sed.t_warehouse END),
		                      ' Receiving Quarantined - KR', '')) AS farm,
		       MAX(se.custom_receiving_batch_id) AS batch
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		LEFT JOIN `tabItem` i ON i.name = sed.item_code
		WHERE se.docstatus = 1 AND IFNULL(se.custom_bucket_id, '') != ''
		  AND se.stock_entry_type IN ('Receiving Quarantined', 'Quarantine Accept', 'Quarantine Rejects')
		GROUP BY UPPER(TRIM(se.custom_bucket_id)), sed.item_code, sed.custom_stem_length, se.custom_stem_length
		HAVING stems > 0
		""",
		as_dict=True,
	)


def _transit_rows():
	return frappe.db.sql(
		"""
		SELECT p.bucket AS bucket_id, p.item_code AS variety, i.item_name, i.item_group,
		       IFNULL(p.stem_length, '') AS length, p.farm, p.stock_qty AS stems, p.parent AS ref,
		       p.sales_order, p.sales_order_item,
		       COALESCE(p.left_farm_at, p.loaded_on_truck_at, p.loaded_in_trolley_at) AS since,
		       (p.loaded_on_truck = 1 OR p.left_farm_at IS NOT NULL) AS on_truck
		FROM `tabPick List Item` p
		INNER JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2
		LEFT JOIN `tabItem` i ON i.name = p.item_code
		WHERE (p.loaded_in_trolley = 1 OR p.loaded_on_truck = 1 OR p.in_transit = 1)
		  AND IFNULL(p.shelved, 0) = 0 AND IFNULL(p.issued, 0) = 0 AND IFNULL(p.issued_offline, 0) = 0
		  AND IFNULL(p.not_found, 0) = 0
		  AND COALESCE(p.left_farm_at, p.loaded_on_truck_at, p.loaded_in_trolley_at) >= %(since)s
		""",
		{"since": add_days(today(), -TRANSIT_DAYS)},
		as_dict=True,
	)


def _forecast(a):
	"""Variety -> {this_week, next_week} stems forecast in the greenhouses (grade only, no length)."""
	if not frappe.db.table_exists("Production Forecast Week"):
		return {}
	iso = a.today.isocalendar()
	nxt = getdate(add_days(a.today, 7)).isocalendar()
	rows = frappe.db.sql(
		"""
		SELECT pf.variety, w.iso_year AS y, w.week_number AS wk,
		       SUM(CASE WHEN IFNULL(w.revised_forecast_stems, 0) > 0 THEN w.revised_forecast_stems
		                WHEN IFNULL(w.manual_budget_stems, 0) > 0 THEN w.manual_budget_stems
		                ELSE IFNULL(w.budget_stems, 0) END) AS stems
		FROM `tabProduction Forecast` pf
		INNER JOIN `tabProduction Forecast Week` w ON w.parent = pf.name
		WHERE pf.status = 'Active' AND IFNULL(w.grade, '') IN ('', 'all')
		  AND ((w.iso_year = %(y1)s AND w.week_number = %(w1)s) OR (w.iso_year = %(y2)s AND w.week_number = %(w2)s))
		  AND pf.revision = (SELECT MAX(p2.revision) FROM `tabProduction Forecast` p2
		        WHERE p2.greenhouse = pf.greenhouse AND p2.variety = pf.variety
		          AND p2.forecast_year = pf.forecast_year AND p2.status = 'Active')
		GROUP BY pf.variety, w.iso_year, w.week_number
		""",
		{"y1": iso[0], "w1": iso[1], "y2": nxt[0], "w2": nxt[1]},
		as_dict=True,
	)
	out = defaultdict(lambda: {"this_week": 0.0, "next_week": 0.0})
	for r in rows:
		out[r.variety]["this_week" if (r.y, r.wk) == (iso[0], iso[1]) else "next_week"] += _f(r.stems)
	return {k: v for k, v in out.items() if v["this_week"] or v["next_week"]}


# ------------------------------------------------------------------ the ledger of stems


def collect(a):
	"""Every stem the system knows about as a list of items, plus the allocation
	ledger and demand. One pass; the endpoints below only slice it."""
	now = now_datetime()
	rows = stock.bucket_rows(farms=a.farms, lengths=a.lengths or None, rose=a.rose)
	rows = [r for r in rows if sv._qmatch(a.q, r.variety, r.item_name)]
	cfg = stock._config()
	fcfg = cfg["farm_config"]
	held = _discards()
	held_ids = set(held)
	shelf_keys = {(_up(r.bucket_id), r.variety, r.stem_length or "") for r in rows}
	if a.farms is not None or a.lengths or a.rose != "all" or a.q:
		# phantom detection must see the whole shelf, not the filtered slice
		full = stock.bucket_rows()
		shelf_keys = {(_up(r.bucket_id), r.variety, r.stem_length or "") for r in full}
	allocs = _allocations(shelf_keys, held_ids)
	by_key = defaultdict(list)
	for al in allocs:
		by_key[al.key].append(al)

	items = []

	def add(state, r_, stems, **kw):
		it = {
			"state": state,
			"variety": r_["variety"],
			"item_name": r_.get("item_name") or "",
			"item_group": r_.get("item_group") or "",
			"length": _ln(r_.get("length", r_.get("stem_length"))),
			"farm": r_.get("farm") or r_.get("shelf_farm") or "Unknown",
			"bucket": r_.get("bucket_id") or "",
			"stems": stems,
			"shelf": r_.get("shelf") or "",
			"age_days": r_.get("age_days"),
			"since": _ts(kw.pop("since", None)),
			"ref": kw.pop("ref", None),
			"detail": kw.pop("detail", ""),
			"ready_at": _ts(kw.pop("ready_at", None)),
			"remote": kw.pop("remote", False),
			"phantom": kw.pop("phantom", False),
			"cut_stage": r_.get("cut_stage") or "",
		}
		it.update(kw)
		items.append(it)

	for r in rows:
		farm = r.shelf_farm or "Unknown"
		known = farm in fcfg
		fc = fcfg.get(farm, {})
		key = (_up(r.bucket_id), r.variety, r.stem_length or "")
		stale = sum(x.qty for x in by_key.get(key, []) if x.reason in RECLAIM_REASONS)
		hb = held.get(r.bucket_id)
		for state, stems, extra in classify_shelf_row(r, held_by=hb, farm_cfg=fc, farm_known=known, stale_qty=stale, now=now):
			detail = ""
			if state == "discard" and hb:
				detail = f"On {hb['name']} ({hb['state']})"
			elif state == "too_old":
				detail = f"{r.age_days} days old; the limit at {farm} is {fc.get('max_allocation_age', 5)} days"
			elif state == "farm_disabled":
				detail = f"{farm} is not enabled in Production Settings"
			elif state == "reclaimable":
				why = sorted({STALE_LABEL[x.reason] for x in by_key.get(key, []) if x.reason in RECLAIM_REASONS})
				detail = "; ".join(why)
			elif state == "committed_live":
				ords = sorted({x.sales_order for x in by_key.get(key, []) if x.sales_order})
				detail = "Orders: " + ", ".join(ords[:4]) + ("..." if len(ords) > 4 else "")
			elif state == "sellable" and extra.get("remote"):
				detail = f"At {farm}: moved to the hub when allocated"
			add(
				state,
				r,
				stems,
				since=r.date_added,
				ref=extra.get("ref") and extra["ref"]["name"] if isinstance(extra.get("ref"), dict) else extra.get("ref"),
				detail=detail,
				ready_at=extra.get("ready_at"),
				remote=extra.get("remote", False),
				after=extra.get("after"),
				from_orders=sorted({x.sales_order for x in by_key.get(key, []) if x.reason in RECLAIM_REASONS and x.sales_order}) if state == "reclaimable" else None,
			)

	# stale allocations whose bucket is on no shelf: they hold no stems, but the order still
	# counts them as covered. (At-risk ones - bucket gone, not found - live in allocation health.)
	for al in allocs:
		if al.key in shelf_keys or al.reason not in RECLAIM_REASONS:
			continue
		if not (sv._qmatch(a.q, al.variety) and (not a.lengths or _ln(al.length) in a.lengths) and _farm_ok(a, al.farm)):
			continue
		add(
			"reclaimable",
			{"variety": al.variety, "length": al.length, "farm": al.farm, "bucket_id": al.bucket_id},
			al.qty,
			since=al.creation,
			ref=al.sales_order,
			detail=STALE_LABEL[al.reason] + " - and the bucket is on no shelf",
			phantom=True,
		)

	groups = _rose_groups(a)

	def keep(r):
		return (
			sv._qmatch(a.q, r.variety, r.get("item_name"))
			and (not a.lengths or _ln(r.get("length")) in a.lengths)
			and _farm_ok(a, r.get("farm"))
			and (groups is None or r.get("item_group") in groups)
		)

	for r in _coldroom_rows(a):
		if keep(r):
			add("coldroom", r, _f(r.stems), since=r.since, ref=r.ref, detail=f"Received at {r.farm or 'Unknown'}, not on a shelf yet")
	for r in _harvest_rows(a):
		if not keep(r):
			continue
		hrs = (now - get_datetime(r.since)).total_seconds() / 3600 if r.since else 0
		if hrs <= AWAITING_HOURS:
			add("awaiting", r, _f(r.stems), since=r.since, ref=r.ref, detail=f"Harvested {int(hrs)}h ago at {r.farm or 'Unknown'}")
		else:
			add("unreceived", r, _f(r.stems), since=r.since, ref=r.ref, detail=f"Harvested {int(hrs // 24)}d ago at {r.farm or 'Unknown'} and never received")
	for r in _quarantine_rows():
		if keep(r):
			add("quarantine", r, _f(r.stems), since=r.since, ref=r.batch, detail="Held in quarantine pending a quality decision")
	for r in _transit_rows():
		if keep(r):
			add("committed_transit", r, _f(r.stems), since=r.since, ref=r.ref, detail=("On the truck" if r.on_truck else "On a trolley") + f" from {r.farm or 'Unknown'}")

	for it in items:
		it["rose_type"] = rose_type(it["item_group"])
	return {"items": items, "allocs": allocs, "now": now}


def _rose_groups(a):
	"""Item groups allowed by the rose filter (None = no filter)."""
	if a.rose == "all":
		return None
	params = {}
	cond = rose_sql("ig.name", a.rose, params)
	if not cond:
		return None
	return set(frappe.db.sql_list(f"SELECT ig.name FROM `tabItem Group` ig WHERE 1=1 {cond}", params))  # noqa: S608


def demand(a, lines_cache=None):
	"""Open order lines delivering from today to a.through, still to allocate."""
	lines = sv._orders(a.farms, a.lengths, a.rose, a.q, a.today, a.through)
	return [ln for ln in lines if ln.to_allocate > 0 or ln.ordered_stems > 0]


# ------------------------------------------------------------------ roll-ups


def _zero():
	return {s: 0.0 for s in STATE_ORDER}


def _group_totals(st):
	g = {"sellable": st["sellable"], "soon": 0.0, "committed": 0.0, "withheld": 0.0}
	for s in STATE_ORDER:
		grp = STATE_META[s][1]
		if grp != "sellable":
			g[grp] += st[s]
	return g


def _physical(st):
	"""Stems that exist in a place (reclaimable phantom allocations are not stems)."""
	return sum(st.values())


@frappe.whitelist(methods=["GET"])
def overview(
	region: Any = None,
	farm: Any = None,
	length: Any = None,
	rose: Any = None,
	q: Any = None,
	through: Any = None,
):
	"""Everything the board needs in one read: KPIs, reasons, varieties by state."""
	_guard()
	try:
		a = _args(dict(region=region, farm=farm, length=length, rose=rose, q=q, through=through))
		c = collect(a)
		lines = demand(a)
		forecast = _forecast(a)

		V = {}

		def var(it):
			v = V.get(it["variety"])
			if v is None:
				v = V[it["variety"]] = {
					"variety": it["variety"],
					"item_name": it["item_name"],
					"item_group": it["item_group"],
					"rose_type": it["rose_type"],
					"states": _zero(),
					"remote": 0.0,
					"phantom": 0.0,
					"buckets": set(),
					"oldest_days": None,
					"by_len": {},
					"farms": defaultdict(_zero),
					"ordered": 0.0,
					"to_allocate": 0.0,
				}
			return v

		def cell(v, length):
			cl = v["by_len"].get(length)
			if cl is None:
				cl = v["by_len"][length] = {"states": _zero(), "ordered": 0.0, "to_allocate": 0.0}
			return cl

		reasons = {s: {"stems": 0.0, "varieties": set(), "buckets": set(), "oldest_days": None, "since": None} for s in STATE_ORDER}
		for it in c["items"]:
			v = var(it)
			s, n = it["state"], it["stems"]
			v["states"][s] += n
			cell(v, it["length"])["states"][s] += n
			v["farms"][it["farm"]][s] += n
			if it["phantom"]:
				v["phantom"] += n
			if s == "sellable" and it["remote"]:
				v["remote"] += n
			if it["bucket"]:
				v["buckets"].add(it["bucket"])
			age = it["age_days"]
			if age is not None and not it["phantom"]:
				v["oldest_days"] = age if v["oldest_days"] is None else max(v["oldest_days"], age)
			r = reasons[s]
			r["stems"] += n
			r["varieties"].add(it["variety"])
			if it["bucket"]:
				r["buckets"].add(it["bucket"])
			if it["since"] and (r["since"] is None or it["since"] < r["since"]):
				r["since"] = it["since"]
			if age is not None and not it["phantom"]:
				r["oldest_days"] = age if r["oldest_days"] is None else max(r["oldest_days"], age)
		for ln in lines:
			v = V.get(ln.item_code)
			if v is None:
				v = V[ln.item_code] = {
					"variety": ln.item_code,
					"item_name": ln.item_name,
					"item_group": ln.item_group,
					"rose_type": rose_type(ln.item_group),
					"states": _zero(),
					"remote": 0.0,
					"phantom": 0.0,
					"buckets": set(),
					"oldest_days": None,
					"by_len": {},
					"farms": defaultdict(_zero),
					"ordered": 0.0,
					"to_allocate": 0.0,
				}
			v["ordered"] += ln.ordered_stems
			v["to_allocate"] += ln.to_allocate
			cl = cell(v, ln.length)
			cl["ordered"] += ln.ordered_stems
			cl["to_allocate"] += ln.to_allocate

		rows = []
		for v in V.values():
			st = v["states"]
			g = _group_totals(st)
			v["groups"] = g
			v["atp"] = st["sellable"] - v["to_allocate"]  # free to promise
			v["cover"] = (
				"none" if v["to_allocate"] <= 0 and g["sellable"] <= 0 and not g["soon"]
				else "short" if v["atp"] < 0
				else "tight" if v["to_allocate"] > 0 and st["sellable"] / v["to_allocate"] < 1.1
				else "ok"
			)
			v["buckets"] = len(v["buckets"])
			v["farms"] = [dict(farm=k, **s) for k, s in sorted(v["farms"].items())]
			v["forecast"] = forecast.get(v["variety"])
			for ln_, cl in v["by_len"].items():
				cl["atp"] = cl["states"]["sellable"] - cl["to_allocate"]
			rows.append(v)
		rows.sort(key=lambda r: r["variety"])

		T = _zero()
		for v in rows:
			for s in STATE_ORDER:
				T[s] += v["states"][s]
		G = _group_totals(T)
		phantom = sum(v["phantom"] for v in rows)
		to_alloc = sum(v["to_allocate"] for v in rows)
		ordered = sum(v["ordered"] for v in rows)
		kpis = {
			**G,
			"states": T,
			"on_hand": _physical(T) - phantom,
			"remote_sellable": sum(v["remote"] for v in rows),
			"reclaimable": T["reclaimable"],
			"phantom_allocations": phantom,
			"to_allocate": to_alloc,
			"ordered": ordered,
			"free_to_promise": T["sellable"] - to_alloc,
			"varieties": len(rows),
			"varieties_short": sum(1 for v in rows if v["cover"] == "short"),
			"forecast_this_week": sum((v["forecast"] or {}).get("this_week", 0) for v in rows),
			"forecast_next_week": sum((v["forecast"] or {}).get("next_week", 0) for v in rows),
		}
		reason_rows = []
		for s in STATE_ORDER:
			r = reasons[s]
			label, group, why, who = STATE_META[s]
			reason_rows.append(
				{
					"state": s, "label": label, "group": group, "why": why, "who": who,
					"stems": r["stems"], "varieties": len(r["varieties"]), "buckets": len(r["buckets"]),
					"oldest_days": r["oldest_days"], "since": r["since"],
				}
			)
		sc = stock._config()
		return {
			"success": True,
			"through": str(a.through),
			"today": str(a.today),
			"kpis": kpis,
			"reasons": reason_rows,
			"rows": rows,
			"limits": {
				"discard_age": sc["discard_age"],
				"farms": {f: {k: c_.get(k) for k in ("sales_shelf", "max_allocation_age", "cooling_hours")} for f, c_ in sc["farm_config"].items()},
			},
			"options": _options(),
			"states": {k: {"label": m[0], "group": m[1], "why": m[2], "who": m[3]} for k, m in STATE_META.items()},
		}
	except Exception:
		frappe.log_error("v2 stock_truth.overview")
		return {"success": False, "error": "Could not load stock visibility; the error was logged."}


def _options():
	farms, lens = sv._options()
	return {"farms": farms, "lengths": lens}


@frappe.whitelist(methods=["GET"])
def ledger(
	state: Any = None,
	variety: Any = None,
	length: Any = None,
	farm: Any = None,
	region: Any = None,
	rose: Any = None,
	q: Any = None,
	limit: Any = 200,
	offset: Any = 0,
	sort: Any = None,
):
	"""Bucket-level rows behind any number on the board."""
	_guard()
	try:
		a = _args(dict(region=region, farm=farm, length=length, rose=rose, q=q))
		items = collect(a)["items"]
		states = set(sv._list(state)) or None
		rows = [
			i
			for i in items
			if (not states or i["state"] in states)
			and (not variety or i["variety"] == variety)
			and (not a.lengths or i["length"] in a.lengths)
		]
		rows.sort(key=lambda i: (-(i["age_days"] or 0), -i["stems"]) if sort != "stems" else (-i["stems"],))
		lim = max(1, min(cint(limit) or 200, 1000))
		return {
			"success": True,
			"total": len(rows),
			"stems": sum(i["stems"] for i in rows),
			"rows": rows[cint(offset) : cint(offset) + lim],
		}
	except Exception:
		frappe.log_error("v2 stock_truth.ledger")
		return {"success": False, "error": "Could not load the stock list; the error was logged."}


@frappe.whitelist(methods=["GET"])
def variety(variety: Any, region: Any = None, farm: Any = None, through: Any = None):
	"""One variety, opened up: every length, every state, the orders holding it."""
	_guard()
	try:
		a = _args(dict(region=region, farm=farm, q=str(variety).lower(), through=through))
		c = collect(a)
		items = [i for i in c["items"] if i["variety"] == variety]
		lines = [ln for ln in demand(a) if ln.item_code == variety]
		lens = defaultdict(lambda: {"states": _zero(), "ordered": 0.0, "to_allocate": 0.0, "oldest_days": None})
		for i in items:
			cl = lens[i["length"]]
			cl["states"][i["state"]] += i["stems"]
			if i["age_days"] is not None and not i["phantom"]:
				cl["oldest_days"] = i["age_days"] if cl["oldest_days"] is None else max(cl["oldest_days"], i["age_days"])
		for ln in lines:
			cl = lens[ln.length]
			cl["ordered"] += ln.ordered_stems
			cl["to_allocate"] += ln.to_allocate
		length_rows = []
		for k, cl in lens.items():
			cl["length"] = k
			cl["atp"] = cl["states"]["sellable"] - cl["to_allocate"]
			cl["groups"] = _group_totals(cl["states"])
			length_rows.append(cl)
		length_rows.sort(key=lambda r: sv._len_key(r["length"]))
		orders = defaultdict(lambda: {"qty": 0.0, "reasons": set(), "buckets": set()})
		for al in c["allocs"]:
			if al.variety != variety or not _farm_ok(a, al.farm):
				continue
			o = orders[al.sales_order]
			o.update(sales_order=al.sales_order, customer=al.customer_name or al.customer, delivery_date=str(al.delivery_date or ""), created=_ts(al.creation))
			o["qty"] += al.qty
			o["buckets"].add(al.bucket_id)
			if al.reason:
				o["reasons"].add(al.reason)
		order_rows = []
		for o in orders.values():
			o["buckets"] = len(o["buckets"])
			o["reasons"] = [{"code": r_, "label": STALE_LABEL[r_]} for r_ in sorted(o["reasons"])]
			order_rows.append(o)
		order_rows.sort(key=lambda o: (not o["reasons"], o["delivery_date"]))
		ts = items[0] if items else {}
		return {
			"success": True,
			"variety": variety,
			"item_name": ts.get("item_name", ""),
			"lengths": length_rows,
			"orders": order_rows,
			"forecast": _forecast(a).get(variety),
			"farms": sorted({i["farm"] for i in items}),
			"demand": [
				{"sales_order": ln.parent, "customer": ln.customer_name or ln.customer, "delivery_date": str(ln.delivery_date), "length": ln.length, "ordered": ln.ordered_stems, "to_allocate": ln.to_allocate}
				for ln in lines
				if ln.to_allocate > 0
			][:200],
		}
	except Exception:
		frappe.log_error("v2 stock_truth.variety")
		return {"success": False, "error": "Could not load the variety; the error was logged."}


@frappe.whitelist(methods=["GET"])
def allocations(region: Any = None, farm: Any = None, length: Any = None, rose: Any = None, q: Any = None, reason: Any = None):
	"""Allocation health: outstanding allocations that hold, or may hold, stock for nobody."""
	_guard()
	try:
		a = _args(dict(region=region, farm=farm, length=length, rose=rose, q=q))
		allocs = collect(a)["allocs"]
		blocked = _blocked_pairs({x.sales_order_item for x in allocs if x.reason in RECLAIM_REASONS})
		want = set(sv._list(reason))
		by_reason = defaultdict(lambda: {"qty": 0.0, "rows": 0, "orders": set()})
		orders = defaultdict(lambda: {"qty": 0.0, "lines": []})
		total_open = 0.0
		reclaim_held = 0.0
		for al in allocs:
			if not (sv._qmatch(a.q, al.variety) and (not a.lengths or _ln(al.length) in a.lengths) and _farm_ok(a, al.farm)):
				continue
			total_open += al.qty
			if not al.reason or (want and al.reason not in want):
				continue
			if al.reason in RECLAIM_REASONS and al.held:
				reclaim_held += al.qty
			b = by_reason[al.reason]
			b["qty"] += al.qty
			b["rows"] += 1
			b["orders"].add(al.sales_order)
			o = orders[al.sales_order]
			o.update(sales_order=al.sales_order, customer=al.customer_name or al.customer, delivery_date=str(al.delivery_date or ""), so_status=al.so_status)
			o["qty"] += al.qty
			o["lines"].append(
				{"sales_order_item": al.sales_order_item, "stem_length": al.length, "releasable": al.reason in RELEASABLE and not blocked.get((al.sales_order_item, _up(al.bucket_id))),
				"release_block": _release_block(al, blocked), "variety": al.variety, "length": _ln(al.length), "bucket": al.bucket_id, "farm": al.farm, "qty": al.qty, "reason": al.reason, "reason_label": STALE_LABEL[al.reason], "allocated_days": al.age_days, "in_transit": bool(al.in_transit), "on_discard": bool(al.held)}
			)
		order_rows = sorted(orders.values(), key=lambda o: -o["qty"])
		return {
			"success": True,
			"total_open": total_open,
			"reclaimable": sum(v["qty"] for k, v in by_reason.items() if k in RECLAIM_REASONS),
			"reclaimable_on_discard": reclaim_held,
			"can_release": _can_release(),
			"at_risk": sum(v["qty"] for k, v in by_reason.items() if k in RISK_REASONS),
			"by_reason": [
				{"code": k, "label": STALE_LABEL[k], "kind": "reclaim" if k in RECLAIM_REASONS else "risk", "qty": v["qty"], "rows": v["rows"], "orders": len(v["orders"])}
				for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["qty"])
			],
			"orders": order_rows[:300],
			"order_count": len(order_rows),
		}
	except Exception:
		frappe.log_error("v2 stock_truth.allocations")
		return {"success": False, "error": "Could not load allocations; the error was logged."}


# ------------------------------------------------------------------ releasing stale allocations

RELEASE_ROLES = ("System Manager", "Sales Manager", "Sales Master Manager", "Sales Representative", "Sales User")  # the Sales Allocation page's own roles
RELEASABLE = ("order_cancelled", "delivery_passed")  # "already delivered" is left to a person: the stems have gone out the door
RELEASE_CHUNK = 10
RELEASE_BUDGET_SECONDS = 20  # a request stops taking new lines after this long, however many it has done


def _can_release():
	return bool(set(frappe.get_roles()) & set(RELEASE_ROLES))


def _blocked_pairs(soi_names):
	"""(sales order item, bucket) -> why a pick-list row is already past the point of releasing."""
	if not soi_names:
		return {}
	out = {}
	for r in frappe.db.sql(
		"""
		SELECT p.sales_order_item AS soi, UPPER(p.bucket) AS b,
		       MAX(IFNULL(p.issued, 0) = 1 OR IFNULL(p.issued_offline, 0) = 1) AS issued,
		       MAX(IFNULL(p.loaded_in_trolley, 0) = 1 OR IFNULL(p.loaded_on_truck, 0) = 1) AS moving
		FROM `tabPick List Item` p
		INNER JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2
		WHERE p.sales_order_item IN %(i)s AND IFNULL(p.bucket, '') != ''
		GROUP BY p.sales_order_item, UPPER(p.bucket)
		""",
		{"i": tuple(soi_names)},
		as_dict=True,
	):
		if r.issued:
			out[(r.soi, r.b)] = "Already issued - undo the issue first"
		elif r.moving:
			out[(r.soi, r.b)] = "On a trolley or truck - let it arrive first"
	return out


def _release_block(al, blocked):
	if al.reason == "dispatched":
		return "The order was delivered - review it in Sales Allocation"
	if al.reason not in RELEASABLE:
		return ""
	return blocked.get((al.sales_order_item, _up(al.bucket_id)), "")


def _release_groups(a, sales_order=None, items=None, skip=None, sales_orders=None):
	"""Releasable allocations grouped by (sales order item, bucket, length) + what is skipped and why."""
	allocs = _allocations(set(), set())  # staleness of cancelled / past-due orders does not need the shelf
	allocs = [x for x in allocs if x.reason in RECLAIM_REASONS]
	allocs = [x for x in allocs if sv._qmatch(a.q, x.variety) and (not a.lengths or _ln(x.length) in a.lengths) and _farm_ok(a, x.farm)]
	if sales_order:
		allocs = [x for x in allocs if x.sales_order == sales_order]
	if sales_orders:
		chosen = set(sales_orders)
		allocs = [x for x in allocs if x.sales_order in chosen]
	if items:
		want = {(i.get("sales_order_item"), _up(i.get("bucket_id")), i.get("stem_length") or "") for i in items}
		allocs = [x for x in allocs if (x.sales_order_item, _up(x.bucket_id), x.length or "") in want]
	if skip:
		gone = {(i.get("sales_order_item"), _up(i.get("bucket_id")), i.get("stem_length") or "") for i in skip}
		allocs = [x for x in allocs if (x.sales_order_item, _up(x.bucket_id), x.length or "") not in gone]
	blocked = _blocked_pairs({x.sales_order_item for x in allocs})
	groups, skipped = {}, defaultdict(lambda: {"stems": 0.0, "orders": set()})
	for x in allocs:
		why = _release_block(x, blocked)
		if x.reason not in RELEASABLE or why:
			k = why or "The order was delivered - review it in Sales Allocation"
			skipped[k]["stems"] += x.qty
			skipped[k]["orders"].add(x.sales_order)
			continue
		key = (x.sales_order_item, x.bucket_id, x.length or "")
		g = groups.setdefault(key, {"sales_order_item": x.sales_order_item, "bucket_id": x.bucket_id, "stem_length": x.length or "", "sales_order": x.sales_order, "variety": x.variety, "qty": 0.0, "reason": x.reason})
		g["qty"] += x.qty
	return list(groups.values()), skipped


def _release_index_missing():
	return bool(frappe.db.has_column("Stock Entry", "custom_issued_to")) and not frappe.db.sql(
		"SHOW INDEX FROM `tabStock Entry` WHERE Column_name = 'custom_issued_to'"
	)


def build_release_index():
	"""Background job: index Stock Entry.custom_issued_to (the patch does the same on migrate)."""
	frappe.db.add_index("Stock Entry", ["custom_issued_to"])
	frappe.db.commit()  # nosemgrep: frappe-manual-commit


def _ensure_release_index():
	"""True while the lookup a release depends on is not indexed yet; starts building it."""
	if not _release_index_missing():
		return False
	frappe.enqueue(
		"upande_packhouse.api.v2.stock_truth.build_release_index",
		queue="long",
		timeout=1800,
		job_id="stock_truth_release_index",
		deduplicate=True,
		enqueue_after_commit=True,
	)
	return True


@frappe.whitelist(methods=["GET", "POST"])
def release_plan(sales_order: Any = None, items: Any = None, region: Any = None, farm: Any = None, length: Any = None, rose: Any = None, q: Any = None, sales_orders: Any = None):
	"""What a release would do, without doing it (feeds the confirmation)."""
	_guard()
	a = _args(dict(region=region, farm=farm, length=length, rose=rose, q=q))
	groups, skipped = _release_groups(a, sales_order, frappe.parse_json(items) if isinstance(items, str) else items, None, frappe.parse_json(sales_orders) if isinstance(sales_orders, str) else sales_orders)
	soi = {g["sales_order_item"] for g in groups}
	opls = sorted(frappe.db.sql_list("SELECT DISTINCT p.parent FROM `tabPick List Item` p INNER JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2 WHERE p.sales_order_item IN %(i)s", {"i": tuple(soi)})) if soi else []
	return {
		"success": True,
		"can_release": _can_release(),
		"index_building": _ensure_release_index(),
		"lines": len(groups),
		"stems": sum(g["qty"] for g in groups),
		"orders": len({g["sales_order"] for g in groups}),
		"order_names": sorted({g["sales_order"] for g in groups})[:50],
		"opls": opls[:50],
		"opl_count": len(opls),
		"skipped": [{"why": k, "stems": v["stems"], "orders": len(v["orders"])} for k, v in skipped.items()],
	}


@frappe.whitelist(methods=["POST"])
def release_stale(sales_order: Any = None, items: Any = None, region: Any = None, farm: Any = None, length: Any = None, rose: Any = None, q: Any = None, limit: Any = RELEASE_CHUNK, confirm: Any = 0, skip: Any = None, sales_orders: Any = None, budget: Any = RELEASE_BUDGET_SECONDS):
	"""Release stale allocations: cancel them, put the stems back on the shelf and take the lines off their pick lists.

	It is the Sales Allocation page's own unallocate step (unallocate_bucket_from_opl), run for
	allocations that are STILL stale at this moment - re-checked here, never trusted from the
	browser - and only for cancelled or past-due orders whose stems have not been issued or
	loaded. Works in chunks; call again while `remaining` is more than 0."""
	_guard()
	if not _can_release():
		frappe.throw(_("You need a sales role to release allocations."), frappe.PermissionError)
	if not cint(confirm):
		frappe.throw(_("Confirm the release first."))
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import unallocate_bucket_from_opl

	a = _args(dict(region=region, farm=farm, length=length, rose=rose, q=q))
	groups, _skipped = _release_groups(a, sales_order, frappe.parse_json(items) if isinstance(items, str) else items, frappe.parse_json(skip) if isinstance(skip, str) else skip, frappe.parse_json(sales_orders) if isinstance(sales_orders, str) else sales_orders)
	todo = groups[: max(1, min(cint(limit) or RELEASE_CHUNK, 50))]
	done, failed, orders = [], [], defaultdict(float)
	t0 = time.monotonic()
	handled = 0
	for g in todo:
		if handled and time.monotonic() - t0 > max(5, min(cint(budget) or RELEASE_BUDGET_SECONDS, 40)):
			break  # out of time for this request: the caller asks again for the rest
		handled += 1
		try:
			res = unallocate_bucket_from_opl(g["sales_order_item"], g["bucket_id"], g["stem_length"])
		except Exception as e:  # the allocator rolls its own failures back; keep going with the next line
			res = {"success": False, "message": str(e)}
		if res.get("success"):
			done.append({k: g[k] for k in ("sales_order", "sales_order_item", "bucket_id", "variety", "qty")})
			orders[g["sales_order"]] += g["qty"]
		else:
			failed.append({"sales_order": g["sales_order"], "sales_order_item": g["sales_order_item"], "stem_length": g["stem_length"], "bucket_id": g["bucket_id"], "variety": g["variety"], "qty": g["qty"], "error": (res.get("message") or "")[:300]})
	for so, qty in orders.items():
		try:
			frappe.get_doc("Sales Order", so).add_comment("Info", f"Stale allocation released from Stock Visibility by {frappe.session.user}: {int(qty):,} stems (order cancelled or delivery date passed).")
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Stock Visibility release comment")
	try:
		frappe.cache.delete_keys("stock_truth_outlook:*")
	except Exception:
		pass
	return {
		"success": True,
		"released": done,
		"failed": failed,
		"stems_released": sum(x["qty"] for x in done),
		"remaining": max(0, len(groups) - handled),
		"seconds": round(time.monotonic() - t0, 1),
	}


# ------------------------------------------------------------------ the availability check


@frappe.whitelist(methods=["POST"])
def check(lines: Any = None, date: Any = None, region: Any = None, farm: Any = None):
	"""Can we confirm these lines for this delivery date? One verdict per line.

	Safe        sellable now, net of orders delivering up to that date and not yet allocated
	Possible    plus stems still cooling / on the coldroom floor / just harvested that will be
	            ready before then
	Downgrade   plus longer stems of the same variety the allocator would accept as a downgrade
	"""
	_guard()
	try:
		req = frappe.parse_json(lines) if isinstance(lines, str) else (lines or [])
		if not req:
			frappe.throw("Add at least one line to check.")
		when = getdate(date) if date else getdate(add_days(today(), 1))
		a = _args(dict(region=region, farm=farm, through=str(when)))
		a.through = when
		c = collect(a)
		dem = demand(a)
		cfgs = stock._config()
		amber = cfgs.get("amber_time", 3)
		by_v = defaultdict(list)
		for i in c["items"]:
			by_v[i["variety"]].append(i)
		dem_by = defaultdict(float)
		for ln in dem:
			dem_by[(ln.item_code, ln.length)] += ln.to_allocate
		out = []
		for r in req[:50]:
			v = (r.get("variety") or "").strip()
			ln = _ln(r.get("length"))
			qty = _f(r.get("qty"))
			known = frappe.db.get_value("Item", v, ["name", "item_name"], as_dict=True) if v else None
			if not known:
				hit = frappe.db.get_value("Item", {"item_name": v}, ["name", "item_name"], as_dict=True) if v else None
				known = hit
			if not known:
				out.append({"variety": v, "length": ln, "qty": qty, "code": "unknown", "label": "Variety not found", "message": f"No variety called '{v}'."})
				continue
			v = known.name
			its = by_v.get(v, [])
			exact = [i for i in its if i["length"] == ln]
			sell = sum(i["stems"] for i in exact if i["state"] == "sellable")
			remote = sum(i["stems"] for i in exact if i["state"] == "sellable" and i["remote"])
			owed = dem_by.get((v, ln), 0.0)
			safe = max(0.0, sell - owed)
			rem_owed = max(0.0, owed - sell)
			soon_items = []
			for i in exact:
				if i["state"] == "cooling" and i["ready_at"] and getdate(get_datetime(i["ready_at"])) <= when:
					soon_items.append(i)
				elif i["state"] in ("coldroom", "awaiting") and when > a.today:
					soon_items.append(i)
			soon_raw = sum(i["stems"] for i in soon_items)
			soon = max(0.0, soon_raw - rem_owed)
			dg = []
			dg_total = 0.0
			req_cm = _cm(ln)
			for L in sorted({i["length"] for i in its if i["state"] == "sellable" and _cm(i["length"]) > req_cm}, key=_cm):
				n = sum(i["stems"] for i in its if i["length"] == L and i["state"] == "sellable")
				n = max(0.0, n - dem_by.get((v, L), 0.0))
				if n > 0:
					age = max((i["age_days"] or 0) for i in its if i["length"] == L and i["state"] == "sellable")
					dg.append({"length": L, "stems": n, "needs_approval": age < amber, "oldest_days": age})
					dg_total += n
			code, label = verdict(qty, safe, soon, dg_total)
			blockers = defaultdict(float)
			for i in exact:
				if STATE_META[i["state"]][1] == "withheld" or i["state"] == "reclaimable":
					blockers[i["state"]] += i["stems"]
			blockers_rows = [
				{"state": s, "label": STATE_META[s][0], "stems": n, "who": STATE_META[s][3]}
				for s, n in sorted(blockers.items(), key=lambda kv: -kv[1])
			]
			other_len = defaultdict(float)
			for i in its:
				if i["length"] != ln and i["state"] == "sellable":
					other_len[i["length"]] += i["stems"]
			parts = [f"{int(safe):,} safe now"]
			if soon:
				parts.append(f"{int(soon):,} more by {when:%d %b}")
			if dg_total:
				parts.append(f"{int(dg_total):,} as longer stems")
			short = max(0.0, qty - safe - soon - dg_total)
			out.append(
				{
					"variety": v,
					"item_name": known.item_name,
					"length": ln,
					"qty": qty,
					"code": code,
					"label": label,
					"safe": safe,
					"soon": soon,
					"downgrade": dg_total,
					"short": short,
					"sellable_now": sell,
					"remote": remote,
					"owed_to_orders": owed,
					"downgrades": dg,
					"blockers": blockers_rows,
					"other_lengths": [{"length": k, "stems": n} for k, n in sorted(other_len.items(), key=lambda kv: _cm(kv[0]))],
					"message": ", ".join(parts) + (f"; short by {int(short):,}" if short else ""),
					"confirm_up_to": safe,
				}
			)
		return {"success": True, "date": str(when), "lines": out}
	except frappe.ValidationError:
		raise
	except Exception:
		frappe.log_error("v2 stock_truth.check")
		return {"success": False, "error": "Could not run the check; the error was logged."}


# ------------------------------------------------------------------ order outlook


SOURCES = (
	("shelf", "On the shelf", "Free, fresh stems on a sales shelf."),
	("remote", "At remote farms", "Free stems at a farm that is not a sales shelf; they move to the hub when allocated."),
	("floor", "Coldroom floor", "Received but not shelved yet."),
	("cooling", "Still cooling", "On a shelf, ready before the delivery date."),
	("arriving", "Harvested, not received", "Cut in the last 36 hours and not booked in yet."),
	("overdue", "Harvested, overdue", "Cut more than 36 hours ago and still not received."),
	("stale", "Held by stale allocations", "Promised to cancelled or past-due orders; free once released."),
	("downgrade", "Longer stems", "Longer stems of the same variety (a downgrade; needs approval)."),
	("greenhouse", "Not cut yet", "Greenhouse forecast for the weeks before delivery (grade level; length not guaranteed)."),
)
SOURCE_KEYS = [x[0] for x in SOURCES]
EXACT_ORDER = ("shelf", "remote", "stale", "cooling", "floor", "arriving", "overdue")
SOON_KEYS = ("remote", "stale", "floor", "arriving", "overdue", "cooling")  # exists, but not on a sales shelf yet
# worst first
STATUS_RANK = {"short": 0, "greenhouse": 1, "downgrade": 2, "action": 3, "available": 4, "allocated": 5}
WHERE_SHOWN = 8
OUTLOOK_CACHE_SECONDS = 10  # several people watching the same date share one computation


def _empty_pool():
	return {k: [] for k in EXACT_ORDER}


def pool_totals(pool, when):
	"""Stems available for one variety and length, by source, as of the delivery date.

	Straight totals of what exists: nothing is split between orders, because
	allocation does that - stock that is not allocated is available."""
	t = {k: sum(i["qty"] for i in (pool or {}).get(k, [])) for k in ("shelf", "remote", "stale", "floor", "arriving", "overdue")}
	t["cooling"] = sum(i["qty"] for i in (pool or {}).get("cooling", []) if i["ready_date"] <= when)
	return t


def line_status(need, av, longer=0.0, forecast=0.0):
	"""allocated / available / action / downgrade / greenhouse / short for one line.

	Each line is measured against everything that exists for its variety and
	length - never against what other orders might take."""
	if need <= 0:
		return "allocated"
	if av["shelf"] >= need:
		return "available"
	soon = sum(av[k] for k in SOON_KEYS)
	if av["shelf"] + soon >= need:
		return "action"
	if av["shelf"] + soon + longer >= need and longer > 0:
		return "downgrade"
	if av["shelf"] + soon + longer + forecast >= need and forecast > 0:
		return "greenhouse"
	return "short"


def _where(pool, when):
	"""The places that stock is, biggest first within each source: shelf and bucket, not a share-out."""
	rows = []
	for src in EXACT_ORDER:
		for it in sorted((pool or {}).get(src, []), key=lambda i: -i["qty"]):
			if src == "cooling" and it["ready_date"] > when:
				continue
			rows.append({"source": src, "stems": it["qty"], "farm": it.get("farm", ""), "shelf": it.get("shelf", ""), "bucket": it.get("bucket", ""), "age_days": it.get("age_days"), "ready_at": it.get("ready_at"), "detail": it.get("detail", ""), "ref": it.get("ref")})
	return rows


def _forecast_window(a, d_to):
	"""{variety: [(available_from, stems_remaining)]} for the weeks from this week to the delivery week."""
	if not frappe.db.table_exists("Production Forecast Week"):
		return {}
	weeks = []
	d = a.today - timedelta(days=a.today.weekday())  # Monday of this week
	while d <= d_to and len(weeks) < 8:
		iso = d.isocalendar()
		weeks.append((iso[0], iso[1], d))
		d += timedelta(days=7)
	if not weeks:
		return {}
	cond = " OR ".join(["(w.iso_year = %s AND w.week_number = %s)"] * len(weeks))
	vals = [x for y, wk, _d in weeks for x in (y, wk)]
	rows = frappe.db.sql(
		f"""
		SELECT pf.variety, w.iso_year AS y, w.week_number AS wk,
		       SUM(CASE WHEN IFNULL(w.revised_forecast_stems, 0) > 0 THEN w.revised_forecast_stems
		                WHEN IFNULL(w.manual_budget_stems, 0) > 0 THEN w.manual_budget_stems
		                ELSE IFNULL(w.budget_stems, 0) END) AS planned,
		       SUM(IFNULL(w.actual_stems, 0)) AS actual
		FROM `tabProduction Forecast` pf INNER JOIN `tabProduction Forecast Week` w ON w.parent = pf.name
		WHERE pf.status = 'Active' AND IFNULL(w.grade, '') IN ('', 'all') AND ({cond})
		  AND pf.revision = (SELECT MAX(p2.revision) FROM `tabProduction Forecast` p2
		        WHERE p2.greenhouse = pf.greenhouse AND p2.variety = pf.variety
		          AND p2.forecast_year = pf.forecast_year AND p2.status = 'Active')
		GROUP BY pf.variety, w.iso_year, w.week_number
		""",  # noqa: S608
		vals,
		as_dict=True,
	)
	start = {(y, wk): d for y, wk, d in weeks}
	out = defaultdict(list)
	for r in rows:
		left = max(0.0, _f(r.planned) - _f(r.actual))
		if left > 0:
			d = start[(r.y, r.wk)]
			out[r.variety].append((max(d, a.today), left))
	for v in out:
		out[v].sort()
	return dict(out)


def _build_pools(items):
	"""Supply pools by (variety, length), item level, oldest stems first (the allocator's order)."""
	pools = defaultdict(_empty_pool)
	for i in sorted(items, key=lambda x: -(x["age_days"] or 0)):
		if i["phantom"]:
			continue
		k = (i["variety"], i["length"])
		s_ = i["state"]
		it = {x: i[x] for x in ("farm", "shelf", "bucket", "length", "age_days", "ready_at", "detail", "ref")}
		it["qty"] = i["stems"]
		if s_ == "sellable":
			pools[k]["remote" if i["remote"] else "shelf"].append(it)
		elif s_ == "coldroom":
			pools[k]["floor"].append(it)
		elif s_ == "awaiting":
			pools[k]["arriving"].append(it)
		elif s_ == "unreceived":
			pools[k]["overdue"].append(it)
		elif s_ == "reclaimable" and i["after"] == "sellable":
			it["detail"] = "Held by " + ", ".join((i.get("from_orders") or [])[:3]) + (" - " + i["detail"] if i["detail"] else "")
			pools[k]["stale"].append(it)
		elif (s_ == "cooling" or (s_ == "reclaimable" and i["after"] == "cooling")) and i["ready_at"]:
			it["ready_date"] = getdate(i["ready_at"])
			pools[k]["cooling"].append(it)
	for p in pools.values():
		p["cooling"].sort(key=lambda x: x["ready_date"])
	return pools


@frappe.whitelist(methods=["GET"])
def outlook(
	from_date: Any = None,
	to_date: Any = None,
	region: Any = None,
	farm: Any = None,
	length: Any = None,
	rose: Any = None,
	q: Any = None,
	customer: Any = None,
	force: Any = None,
):
	"""Orders delivering in the window against the stock that exists right now.

	Every line is shown with what is allocated to it and what is available for its
	variety and length, by place. Nothing is distributed between orders: allocation
	does that, and stock that is not allocated is available. Read-only."""
	_guard()
	key = "stock_truth_outlook2:" + frappe.as_json([from_date, to_date, region, farm, length, rose, q, customer], indent=None)
	if not cint(force):
		hit = frappe.cache.get_value(key)
		if hit:
			return hit  # at most OUTLOOK_CACHE_SECONDS old; every response carries its own as_of
	try:
		a = _args(dict(region=region, farm=farm, length=length, rose=rose, q=q))
		d_from = getdate(from_date) if from_date else getdate(add_days(a.today, 1))
		d_to = getdate(to_date) if to_date else d_from
		if d_from > d_to:
			d_from, d_to = d_to, d_from
		a.through = d_to
		c = collect(a)
		lines = sv._orders(a.farms, a.lengths, a.rose, a.q, d_from, d_to)
		cust = (customer or "").strip()
		if cust:
			lines = [ln for ln in lines if ln.customer == cust or (ln.customer_name or "") == cust]
		lines.sort(key=lambda ln: (str(ln.delivery_date), ln.parent, ln.idx))
		pools = _build_pools(c["items"])
		fc = _forecast_window(a, d_to)
		by_variety = defaultdict(list)
		for (v, ln_), pool in pools.items():
			by_variety[v].append((_cm(ln_), ln_, pool))

		def longer_than(v, cm, when):
			tot, lens = 0.0, []
			for cm2, l2, pool in by_variety.get(v, []):
				if cm and cm2 > cm:
					n = sum(i["qty"] for k in ("shelf", "remote") for i in pool.get(k, []))
					if n > 0:
						tot += n
						lens.append(l2)
			return tot, lens

		def forecast_for(v, when):
			return sum(n for avail, n in fc.get(v, []) if avail <= when)

		opl_docs = {}
		names = {o for ln in lines for o in ln.opls}
		if names:
			opl_docs = {r.name: r for r in frappe.db.get_all("Order Pick List", {"name": ["in", list(names)]}, ["name", "docstatus", "team"])}

		pairs = {}
		for ln in lines:
			p = pairs.setdefault((ln.item_code, ln.length), {"variety": ln.item_code, "item_name": ln.item_name, "length": ln.length, "need": 0.0, "lines": 0, "orders": set()})
			if _f(ln.to_allocate) > 0:
				p["need"] += _f(ln.to_allocate)
				p["lines"] += 1
				p["orders"].add(ln.parent)

		orders, status_lines, memo = {}, defaultdict(int), {}
		for ln in lines:
			need = _f(ln.to_allocate)
			pool = pools.get((ln.item_code, ln.length))
			when = getdate(ln.delivery_date)
			mk = (ln.item_code, ln.length, when)
			if mk not in memo:  # one answer per variety, length and day: the stock does not change between lines
				av0 = pool_totals(pool, when)
				longer0, lens0 = longer_than(ln.item_code, _cm(ln.length), when)
				memo[mk] = (av0, longer0, lens0, forecast_for(ln.item_code, when), _where(pool, when))
			av, longer, lens, gh, where_all = memo[mk]
			status = line_status(need, av, longer, gh)
			covered = av["shelf"] + sum(av[k] for k in SOON_KEYS)
			status_lines[status] += 1
			o = orders.get(ln.parent)
			if o is None:
				o = orders[ln.parent] = {
					"sales_order": ln.parent, "customer": ln.customer_name or ln.customer, "delivery_date": str(ln.delivery_date),
					"delivery_point": ln.custom_delivery_point or "", "status": "allocated", "ordered": 0.0, "to_allocate": 0.0, "lines": [],
				}
			o["ordered"] += _f(ln.ordered_stems)
			o["to_allocate"] += need
			if STATUS_RANK[status] < STATUS_RANK[o["status"]]:
				o["status"] = status
			opls = [{"name": n, "docstatus": opl_docs[n].docstatus if n in opl_docs else None, "team": opl_docs[n].team if n in opl_docs else ""} for n in ln.opls]
			where = where_all if need > 0 else []
			o["lines"].append(
				{
					"line": ln.name, "idx": ln.idx, "variety": ln.item_code, "item_name": ln.item_name, "length": ln.length,
					"ordered": _f(ln.ordered_stems), "allocated": ln.alloc_to_line, "to_allocate": need,
					"issued": _f(ln.issued_stems), "packed": _f(ln.packed_stems),
					"stage": pipeline.line_stage_label(ln), "opls": opls, "status": status,
					"available": {k: v for k, v in av.items() if v > 0},
					"longer": longer, "longer_lengths": lens, "forecast": gh,
					"short_by": max(0.0, need - covered - longer - gh) if need > 0 else 0.0,
					"pool_need": pairs[(ln.item_code, ln.length)]["need"],
					"where": where[:WHERE_SHOWN], "where_total": len(where),
				}
			)
		# stock for the sizes these orders need: each variety and length counted once
		stock = {k: 0.0 for k in SOURCE_KEYS}
		seen_v = set()
		pair_rows = []
		for (v, ln_), p in pairs.items():
			if p["need"] <= 0:
				continue
			when = d_to
			av = pool_totals(pools.get((v, ln_)), when)
			for k, n in av.items():
				stock[k] += n
			longer, lens = longer_than(v, _cm(ln_), when)
			stock["downgrade"] += longer
			if v not in seen_v:
				seen_v.add(v)
				stock["greenhouse"] += forecast_for(v, when)
			gh_p = forecast_for(v, when)
			covered_p = av["shelf"] + sum(av[k] for k in SOON_KEYS)
			pair_rows.append({"variety": v, "item_name": p["item_name"], "length": ln_, "need": p["need"], "lines": p["lines"], "orders": len(p["orders"]), "available": {k: n for k, n in av.items() if n > 0}, "longer": longer, "longer_lengths": lens, "forecast": gh_p, "status": line_status(p["need"], av, longer, gh_p), "short_by": max(0.0, p["need"] - covered_p - longer - gh_p)})
		order_rows = sorted(orders.values(), key=lambda o: (STATUS_RANK[o["status"]], o["delivery_date"], o["sales_order"]))
		order_counts = defaultdict(int)
		for o in order_rows:
			order_counts[o["status"]] += 1
		demand = sum(o["to_allocate"] for o in order_rows)
		result = {
			"success": True,
			"from_date": str(d_from),
			"to_date": str(d_to),
			"ordered": sum(o["ordered"] for o in order_rows),
			"demand": demand,
			"allocated": sum(o["ordered"] - o["to_allocate"] for o in order_rows),
			"lines": len(lines),
			"order_counts": dict(order_counts),
			"line_counts": dict(status_lines),
			"stock": [{"key": k, "label": lb, "hint": h, "stems": stock[k]} for k, lb, h in SOURCES],
			"pairs": sorted(pair_rows, key=lambda r: -r["need"])[:400],
			"orders": order_rows[:600],
			"order_total": len(order_rows),
			"forecast_weeks_used": bool(fc),
			"customers": sorted({ln.customer_name or ln.customer for ln in lines if (ln.customer_name or ln.customer)}),
			"as_of": str(now_datetime()),
		}
		frappe.cache.set_value(key, result, expires_in_sec=OUTLOOK_CACHE_SECONDS)
		return result
	except Exception:
		frappe.log_error("v2 stock_truth.outlook")
		return {"success": False, "error": "Could not load the outlook; the error was logged."}
