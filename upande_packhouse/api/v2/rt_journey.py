# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Remote Transfers v2 -> Bucket Journey tab (templates/remote_transfer_v2/bucket_journey.html).

One bucket, one clean timeline per journey ("cycle"), capturing everything the
catalog (docs/bucket-journey/EVENT_CATALOG.md) lists that can happen to a bucket.

DATA: `read_bucket()` reads every bucket-keyed table without the v1 row caps. The v1
reader `api.remote_transfer.bucket_journey.getBucketJourney` is left untouched; the rows
it returns for the stock entries, Shelving Log, Shelf Item and pick rows are the same rows
read here (tests/test_v2_rt_journey.py compares them), and the remote-transfer trace is the
v1 `transfer_scheduling.bucket_transfer_trace`, called without its limit of 5.

JOURNEY = ONE CYCLE, keyed by the Receiving stock entry (catalog B4):
  * every Harvesting entry carries `custom_receiving_entry` = the Receiving entry that
    claimed it, so all scans of a session are ONE journey (v1 made one journey per scan);
  * harvests nobody claimed form one open journey per unbroken run;
  * a Grading entry rides with its harvest (`custom_grading_entry`); an unlinked Grading,
    or a Receiving no harvest points at, is placed by time, or opens a journey of its own.

PLACEMENT of every other row is by FULL timestamp (the journey with the latest start at or
before the event), never by date and variety, so two journeys of one day no longer swap
rows. Rows with an explicit cycle key use it: quarantine entries by
`custom_receiving_batch_id`, a Bucket Reuse Anomaly by `harvest_stock_entry`, pick rows of a
replaced bucket by the replacement time, transfers by their pick list. A row that cannot be
tied to a journey (before the first harvest on record, or the bucket has no harvest at all)
is returned in `unplaced` with the reason, never dropped.

Reads only. Nothing is written or committed (README rule 1).
"""

import json
import re
from datetime import datetime

import frappe
from frappe import _

# Highlights added on top of the data. They never change a journey.
SLOW_RECEIVE_H = 24  # harvest -> received
SLOW_SHELVE_H = 12  # received -> first shelved
LONG_SHELF_H = 72  # on a shelf this long
LOST_AFTER_H = 12  # latest journey, no place known for this long

ISSUE_REASONS = ("Issued to Sales Order", "Offline Issuing")
S2S = "Transferred (Shelf-to-Shelf)"
TRUCK_REASON = "Transferred (Trolley/Truck)"
QUARANTINE_TYPES = ("Receiving Quarantined", "Quarantine Rejects", "Quarantine Accept", "Quarantine Transfer", "Material Transfer")
REJECT_TYPES = ("Packhouse Rejects", "Airport rejects", "Coldroom Rejects", "Coldroom rejects")
LEDGER_TYPES = (
	"Move To Graded Sold", "Issuing From Cold Store", "Issue From The Cold Store", "Farm Transfer", "Remote Transfers",
	"Offline Issuing", "Material Receipt", "Late Receipt", "Airport Reuse", "Packing", "Dispatch",
)

# The flow strip: (key, label, tracked, note). tracked: yes | partial | no.
FLOW = [
	("bucket_dispatch", "Empty bucket sent to farm", "no", "Bucket Dispatch records are not read"),
	("harvest", "Harvesting", "yes", ""),
	("grade", "Grading", "yes", ""),
	("receive", "Receiving", "yes", ""),
	("qc", "Quality check", "no", "A passed check leaves no record for the bucket"),
	("quarantine", "Quarantine", "yes", ""),
	("release", "Release", "yes", ""),
	("reject", "Rejected", "yes", ""),
	("accept", "Accepted", "yes", ""),
	("shelve", "Shelving", "yes", ""),
	("shelf_move", "Shelf move", "yes", ""),
	("transfer_req", "Transfer requested", "yes", ""),
	("transfer_transit", "In transit", "yes", ""),
	("transfer_recv", "Received at packhouse", "yes", ""),
	("allocate", "Allocated (OPL / orders)", "yes", ""),
	("issue", "Issuing", "yes", ""),
	("replace", "Replaced", "yes", ""),
	("not_found", "Not found", "yes", ""),
	("pack", "Packed", "partial", "Only the ready-for-packing flag and the pack scan on graded bunches; boxes are not linked to a bucket"),
	("dispatch", "Dispatched", "no", "Loading and dispatch are box level; they carry no bucket id"),
	("discard_listed", "On a discard request", "yes", ""),
	("discard", "Discarded", "yes", ""),
	("clear", "Shelf cleared", "yes", ""),
	("anomaly", "Reuse anomaly", "yes", ""),
	("count", "Stock count", "yes", ""),
	("edit", "Harvest edited / details corrected", "no", "Edits overwrite the record in place and leave no history"),
	("move_bunch", "Bunch moved to another bucket", "no", "Only a pending-reshelving flag is kept, not read"),
]
FLOW_KEYS = {f[0] for f in FLOW}


# ── helpers ────────────────────────────────────────────────────────────────────
def _ts(v):
	"""Sortable 'YYYY-MM-DD HH:MM:SS' from a JSON string (drops fractions, pads '15:33:8')."""
	s = str(v or "").replace("T", " ").strip()
	if not s:
		return ""
	d, _sp, t = s.partition(" ")
	parts = (t.split(".")[0] or "00:00:00").split(":")
	parts = (parts + ["0", "0", "0"])[:3]
	try:
		return "%s %02d:%02d:%02d" % (d, int(parts[0]), int(parts[1]), int(parts[2]))
	except ValueError:
		return d + " 00:00:00"


def _end_of_day(v):
	"""A date-only value (or midnight) means 'that day': use its last second."""
	t = _ts(v)
	return t[:10] + " 23:59:59" if t.endswith(" 00:00:00") else t


def _dt(ts):
	try:
		return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
	except Exception:
		return None


def _hours(a, b):
	da, db = _dt(_ts(a)), _dt(_ts(b))
	if not da or not db:
		return None
	return round((db - da).total_seconds() / 3600.0, 4)


def _wh(v):
	return str(v or "").split(" - ")[0]


def _who(v):
	return str(v or "").split("@")[0]


def _num(v):
	try:
		return float(v or 0)
	except Exception:
		return 0.0


def _n(n):
	n = float(n or 0)
	return str(int(n)) if n == int(n) else str(round(n, 1))


def _stems(n):
	return "%s stems" % _n(n) if n else ""


def _up(v):
	return str(v or "").upper()


def _plain(raw):
	"""Frappe's JSON serialisation round trip: dates and times become strings."""
	return json.loads(frappe.as_json(raw))


def _near(a, b, hours):
	h = _hours(a, b)
	return h is not None and abs(h) < hours


def gap_label(h):
	if h is None:
		return ""
	if h < 1:
		return "%d min" % round(h * 60)
	if h < 48:
		return "%g h" % round(h, 1)
	return "%g days" % round(h / 24.0, 1)


def _doc(doctype, name, label=None):
	return {"doctype": doctype, "name": name, "label": label or name} if name else None


# ── reading ────────────────────────────────────────────────────────────────────
def _has(doctype, column):
	return frappe.db.has_column(doctype, column)


def _variants(b):
	return tuple({b, b.upper(), b.lower()})


SE_COLS = (
	"name", "stock_entry_type", "posting_date", "posting_time", "creation", "owner", "farm", "custom_greenhouse",
	"custom_stem_length", "custom_bucket_id", "custom_harvester", "custom_graded_by", "custom_bunch_id",
	"custom_bunched_by", "custom_grading_entry", "custom_harvest_entry", "custom_receiving_entry",
	"custom_receiving_batch_id", "custom_scanned_packing", "custom_opl_scanned", "custom_total_rejected_qty",
	"custom_rejection_stock_entry", "custom_rejection_date", "custom_issued_to", "custom_cut_stage",
	"to_warehouse", "from_warehouse", "docstatus", "amended_from",
)


