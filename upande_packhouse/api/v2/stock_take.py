# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Stock Take v2 (www/stock-take-v2.html).

Compares what a cold-store stock take SCANNED (Cold Store Stock Take /
Cold Store Stock Take Bucket, written by the mobile app) with what the system
EXPECTED to be in that cold store at the moment of the stock take.

Point in time
    T = the take's latest `scanned_at` (end of `stock_take_date` if no row has
    one). Every ledger / shelf / allocation fact is read "as of T": Stock Entry
    TIMESTAMP(posting_date, posting_time) <= T, Shelving Log shelved_on <= T,
    allocation / pick-row / discard rows created <= T, and a later change flag
    (cancelled / issued / discarded) only counts when its row was last modified
    <= T. Docs are never read at their present state when that would leak
    post-take events into the take.

A bucket's CYCLE (bucket ids are reused)
    Each bucket is evaluated only inside its latest cycle before T: the latest
    submitted "Harvesting" Stock Entry with posting datetime <= T. Everything
    older is ignored; a shelf record older than the cycle start is reported as
    "older cycle", never as stock.

State machine over the cycle's events, in time order
    Receiving / Late Receipt / Quarantine Accept  -> in cold store (ledger farm =
                                                     farm of the target warehouse)
    shelved (Shelving Log, else live Shelf Item)  -> in cold store, on that shelf
    Receiving Quarantined                         -> quarantine (not in store)
    Move To Graded Sold (or Material Transfer to a Graded Sold warehouse)
                                                  -> allocated away (sold leg)
    Issue From / Issuing From Cold Store, Offline Issuing, Packing, Dispatch,
    Material Transfer to packhouse/dispatch/truck, shelf removal "Issued to Sales
    Order" / "Offline Issuing"                    -> issued
    Discard, Packhouse/Airport/Quarantine Rejects -> discarded
    Remote Transfers / Farm Transfer / Material Transfer between receiving cold
    stores                                        -> ledger farm changes only
    On top: Pick List Item issued / issued_offline (<= T) -> issued;
    Discard Request Bucket discarded (<= T) -> discarded.

Location
    The SHELF's farm (Shelf.farm) when the bucket sits on a shelf at T, else the
    ledger farm of its latest receipt/transfer. Shelf Item.warehouse and the
    "arrival" Remote Transfer posted at shelving are never used to place a bucket.

