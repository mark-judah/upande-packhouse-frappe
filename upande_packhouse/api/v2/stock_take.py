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

EXPECTED at farm F   = in cold store at F, harvested + received (or shelved) in
                       the current cycle, and not allocated / issued / discarded /
                       transferred away as of T.
ALLOCATED (accounted) = in store at F but with an outstanding Bucket Allocation
                       (created <= T, not cancelled, not issued by T) or an
                       unissued Pick List Item. Not expected, not missing.
FOUND                = every scanned bucket.
  found + expected   = scanned and EXPECTED  (the system agrees)
UNEXPECTED           = scanned, but the system has it allocated / issued /
                       discarded / elsewhere / not received / older cycle / unknown.
MISSING              = EXPECTED and not scanned.
accuracy %           = found+expected / (found+expected + missing)

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

LOOKBACK_DAYS = 60  # receipts older than this are not searched for "expected"
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
			  AND se.docstatus = 1 AND se.posting_date <= %(d)s""",
			{"ids": tuple(ch), "d": tdate},
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
				res.step = {
					"alloc": "Sold leg (Move To Graded Sold)",
					"issued": "Issued",
					"discarded": "Discarded",
				}[kind]
				res.detail = pl.get("type", "")
			elif kind == "move":
				if res.state == "in_store":
					res.ledger_farm = pl["farm"] or res.ledger_farm
					res.detail = "Remote transfer (ledger) to " + (pl["farm"] or "?")

		# overlays: issued / discarded flags stamped <= T inside the cycle
		if res.state in ("in_store", "quarantine", "field"):
			for r in pli.get(k, []):
				if r.creation >= h.dt and (cint(r.issued) or cint(r.issued_offline)) and r.modified <= T:
					res.state, res.step, res.detail = "issued", "Issued", "Pick list " + r.parent
					res.shelf = res.shelf_farm = None
			for r in disc.get(k, []):
				if r.modified >= h.dt:
					res.state, res.step, res.detail = "discarded", "Discarded", "Discard request"
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


def _unexpected_reason(res, farm):
	if not res.cycle:
		return "No harvest record before the stock take"
	if res.older_shelf and res.state in ("unknown", "field"):
		return "Shelf record is from an older cycle"
	if res.state == "allocated":
		return "Allocated to an order" + (f" ({res.detail})" if res.detail else "")
	if res.state == "issued":
		return "Already issued" + (f" ({res.detail})" if res.detail else "")
	if res.state == "discarded":
		return "Already discarded"
	if res.state == "alloc":
		return "Moved to graded sold"
	if res.state == "field":
		return "Harvested, never received"
	if res.state == "quarantine":
		return "In quarantine"
	if res.state == "in_store" and res.loc != farm:
		return f"System has it at {res.loc or 'no known farm'}"
	return "Not expected here"


def _row(take, res, srow, age_days, T):
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
		"step": res.step,
		"state": res.state,
		"system_at": res.loc or "",
		"harvest_date": str(res.harvest_date) if res.harvest_date else "",
		"received_at": str(res.received_at) if res.received_at else "",
	}


def _evaluate_take(take, ctx, a):
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
	for k, s in scanned.items():
		allk.setdefault(k, s.bucket_id)
	ev = _evaluate(take, T, list(allk.values()), set(scanned), ctx)

	found, missing, unexpected, allocated_unscanned = [], [], [], []
	for k, res in ev.items():
		res.raw = allk.get(k, k)
		age = (getdate(T) - res.harvest_date).days if res.harvest_date else None
		s = scanned.get(k)
		expected = res.cycle and res.state == "in_store" and res.loc == take.farm
		if s:
			row = _row(take, res, s, age if age is not None else s.age_days, T)
			row["expected"] = bool(expected)
			row["reason"] = "" if expected else _unexpected_reason(res, take.farm)
			found.append(row)
			if not expected:
				unexpected.append(row)
		elif expected:
			missing.append(_row(take, res, None, age, T))
		elif res.cycle and res.state == "allocated" and res.loc == take.farm:
			allocated_unscanned.append(_row(take, res, None, age, T))
	return T, found, missing, unexpected, allocated_unscanned


def _cached_take(take, ctx, a):
	# A take is a historical fact; evaluating it scans ~60 days of receipts (several
	# seconds). Cache the unfiltered result 10 min, keyed on the take's modified stamp.
	mod = frappe.db.get_value("Cold Store Stock Take", take.name, "modified")
	key = f"ph2_stock_take::{take.name}::{mod}"
	hit = frappe.cache.get_value(key)
	if hit:
		return hit
	res = _evaluate_take(take, ctx, a)
	frappe.cache.set_value(key, res, expires_in_sec=600)
	return res


# ───────────────────────── filters + roll-ups ─────────────────────────


def _keep(row, a, ctx):
	if a["variety"] and row["variety"] != a["variety"]:
		return False
	if rose_core.normalize(a["rose"]) != "all":
		if rose_core.rose_type(ctx.group_of(row["variety"])) != rose_core.normalize(a["rose"]):
			return False
	return True


def _rollup(rows, key):
	g = defaultdict(lambda: {"buckets": 0, "stems": 0.0})
	for r in rows:
		x = g[r[key]]
		x["buckets"] += 1
		x["stems"] += r["stems"]
	return g


@frappe.whitelist()
def get_stock_take(**kw):
	"""Everything the page shows for the filters."""
	a = _args(kw)
	takes_all = _takes(a, ignore_take=True)
	takes = [t for t in takes_all if not a["stock_take"] or t.name == a["stock_take"]]
	ctx = Ctx()
	found, missing, unexpected, alloc_un = [], [], [], []
	per_take = []
	for t in takes:
		T, f, m, u, au = _cached_take(t, ctx, a)
		f = [r for r in f if _keep(r, a, ctx)]
		m = [r for r in m if _keep(r, a, ctx)]
		u = [r for r in u if _keep(r, a, ctx)]
		au = [r for r in au if _keep(r, a, ctx)]
		found += f
		missing += m
		unexpected += u
		alloc_un += au
		fe = sum(1 for r in f if r["expected"])
		per_take.append(
			{
				"name": t.name,
				"farm": t.farm,
				"date": str(t.stock_take_date),
				"as_of": str(T),
				"scanned": len(f),
				"found_expected": fe,
				"missing": len(m),
				"unexpected": len(u),
				"missing_stems": sum(r["stems"] for r in m),
			}
		)

	found_expected = sum(1 for r in found if r["expected"])
	kpi = {
		"scanned_buckets": len(found),
		"scanned_stems": sum(r["stems"] for r in found),
		"found_expected": found_expected,
		"expected_buckets": found_expected + len(missing),
		"expected_stems": sum(r["stems"] for r in found if r["expected"]) + sum(r["stems"] for r in missing),
		"missing_buckets": len(missing),
		"missing_stems": sum(r["stems"] for r in missing),
		"unexpected_buckets": len(unexpected),
		"unexpected_stems": sum(r["stems"] for r in unexpected),
		"allocated_unscanned_buckets": len(alloc_un),
		"allocated_unscanned_stems": sum(r["stems"] for r in alloc_un),
		"accuracy_pct": round(100.0 * found_expected / (found_expected + len(missing)), 1)
		if (found_expected + len(missing))
		else None,
		"takes": len(takes),
	}

	varieties = sorted({r["variety"] for r in found + missing if r["variety"]})
	by_var = {}
	for v in varieties:
		by_var[v] = {
			"variety": v,
			"scanned": 0,
			"found_expected": 0,
			"missing": 0,
			"missing_stems": 0.0,
			"unexpected": 0,
		}
	for r in found:
		x = by_var.get(r["variety"])
		if x:
			x["scanned"] += 1
			x["found_expected"] += 1 if r["expected"] else 0
			x["unexpected"] += 0 if r["expected"] else 1
	for r in missing:
		x = by_var.get(r["variety"])
		if x:
			x["missing"] += 1
			x["missing_stems"] += r["stems"]
	by_variety = sorted(by_var.values(), key=lambda x: (-x["missing"], -x["scanned"], x["variety"]))

	by_age = []
	for label, _lo, _hi in AGE_BANDS:
		by_age.append(
			{
				"band": label,
				"scanned": sum(1 for r in found if r["age_band"] == label),
				"missing": sum(1 for r in missing if r["age_band"] == label),
				"missing_stems": sum(r["stems"] for r in missing if r["age_band"] == label),
			}
		)

	zones = defaultdict(lambda: {"scanned": 0, "missing": 0, "missing_stems": 0.0})
	for r in found:
		zones[r["zone"]]["scanned"] += 1
	for r in missing:
		zones[r["zone"]]["missing"] += 1
		zones[r["zone"]]["missing_stems"] += r["stems"]
	by_zone = sorted(
		({"zone": z, **v} for z, v in zones.items()), key=lambda x: (-x["missing"], -x["scanned"], x["zone"])
	)

	steps = _rollup(missing, "step")
	by_step = sorted(
		({"step": s, "buckets": v["buckets"], "stems": v["stems"]} for s, v in steps.items()),
		key=lambda x: -x["buckets"],
	)
	reasons = _rollup(unexpected, "reason")
	by_reason = sorted(
		({"reason": s, "buckets": v["buckets"], "stems": v["stems"]} for s, v in reasons.items()),
		key=lambda x: -x["buckets"],
	)

	def cap(rows):
		return rows[:ROW_LIMIT]

	missing.sort(key=lambda r: (-(r["age_days"] or 0), r["bucket"]))
	return {
		"success": True,
		"kpis": kpi,
		"takes": [
			{"name": t.name, "farm": t.farm, "date": str(t.stock_take_date), "buckets": t.buckets}
			for t in takes_all
		],
		"per_take": per_take,
		"farms": sorted(
			{t.farm for t in _takes({**a, "farm": "", "region": ""}, ignore_take=True) if t.farm}
		),
		"varieties": varieties,
		"by_variety": by_variety,
		"by_age": by_age,
		"by_zone": by_zone,
		"by_step": by_step,
		"by_reason": by_reason,
		"found": cap(found),
		"missing": cap(missing),
		"unexpected": cap(unexpected),
		"allocated_unscanned": cap(alloc_un),
		"truncated": any(len(x) > ROW_LIMIT for x in (found, missing, unexpected, alloc_un)),
		"row_limit": ROW_LIMIT,
		"today": today(),
	}