def read_bucket(bucket_id, trace=True):
	"""Every bucket-keyed row, uncapped, JSON-normalised."""
	ids = _variants(bucket_id)
	R = {"id": bucket_id}
	cols = ", ".join("`%s`" % c for c in SE_COLS if _has("Stock Entry", c))
	ses = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- fixed column list; values bound
		f"""SELECT {cols} FROM `tabStock Entry` WHERE custom_bucket_id IN %(ids)s AND docstatus < 2
		ORDER BY posting_date, posting_time, creation""",
		{"ids": ids}, as_dict=True)
	names = [s["name"] for s in ses]
	items = {}
	if names:
		for r in frappe.db.sql(
			"""SELECT parent, item_code, COALESCE(NULLIF(transfer_qty, 0), qty) AS stems, s_warehouse, t_warehouse,
			       custom_stem_length AS length
			FROM `tabStock Entry Detail` WHERE parent IN %(n)s ORDER BY parent, idx""", {"n": tuple(names)}, as_dict=True):
			items.setdefault(r.parent, []).append(r)
	for s in ses:
		it = items.get(s["name"], [])
		s["items"] = [{"item": i.item_code, "qty": _num(i.stems), "length": i.length, "s": i.s_warehouse, "t": i.t_warehouse} for i in it]
		s["qty"] = sum(_num(i.stems) for i in it)
	R["ses"] = ses
	R["cancelled"] = frappe.db.sql(
		"SELECT stock_entry_type, COUNT(*) FROM `tabStock Entry` WHERE custom_bucket_id IN %(ids)s AND docstatus = 2 GROUP BY stock_entry_type",
		{"ids": ids})
	R["shelf_log"] = frappe.db.sql(
		"""SELECT name, shelf, farm, variety, stem_qty, stem_length, greenhouse, shelved_on, removed_on, reason, shelved_by,
		       harvest_date, receiving_date, shelf_item, harvester, graded_by, cut_stage
		FROM `tabShelving Log` WHERE bucket_id IN %(ids)s ORDER BY shelved_on, creation""", {"ids": ids}, as_dict=True)
	farm_col = "COALESCE(NULLIF(si.farm, ''), sh.farm)" if _has("Shelf Item", "farm") else "sh.farm"
	R["shelf_items"] = frappe.db.sql(
		f"""SELECT si.name, si.parent, {farm_col} AS farm, si.variety, si.stem_qty, si.stem_length, si.greenhouse, si.date_added,
		       si.harvest_date, si.receiving_date
		FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
		WHERE si.bucket_id IN %(ids)s ORDER BY si.date_added""", {"ids": ids}, as_dict=True)  # nosemgrep
	log_names = [r["name"] for r in R["shelf_log"]]
	R["log_comments"] = (
		frappe.db.sql(
			"""SELECT reference_name, content, creation, owner FROM `tabComment`
			WHERE reference_doctype = 'Shelving Log' AND comment_type = 'Info' AND reference_name IN %(n)s ORDER BY creation""",
			{"n": tuple(log_names)}, as_dict=True) if log_names else [])
	R["discard_rows"] = frappe.db.sql(
		"""SELECT b.name, b.parent, b.discarded, b.farm, b.greenhouse, b.shelf, b.is_shelved, b.age_days, b.variety, b.stem_qty,
		       b.stem_length, b.harvest_date, b.creation, d.workflow_state, d.requested_date, d.approval_date, d.docstatus
		FROM `tabDiscard Request Bucket` b JOIN `tabDiscard Request` d ON d.name = b.parent
		WHERE b.bucket_id IN %(ids)s AND d.docstatus < 2 ORDER BY b.creation""", {"ids": ids}, as_dict=True) if frappe.db.exists("DocType", "Discard Request Bucket") else []
	opl_hdr = {"customer": "opl_customer", "order_name": "opl_order_name", "team": "opl_team", "date_created": "opl_date",
		"sales_order": "opl_sales_order", "status": "opl_status", "issuing_percentage": "opl_issuing_pct", "consignee": "opl_consignee"}
	hdr = ", ".join(f"opl.`{c}` AS {a}" for c, a in opl_hdr.items() if _has("Order Pick List", c))
	R["pli"] = frappe.db.sql(  # nosemgrep
		f"""SELECT pli.*, opl.name AS opl, opl.docstatus AS opl_docstatus{", " + hdr if hdr else ""}
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket IN %(ids)s
		ORDER BY pli.creation, pli.idx""", {"ids": ids}, as_dict=True)
	R["bas"] = frappe.db.sql(
		"""SELECT name, bucket_id, item_code, farm, stem_length, shelf_location, shelf_farm, harvest_date, receiving_entry,
		       total_quantity, allocated_quantity, available_quantity, in_transit, creation, modified
		FROM `tabBucket Allocation Status` WHERE bucket_id IN %(ids)s ORDER BY creation""", {"ids": ids}, as_dict=True)
	bnames = [b["name"] for b in R["bas"]]
	R["bas_alloc"] = (
		frappe.db.sql(
			"""SELECT parent, sales_order, sales_order_item, quantity_allocated, cancelled, issued, creation
			FROM `tabBucket Allocations` WHERE parent IN %(n)s ORDER BY creation""", {"n": tuple(bnames)}, as_dict=True) if bnames else [])
	from upande_packhouse.api import bucket_replacement

	R["replacements"] = bucket_replacement.for_bucket(bucket_id)
	ropl = tuple({x.get("order_pick_list") for x in R["replacements"] if x.get("order_pick_list")})
	R["opl_so"] = dict(frappe.db.sql("SELECT name, sales_order FROM `tabOrder Pick List` WHERE name IN %(n)s", {"n": ropl})) if ropl else {}
	R["anomalies"] = frappe.db.sql(
		"""SELECT name, skipped_step, detected_on, farm, greenhouse, variety, stems, previous_shelf, discard_request, harvest_stock_entry
		FROM `tabBucket Reuse Anomaly` WHERE bucket_id IN %(ids)s ORDER BY detected_on""", {"ids": ids}, as_dict=True) if frappe.db.exists("DocType", "Bucket Reuse Anomaly") else []
	R["tevents"] = frappe.db.sql(
		"""SELECT name, stage, outcome, event_time, user, farm, shelf, order_pick_list, trip, vehicle, stock_entry, details
		FROM `tabBucket Transfer Event` WHERE bucket IN %(ids)s ORDER BY event_time""", {"ids": ids}, as_dict=True) if frappe.db.exists("DocType", "Bucket Transfer Event") else []
	R["stem_repl"] = frappe.db.sql(
		"""SELECT name, opl, destination_bucket, donor_bucket, donor_shelf, variety, stem_length, stems, replaced_at, reason
		FROM `tabStem Replacement Log` WHERE destination_bucket IN %(ids)s OR donor_bucket IN %(ids)s ORDER BY replaced_at""", {"ids": ids}, as_dict=True) if frappe.db.exists("DocType", "Stem Replacement Log") else []
	R["pack_reject"] = frappe.db.sql(
		"""SELECT name, order_pick_list, bucket, donor_bucket, stems_rejected, stems_replaced, status, reason, reject_stock_entry, creation
		FROM `tabPacking Reject Log` WHERE bucket IN %(ids)s OR donor_bucket IN %(ids)s ORDER BY creation""", {"ids": ids}, as_dict=True) if frappe.db.exists("DocType", "Packing Reject Log") else []
	R["stock_take"] = frappe.db.sql(
		"SELECT name, parent, status, shelf, qty, scanned_at FROM `tabCold Store Stock Take Bucket` WHERE bucket_id IN %(ids)s ORDER BY scanned_at",
		{"ids": ids}, as_dict=True) if frappe.db.exists("DocType", "Cold Store Stock Take Bucket") else []
	R["bucket_count"] = frappe.db.sql(
		"SELECT name, parent, location, scanned_at FROM `tabBucket Count Bucket` WHERE bucket_id IN %(ids)s ORDER BY scanned_at",
		{"ids": ids}, as_dict=True) if frappe.db.exists("DocType", "Bucket Count Bucket") else []
	R["trace"] = []
	if trace:
		try:
			from upande_packhouse.api.remote_transfer import transfer_scheduling

			R["trace"] = transfer_scheduling.bucket_transfer_trace(bucket_id, limit=1000, after_packhouse=True)
		except Exception:
			frappe.log_error("rt_journey: transfer trace failed")
	R["qr"] = frappe.db.get_value("Bucket QR Code", bucket_id, ["status", "last_stock_entry", "current_journey_start"], as_dict=True) or {}
	return _plain(R)


# ── cycles ─────────────────────────────────────────────────────────────────────
def _se_ts(se):
	return _ts("%s %s" % (se.get("posting_date") or "", se.get("posting_time") or "00:00:00")) if se.get("posting_date") else _ts(se.get("creation"))


def make_cycles(R, pseudo_start=None):
	"""Journeys keyed by the Receiving entry (see module docstring)."""
	ses = R["ses"]
	harvest = sorted((s for s in ses if s["stock_entry_type"] == "Harvesting"), key=_se_ts)
	grading = {s["name"]: s for s in ses if s["stock_entry_type"] == "Grading"}
	receiving = {s["name"]: s for s in ses if s["stock_entry_type"] == "Receiving"}
	cycles, by_recv, run = [], {}, None
	used_g, used_r = set(), set()

	def new(kind, key):
		c = {"key": key, "kind": kind, "harvest": [], "grading": [], "receiving": [], "ev": [], "ledger": [], "start": ""}
		cycles.append(c)
		return c

	for h in harvest:
		g = grading.get(h.get("custom_grading_entry"))
		if g:
			used_g.add(g["name"])
		rn = h.get("custom_receiving_entry")
		if rn in receiving:
			run = None
			c = by_recv.get(rn) or by_recv.setdefault(rn, new("received", "r:" + rn))
			if rn not in used_r:
				c["receiving"].append(receiving[rn])
				used_r.add(rn)
		else:
			if run is None:
				run = new("open", "h:" + h["name"])
			c = run
		c["harvest"].append(h)
		if g:
			c["grading"].append(g)
	for r in sorted(receiving.values(), key=_se_ts):
		if r["name"] not in used_r:
			c = new("orphan_receiving", "r:" + r["name"])
			c["receiving"].append(r)
	for c in cycles:
		c["start"] = min([_se_ts(s) for s in c["harvest"] + c["receiving"]] or [""])
	if pseudo_start:
		new("no_harvest", "none")["start"] = pseudo_start
	cycles.sort(key=lambda c: (c["start"], c["key"]))
	for g in sorted((g for n, g in grading.items() if n not in used_g), key=_se_ts):
		c = place(cycles, _se_ts(g))
		if c is None:
			c = new("orphan_grading", "g:" + g["name"])
			c["start"] = _se_ts(g)
			cycles.append(c)
			cycles.sort(key=lambda x: (x["start"], x["key"]))
		c["grading"].append(g)
	for i, c in enumerate(cycles):
		c["n"] = i + 1
		c["_next_start"] = cycles[i + 1]["start"] if i + 1 < len(cycles) else ""
		for k in ("harvest", "grading", "receiving"):
			c[k].sort(key=_se_ts)
	return cycles


def place(cycles, ts):
	"""The journey with the latest start at or before `ts`; None when `ts` is earlier than all."""
	best = None
	for c in cycles:
		if c["start"] and c["start"] <= ts and (best is None or c["start"] >= best["start"]):
			best = c
	return best


def place_shelf(cycles, on, receiving_date):
	"""A shelving row names the receiving date it came from: prefer the journey received that
	day (latest start at or before the shelving); else place by time."""
	if receiving_date:
		cand = [c for c in cycles if c["start"] and c["start"] <= on and any(str(r.get("posting_date"))[:10] == str(receiving_date)[:10] for r in c["receiving"])]
		if cand:
			return max(cand, key=lambda c: c["start"])
	return place(cycles, on)


# ── events ─────────────────────────────────────────────────────────────────────
def _e(ev, t, title, where="", who="", line="", tone="", facts=None, docs=None, **extra):
	d = {"ev": ev, "t": _ts(t), "title": title, "where": where or "", "who": who or "", "line": line or "", "tone": tone,
		"flags": [], "gap_h": None, "facts": [f for f in (facts or [])], "docs": [x for x in (docs or []) if x]}
	d.update(extra)
	return d