WHAT THE PAGE RECONCILES (per bucket, as of T, for the take's farm)

    Received         every bucket received (or shelved) at the farm in the last LOOKBACK_DAYS
    - Issued         left through the cold-store issue / packing / dispatch
    - Issued offline left through Offline Issuing (stock entry, shelf removal or pick-list flag)
    - Discarded / Sold leg / Quarantine / Moved to another farm
    = Still in this store (system view), of which
        on a shelf      -> expected to be scanned
        not shelved     -> received but never shelved; shelving is always scanned, so these
                           are expected in the count and are a real gap when not found
        reserved        -> allocated to an order, still physically in the store

    Counted          every scanned bucket
    Expected         on a shelf, or received and never shelved
    Not located      expected, in a zone the count covered, not scanned
    Outside count    on a shelf per the system, in a zone the count never touched (a partial
                     count): neither found nor flagged
    Scanned, not expected = scanned, but the system shows it issued / discarded / elsewhere /
                     in another cycle.

Located % = counted-as-expected / (counted-as-expected + not located).

Stock take filters (applied on the Cold Store Stock Take header, then per row):
region / farm (take.farm), from/to over stock_take_date, one take, variety
(scanned variety, else the system's), rose type (Item Group tree of the variety).
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

import frappe
from frappe.utils import cint, flt, getdate, today

from upande_packhouse.api.v2.core import region as region_core
from upande_packhouse.api.v2.core import rose as rose_core

LOOKBACK_DAYS = 30  # receipts/harvests older than this are not searched (roses do not keep longer)
ROW_LIMIT = 3000
CHUNK = 1500

RECEIPT_TYPES = ("Receiving", "Late Receipt", "Quarantine Accept")
ALLOC_TYPES = ("Move To Graded Sold", "Graded Sold")
ISSUE_TYPES = (
	"Issue From The Cold Store",
	"Issuing From Cold Store",
	"Offline Issuing",
	"Packing",
	"Dispatch",
)
DISCARD_TYPES = ("Discard", "Packhouse Rejects", "Airport rejects", "Quarantine Rejects")
MOVE_TYPES = ("Remote Transfers", "Farm Transfer", "Material Transfer")
SKIP_TYPES = ("Harvesting", "Grading")
ALL_EVENT_TYPES = (
	RECEIPT_TYPES + ALLOC_TYPES + ISSUE_TYPES + DISCARD_TYPES + MOVE_TYPES + ("Receiving Quarantined",)
)

AGE_BANDS = (("0 d", 0, 1), ("1 d", 1, 2), ("2 d", 2, 3), ("3-4 d", 3, 5), ("5-6 d", 5, 7), ("7+ d", 7, None))


def _args(kw):
	g = lambda k, d="": kw.get(k) if kw.get(k) is not None else d  # noqa: E731
	return {
		"from_date": g("from_date") or None,
		"to_date": g("to_date") or None,
		"stock_take": (g("stock_take") or "").strip(),
		"region": g("region"),
		"farm": (g("farm") or "").strip(),
		"variety": (g("variety") or "").strip(),
		"rose": g("rose", "all"),
	}


def _key(b):
	return (b or "").strip().upper()


def _chunks(seq, n=CHUNK):
	seq = list(seq)
	for i in range(0, len(seq), n):
		yield seq[i : i + n]


# ──────────────────────────── takes ────────────────────────────


def _takes(a, ignore_take=False):
	cond = ["1=1"]
	p = {}
	if a["from_date"]:
		cond.append("t.stock_take_date >= %(f)s")
		p["f"] = a["from_date"]
	if a["to_date"]:
		cond.append("t.stock_take_date <= %(t)s")
		p["t"] = a["to_date"]
	farms = region_core.farms_for(region=a["region"], farm=a["farm"])
	if farms is not None:
		cond.append("t.farm IN %(farms)s")
		p["farms"] = region_core.sql_tuple(farms)
	if a["stock_take"] and not ignore_take:
		cond.append("t.name = %(n)s")
		p["n"] = a["stock_take"]
	return frappe.db.sql(
		f"""SELECT t.name, t.farm, t.coldstore, t.stock_take_date,
		       (SELECT MAX(b.scanned_at) FROM `tabCold Store Stock Take Bucket` b WHERE b.parent = t.name) AS last_scan,
		       (SELECT COUNT(*) FROM `tabCold Store Stock Take Bucket` b WHERE b.parent = t.name) AS buckets
		FROM `tabCold Store Stock Take` t
		WHERE {" AND ".join(cond)} AND t.docstatus < 2
		ORDER BY t.stock_take_date DESC, t.farm""",
		p,
		as_dict=True,
	)  # nosemgrep: fixed fragments, values bound


def _cutoff(take):
	d = getdate(take.stock_take_date)
	end = datetime.combine(d, time(23, 59, 59))
	return take.last_scan if take.last_scan else end


# ─────────────────────── per-take evaluation ───────────────────────


class Ctx:
	"""Lookups shared by every take in one request."""

	def __init__(self):
		self.wh_farm = {
			r.name: r.custom_farm
			for r in frappe.db.sql("SELECT name, custom_farm FROM `tabWarehouse`", as_dict=True)
		}
		self.shelf_farm = {
			r.name: r.farm for r in frappe.db.sql("SELECT name, farm FROM `tabShelf`", as_dict=True)
		}
		self.item_group = {}

	def group_of(self, variety):
		if variety not in self.item_group:
			self.item_group[variety] = frappe.db.get_value("Item", variety, "item_group") if variety else None
		return self.item_group[variety]


def _classify_se(ev, ctx):
	"""-> (kind, farm) for one Stock Entry event. kind in
	receipt | quarantine | alloc | issued | discarded | move | None"""
	t = ev["type"]
	tw, sw = ev.get("t_wh") or "", ev.get("s_wh") or ""
	if t in RECEIPT_TYPES:
		return "receipt", ctx.wh_farm.get(tw) or ev.get("farm")
	if t == "Receiving Quarantined":
		return "quarantine", None
	if t in ALLOC_TYPES:
		return "alloc", None
	if t in ISSUE_TYPES:
		return "issued", None
	if t in DISCARD_TYPES:
		return "discarded", None
	if t in MOVE_TYPES:
		if "Graded Sold" in tw:
			return "alloc", None
		if any(x in tw for x in ("Packhouse", "Dispatch", "Truck", "Ungraded Sold")):
			return "issued", None
		if "Receiving Cold Store" in tw and ctx.wh_farm.get(tw):
			return "move", ctx.wh_farm.get(tw)
		if not tw and sw:
			return "issued", None
		return "move", ctx.wh_farm.get(tw)
	return None, None


def _evaluate(take, T, bucket_ids, scanned_keys, ctx):
	"""State of every bucket in `bucket_ids` as of T. -> {KEY: dict}"""
	out = {}
	tdate = getdate(T)
	ids = list(bucket_ids)

	harvest = defaultdict(list)  # key -> [(dt, se_name, farm, length)]
	for ch in _chunks(ids):
		for r in frappe.db.sql(
			"""SELECT se.custom_bucket_id AS b, se.name, se.farm, se.custom_stem_length AS len,
			       TIMESTAMP(se.posting_date, se.posting_time) AS dt
			FROM `tabStock Entry` se
			WHERE se.custom_bucket_id IN %(ids)s AND se.stock_entry_type = 'Harvesting'
			  AND se.docstatus = 1 AND se.posting_date BETWEEN %(a)s AND %(d)s""",
			{"ids": tuple(ch), "a": tdate - timedelta(days=LOOKBACK_DAYS), "d": tdate},
			as_dict=True,
		):
			if r.dt <= T:
				harvest[_key(r.b)].append(r)
	latest_h = {k: max(v, key=lambda r: r.dt) for k, v in harvest.items()}

	min_date = min((h.dt for h in latest_h.values()), default=T)
	se_events = defaultdict(list)
	for ch in _chunks(ids):
		for r in frappe.db.sql(
			"""SELECT se.custom_bucket_id AS b, se.name, se.stock_entry_type AS type, se.farm,
			       se.custom_stem_length AS len, TIMESTAMP(se.posting_date, se.posting_time) AS dt,
			       d.item_code, COALESCE(NULLIF(d.transfer_qty, 0), d.qty) AS stems,
			       d.s_warehouse AS s_wh, d.t_warehouse AS t_wh
			FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` d ON d.parent = se.name
			WHERE se.custom_bucket_id IN %(ids)s AND se.docstatus = 1
			  AND se.stock_entry_type IN %(types)s
			  AND se.posting_date BETWEEN %(a)s AND %(d)s""",
			{"ids": tuple(ch), "types": ALL_EVENT_TYPES, "a": getdate(min_date), "d": tdate},
			as_dict=True,
		):
			if r.dt <= T:
				se_events[_key(r.b)].append(r)

	# A harvest is often several entries seconds apart (one per variety/pack of 10): the
	# cycle's harvest = the latest entry plus those within an hour before it.
	burst_of = {}
	for k, h in latest_h.items():
		burst_of[k] = [x.name for x in harvest[k] if h.dt - timedelta(hours=1) <= x.dt <= h.dt]
	hnames = {n for v in burst_of.values() for n in v}
	harvest_items = {}
	for ch in _chunks(hnames):
		for r in frappe.db.sql(
			"""SELECT parent, item_code, COALESCE(NULLIF(transfer_qty, 0), qty) AS stems
			FROM `tabStock Entry Detail` WHERE parent IN %(n)s ORDER BY idx""",
			{"n": tuple(ch)},
			as_dict=True,
		):
			harvest_items.setdefault(r.parent, []).append((r.item_code, flt(r.stems)))

	shelf_rows = defaultdict(list)  # shelved events
	for ch in _chunks(ids):
		log_items = set()
		for r in frappe.db.sql(
			"""SELECT bucket_id AS b, shelf, variety, stem_length AS len, stem_qty AS stems,
			       shelved_on, removed_on, reason, shelf_item
			FROM `tabShelving Log` WHERE bucket_id IN %(ids)s AND shelved_on <= %(t)s""",
			{"ids": tuple(ch), "t": T},
			as_dict=True,
		):
			if r.shelf_item:
				log_items.add(r.shelf_item)
			shelf_rows[_key(r.b)].append(("log", r))
		for r in frappe.db.sql(
			"""SELECT bucket_id AS b, name, parent AS shelf, variety, stem_length AS len, stem_qty AS stems,
			       date_added AS shelved_on
			FROM `tabShelf Item` WHERE bucket_id IN %(ids)s AND date_added <= %(t)s""",
			{"ids": tuple(ch), "t": T},
			as_dict=True,
		):
			if r.name not in log_items:
				shelf_rows[_key(r.b)].append(("item", r))

	alloc = defaultdict(list)
	for ch in _chunks(ids):
		for r in frappe.db.sql(
			"""SELECT bas.bucket_id AS b, ba.sales_order, ba.creation, ba.modified, ba.cancelled, ba.issued
			FROM `tabBucket Allocations` ba JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
			WHERE bas.bucket_id IN %(ids)s AND ba.creation <= %(t)s""",
			{"ids": tuple(ch), "t": T},
			as_dict=True,
		):
			alloc[_key(r.b)].append(r)

	pli = defaultdict(list)
	for ch in _chunks(ids):
		for r in frappe.db.sql(
			"""SELECT p.bucket AS b, p.parent, p.creation, p.modified, p.issued, p.issued_offline
			FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name = p.parent
			WHERE p.parenttype = 'Order Pick List' AND p.bucket IN %(ids)s AND o.docstatus < 2
			  AND p.creation <= %(t)s""",
			{"ids": tuple(ch), "t": T},
			as_dict=True,
		):
			pli[_key(r.b)].append(r)

	disc = defaultdict(list)
	for ch in _chunks(ids):
		for r in frappe.db.sql(
			"""SELECT bucket_id AS b, modified FROM `tabDiscard Request Bucket`
			WHERE bucket_id IN %(ids)s AND discarded = 1 AND modified <= %(t)s""",
			{"ids": tuple(ch), "t": T},
			as_dict=True,
		):
			disc[_key(r.b)].append(r)

	for k in {_key(i) for i in ids}:
		h = latest_h.get(k)
		res = frappe._dict(
			key=k,
			state="unknown",
			step="No harvest record",
			loc=None,
			ledger_farm=None,
			shelf=None,
			shelf_farm=None,
			harvest_dt=None,
			harvest_date=None,
			received_at=None,
			variety=None,
			stems=0.0,
			length=None,
			older_shelf=False,
			detail="",
			cycle=False,
			out_kind=None,
			out_at=None,
			last_at=None,
		)
		out[k] = res
		if not h:
			# no harvest at all before T: may still be on a shelf, cannot place it in a cycle
			continue
		res.harvest_dt, res.harvest_date, res.cycle = h.dt, getdate(h.dt), True
		by_v = defaultdict(float)
		for n in burst_of.get(k, [h.name]):
			for item, q in harvest_items.get(n, []):
				by_v[item] += q
		res.variety = max(by_v, key=by_v.get) if by_v else None
		res.length = h.len
		harvest_stems = sum(by_v.values())
		res.state, res.step = "field", "Harvested"

		events = []  # (dt, prio, kind, payload)
		rec_stems, rec_variety, rec_len = 0.0, None, None
		seen_se = {}
		for e in se_events.get(k, []):
			if e.dt < h.dt:
				continue
			kind, farm = _classify_se(e, ctx)
			if not kind:
				continue
			if kind == "receipt" and e.type in ("Receiving", "Late Receipt"):
				rec_stems += flt(e.stems)
				rec_variety = rec_variety or e.item_code
				rec_len = rec_len or e.len
			if (e.name, kind) in seen_se:
				continue
			seen_se[(e.name, kind)] = 1
			events.append((e.dt, 1, kind, {"farm": farm, "type": e.type, "name": e.name}))
		shelf_stems, shelf_variety, shelf_len = 0.0, None, None
		for src, s in shelf_rows.get(k, []):
			if s.shelved_on < h.dt:
				res.older_shelf = True  # shelf record from an earlier cycle
				continue
			events.append((s.shelved_on, 2, "shelved", {"shelf": s.shelf, "row": s}))
			if src == "log" and s.removed_on and s.removed_on <= T:
				reason = (s.reason or "").lower()
				kind = "issued" if (reason.startswith("issued") or "offline" in reason) else "unshelve"
				events.append((s.removed_on, 3, kind, {"type": "Shelf: " + (s.reason or "removed")}))
		events.sort(key=lambda x: (x[0], x[1]))
		# stems on the shelf at T: every cycle row still shelved at T (a bucket can hold 2 varieties)
		for src, s in shelf_rows.get(k, []):
			if s.shelved_on >= h.dt and not (src == "log" and s.removed_on and s.removed_on <= T):
				shelf_stems += flt(s.stems)

		for dt, _p, kind, pl in events:
			res.last_at = dt
			if kind == "receipt":
				res.state, res.step, res.ledger_farm, res.received_at = "in_store", "Received", pl["farm"], dt
				if pl["type"] == "Quarantine Accept":
					res.step = "Quarantine accepted"
			elif kind == "quarantine":
				res.state, res.step, res.detail = "quarantine", "In quarantine", ""
			elif kind == "shelved":
				res.state, res.step, res.shelf = "in_store", "Shelved", pl["shelf"]
				res.shelf_farm = ctx.shelf_farm.get(pl["shelf"])
				res.received_at = res.received_at or dt
				s = pl["row"]
				shelf_variety, shelf_len = shelf_variety or s.variety, shelf_len or s.len
			elif kind == "unshelve":
				res.shelf, res.shelf_farm = None, None
			elif kind in ("alloc", "issued", "discarded"):
				res.state, res.shelf, res.shelf_farm = kind, None, None
				offline = kind == "issued" and "offline" in (pl.get("type") or "").lower()
				res.out_kind, res.out_at = ("issued_offline" if offline else kind), dt
				res.step = {
					"alloc": "Sold (moved to graded sold)",
					"issued": "Issued",
					"discarded": "Discarded",
				}[kind]
				if offline:
					res.step = "Issued offline"
				res.detail = pl.get("type", "")
			elif kind == "move":
				if res.state == "in_store":
					res.ledger_farm = pl["farm"] or res.ledger_farm
					res.detail = "Remote transfer (ledger) to " + (pl["farm"] or "?")

		# overlays: issued / discarded flags stamped <= T inside the cycle
		if res.state in ("in_store", "quarantine", "field"):
			for r in pli.get(k, []):
				if r.creation >= h.dt and (cint(r.issued) or cint(r.issued_offline)) and r.modified <= T:
					off = bool(cint(r.issued_offline))
					res.state, res.step, res.detail = "issued", ("Issued offline" if off else "Issued"), "Pick list " + r.parent
					res.out_kind, res.out_at = ("issued_offline" if off else "issued"), r.modified
					res.shelf = res.shelf_farm = None
			for r in disc.get(k, []):
				if r.modified >= h.dt:
					res.state, res.step, res.detail = "discarded", "Discarded", "Discard request"
					res.out_kind, res.out_at = "discarded", r.modified
					res.shelf = res.shelf_farm = None
		if res.state == "in_store":
			orders = {
				r.sales_order
				for r in alloc.get(k, [])
				if r.creation >= h.dt
				and not (cint(r.cancelled) and r.modified <= T)
				and not (cint(r.issued) and r.modified <= T)
			}
			opls = {
				r.parent
				for r in pli.get(k, [])
				if r.creation >= h.dt and not cint(r.issued) and not cint(r.issued_offline)
			}
			if orders or opls:
				res.state = "allocated"
				res.detail = ", ".join(sorted(o for o in orders if o) + sorted(opls))[:80]

		res.loc = res.shelf_farm or res.ledger_farm
		if (
			res.state in ("in_store", "allocated", "quarantine")
			and res.shelf is None
			and res.ledger_farm is None
		):
			res.loc = None
		res.variety = rec_variety or shelf_variety or res.variety
		res.length = rec_len or shelf_len or res.length
		# Receiving is sometimes posted twice for one harvest: never more than was harvested.
		if rec_stems and harvest_stems:
			rec_stems = min(rec_stems, harvest_stems)
		res.stems = rec_stems or shelf_stems or harvest_stems
		if res.state == "field":
			res.step = "Harvested, not received"
	return out


def _shelf_candidates(farm, T):
	"""Buckets on a shelf of `farm` at T (current Shelf Items or open Shelving Log rows)."""
	ids = set()
	for r in frappe.db.sql(
		"""SELECT DISTINCT si.bucket_id FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		WHERE s.farm = %(f)s AND si.date_added <= %(t)s AND IFNULL(si.bucket_id, '') != ''""",
		{"f": farm, "t": T},
	):
		ids.add(r[0])
	for r in frappe.db.sql(
		"""SELECT DISTINCT l.bucket_id FROM `tabShelving Log` l JOIN `tabShelf` s ON s.name = l.shelf
		WHERE s.farm = %(f)s AND l.shelved_on <= %(t)s AND (l.removed_on IS NULL OR l.removed_on > %(t)s)
		  AND IFNULL(l.bucket_id, '') != ''""",
		{"f": farm, "t": T},
	):
		ids.add(r[0])
	return ids


def _receipt_candidates(farm, T):
	rows = frappe.db.sql(
		"""SELECT DISTINCT custom_bucket_id FROM `tabStock Entry`
		FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		WHERE stock_entry_type IN ('Receiving', 'Late Receipt') AND docstatus = 1
		  AND posting_date BETWEEN %(a)s AND %(d)s AND farm = %(f)s AND IFNULL(custom_bucket_id, '') != ''""",
		{"a": getdate(T) - timedelta(days=LOOKBACK_DAYS), "d": getdate(T), "f": farm},
	)
	return {r[0] for r in rows}


def _age_band(days):
	for label, lo, hi in AGE_BANDS:
		if days >= lo and (hi is None or days < hi):
			return label
	return AGE_BANDS[0][0]


def _zone(shelf):
	"""KPT-I17T -> KPT-I (cold store prefix + block letter)."""
	if not shelf:
		return "No shelf"
	head, _, tail = shelf.partition("-")
	return f"{head}-{tail[:1]}" if tail else shelf


STATUS_LABEL = {
	"on_shelf": "On shelf",
	"not_shelved": "Received, not shelved yet",
	"reserved": "Reserved for an order",
	"issued": "Issued",
	"issued_offline": "Issued offline",
	"discarded": "Discarded",
	"sold": "Sold (graded sold)",
	"moved": "Moved to another farm",
	"quarantine": "In quarantine",
	"not_received": "Harvested, not received",
	"no_cycle": "No harvest record",
	"other": "Other",
}
EXPECTED_STATUS = ("on_shelf", "not_shelved", "reserved")  # physically in this cold store per the system
OUT_STATUS = ("issued", "issued_offline", "discarded", "sold", "moved", "quarantine")


def _status(res, farm):
	"""The system's view of one bucket at T, for the take's farm."""
	if not res.cycle:
		return "no_cycle"
	st = res.state
	if st == "field":
		return "not_received"
	if st == "quarantine":
		return "quarantine"
	if st == "discarded":
		return "discarded"
	if st == "alloc":
		return "sold"
	if st == "issued":
		return res.out_kind or "issued"
	if st in ("in_store", "allocated"):
		if res.loc != farm:
			return "moved"
		if st == "allocated":
			return "reserved"
		return "on_shelf" if res.shelf else "not_shelved"
	return "other"


def _system_says(res, status, farm):
	"""One short sentence: what the ledger says about a bucket that is not where the count found it."""
	if status == "no_cycle":
		return "No harvest in the last %d days" % LOOKBACK_DAYS + (" (shelf record from an older cycle)" if res.older_shelf else "")
	if status == "moved":
		return "System has it at " + (res.loc or "another farm")
	if status == "reserved":
		return "Reserved" + (" for " + res.detail if res.detail else " for an order")
	if status in ("issued", "issued_offline"):
		return STATUS_LABEL[status] + (" (" + res.detail + ")" if res.detail else "")
	return STATUS_LABEL.get(status, "Not expected here")


def _row(take, res, srow, age_days, status, farm):
	return {
		"stock_take": take.name,
		"take_farm": take.farm,
		"bucket": srow.bucket_id if srow else res.get("raw") or res.key,
		"variety": (srow.variety if srow and srow.variety else res.variety) or "",
		"stem_length": (srow.stem_length if srow and srow.stem_length else res.length) or "",
		"stems": flt(res.stems),
		"age_days": age_days,
		"age_band": _age_band(age_days or 0),
		"shelf": (srow.shelf if srow and srow.shelf else res.shelf) or "",
		"sys_shelf": res.shelf or "",
		"zone": _zone(srow.shelf if srow and srow.shelf else res.shelf),
		"scan_status": srow.status if srow else "",
		"scanned_at": str(srow.scanned_at) if srow and srow.scanned_at else "",
		"step": "Received, no shelving scan" if status == "not_shelved" else res.step,
		"status": status,
		"status_label": STATUS_LABEL.get(status, status),
		"system_says": _system_says(res, status, farm),
		"system_at": res.loc or "",
		"harvest_date": str(res.harvest_date) if res.harvest_date else "",
		"received_at": str(res.received_at) if res.received_at else "",
		"left_at": str(res.out_at) if res.out_at else "",
		# when it stopped being in this store: the stamp of the outflow, or of the last
		# ledger event for buckets that moved away / went to quarantine
		"gone_at": str(res.out_at or (res.last_at if status in ("moved", "quarantine") else "") or ""),
	}


def _evaluate_take(take, ctx, a):
	"""-> (T, scans, system) where
	scans  = one row per scanned bucket  (row["expected"] says the system has it in this store)
	system = one row per candidate bucket the system knows at this farm (counted or not) with
	         row["scanned"], row["status"] and, for shelf buckets not scanned, row["covered"]"""
	T = _cutoff(take)
	srows = frappe.db.sql(
		"""SELECT bucket_id, status, shelf, variety, stem_length, age_days, scanned_at
		FROM `tabCold Store Stock Take Bucket` WHERE parent = %(p)s ORDER BY idx""",
		{"p": take.name},
		as_dict=True,
	)
	scanned = {}
	for s in srows:
		scanned.setdefault(_key(s.bucket_id), s)
	cands = _shelf_candidates(take.farm, T) | _receipt_candidates(take.farm, T)
	allk = {_key(b): b for b in cands}
	cand_keys = set(allk)
	for k, s in scanned.items():
		allk.setdefault(k, s.bucket_id)
	ev = _evaluate(take, T, list(allk.values()), set(scanned), ctx)

	# zones the count touched: a partial count says nothing about shelves it never visited
	counted_zones = {_zone(s.shelf) for s in scanned.values() if s.shelf}

	scans, system = [], []
	for k, res in ev.items():
		res.raw = allk.get(k, k)
		age = (getdate(T) - res.harvest_date).days if res.harvest_date else None
		s = scanned.get(k)
		status = _status(res, take.farm)
		row = _row(take, res, s, age if age is not None else (s.age_days if s else None), status, take.farm)
		row["scanned"] = bool(s)
		if s:
			row["expected"] = status in EXPECTED_STATUS
			row["result"] = "As expected" if row["expected"] else "Not expected here"
			scans.append(row)
		if k in cand_keys:
			row["covered"] = (not res.shelf) or (_zone(res.shelf) in counted_zones)
			system.append(row)
	return T, scans, system


def _cached_take(take, ctx, a):
	# A take is a historical fact; evaluating it scans a month of receipts (seconds).
	# Cache the unfiltered result keyed on the take's modified stamp.
	mod = frappe.db.get_value("Cold Store Stock Take", take.name, "modified")
	key = f"ph2_stock_take_v5::{take.name}::{mod}"
	hit = frappe.cache.get_value(key)
	if hit:
		return hit
	res = _evaluate_take(take, ctx, a)
	frappe.cache.set_value(key, res, expires_in_sec=6 * 3600)
	return res


# ───────────────────────── filters + roll-ups ─────────────────────────


def _keep(row, a, ctx):
	if a["variety"] and row["variety"] != a["variety"]:
		return False
	if rose_core.normalize(a["rose"]) != "all":
		if rose_core.rose_type(ctx.group_of(row["variety"])) != rose_core.normalize(a["rose"]):
			return False
	return True


def _bs(rows):
	return {"buckets": len(rows), "stems": float(sum(r["stems"] for r in rows))}


def _reconcile(system, scans):
	"""The reconciliation for a set of rows (one take or all of them)."""
	by = defaultdict(list)
	for r in system:
		if r["status"] not in ("not_received", "no_cycle", "other"):
			by[r["status"]].append(r)
	received = [r for st in by.values() for r in st]
	in_store = by["on_shelf"] + by["not_shelved"] + by["reserved"]
	on_shelf = by["on_shelf"]
	# Shelving is always scanned, so a received bucket with no shelving scan is as much a
	# gap as one that left its shelf: both are expected in the store and neither is on a shelf.
	expected = on_shelf + by["not_shelved"]
	found = [r for r in expected if r["scanned"]]
	not_located = [r for r in expected if not r["scanned"] and r["covered"]]
	outside = [r for r in expected if not r["scanned"] and not r["covered"]]
	reserved_unscanned = [r for r in by["reserved"] if not r["scanned"] and r["covered"]]
	not_expected = [r for r in scans if not r["expected"]]
	counted_as_expected = [r for r in scans if r["expected"]]
	den = len(found) + len(not_located)
	return {
		"received": _bs(received),
		"issued": _bs(by["issued"]),
		"issued_offline": _bs(by["issued_offline"]),
		"discarded": _bs(by["discarded"]),
		"sold": _bs(by["sold"]),
		"moved": _bs(by["moved"]),
		"quarantine": _bs(by["quarantine"]),
		"in_store": _bs(in_store),
		"on_shelf": _bs(on_shelf),
		"expected": _bs(expected),
		"not_located_unshelved": _bs([r for r in not_located if r["status"] == "not_shelved"]),
		"reserved": _bs(by["reserved"]),
		"not_shelved": _bs(by["not_shelved"]),
		"counted": _bs(scans),
		"counted_as_expected": _bs(counted_as_expected),
		"found_on_shelf": _bs(found),
		"not_located": _bs(not_located),
		"outside_count": _bs(outside),
		"reserved_unscanned": _bs(reserved_unscanned),
		"not_expected": _bs(not_expected),
		"located_pct": round(100.0 * len(found) / den, 1) if den else None,
	}, not_located, not_expected


def _day(system, take):
	"""The count day: balance from the day before + received - issued (online / offline) - other
	outflows = in the store at the count. Day = the stock take date, from 00:00 to the last scan."""
	start = str(getdate(take.stock_take_date)) + " 00:00:00"
	rows = [r for r in system if r["status"] not in ("not_received", "no_cycle", "other")]
	opening, today_in = [], []
	out = defaultdict(list)
	for r in rows:
		rec = r["received_at"] or ""
		gone = r["gone_at"]
		if r["status"] in OUT_STATUS and not gone:
			gone = start  # no stamp: count the outflow on the day
		if rec >= start:
			today_in.append(r)
		elif not gone or gone >= start:
			opening.append(r)
		else:
			continue
		if r["status"] in OUT_STATUS and gone >= start:
			out[r["status"]].append(r)
	other = out["discarded"] + out["sold"] + out["moved"] + out["quarantine"]
	return {
		"opening": _bs(opening),
		"received": _bs(today_in),
		"issued": _bs(out["issued"]),
		"issued_offline": _bs(out["issued_offline"]),
		"other_out": _bs(other),
		"discarded": _bs(out["discarded"]),
		"sold": _bs(out["sold"]),
		"moved": _bs(out["moved"]),
		"quarantine": _bs(out["quarantine"]),
	}


def _merge(a, b):
	out = {}
	for k in a:
		if isinstance(a[k], dict):
			out[k] = {"buckets": a[k]["buckets"] + b[k]["buckets"], "stems": a[k]["stems"] + b[k]["stems"]}
	return out


@frappe.whitelist()
def get_stock_take(**kw):
	"""Everything the page shows for the filters."""
	a = _args(kw)
	takes_all = _takes(a, ignore_take=True)
	takes = [t for t in takes_all if not a["stock_take"] or t.name == a["stock_take"]]
	ctx = Ctx()
	scans_all, system_all = [], []
	per_take = []
	day_tot = None
	for t in takes:
		T, scans, system = _cached_take(t, ctx, a)
		scans = [r for r in scans if _keep(r, a, ctx)]
		system = [r for r in system if _keep(r, a, ctx)]
		scans_all += scans
		system_all += system
		rec, _nl, _ne = _reconcile(system, scans)
		day = _day(system, t)
		day_tot = day if day_tot is None else _merge(day_tot, day)
		per_take.append(
			{
				"opening": day["opening"]["buckets"],
				"received_day": day["received"]["buckets"],
				"issued": day["issued"]["buckets"],
				"issued_offline": day["issued_offline"]["buckets"],
				"other_out": day["other_out"]["buckets"],
				"name": t.name,
				"farm": t.farm,
				"date": str(t.stock_take_date),
				"as_of": str(T),
				"received": rec["received"]["buckets"],
				"left_store": sum(
					rec[k]["buckets"] for k in ("issued", "issued_offline", "discarded", "sold", "moved", "quarantine")
				),
				"held": rec["reserved"]["buckets"],
				"expected": rec["expected"]["buckets"],
				"counted": rec["counted"]["buckets"],
				"not_located": rec["not_located"]["buckets"],
				"not_located_stems": rec["not_located"]["stems"],
				"not_expected": rec["not_expected"]["buckets"],
				"outside_count": rec["outside_count"]["buckets"],
				"located_pct": rec["located_pct"],
			}
		)

	rec, not_located, not_expected = _reconcile(system_all, scans_all)
	day_dates = sorted({str(t.stock_take_date) for t in takes})

	varieties = sorted({r["variety"] for r in scans_all + system_all if r["variety"]})
	by_var = {
		v: {"variety": v, "expected": 0, "counted": 0, "not_located": 0, "not_located_stems": 0.0, "not_expected": 0}
		for v in varieties
	}
	for r in system_all:
		if r["status"] in ("on_shelf", "not_shelved") and r["variety"] in by_var:
			by_var[r["variety"]]["expected"] += 1
	for r in scans_all:
		x = by_var.get(r["variety"])
		if x:
			x["counted"] += 1
			x["not_expected"] += 0 if r["expected"] else 1
	for r in not_located:
		x = by_var.get(r["variety"])
		if x:
			x["not_located"] += 1
			x["not_located_stems"] += r["stems"]
	by_variety = sorted(by_var.values(), key=lambda x: (-x["not_located"], -x["counted"], x["variety"]))

	by_age = [
		{
			"band": label,
			"counted": sum(1 for r in scans_all if r["age_band"] == label),
			"not_located": sum(1 for r in not_located if r["age_band"] == label),
		}
		for label, _lo, _hi in AGE_BANDS
	]

	zones = defaultdict(lambda: {"expected": 0, "counted": 0, "not_located": 0, "not_located_stems": 0.0})
	for r in system_all:
		if r["status"] in ("on_shelf", "not_shelved"):
			zones[r["zone"]]["expected"] += 1
	for r in scans_all:
		zones[r["zone"]]["counted"] += 1
	for r in not_located:
		zones[r["zone"]]["not_located"] += 1
		zones[r["zone"]]["not_located_stems"] += r["stems"]
	by_zone = sorted(
		({"zone": z, **v} for z, v in zones.items()), key=lambda x: (-x["not_located"], -x["counted"], x["zone"])
	)

	def roll(rows, key):
		g = defaultdict(lambda: {"buckets": 0, "stems": 0.0})
		for r in rows:
			g[r[key]]["buckets"] += 1
			g[r[key]]["stems"] += r["stems"]
		return sorted(({"label": k, **v} for k, v in g.items()), key=lambda x: -x["buckets"])

	last_step = roll(not_located, "step")
	why_not_expected = roll(not_expected, "system_says")

	not_located.sort(key=lambda r: (-(r["age_days"] or 0), r["bucket"]))
	cap = lambda rows: rows[:ROW_LIMIT]  # noqa: E731
	return {
		"success": True,
		"reconciliation": rec,
		"day": day_tot,
		"day_dates": day_dates,
		"takes": [
			{"name": t.name, "farm": t.farm, "date": str(t.stock_take_date), "buckets": t.buckets}
			for t in takes_all
		],
		"n_takes": len(takes),
		"per_take": per_take,
		"farms": sorted({t.farm for t in _takes({**a, "farm": "", "region": ""}, ignore_take=True) if t.farm}),
		"varieties": varieties,
		"by_variety": by_variety,
		"by_age": by_age,
		"by_zone": by_zone,
		"last_step": last_step,
		"why_not_expected": why_not_expected,
		"counted": cap(scans_all),
		"not_located": cap(not_located),
		"not_expected": cap(not_expected),
		"truncated": any(len(x) > ROW_LIMIT for x in (scans_all, not_located, not_expected)),
		"row_limit": ROW_LIMIT,
		"lookback_days": LOOKBACK_DAYS,
		"today": today(),
	}