ORDER = {"harvest": 1, "grade": 2, "receive": 3, "qc": 3, "quarantine": 4, "release": 5, "reject": 5, "accept": 5, "shelve": 6,
	"allocate": 7, "pick": 8, "transfer_req": 9, "transfer_transit": 10, "transfer_recv": 11, "issue": 12, "shelf_move": 12,
	"replace": 13, "clear": 14, "discard_listed": 15, "discard": 16}


def classify(tr):
	"""Remote-transfer trace stage -> (flow key, title, tone)."""
	m = {
		"Awaiting transfer": ("transfer_req", "Requested for transfer", "info"),
		"Not found at farm": ("not_found", "Marked not found at the farm", "bad"),
		"Left remote shelf": ("transfer_transit", "Left the farm shelf", "info"),
		"On trolley": ("transfer_transit", "Loaded on trolley", "info"),
		"Loaded in trolley": ("transfer_transit", "Loaded on trolley", "info"),
		"Load refused": ("transfer_transit", "Load refused", "bad"),
		"Loaded on truck": ("transfer_transit", "Loaded on truck", "info"),
		"Left the farm": ("transfer_transit", "Truck left the farm", "info"),
		"In transit": ("transfer_transit", "In transit", "info"),
		"Arrived at packhouse": ("transfer_recv", "Truck arrived at the packhouse", "info"),
		"Off the truck": ("transfer_transit", "Off the truck, not shelved", "warn"),
		"Shelving farm corrected": ("shelve", "Shelving farm corrected", "info"),
		"Shelving refused": ("shelve", "Shelving refused", "warn"),
		"Shelved at remote farm": ("shelve", "Shelved at the farm, not the packhouse", "warn"),
		"Removed from wrong shelf": ("shelve", "Removed from a wrong shelf", "warn"),
		"Shelved at sales farm": ("transfer_recv", "Shelved at the packhouse", "ok"),
		"Ready for packing": ("pack", "Ready for packing", "ok"),
		"Issued": ("issue", "Issued", "ok"),
		"Carried to next trip": ("transfer_transit", "Carried to the next trip", "warn"),
		"Not shelved when trip ended": ("transfer_transit", "Not shelved when the trip ended", "warn"),
		"Back to awaiting transfer": ("transfer_req", "Back to awaiting transfer", "warn"),
		"Back on farm shelf": ("transfer_req", "Back on the farm shelf", "warn"),
		"Load recorded on arrival": ("transfer_transit", "Load recorded on arrival", "info"),
	}
	return m.get(tr, ("transfer_transit", tr, "info"))


_SUPERSEDED = {"On trolley": ("Loaded on truck", "In transit", "Shelved at sales farm"), "Left the farm": ("In transit",),
	"Arrived at packhouse": ("Shelved at sales farm",)}


def build(R, now_ts=None, hub="", pseudo_start=None):
	"""Pure: R (read_bucket output) -> {cycles, unplaced, flow}."""
	now_ts = now_ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	bucket = R["id"]
	cycles = make_cycles(R, pseudo_start)
	unplaced = []

	def to_cycle(ts, ev, reason="before the first harvest on record", cycle=None):
		c = cycle or (place(cycles, _ts(ts)) if ts else None)
		if c is None:
			ev["reason"] = ("no timestamp recorded" if not ts else reason) if cycles else "this bucket has no harvest or receiving record"
			unplaced.append(ev)
			return None
		c["ev"].append(ev)
		return c

	# --- stock entries ---------------------------------------------------------------
	recv_batch = {}
	for c in cycles:
		for r in c["receiving"]:
			if r.get("custom_receiving_batch_id"):
				recv_batch[_up(r["custom_receiving_batch_id"])] = c
	cyc_of_se = {}
	for c in cycles:
		for h in c["harvest"]:
			cyc_of_se[h["name"]] = c
	for c in cycles:
		hv, gr, rc = c["harvest"], c["grading"], c["receiving"]
		if hv:
			h = hv[0]
			stems = sum(s["qty"] for s in hv)
			vars_ = []
			for s in hv:
				for i in s["items"]:
					if i["item"] and i["item"] not in vars_:
						vars_.append(i["item"])
			span = _hours(_se_ts(hv[0]), _se_ts(hv[-1]))
			c["ev"].append(_e("harvest", _se_ts(h), "Harvested", where=" · ".join(x for x in (h.get("farm"), _wh(h.get("custom_greenhouse"))) if x),
				who=h.get("custom_harvester") or _who(h.get("owner")),
				line=" · ".join(x for x in (_stems(stems), ", ".join(vars_[:3]) + (" +%d" % (len(vars_) - 3) if len(vars_) > 3 else ""),
					h.get("custom_stem_length"), "%d scans over %s" % (len(hv), gap_label(span)) if len(hv) > 1 and span else ("%d scans" % len(hv) if len(hv) > 1 else "")) if x),
				tone="ok",
				facts=[["Farm", h.get("farm")], ["Greenhouse", _wh(h.get("custom_greenhouse"))], ["Stem length", h.get("custom_stem_length")],
					["Harvester", h.get("custom_harvester")], ["Cut stage", h.get("custom_cut_stage")], ["To warehouse", _wh(h.get("to_warehouse"))],
					["Scans", len(hv)], ["First scan", _se_ts(hv[0])], ["Last scan", _se_ts(hv[-1])]],
				docs=[_doc("Stock Entry", s["name"]) for s in hv], vars=vars_, stems=stems))
		if gr:
			g = gr[0]
			c["ev"].append(_e("grade", _se_ts(g), "Graded and bunched", where=g.get("farm") or "", who=g.get("custom_graded_by") or _who(g.get("owner")),
				line=" · ".join(x for x in ("%d bunch%s" % (len(gr), "" if len(gr) == 1 else "es"), g.get("custom_stem_length")) if x), tone="info",
				facts=[["Graded by", g.get("custom_graded_by")], ["Bunched by", g.get("custom_bunched_by")], ["Bunch ID", g.get("custom_bunch_id")],
					["Packing scanned", "%d of %d bunches" % (sum(1 for x in gr if x.get("custom_scanned_packing")), len(gr))]],
				docs=[_doc("Stock Entry", s["name"]) for s in gr]))
			packed = [x for x in gr if x.get("custom_scanned_packing")]
			if packed:
				p0 = max(packed, key=lambda x: x.get("modified") or _se_ts(x))
				c["ev"].append(_e("pack", p0.get("modified") or _se_ts(p0), "Bunches pack-scanned", who=_who(p0.get("owner")), tone="ok",
					line="%d of %d bunches%s" % (len(packed), len(gr), " · " + ", ".join(sorted({x["custom_opl_scanned"] for x in packed if x.get("custom_opl_scanned")})[:3]) if any(x.get("custom_opl_scanned") for x in packed) else ""),
					facts=[["Time", "approximate: last change of the grading entry"]], docs=[_doc("Stock Entry", x["name"]) for x in packed], approx=True))
		for r in rc:
			its = r["items"]
			vs = ["%s %s" % (i["item"], _stems(i["qty"])) for i in its if i["item"]]
			rej = _num(r.get("custom_total_rejected_qty"))
			c["ev"].append(_e("receive", _se_ts(r), "Received", where=_wh(r.get("to_warehouse")), who=_who(r.get("owner")),
				line=" · ".join(x for x in (_stems(r["qty"]), r.get("custom_stem_length"), "%s rejected at intake" % _stems(rej) if rej else "") if x), tone="ok",
				facts=[["Varieties", "; ".join(vs)], ["Batch", r.get("custom_receiving_batch_id")], ["From", _wh(r.get("from_warehouse"))],
					["Rejected at intake", _stems(rej) if rej else ""], ["Rejection entry", r.get("custom_rejection_stock_entry")], ["Rejection date", r.get("custom_rejection_date")]],
				docs=[_doc("Stock Entry", r["name"])], stems=r["qty"]))
	# quarantine, rejects, discards, ledger legs
	has_qin = {}
	for s in R["ses"]:
		if s["stock_entry_type"] == "Receiving Quarantined":
			has_qin[_up(s.get("custom_receiving_batch_id"))] = True
	for s in R["ses"]:
		t = s["stock_entry_type"]
		ts = _se_ts(s)
		it0 = (s["items"] or [{}])[0]
		line_wh = "%s → %s" % (_wh(it0.get("s")), _wh(it0.get("t"))) if it0.get("s") or it0.get("t") else ""
		c0 = recv_batch.get(_up(s.get("custom_receiving_batch_id"))) if s.get("custom_receiving_batch_id") else None
		if t == "Receiving Quarantined":
			to_cycle(ts, _e("quarantine", ts, "Put in quarantine", where=s.get("farm") or "", who=_who(s.get("owner")), line=_stems(s["qty"]), tone="warn",
				facts=[["Moved", line_wh], ["Batch", s.get("custom_receiving_batch_id")]], docs=[_doc("Stock Entry", s["name"])], qty=s["qty"], batch=s.get("custom_receiving_batch_id")), cycle=c0)
		elif t in ("Quarantine Accept", "Quarantine Transfer") or (t == "Material Transfer" and has_qin.get(_up(s.get("custom_receiving_batch_id")))):
			to_cycle(ts, _e("accept" if t == "Quarantine Accept" else "release", ts, "Accepted out of quarantine" if t == "Quarantine Accept" else "Released from quarantine",
				where=s.get("farm") or "", who=_who(s.get("owner")), line=_stems(s["qty"]) + " accepted", tone="ok",
				facts=[["Moved", line_wh], ["Batch", s.get("custom_receiving_batch_id")]], docs=[_doc("Stock Entry", s["name"])], qty=s["qty"]), cycle=c0)
		elif t == "Quarantine Rejects":
			to_cycle(ts, _e("reject", ts, "Rejected", where=s.get("farm") or "", who=_who(s.get("owner")), line=_stems(s["qty"]) + " rejected", tone="bad",
				facts=[["Moved", line_wh], ["Batch", s.get("custom_receiving_batch_id")]], docs=[_doc("Stock Entry", s["name"])], qty=s["qty"]), cycle=c0)
		elif t in REJECT_TYPES:
			to_cycle(ts, _e("reject", ts, "Rejected (%s)" % t.replace("rejects", "Rejects"), where=s.get("farm") or "", who=_who(s.get("owner")),
				line=_stems(s["qty"]) + " rejected", tone="bad", facts=[["Moved", line_wh]], docs=[_doc("Stock Entry", s["name"])], qty=s["qty"]))
		elif t == "Discard":
			to_cycle(_ts(s.get("creation")), _e("discard", s.get("creation"), "Discarded", where=s.get("farm") or "", who=_who(s.get("owner")),
				line=_stems(s["qty"]), tone="bad", facts=[["Moved", line_wh]], docs=[_doc("Stock Entry", s["name"])], qty=s["qty"]))
		elif t not in ("Harvesting", "Grading", "Receiving"):
			to_cycle(ts, _e("ledger", ts, t, where=line_wh, who=_who(s.get("owner")), line=_stems(s["qty"]) + (" · issued to %s" % s["custom_issued_to"] if s.get("custom_issued_to") else ""),
				tone="", docs=[_doc("Stock Entry", s["name"])], ledger=True))

	# --- shelf rows ------------------------------------------------------------------
	live = {i["name"]: i for i in R["shelf_items"]}
	live_keys = {(i["parent"], _ts(i["date_added"])) for i in R["shelf_items"]}
	comments = {}
	for cm in R["log_comments"]:
		comments.setdefault(cm["reference_name"], []).append(re.sub(r"<[^>]+>", "", cm["content"] or "").strip())
	rows = []
	open_log_keys = set()
	for r in R["shelf_log"]:
		on, off = _ts(r.get("shelved_on")), _ts(r.get("removed_on"))
		ghost = bool(not off and r.get("shelf_item") and r["shelf_item"] not in live and (r["shelf"], on) not in live_keys)
		if not off and not r.get("shelf_item") and (r["shelf"], on) not in live_keys:
			ghost = not any(i["parent"] == r["shelf"] and _near(_ts(i["date_added"]), on, 0.01) for i in R["shelf_items"])
		phantom = bool(r.get("reason") == "Shelf Cleared" and off and _near(on, off, 0.0006))  # created closed by the clear itself: same second
		rows.append({"src": "log", "name": r["name"], "shelf": r["shelf"], "farm": r.get("farm") or "", "variety": r.get("variety") or "",
			"stems": _num(r.get("stem_qty")), "length": r.get("stem_length") or "", "gh": r.get("greenhouse") or "", "on": on, "off": off,
			"reason": r.get("reason") or "", "by": r.get("shelved_by") or "", "ghost": ghost, "phantom": phantom,
			"comments": comments.get(r["name"], []), "harvester": r.get("harvester"), "rdate": r.get("receiving_date")})
		if not off:
			open_log_keys.add((r["shelf"], on))
	for i in R["shelf_items"]:
		on = _ts(i["date_added"])
		if (i["parent"], on) in open_log_keys or any(r["src"] == "log" and not r["off"] and r["shelf"] == i["parent"] and _near(r["on"], on, 0.01) for r in rows):
			continue
		rows.append({"src": "item", "name": i["name"], "shelf": i["parent"], "farm": i.get("farm") or "", "variety": i.get("variety") or "",
			"stems": _num(i.get("stem_qty")), "length": i.get("stem_length") or "", "gh": i.get("greenhouse") or "", "on": on, "off": "",
			"reason": "", "by": "", "ghost": False, "phantom": False, "comments": [], "harvester": None, "rdate": i.get("receiving_date")})
	for r in rows:
		r["_live"] = (r["src"] == "item") or (not r["off"] and not r["ghost"])
	repl_old = [x for x in R["replacements"] if _up(x.get("old_bucket")) == _up(bucket)]
	repl_new = [x for x in R["replacements"] if _up(x.get("new_bucket")) == _up(bucket)]

	def rdoc(r):
		return _doc("Shelving Log" if r["src"] == "log" else "Shelf Item", r["name"], "%s · %s" % (r["shelf"], r["name"]))

	on_groups, off_groups, consumed = {}, {}, set()
	# rows of one bucket written by ONE request (a multi-variety bucket: one row per variety) lie within
	# a couple of seconds of each other; separate shelvings are always further apart
	def stamp(last, key, ts):
		prev = last.get(key)
		if prev and abs(_hours(prev[0], ts) or 0) <= 2 / 3600.0:
			last[key] = (ts, prev[1])
			return prev[1]
		last[key] = (ts, ts)
		return ts

	last_on, last_off = {}, {}
	for r in sorted(rows, key=lambda x: x["on"]):
		if not r["phantom"]:
			on_groups.setdefault((r["shelf"], stamp(last_on, r["shelf"], r["on"])), []).append(r)
	for r in sorted((x for x in rows if x["off"]), key=lambda x: x["off"]):
		off_groups.setdefault((r["shelf"], stamp(last_off, (r["shelf"], r["reason"]), r["off"]), r["reason"]), []).append(r)
	s2s_targets = {}
	for (shelf, off_m, reason), rs in off_groups.items():
		if reason != S2S:
			continue
		# the new shelving row is written in the same request that closes the old one: nearest within seconds
		best = None
		for gk, g in on_groups.items():
			if gk[0] == shelf or gk in consumed:
				continue
			h = _hours(g[0]["on"], rs[0]["off"])
			h = 99 if h is None else abs(h)
			if h < 30 / 3600.0 and (best is None or h < best[0]):
				best = (h, gk, g)
		if best:
			s2s_targets[(shelf, off_m, reason)] = best[2]
			consumed.add(best[1])
	for gk, g in on_groups.items():
		if gk in consumed:
			continue
		f = g[0]
		stems = sum(x["stems"] for x in g)
		vs = sorted({x["variety"] for x in g if x["variety"]})
		ev = _e("shelve", f["on"], "Shelved", where=" · ".join(x for x in (f["shelf"], f["farm"]) if x), who=_who(f["by"]),
			line=" · ".join(x for x in (_stems(stems), ", ".join(vs), f["length"]) if x), tone="ok",
			facts=[["Shelf", f["shelf"]], ["Farm", f["farm"]], ["Greenhouse", f["gh"]], ["Rows", len(g)]], docs=[rdoc(x) for x in g],
			shelf=f["shelf"], open=any(x["_live"] for x in g), stems=stems, on=f["on"])
		to_cycle(f["on"], ev, cycle=place_shelf(cycles, f["on"], f.get("rdate")))
	for (shelf, off_m, reason), g in off_groups.items():
		f = g[0]
		stems = sum(x["stems"] for x in g)
		on_since = min(x["on"] for x in g if x["on"]) if any(x["on"] for x in g) else f["off"]
		cmt = [c for x in g for c in x["comments"]]
		dest = s2s_targets.get((shelf, off_m, reason))
		old_hit = next((x for x in repl_old if abs(_hours(_ts(x.get("reported_at")), f["off"]) or 99) < 10 / 3600.0), None)  # same request
		if reason == S2S:
			ev = _e("shelf_move", f["off"], "Moved to another shelf", where=" → ".join([" · ".join(y for y in (shelf, f["farm"]) if y)] + ([dest[0]["shelf"]] if dest else [])),
				line=_stems(stems), tone="info", facts=[["From", shelf], ["To", dest[0]["shelf"] if dest else "no new shelving row found"]], docs=[rdoc(x) for x in g + (dest or [])], on_since=on_since, stems=stems)
		elif reason in ("Issued to Sales Order",):
			ev = _e("issue", f["off"], "Off the shelf: issued", where=" · ".join(x for x in (shelf, f["farm"]) if x), line=_stems(stems), tone="ok",
				docs=[rdoc(x) for x in g], on_since=on_since, stems=stems, shelf_empty=True)
		elif reason == "Offline Issuing":
			ev = _e("issue", f["off"], "Issued offline", where=" · ".join(x for x in (shelf, f["farm"]) if x),
				line=" · ".join(x for x in (_stems(stems), cmt[0] if cmt else "reason not recorded") if x), tone="warn",
				facts=[["Reason", "; ".join(cmt)]], docs=[rdoc(x) for x in g], on_since=on_since, stems=stems, shelf_empty=True)
		elif reason == TRUCK_REASON:
			ev = _e("transfer_transit", f["off"], "Taken off the shelf for transfer", where=" · ".join(x for x in (shelf, f["farm"]) if x), line=_stems(stems),
				tone="info", docs=[rdoc(x) for x in g], on_since=on_since, stems=stems, shelf_empty=True)
		elif reason == "Replaced":
			rep_hit = next((x for x in repl_new if abs(_hours(_ts(x.get("reported_at")), f["off"]) or 99) < 10 / 3600.0), None)
			ev = _e("replace", f["off"], "Taken off the shelf: used as the replacement" if rep_hit else "Used as a replacement for another bucket", where=" · ".join(x for x in (shelf, f["farm"]) if x),
				line=" · ".join(x for x in (_stems(stems), ("for %s" % repl_new[0]["old_bucket"]) if repl_new else "") if x), tone="warn",
				docs=[rdoc(x) for x in g], on_since=on_since, stems=stems, shelf_empty=True)
		elif reason == "Shelf Cleared":
			title = "Cleared off the shelf (replaced by %s)" % old_hit["new_bucket"] if old_hit else "Shelf cleared"
			ev = _e("clear", f["off"], title, where=" · ".join(x for x in (shelf, f["farm"]) if x),
				line=" · ".join(x for x in (_stems(stems), "no shelving record existed" if f["phantom"] else "") if x), tone="" if not old_hit else "warn",
				docs=[rdoc(x) for x in g], on_since=on_since, stems=stems, shelf_empty=True)
		else:
			ev = _e("clear", f["off"], reason or "Taken off the shelf", where=" · ".join(x for x in (shelf, f["farm"]) if x), line=_stems(stems), docs=[rdoc(x) for x in g], on_since=on_since, stems=stems, shelf_empty=True)
		to_cycle(on_since, ev, cycle=place_shelf(cycles, on_since, f.get("rdate")))  # a removal belongs to the journey of the shelving it closes
	ghost_rows = [r for r in rows if r["ghost"]]

	# --- discard requests -------------------------------------------------------------
	for d in R["discard_rows"]:
		ts = _end_of_day(d.get("harvest_date")) if d.get("harvest_date") else _ts(d.get("creation"))
		listed = _ts(d.get("approval_date") or d.get("requested_date") or d.get("creation"))
		if listed.endswith(" 00:00:00"):  # dates only on the request: the row creation has the clock time
			listed = _ts(d.get("creation")) or listed
		# reuse clean-up force-marks stale request rows discarded; with no Discard entry that is NOT a discard
		if _num(d.get("discarded")) and any(a.get("discard_request") == d["parent"] for a in R["anomalies"]) and not any(x.get("stock_entry_type") == "Discard" for x in R["ses"]):
			d = dict(d, discarded=0, swept=1)
		ev = _e("discard_listed", listed, ("On a discard request, never discarded (closed when the bucket was reused)" if d.get("swept") else "On a discard request") if not _num(d.get("discarded")) else "Listed on a discard request (discarded)", where=" · ".join(x for x in (d.get("farm"), d.get("shelf")) if x),
			line=" · ".join(x for x in (_stems(_num(d.get("stem_qty"))), "age %s days" % _n(d.get("age_days")) if d.get("age_days") else "", d.get("workflow_state")) if x),
			tone="warn" if not _num(d.get("discarded")) else "bad",
			facts=[["Request", d["parent"]], ["Discarded", "yes" if _num(d.get("discarded")) else "no"], ["Variety", d.get("variety")], ["Stem length", d.get("stem_length")],
				["Cycle harvested", d.get("harvest_date")], ["Approved", d.get("approval_date")]], docs=[_doc("Discard Request", d["parent"])],
			listed_only=not _num(d.get("discarded")), request=d["parent"])
		c = place(cycles, ts)
		if c is None:
			ev["reason"] = ("no timestamp recorded" if not ts else "before the first harvest on record") if cycles else "this bucket has no harvest or receiving record"
			unplaced.append(ev)
		else:
			ev["_cycle_ts"] = ts
			c["ev"].append(ev)

	# --- allocation, pick rows -----------------------------------------------------------
	repl_new_by_opl = {}
	for x in repl_new:
		repl_new_by_opl.setdefault(x.get("order_pick_list"), []).append(x)
	opl_cycle, groups = {}, {}
	for p in R["pli"]:
		groups.setdefault(p["opl"], []).append(p)
	for opl, rs in groups.items():
		p0 = rs[0]
		ts = _ts(min(r.get("creation") or "" for r in rs))
		rep = repl_new_by_opl.get(opl)
		if rep:
			ts = _ts(rep[0].get("reported_at")) or ts
		c = place(cycles, ts)
		stems = sum(_num(r.get("stock_qty")) for r in rs)
		iss = [r for r in rs if _num(r.get("issued")) or _num(r.get("issued_offline"))]
		iss_st = sum(_num(r.get("stock_qty")) for r in iss)
		boxes = sorted({str(r.get("custom_box_id")) for r in rs if r.get("custom_box_id")})
		order = p0.get("opl_order_name") or p0.get("opl_sales_order") or opl
		cancelled = _num(p0.get("opl_docstatus")) == 2
		table = [[("box %s" % r["custom_box_id"]) if r.get("custom_box_id") else "row %s" % r.get("idx"), _n(_num(r.get("stock_qty"))),
			"issued offline" if _num(r.get("issued_offline")) else ("issued" if _num(r.get("issued")) else ("not found" if _num(r.get("not_found")) else "open")),
			r.get("sales_order_item") or r.get("custom_sale_order_item") or "", r.get("shelf") or ""] for r in rs]
		ev = _e("allocate", ts, ("Allocated to %s" % order) + (" · %s" % opl if sum(1 for q in groups.values() if (q[0].get("opl_order_name") or q[0].get("opl_sales_order")) == (p0.get("opl_order_name") or p0.get("opl_sales_order"))) > 1 else "") + (" (cancelled pick list)" if cancelled else ""), where=p0.get("shelf") or "", who=_who(p0.get("owner")),
			line=" · ".join(x for x in (_stems(stems), "%d row%s" % (len(rs), "" if len(rs) == 1 else "s"), p0.get("opl_customer"),
				("issued %s of %s" % (_n(iss_st), _stems(stems))) if iss else "not issued") if x),
			tone="" if cancelled else ("ok" if iss and iss_st >= stems else "info"),
			facts=[["Customer", " · ".join(x for x in (p0.get("opl_customer"), p0.get("opl_consignee")) if x)], ["Sales order", p0.get("opl_sales_order")], ["Team", p0.get("opl_team")],
				["Boxes", ", ".join(boxes[:12])], ["Stems allocated", _n(stems)], ["Stems issued", _n(iss_st)], ["Order status", p0.get("opl_status")],
				["Replaced into this bucket", "yes, for %s" % rep[0]["old_bucket"] if rep else ""]],
			docs=[_doc("Order Pick List", opl)], table={"cols": ["Row", "Stems", "State", "Order line", "Shelf"], "rows": table},
			stems=stems, issued_stems=iss_st, opl=None if cancelled else opl, opl_all=opl, cancelled=cancelled, rows=len(rs), issued_rows=len(iss))
		if c is None:
			ev["reason"] = "before the first harvest on record" if cycles else "this bucket has no harvest or receiving record"
			unplaced.append(ev)
			continue
		c["ev"].append(ev)
		opl_cycle[opl] = c
		if cancelled:
			continue
		if iss:
			ts_i = _ts(max((r.get("issued_at") or "") for r in iss)) or ""
			approx = not ts_i
			ts_i = ts_i or _ts(max(r.get("modified") or "" for r in iss))
			offline = any(_num(r.get("issued_offline")) for r in iss)
			c["ev"].append(_e("issue", ts_i, "Issued %s%s" % (_stems(iss_st), " (offline)" if offline else ""), where=p0.get("shelf") or "", who=_who(next((r.get("issued_by") for r in iss if r.get("issued_by")), "")),
				line="%s · %d of %d rows" % (order, len(iss), len(rs)), tone="ok",
				facts=[["Stems issued", _n(iss_st)], ["Stems still open on this order", _n(stems - iss_st)], ["Time", "approximate: last change of the pick row" if approx else ""]],
				docs=[_doc("Order Pick List", opl)], approx=approx, issued_stems=iss_st, opl=opl))
		nf = [r for r in rs if _num(r.get("not_found"))]
		traced = any(e.get("stage") == "Not found at farm" for t in R["trace"] if t.get("opl") == opl for e in t.get("events") or [])
		if nf and not traced:
			c["ev"].append(_e("not_found", _ts(nf[0].get("not_found_at")) or ts, "Marked not found", where=nf[0].get("farm") or "", who=_who(nf[0].get("not_found_by")),
				line="%s · %s" % (order, _stems(sum(_num(r.get("stock_qty")) for r in nf))), tone="bad", docs=[_doc("Order Pick List", opl)]))
		ready = [r for r in rs if _num(r.get("custom_ready_for_packing"))]
		if ready and not any(e.get("ev") == "pack" and e.get("opl") == opl for e in c["ev"]):
			c["ev"].append(_e("pack", _ts(max(r.get("modified") or "" for r in ready)), "Ready for packing", where=p0.get("shelf") or "",
				line="%s%s" % (order, " · box " + str(ready[0]["custom_box_id"]) if ready[0].get("custom_box_id") else ""), tone="ok",
				facts=[["Time", "approximate: last change of the pick row"]], docs=[_doc("Order Pick List", opl)], approx=True, opl=opl))
	# Bucket Allocation Status: ONE row per bucket+variety+length for ALL journeys. Its child lines carry their own
	# creation time, so each journey gets only its own lines (the header numbers are the current state, not history).
	soi_cycle = {}
	soi_ts = {}
	for p in R["pli"]:
		for key in (p.get("sales_order_item"), p.get("custom_sale_order_item")):
			if key and opl_cycle.get(p["opl"]) is not None:
				soi_cycle[key] = opl_cycle[p["opl"]]
				soi_ts[key] = min(soi_ts.get(key) or "9", str(p.get("creation")))
	allocs = {}
	for a in R["bas_alloc"]:
		allocs.setdefault(a["parent"], []).append(a)
	for b in R["bas"]:
		al = allocs.get(b["name"], [])
		per = {}
		for a in al:
			c = soi_cycle.get(a.get("sales_order_item"))
			if c is None:
				rp = next((x for x in repl_old if (x.get("sales_order") or R.get("opl_so", {}).get(x.get("order_pick_list"))) == a.get("sales_order")), None)
				c = place(cycles, _ts(rp.get("reported_at"))) if rp else None
			if c is None:
				c = place(cycles, _ts(a.get("creation"))) or place(cycles, _ts(b.get("creation")))
			per.setdefault(id(c) if c else 0, (c, []))[1].append(a)
		if not al:
			c = next((c for c in cycles if b.get("receiving_entry") and any(r["name"] == b["receiving_entry"] for r in c["receiving"])), None) or place(cycles, _ts(b.get("creation")))
			per[id(c) if c else 0] = (c, [])
		for c, lines in per.values():
			live_al = [a for a in lines if not _num(a.get("cancelled"))]
			st = sum(_num(a.get("quantity_allocated")) for a in live_al)
			ts = _ts(min([soi_ts.get(a.get("sales_order_item")) or a.get("creation") or "" for a in lines] or [b.get("creation")]))
			if c is not None and ts < c["start"]:
				ts = c["start"]
			ev = _e("allocate", ts, "Allocation record: %s on %d order line%s" % (_stems(st) or "no stems", len(live_al), "" if len(live_al) == 1 else "s"),
				where=b.get("shelf_location") or "", line="%d line%s%s · current record: %s of %s allocated" % (len(lines), "" if len(lines) == 1 else "s", ", %d cancelled" % (len(lines) - len(live_al)) if len(lines) > len(live_al) else "",
					_n(_num(b.get("allocated_quantity"))), _n(_num(b.get("total_quantity")))),
				tone="info", facts=[["Variety", b.get("item_code")], ["Stem length", b.get("stem_length")], ["Total (now)", b.get("total_quantity")], ["Allocated (now)", b.get("allocated_quantity")],
					["Available (now)", b.get("available_quantity")], ["In transit (now)", "yes" if _num(b.get("in_transit")) else ""], ["Receiving entry", b.get("receiving_entry")]],
				docs=[_doc("Bucket Allocation Status", b["name"])] + [_doc("Sales Order", a["sales_order"], "%s (%s%s%s)" % (a["sales_order"], _n(_num(a.get("quantity_allocated"))), ", cancelled" if _num(a.get("cancelled")) else "", ", issued" if _num(a.get("issued")) else "")) for a in lines],
				approx=not lines, bas=True)
			if c is None:
				ev["reason"] = "before the first harvest on record" if cycles else "this bucket has no harvest or receiving record"
				unplaced.append(ev)
			else:
				c["ev"].append(ev)
				c.setdefault("bas", []).append(b)

	# --- replacements ---------------------------------------------------------------------
	for x in R["replacements"]:
		old = _up(x.get("old_bucket")) == _up(bucket)
		during = "during a transfer" if (x.get("trip") or x.get("vehicle") or (hub and x.get("farm") and _up(x["farm"]) != _up(hub))) else "while issuing"
		order = x.get("order_name") or x.get("order_pick_list") or "an order"
		if old:
			title = "Replaced by %s %s" % (x.get("new_bucket") or "?", during)
			line = " · ".join(y for y in (order, x.get("reason"), ("still missing" if x.get("status") == "Open" else x.get("status") + (" · " + x["resolution"] if x.get("resolution") else ""))) if y)
		else:
			title = "Used as the replacement for %s %s" % (x.get("old_bucket") or "?", during)
			line = " · ".join(y for y in (order, x.get("reason")) if y)
		ev = _e("replace", x.get("reported_at"), title, where=x.get("farm") or "", who=_who(x.get("reported_by")), line=line, tone="warn" if x.get("status") == "Open" else "",
			facts=[["Old bucket", x.get("old_bucket")], ["New bucket", x.get("new_bucket")], ["Status", x.get("status")], ["Reason", x.get("reason")], ["Notes", x.get("notes")],
				["Expected shelf", x.get("expected_shelf")], ["New shelf", x.get("new_shelf")], ["Trip", x.get("trip")], ["Vehicle", x.get("vehicle")], ["Stems", x.get("stems")],
				["Resolved", x.get("resolved_at")]], docs=[_doc("Bucket Replacement", x["name"]), _doc("Order Pick List", x.get("order_pick_list"))],
			replacement=x["name"], old=old)
		to_cycle(_ts(x.get("reported_at")), ev)
	for x in R["stem_repl"]:
		donor = _up(x.get("donor_bucket")) == _up(bucket)
		to_cycle(_ts(x.get("replaced_at")), _e("replace", x.get("replaced_at"), ("Gave %s to %s" % (_stems(_num(x.get("stems"))), x.get("destination_bucket"))) if donor else ("Received %s from %s" % (_stems(_num(x.get("stems"))), x.get("donor_bucket"))),
			line=" · ".join(y for y in (x.get("variety"), x.get("reason")) if y), tone="warn", docs=[_doc("Stem Replacement Log", x["name"]), _doc("Order Pick List", x.get("opl"))]))
	for x in R["pack_reject"]:
		to_cycle(_ts(x.get("creation")), _e("reject", x.get("creation"), "Packing reject", line=" · ".join(y for y in (_stems(_num(x.get("stems_rejected"))) + " rejected", _stems(_num(x.get("stems_replaced"))) + " replaced" if x.get("stems_replaced") else "", x.get("reason")) if y),
			tone="bad", docs=[_doc("Packing Reject Log", x["name"]), _doc("Stock Entry", x.get("reject_stock_entry"))]))

	# --- remote transfers (trace) and logged transfer events -------------------------------
	seen_ev = set()
	for tr in R["trace"]:
		evs = [e for e in (tr.get("events") or []) if e.get("datetime")]
		first = min([_ts(e["datetime"]) for e in evs] or [""])
		c = opl_cycle.get(tr.get("opl")) or (place(cycles, first) if first else None)
		stages = {e.get("stage") for e in tr.get("events") or []}
		for e in tr.get("events") or []:
			st = e.get("stage")
			if any(x in stages for x in _SUPERSEDED.get(st, ())) or st == "Stock moved":
				continue
			key, title, tone = classify(st)
			if e.get("refused"):
				tone = "bad"
			at = _ts(e.get("datetime"))
			seen_ev.add((st, at[:16]))
			where = e.get("vehicle") or ""
			if key == "transfer_req" or st in ("Left remote shelf", "Not found at farm"):
				where = tr.get("from_farm") or ""
			elif st in ("Shelved at sales farm", "Ready for packing", "Issued", "Arrived at packhouse"):
				where = hub
			if st == "Issued" and c and any(x["ev"] == "issue" for x in c["ev"]):
				continue
			if st == "Ready for packing" and c and any(x["ev"] == "pack" and x.get("opl") == tr.get("opl") for x in c["ev"]):
				continue
			if st == "Shelved at sales farm" and c and any(x["ev"] == "shelve" and x.get("farm_hub") for x in c["ev"]):
				continue
			line = e.get("detail") or ""
			if st == "Awaiting transfer":  # the trace reads the pick row's CURRENT farm/shelf: use the shelf the bucket really stood on
				sr = next((r for r in sorted(rows, key=lambda x: x["on"]) if r["reason"] == TRUCK_REASON and r["off"] >= at), None) or next(
					(r for r in sorted(rows, key=lambda x: x["on"], reverse=True) if r["on"] <= at and r["shelf"] and r["farm"]), None)
				if sr:
					where = sr["farm"]
					line = "%s shelf %s → %s for %s" % (sr["farm"], sr["shelf"], tr.get("to_farm") or hub or "packhouse", tr.get("order_name") or tr.get("opl"))
			ev = _e(key, at, title, where=where, who=_who(e.get("user")), line=line, tone=tone, stage=st, transfer=tr.get("opl"),
				docs=[_doc("Bucket Request Trip", e.get("trip")), _doc("Order Pick List", tr.get("opl"))], opl=tr.get("opl"))
			if c is None:
				ev["reason"] = "before the first harvest on record" if cycles else "this bucket has no harvest or receiving record"
				unplaced.append(ev)
			else:
				c["ev"].append(ev)
		if c is not None:
			c.setdefault("traces", []).append(tr)
	for e in R["tevents"]:
		at = _ts(e.get("event_time"))
		if (e.get("stage"), at[:16]) in seen_ev:
			continue
		key, title, tone = classify(e.get("stage"))
		if e.get("outcome") == "Refused":
			tone = "bad"
		c = opl_cycle.get(e.get("order_pick_list")) or place(cycles, at)
		ev = _e(key, at, title, where=" · ".join(y for y in (e.get("farm"), e.get("shelf"), e.get("vehicle")) if y), who=_who(e.get("user")), line=e.get("details") or "", tone=tone,
			docs=[_doc("Bucket Transfer Event", e["name"]), _doc("Bucket Request Trip", e.get("trip")), _doc("Stock Entry", e.get("stock_entry"))], stage=e.get("stage"))
		if c is None:
			ev["reason"] = "before the first harvest on record" if cycles else "this bucket has no harvest or receiving record"
			unplaced.append(ev)
		else:
			c["ev"].append(ev)

	# --- reuse anomalies, stock counts --------------------------------------------------------
	for a in R["anomalies"]:
		step = a.get("skipped_step")
		c = None
		if a.get("harvest_stock_entry"):
			c = cyc_of_se.get(a["harvest_stock_entry"])
		c = c or place(cycles, _ts(a.get("detected_on")))
		text = {"Issuing Skipped": "Previous batch was never issued", "Discarding Skipped": "Previous batch was on a discard request but never discarded",
			"Skipped Transfer": "Transfer step was skipped: shelved here while still on the farm shelf"}.get(step, step)
		ev = _e("anomaly", a.get("detected_on"), "Reuse anomaly: " + text, where=a.get("farm") or "",
			line=" · ".join(y for y in (("left %s on %s" % (_stems(_num(a.get("stems"))), a.get("previous_shelf"))) if a.get("previous_shelf") else _stems(_num(a.get("stems"))), a.get("variety")) if y), tone="bad",
			facts=[["Skipped step", step], ["Previous shelf", a.get("previous_shelf")], ["Previous variety", a.get("variety")], ["Previous stems", a.get("stems")],
				["Discard request", a.get("discard_request")], ["Started by harvest", a.get("harvest_stock_entry")]],
			docs=[_doc("Bucket Reuse Anomaly", a["name"]), _doc("Discard Request", a.get("discard_request")), _doc("Stock Entry", a.get("harvest_stock_entry"))], anomaly=step,
			prev_stems=_num(a.get("stems")), prev_shelf=a.get("previous_shelf"))
		if c is None:
			ev["reason"] = "before the first harvest on record" if cycles else "this bucket has no harvest or receiving record"
			unplaced.append(ev)
			continue
		c["ev"].append(ev)
		if step in ("Issuing Skipped", "Discarding Skipped") and a.get("harvest_stock_entry"):
			prev = next((x for x in reversed(cycles) if x["n"] < c["n"]), None)
			if prev is not None:
				prev["ev"].append(_e("anomaly", a.get("detected_on"), "Left on the shelf, then cleaned up when the bucket was reused",
					where=a.get("previous_shelf") or "", line="%s %s" % (_stems(_num(a.get("stems"))), "never issued" if step == "Issuing Skipped" else "listed for discard, never discarded"),
					tone="bad", docs=[_doc("Bucket Reuse Anomaly", a["name"])], anomaly=step, leftover=True))
	for x in R["stock_take"]:
		to_cycle(_ts(x.get("scanned_at")), _e("count", x.get("scanned_at"), "Counted in a stock take", where=x.get("shelf") or "", line=" · ".join(y for y in (x.get("status"), _stems(_num(x.get("qty")))) if y),
			docs=[_doc("Cold Store Stock Take", x.get("parent"))]))
	for x in R["bucket_count"]:
		to_cycle(_ts(x.get("scanned_at")), _e("count", x.get("scanned_at"), "Scanned in a bucket count", where=x.get("location") or "", docs=[_doc("Bucket Count", x.get("parent"))]))

	# --- virtual close of shelving rows left open without a Shelf Item ------------------------------
	for gr in ghost_rows:
		c = place_shelf(cycles, gr["on"], gr.get("rdate"))
		if c is None:
			continue
		closers = [e["t"] for e in c["ev"] if e["ev"] == "discard" and e["t"] >= gr["on"]]
		# a Skipped Transfer anomaly names the remote shelf it removed, and when
		closers += [_ts(an.get("detected_on")) for an in R["anomalies"] if an.get("skipped_step") == "Skipped Transfer" and an.get("previous_shelf") == gr["shelf"] and _ts(an.get("detected_on")) >= gr["on"]]
		nxt = next((x["start"] for x in cycles if x["n"] == c["n"] + 1), "")
		closers += [t for t in (nxt,) if t]
		close = min(closers) if closers else ""
		if close and not any(e.get("ghost_close") == gr["name"] for e in c["ev"]):
			c["ev"].append(_e("clear", close, "Taken off the shelf by the discard (shelving log left open)" if close in [e["t"] for e in c["ev"] if e["ev"] == "discard"] else "Left the shelf (not recorded)", where=" · ".join(y for y in (gr["shelf"], gr["farm"]) if y), tone="warn",
				line="the shelf item is gone but the shelving log was never closed", docs=[rdoc(gr)], approx=True, ghost_close=gr["name"], shelf_empty=True, on_since=gr["on"]))

	for c in cycles:
		c["live_items"], c["ghosts"] = [], []
	for i in R["shelf_items"]:
		c = place_shelf(cycles, _ts(i["date_added"]), i.get("receiving_date"))
		if c is not None:
			c["live_items"].append(i)
	for g in ghost_rows:
		c = place_shelf(cycles, g["on"], g.get("rdate"))
		if c is not None:
			c["ghosts"].append(g)
	out = []
	for c in cycles:
		j = finish_cycle(c, R, now_ts, ghost_rows, hub)
		j["fix"] = fix_rows(c, R)
		out.append(j)
	counts = {k: 0 for k in FLOW_KEYS}
	for c in out:
		for e in c["steps"] + c["ledger"]:
			counts[e["ev"]] = counts.get(e["ev"], 0) + 1
	for e in unplaced:
		counts[e["ev"]] = counts.get(e["ev"], 0) + 1
	unplaced.sort(key=lambda e: e["t"])
	return {"cycles": out, "unplaced": unplaced, "counts": counts}


def finish_cycle(c, R, now_ts, ghost_rows, hub):
	evs = sorted(c["ev"], key=lambda e: (e["t"], ORDER.get(e["ev"], 9)))
	ledger = [e for e in evs if e.get("ledger")]
	steps = [e for e in evs if not e.get("ledger")]
	prev = None
	last = {}
	for s in steps:
		if prev and prev["t"] and s["t"]:
			s["gap_h"] = max(0.0, _hours(prev["t"], s["t"]) or 0.0)
		k = s["ev"]
		if k == "receive" and last.get("harvest"):
			h = _hours(last["harvest"]["t"], s["t"])
			if h is not None and h > SLOW_RECEIVE_H:
				s["flags"].append("%s from harvest to receiving" % gap_label(h))
		if k == "shelve" and not last.get("shelve") and last.get("receive"):
			h = _hours(last["receive"]["t"], s["t"])
			if h is not None and h > SLOW_SHELVE_H:
				s["flags"].append("Waited %s after receiving" % gap_label(h))
		if s.get("shelf_empty") and s.get("on_since"):
			h = _hours(s["on_since"], s["t"])
			if h is not None and h > LONG_SHELF_H:
				s["flags"].append("Sat on the shelf for %s" % gap_label(h))
		if s.get("listed_only"):
			s["flags"].append("Listed for discard, never discarded")
		last[k] = s
		prev = s
	# summary numbers
	kinds = [s["ev"] for s in steps]
	stems_in = sum(s.get("stems", 0) for s in steps if s["ev"] == "receive") or sum(s.get("stems", 0) for s in steps if s["ev"] == "harvest")
	alloc = sum(s.get("stems", 0) for s in steps if s["ev"] == "allocate" and s.get("opl"))
	issued = sum(s.get("issued_stems", 0) for s in steps if s["ev"] == "issue" and s.get("opl") and s["title"].startswith("Issued "))
	q_in = sum(s.get("qty", 0) for s in steps if s["ev"] == "quarantine")
	accepted = sum(s.get("qty", 0) for s in steps if s["ev"] in ("accept", "release"))
	rejected = sum(s.get("qty", 0) for s in steps if s["ev"] == "reject" and s.get("qty"))
	on_shelf_now = c["live_items"]
	on_stems = sum(_num(i.get("stem_qty")) for i in on_shelf_now)
	vars_ = next((s["vars"] for s in steps if s["ev"] == "harvest"), [])
	if not vars_:
		rcv = next((r for r in c["receiving"]), None)
		vars_ = [i["item"] for i in (rcv or {}).get("items", []) if i["item"]]
	state = stage_of(c, steps, on_shelf_now, issued, alloc, q_in, accepted, rejected, now_ts, R)
	flags = []
	if c["kind"] == "no_harvest":
		flags.append({"level": "warn", "text": "No harvest or receiving record for this journey"})
	if any(s.get("anomaly") and not s.get("leftover") for s in steps):
		flags.append({"level": "bad", "text": "Reuse anomaly recorded when this cycle started"})
	if any(s.get("leftover") for s in steps):
		flags.append({"level": "bad", "text": "Stems left behind: cleaned up when the bucket was reused"})
	if any(s.get("listed_only") for s in steps):
		flags.append({"level": "warn", "text": "Listed on a discard request but not discarded"})
	gh = c["ghosts"]
	if gh and not any(s.get("ghost_close") for s in steps):
		flags.append({"level": "warn", "text": "Shelving log open but no shelf item exists"})
	elif gh:
		flags.append({"level": "warn", "text": "Shelving log was never closed"})
	if len({i["parent"] for i in on_shelf_now}) > 1:
		flags.append({"level": "warn", "text": "Recorded on %d shelves at once" % len({i["parent"] for i in on_shelf_now})})
	if c["receiving"] and not any(k in ("shelve", "transfer_transit", "discard", "quarantine", "reject", "clear", "issue") for k in kinds) and c is not None:
		end = steps[-1]["t"] if steps else ""
		if c.get("n") and _hours(end, now_ts) and _hours(end, now_ts) > SLOW_SHELVE_H:
			flags.append({"level": "warn", "text": "Received but never shelved"})
	if c["kind"] == "open" and any(True for _ in [0]) and state["key"] == "harvested" and _hours(c["start"], now_ts) and _hours(c["start"], now_ts) > SLOW_RECEIVE_H:
		flags.append({"level": "warn", "text": "Harvested %s ago and not received" % gap_label(_hours(c["start"], now_ts))})
	if on_shelf_now and (_hours(min(_ts(i["date_added"]) for i in on_shelf_now), now_ts) or 0) > LONG_SHELF_H:
		flags.append({"level": "warn", "text": "On the shelf for %s" % gap_label(_hours(min(_ts(i["date_added"]) for i in on_shelf_now), now_ts))})
	if state["key"] == "quarantine":
		flags.append({"level": "warn", "text": "Still in quarantine"})
	return {
		"number": c["n"], "key": c["key"], "kind": c["kind"], "state": state["label"], "stage": state, "tone": state["tone"],
		"variety": ", ".join(vars_[:3]) + (" +%d" % (len(vars_) - 3) if len(vars_) > 3 else ""), "varieties": vars_,
		"stem_length": next((s.get("custom_stem_length") for s in c["harvest"] + c["grading"] + c["receiving"] if s.get("custom_stem_length")), ""),
		"farm": next((s.get("farm") for s in c["harvest"] + c["receiving"] + c["grading"] if s.get("farm")), "") or next((i.get("farm") for i in R["shelf_items"] if i.get("farm")), ""),
		"stems": stems_in, "allocated_stems": alloc, "issued_stems": issued, "on_shelf_stems": on_stems, "quarantined_stems": q_in, "accepted_stems": accepted, "rejected_stems": rejected,
		"start": steps[0]["t"] if steps else c["start"], "end": steps[-1]["t"] if steps else "", "start_date": (steps[0]["t"] if steps else c["start"])[:10], "end_date": (steps[-1]["t"] if steps else "")[:10],
		"flags": flags, "steps": steps, "ledger": ledger, "is_latest": False,
		"counts": {k: kinds.count(k) for k in set(kinds)},
		"fix": [], "batch": (c["harvest"] or c["receiving"] or c["grading"] or [{}])[0].get("name") or "",
	}


def _cycle_window(c, ts):
	nxt = c.get("_next_start")
	return c["start"] <= ts and (not nxt or ts < nxt)


def stage_of(c, steps, on_shelf_now, issued, alloc, q_in, accepted, rejected, now_ts, R):
	"""The current stage of the journey as one label + a detail line."""
	k = {s["ev"] for s in steps}
	last = steps[-1] if steps else {}

	def S(key, label, detail="", tone="info", since=None):
		return {"key": key, "label": label, "detail": detail, "tone": tone, "since": since if since is not None else last.get("t", "")}

	if any(s["ev"] == "discard" and not s.get("listed_only") for s in steps):
		d = next(s for s in reversed(steps) if s["ev"] == "discard" and not s.get("listed_only"))
		return S("discarded", "Discarded", d.get("where", ""), "bad", d["t"])
	if alloc and issued >= alloc and issued > 0:
		return S("issued", "Issued", "%s issued" % _stems(issued), "ok")
	if issued > 0 and not on_shelf_now and alloc and issued < alloc:
		return S("partly_issued", "Partly issued", "%s of %s issued" % (_n(issued), _stems(alloc)), "warn")
	if issued > 0:
		return S("partly_issued", "Partly issued", "%s issued, %s still on the shelf" % (_stems(issued), _stems(sum(_num(i.get("stem_qty")) for i in on_shelf_now))), "info")
	tr = [t for t in c.get("traces", []) if t.get("state") in ("at_farm", "on_trolley", "in_transit", "not_found")]
	# waiting for the truck the bucket still stands on its (remote) shelf; later stages mean it has left it
	tr = [t for t in tr if not on_shelf_now or t.get("state") == "at_farm"]
	if tr:
		t = tr[0]
		lab = {"at_farm": "Awaiting transfer", "on_trolley": "On trolley", "in_transit": "In transit", "not_found": "Marked not found"}[t["state"]]
		return S("transfer", lab, "%s → %s for %s" % (t.get("from_farm") or "?", t.get("to_farm") or "packhouse", t.get("order_name") or t.get("opl")), "bad" if t["state"] == "not_found" else "info")
	if any(s["ev"] == "allocate" and s.get("opl") for s in steps):
		return S("allocated", "Allocated, not yet issued", "%s on %d order%s%s" % (_stems(alloc), len({s["opl"] for s in steps if s["ev"] == "allocate" and s.get("opl")}), "" if len({s["opl"] for s in steps if s["ev"] == "allocate" and s.get("opl")}) == 1 else "s", (" · on shelf " + ", ".join(sorted({i["parent"] for i in on_shelf_now}))) if on_shelf_now else ""), "warn")
	old_rep = [s for s in steps if s["ev"] == "replace" and s.get("old")]
	if old_rep and not on_shelf_now:
		return S("replaced", "Replaced", old_rep[-1]["title"], "")
	if q_in and (q_in - accepted - rejected) > 0.5 and not on_shelf_now:
		return S("quarantine", "In quarantine", "%s waiting for a decision" % _stems(q_in - accepted - rejected), "warn")
	if "reject" in k and not on_shelf_now and "shelve" not in k:
		return S("rejected", "Rejected", _stems(rejected), "bad")
	if on_shelf_now:
		shelves = sorted({i["parent"] for i in on_shelf_now})
		return S("on_shelf", "On shelf " + ", ".join(shelves[:2]), " · ".join(y for y in (on_shelf_now[0].get("farm"), _stems(sum(_num(i.get("stem_qty")) for i in on_shelf_now))) if y), "info",
			min(_ts(i["date_added"]) for i in on_shelf_now))
	if "shelve" in k or "shelf_move" in k:
		last_off = next((s for s in reversed(steps) if s.get("shelf_empty")), None)
		return S("off_shelf", "Off the shelf", (last_off or {}).get("title", ""), "")
	if c["receiving"]:
		return S("received", "Received, not shelved", "", "info")
	if c["grading"] and not c["harvest"]:
		return S("graded", "Graded", "", "info")
	if not c["harvest"] and not c["receiving"] and not c["grading"]:
		return S("no_record", "No harvest record", "events exist but no harvest or receiving on file", "warn")
	return S("harvested", "Harvested, not received", "", "ok")


# ── API ────────────────────────────────────────────────────────────────────────────────
def fix_rows(c, R):
	rows = []
	for typ, lst in (("Harvest", c["harvest"]), ("Grading", c["grading"]), ("Receiving", c["receiving"])):
		for s in lst:
			rows.append({"type": typ, "doctype": "Stock Entry", "name": s["name"], "current": s.get("custom_stem_length") or "—"})
	for i in c.get("live_items", []):
		if True:
			rows.append({"type": "Shelf Item", "doctype": "Shelf Item", "name": i["name"], "label": "%s/%s" % (i["parent"], i["name"]), "current": "%s · %s" % (i.get("stem_length") or "—", i.get("variety") or "—")})
	for b in c.get("bas", []):
		rows.append({"type": "Allocation", "doctype": "Bucket Allocation Status", "name": b["name"], "current": "%s · %s" % (b.get("stem_length") or "—", b.get("item_code") or "—")})
	return rows


def build_bucket(R, now_ts=None, hub=""):
	"""Pure: read_bucket output -> the page's journeys (`cycles`), `unplaced` and the flow `counts`."""
	res = build(R, now_ts, hub)
	pseudo = None
	early = [e["t"] for e in res["unplaced"] if e["t"] and e.get("reason")]
	if early:
		pseudo = min(early)
		res = build(R, now_ts, hub, pseudo)
	if res["cycles"]:
		res["cycles"][-1]["is_latest"] = True
	return res


@frappe.whitelist()
def get_bucket_journeys(bucket_id: str | None = None, limit: int = 5, from_date: str | None = None, to_date: str | None = None, only: str | None = None):
	"""One bucket's journeys, ready to draw. `only` (comma separated numbers), else those that
	started between from_date and to_date, else the latest `limit` (0 = all)."""
	bucket_id = (bucket_id or "").strip()
	if not bucket_id:
		return {"success": False, "error": _("Enter a bucket ID.")}
	R = read_bucket(bucket_id)
	has = any(R.get(k) for k in ("ses", "shelf_log", "shelf_items", "pli", "discard_rows", "bas", "replacements", "anomalies", "trace", "tevents"))
	if not has:
		return {"success": True, "found": False, "bucket": {"id": bucket_id.upper()}}
	hub = ""
	try:
		from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

		hub = transfer_hub(required=False) or ""
	except Exception:
		pass
	now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	res = build_bucket(R, now_ts, hub)
	journeys = res["cycles"]
	flow = [{"key": k, "label": lb, "tracked": tr, "note": nt, "count": res["counts"].get(k, 0)} for k, lb, tr, nt in FLOW]
	latest = journeys[-1] if journeys else None
	index = [{"number": j["number"], "state": j["state"], "tone": j["tone"], "variety": j["variety"], "start_date": j["start_date"], "stems": j["stems"], "is_latest": j["is_latest"], "flags": len(j["flags"])} for j in journeys]
	if only:
		want = {int(x) for x in str(only).split(",") if x.strip().isdigit()}
		shown = [j for j in journeys if j["number"] in want]
	elif from_date or to_date:
		shown = [j for j in journeys if not j["start_date"] or ((not from_date or j["start_date"] >= from_date) and (not to_date or j["start_date"] <= to_date))]
	else:
		limit = int(limit or 0)
		shown = journeys[-limit:] if limit > 0 else journeys
	dates = sorted(x for x in (s.get("posting_date") for s in R["ses"] if s["stock_entry_type"] in ("Harvesting", "Grading", "Receiving")) if x)
	return {
		"success": True, "found": True, "now_ts": now_ts, "hub": hub, "flow": flow,
		"bucket": {
			"id": (R.get("id") or bucket_id).upper(), "journeys": len(journeys), "first_use": dates[0] if dates else "", "latest_activity": dates[-1] if dates else "",
			"state": latest["state"] if latest else "No journey", "tone": latest["tone"] if latest else "", "variety": latest["variety"] if latest else "",
			"stem_length": latest["stem_length"] if latest else "", "stems": latest["stems"] if latest else 0, "farm": latest["farm"] if latest else "",
			"cancelled": R.get("cancelled") or [],
		},
		"now": latest["stage"] if latest else {"key": "none", "label": "No journey on record", "detail": "", "tone": "", "since": ""},
		"replacements": [x for x in R["replacements"] if x.get("status") == "Open"],
		"unplaced": res["unplaced"], "index": index, "journeys": shown,
	}


@frappe.whitelist()
def search_buckets(q: str | None = None, date: str | None = None, limit: int = 14):
	"""Find a bucket to open. q: part of an id. date: buckets received that day. With neither: the most recently shelved."""
	limit = max(1, min(int(limit or 14), 50))
	q, date = (q or "").strip(), (date or "").strip()
	if q:
		rows = frappe.db.sql(
			"""SELECT name AS bucket, status FROM `tabBucket QR Code` WHERE name LIKE %(p)s ORDER BY (name = %(q)s) DESC, name LIMIT %(n)s""",
			{"p": "%" + q.replace("%", "") + "%", "q": q, "n": limit}, as_dict=True)
		for r in rows:
			r["note"] = "in use" if r["status"] == "In Use" else ""
		return {"success": True, "mode": "search", "rows": rows}
	if date:
		try:
			frappe.utils.getdate(date)
		except Exception:
			return {"success": False, "error": _("That is not a date.")}
		rows = frappe.db.sql(
			"""SELECT se.custom_bucket_id AS bucket, MIN(se.posting_time) AS at, MAX(se.farm) AS farm, SUM(d.transfer_qty) AS stems, MIN(d.item_code) AS variety
			FROM `tabStock Entry` se JOIN `tabStock Entry Detail` d ON d.parent = se.name
			WHERE se.stock_entry_type = 'Receiving' AND se.docstatus = 1 AND se.posting_date = %(d)s AND IFNULL(se.custom_bucket_id, '') != ''
			GROUP BY se.custom_bucket_id ORDER BY at DESC LIMIT %(n)s""", {"d": date, "n": limit}, as_dict=True)
		for r in rows:
			r["at"] = str(r["at"] or "")[:5]
			r["stems"] = _num(r["stems"])
			r["note"] = " · ".join(x for x in (r.get("farm"), "received " + r["at"]) if x)
		return {"success": True, "mode": "date", "rows": rows, "date": date}
	rows = frappe.db.sql(
		"""SELECT bucket_id AS bucket, MAX(shelf) AS shelf, MAX(farm) AS farm, MAX(variety) AS variety, SUM(stem_qty) AS stems, MAX(shelved_on) AS at
		FROM (SELECT bucket_id, shelf, farm, variety, stem_qty, shelved_on FROM `tabShelving Log` WHERE shelved_on IS NOT NULL ORDER BY shelved_on DESC LIMIT 400) x
		GROUP BY bucket_id ORDER BY at DESC LIMIT %(n)s""", {"n": limit}, as_dict=True)
	for r in rows:
		r["at"] = _ts(r["at"])
		r["stems"] = _num(r["stems"])
		r["note"] = " · ".join(x for x in (r.get("shelf"), r.get("farm")) if x)
	return {"success": True, "mode": "recent", "rows": rows}
