# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Remote Transfers v2 -> Bucket Journey -> Watchlist.

A watch says "I have an issue with this bucket: tell me when it appears next anywhere in the
process". Buckets marked NOT FOUND while issuing or during a transfer are added automatically.

WHERE 'NOT FOUND' IS RECORDED (and how it reaches the list, without touching the mobile apps):
  * ISSUING: `api.bucket_replacement.record()` inserts a Bucket Replacement (reason 'Missing') for
    the bucket that could not be found (mobile Bucket Requests -> `sales_allocation._replace_requested_bucket`;
    `offline_issue.replace_for_issuing`, whose app reason 'not_found' is stored as 'Missing').
    -> doc_event `Bucket Replacement.after_insert` = `on_replacement_insert`.
  * TRANSFER: `transfer_scheduling.mark_bucket_not_found` (farm app) sets `Pick List Item.not_found`,
    `not_found_at`, `not_found_by` with `frappe.db.set_value`, which fires NO doc event. -> one guarded
    call at the end of that function (`safe_add_transfer_not_found`), and `sync_not_found()`
    (run on every `run_watchlist` and by `backfill_not_found`) as a safety net for any other path.
  A replacement made while a transfer is running (trip/vehicle set, or a remote farm) counts as 'during
  transfer'; otherwise 'during issuing' (same rule as the journey page).
  Every hook goes through `_safe`, which swallows and logs any error behind a savepoint: it can never
  raise into the caller.

EVIDENCE OF REAPPEARING = the physical events of core/bucket_activity.py newer than the watch start
(+3 min grace for an auto-added watch: the not-found request itself writes swap entries and clears the
shelf). Allocation and the 'Shelf Cleared' clean-up do not count.

README rules: `get_*` is side-effect free; writes are POST (`add_`, `close_`, `run_`, `remove_`, `reopen_`).
"""

import time
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from upande_packhouse.api.v2.core import bucket_activity as act
from upande_packhouse.api.v2.core import region as rgn

DT = "Bucket Watch"
OPEN = ("Watching", "Reappeared")
RESOLVED, CONFIRMED = "Closed - Resolved", "Closed - Confirmed Missing"
SRC_ISSUING, SRC_TRANSFER, SRC_MANUAL = "Not found during issuing", "Not found during transfer", "Manual"
GRACE_SECONDS = 180  # an auto-added watch ignores what the not-found request itself wrote
RUN_JOURNEY_BUDGET_S = 12  # run_watchlist: journey builder time budget (it is the slow part)


# ── helpers ────────────────────────────────────────────────────────────────────
def _s(ts):
	return act._dt(ts).strftime("%Y-%m-%d %H:%M:%S") if act._dt(ts) else ""


def _need(ptype):
	frappe.has_permission(DT, ptype, throw=True)


def _hub():
	try:
		from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

		return transfer_hub(required=False) or ""
	except Exception:
		return ""


def known_farm(bucket):
	"""Where the bucket was last known: its shelf's farm, else its latest shelving row."""
	r = frappe.db.sql(
		"""SELECT COALESCE(NULLIF(si.farm, ''), sh.farm) FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
		WHERE si.bucket_id = %s ORDER BY si.date_added DESC LIMIT 1""",
		bucket,
	)
	if r and r[0][0]:
		return r[0][0]
	r = frappe.db.sql(
		"SELECT farm FROM `tabShelving Log` WHERE bucket_id = %s AND IFNULL(farm, '') != '' ORDER BY shelved_on DESC LIMIT 1",
		bucket,
	)
	return r[0][0] if r else ""


def _safe(fn):
	"""Wrap an automatic hook: never raise into the caller, never leave a half-written row."""

	def inner(*a, **k):
		try:
			frappe.db.savepoint("bucket_watch_auto")
			return fn(*a, **k)
		except Exception:
			try:
				frappe.db.rollback(save_point="bucket_watch_auto")
			except Exception:
				pass
			try:
				frappe.log_error(title="Bucket Watch auto-add failed", message=frappe.get_traceback())
			except Exception:
				pass
			return None

	inner.__name__ = fn.__name__
	return inner


# ── adding ─────────────────────────────────────────────────────────────────────
def add_auto(bucket, when, source, farm="", reason="", ref_doctype="", ref_name="", opl=""):
	"""Add (or update) the watch for a not-found event. Idempotent: an event no newer than the
	watch's start is ignored; a newer one reopens/refreshes it. Returns 'added'|'updated'|'reopened'|'same'."""
	bucket = act.up(bucket).strip()
	when = act._dt(when) or now_datetime()
	if not bucket:
		return "same"
	name = frappe.db.get_value(DT, {"bucket_id": bucket}, "name")
	vals = {
		"source": source,
		"farm": farm or known_farm(bucket),
		"reason": reason or "",
		"watching_since": when,
		"order_pick_list": opl or "",
		"reference_doctype": ref_doctype or "",
		"reference_name": ref_name or "",
	}
	if not name:
		doc = frappe.get_doc(
			{
				"doctype": DT,
				"bucket_id": bucket,
				"status": "Watching",
				"added_by": frappe.session.user,
				"added_on": now_datetime(),
				**vals,
			}
		)
		doc.insert(ignore_permissions=True)
		return "added"
	w = frappe.db.get_value(
		DT, name, ["status", "watching_since", "closed_on", "reference_name"], as_dict=True
	)
	same_event = ref_name and w.reference_name == ref_name
	if same_event or (w.watching_since and when <= act._dt(w.watching_since)):
		return "same"
	if w.status not in OPEN and w.closed_on and when <= act._dt(w.closed_on):
		return "same"  # an old event, closed since
	doc = frappe.get_doc(DT, name)
	was_open = w.status in OPEN
	doc.update(
		{
			**vals,
			"status": "Watching",
			"last_seen_at": None,
			"last_seen_event": "",
			"last_seen_journey": 0,
			"last_seen_location": "",
			"last_seen_stage": "",
			"closed_by": None,
			"closed_on": None,
			"close_note": "",
		}
	)
	doc.save(ignore_permissions=True)
	return "updated" if was_open else "reopened"


def _replacement_source(farm, trip, vehicle):
	hub = _hub()
	return (
		SRC_TRANSFER if (trip or vehicle or (hub and farm and act.up(farm) != act.up(hub))) else SRC_ISSUING
	)


@_safe
def _on_replacement(doc):
	if (doc.reason or "Missing") != "Missing" or not doc.old_bucket:
		return
	src = _replacement_source(doc.farm, doc.trip, doc.vehicle)
	add_auto(
		doc.old_bucket,
		doc.reported_at or now_datetime(),
		src,
		doc.farm or "",
		(
			"%s on %s" % (doc.notes, doc.order_name)
			if doc.notes
			else "Marked not found on %s" % (doc.order_name or doc.order_pick_list or "an order")
		),
		"Bucket Replacement",
		doc.name,
		doc.order_pick_list or "",
	)


def on_replacement_insert(doc, method=None):
	"""doc_events: Bucket Replacement after_insert."""
	_on_replacement(doc)


@_safe
def safe_add_transfer_not_found(bucket, farm, opls, when=None, notes=""):
	"""Called by transfer_scheduling.mark_bucket_not_found (guarded; never raises)."""
	if not bucket:
		return
	opl = ", ".join(sorted(opls or []))
	add_auto(
		bucket,
		when or now_datetime(),
		SRC_TRANSFER,
		farm or "",
		"Not found at %s%s" % (farm or "the farm", (" · " + notes) if notes else ""),
		"Pick List Item",
		"pli-nf:%s:%s" % (bucket, _s(when or now_datetime())),
		opl,
	)


def sync_not_found(since=None):
	"""Add every bucket currently marked not found that is not yet watched (or was lost again after).
	Idempotent. Returns {'replacements': n, 'transfers': n, 'added': n}."""
	out = {"replacements": 0, "transfers": 0, "added": 0, "updated": 0}
	events = []
	cond = "AND reported_at >= %(since)s" if since else ""
	for r in frappe.db.sql(
		f"""SELECT name, old_bucket, reported_at, farm, trip, vehicle, notes, order_name, order_pick_list FROM `tabBucket Replacement`
		WHERE reason = 'Missing' AND status = 'Open' AND IFNULL(old_bucket, '') != '' {cond}""",
		{"since": since},
		as_dict=True,
	):  # nosemgrep
		events.append(
			(
				act._dt(r.reported_at),
				r.old_bucket,
				_replacement_source(r.farm, r.trip, r.vehicle),
				r.farm,
				(
					"%s on %s" % (r.notes, r.order_name)
					if r.notes
					else "Marked not found on %s" % (r.order_name or r.order_pick_list or "an order")
				),
				"Bucket Replacement",
				r.name,
				r.order_pick_list or "",
			)
		)
		out["replacements"] += 1
	cond = "AND pli.not_found_at >= %(since)s" if since else ""
	for r in frappe.db.sql(
		f"""SELECT pli.bucket, MIN(pli.not_found_at) AS at, MAX(pli.farm) AS farm, GROUP_CONCAT(DISTINCT pli.parent) AS opls
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND pli.not_found = 1 AND pli.not_found_at IS NOT NULL AND IFNULL(pli.bucket, '') != '' AND o.docstatus < 2 {cond}
		GROUP BY pli.bucket""",
		{"since": since},
		as_dict=True,
	):  # nosemgrep
		events.append(
			(
				act._dt(r.at),
				r.bucket,
				SRC_TRANSFER,
				r.farm,
				"Not found at %s" % (r.farm or "the farm"),
				"Pick List Item",
				"pli-nf:%s:%s" % (act.up(r.bucket), _s(r.at)),
				r.opls or "",
			)
		)
		out["transfers"] += 1
	events.sort(key=lambda e: e[0] or datetime.min)  # oldest first: the newest event wins
	for when, bucket, src, farm, reason, rd, rn, opl in events:
		res = add_auto(bucket, when, src, farm or "", reason, rd, rn, opl)
		if res in ("added", "reopened"):
			out["added"] += 1
		elif res == "updated":
			out["updated"] += 1
	return out


def backfill_not_found():
	"""One-off (idempotent): bench --site <site> execute upande_packhouse.api.v2.rt_watchlist.backfill_not_found"""
	r = sync_not_found()
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- bench-executed one-off
	print(r)
	return r


def hourly_sync():
	"""Scheduler safety net for not-found marks written by a path with no hook."""
	_safe(sync_not_found)()


# ── latest appearance ──────────────────────────────────────────────────────────
def _starts(rows):
	out = {}
	for w in rows:
		t = act._dt(w["watching_since"] or w.get("added_on"))
		if t is not None and w.get("source") != SRC_MANUAL:
			t = t + timedelta(seconds=GRACE_SECONDS)
		out[act.up(w["bucket_id"])] = t
	return out


def latest_appearances(rows):
	"""{BUCKET: event|None}: each watch's newest physical event after its start. Batched: one set of
	queries for all watches, filtered per bucket in Python."""
	starts = _starts(rows)
	valid = {b: t for b, t in starts.items() if t}
	if not valid:
		return {}
	evs = act.events(tuple(valid), since=min(valid.values()), physical_only=True)
	best = {}
	for e in evs:  # ascending: the last one per bucket wins
		if e["ts"] > valid.get(e["b"], datetime.max):
			best[e["b"]] = e
	return best


def _row(w, ev, now):
	since = act._dt(w["watching_since"])
	stored = act._dt(w["last_seen_at"])
	live = bool(ev)
	if ev:
		out_ev = {
			"at": _s(ev["ts"]),
			"event": ev["title"],
			"where": ev["loc"] or "",
			"farm": ev["farm"] or "",
			"kind": ev["kind"],
			"ref": ev["ref"],
		}
	elif stored and w["status"] in (RESOLVED, CONFIRMED, "Reappeared"):
		out_ev = {
			"at": _s(stored),
			"event": w["last_seen_event"] or "",
			"where": w["last_seen_location"] or "",
			"farm": "",
			"kind": "",
			"ref": "",
		}
	else:
		out_ev = None
	same = bool(out_ev and stored and _s(stored) == out_ev["at"])
	end = act._dt(w["closed_on"]) if w["status"] in (RESOLVED, CONFIRMED) and w["closed_on"] else now
	return {
		"name": w["name"],
		"bucket_id": w["bucket_id"],
		"status": w["status"],
		"live_status": ("Reappeared" if live and w["status"] == "Watching" else w["status"]),
		"source": w["source"],
		"reason": w["reason"] or "",
		"farm": w["farm"] or "",
		"order_pick_list": w["order_pick_list"] or "",
		"watching_since": _s(since),
		"added_by": (w["added_by"] or "").split("@")[0],
		"added_on": _s(w["added_on"]),
		"days_missing": round(((end - since).total_seconds() / 86400.0), 1) if since else 0,
		"latest": out_ev,
		"journey": cint(w["last_seen_journey"]) if same else 0,
		"stage": (w["last_seen_stage"] if same else "") or "",
		"closed_by": (w["closed_by"] or "").split("@")[0],
		"closed_on": _s(w["closed_on"]),
		"close_note": w["close_note"] or "",
		"last_checked_at": _s(w["last_checked_at"]),
	}


WATCH_COLS = (
	"name",
	"bucket_id",
	"status",
	"source",
	"reason",
	"farm",
	"order_pick_list",
	"watching_since",
	"added_by",
	"added_on",
	"last_seen_at",
	"last_seen_event",
	"last_seen_journey",
	"last_seen_location",
	"last_seen_stage",
	"last_checked_at",
	"closed_by",
	"closed_on",
	"close_note",
)


def _filtered(search=None, farm=None, region=None, statuses=None):
	cond, P = ["1=1"], {}
	farms = rgn.farms_for(region=region, farm=farm)
	if farms is not None:
		cond.append("farm IN %(farms)s")
		P["farms"] = rgn.sql_tuple(farms)
	if search:
		cond.append("(bucket_id LIKE %(q)s OR reason LIKE %(q)s)")
		P["q"] = "%" + str(search).strip().replace("%", "") + "%"
	if statuses:
		cond.append("status IN %(st)s")
		P["st"] = tuple(statuses)
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- fixed columns/conditions; values bound
		"SELECT "
		+ ", ".join("`%s`" % c for c in WATCH_COLS)
		+ " FROM `tabBucket Watch` WHERE "
		+ " AND ".join(cond)
		+ " ORDER BY watching_since DESC",
		P,
		as_dict=True,
	)


def _compute(search=None, farm=None, region=None, status=None):
	now = now_datetime()
	allrows = _filtered(search, farm, region)
	openrows = [w for w in allrows if w["status"] in OPEN]
	ev = latest_appearances(openrows)
	rows = [_row(w, ev.get(act.up(w["bucket_id"])) if w["status"] in OPEN else None, now) for w in allrows]
	kpi = {"watching": 0, "reappeared": 0, "resolved": 0, "confirmed": 0, "total": len(rows)}
	for r in rows:
		k = r["live_status"]
		kpi[
			"watching"
			if k == "Watching"
			else "reappeared"
			if k == "Reappeared"
			else "resolved"
			if k == RESOLVED
			else "confirmed"
		] += 1
	if status:
		want = {
			"watching": "Watching",
			"reappeared": "Reappeared",
			"resolved": RESOLVED,
			"confirmed": CONFIRMED,
		}.get(str(status).lower(), status)
		rows = [r for r in rows if r["live_status"] == want]
	return rows, kpi, ev, allrows


# ── reads ──────────────────────────────────────────────────────────────────────
@frappe.whitelist()
def get_watchlist(
	status: str | None = None, search: str | None = None, farm: str | None = None, region: str | None = None
):
	"""The watchlist with each open watch's latest appearance computed NOW (nothing is written; the
	persisted last_seen_* is only updated by run_watchlist). KPIs follow search/farm/region, not `status`."""
	_need("read")
	t0 = time.time()
	rows, kpi, _ev, _all = _compute(search, farm, region, status)
	return {
		"success": True,
		"rows": rows,
		"kpi": kpi,
		"now_ts": _s(now_datetime()),
		"ms": int((time.time() - t0) * 1000),
	}


@frappe.whitelist()
def get_bucket_watch(bucket_id: str | None = None):
	"""One bucket's watch (or null) with its live appearance, for the Bucket view's button and status line."""
	_need("read")
	b = act.up(bucket_id).strip()
	w = _filtered(search=None)
	w = [x for x in w if act.up(x["bucket_id"]) == b]
	if not w:
		return {"success": True, "watch": None}
	w = w[0]
	ev = latest_appearances([w]).get(b) if w["status"] in OPEN else None
	return {"success": True, "watch": _row(w, ev, now_datetime())}


# ── writes ─────────────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
def add_watch(bucket_id: str | None = None, note: str | None = None):
	"""Watch a bucket by hand. One watch per bucket: adding again reopens it (from now) and updates the note."""
	_need("create")
	b = act.up(bucket_id).strip()
	if not b:
		return {"success": False, "error": _("Enter a bucket ID.")}
	if (
		not frappe.db.exists("Bucket QR Code", b)
		and not frappe.db.exists("Shelving Log", {"bucket_id": b})
		and not frappe.db.exists("Stock Entry", {"custom_bucket_id": b})
	):
		return {"success": False, "error": _("No bucket {0} on record.").format(b)}
	name = frappe.db.get_value(DT, {"bucket_id": b}, "name")
	if name:
		doc = frappe.get_doc(DT, name)
		if doc.status in OPEN:
			if note:
				doc.reason = note
				doc.save()
			return {"success": True, "name": name, "action": "already watching"}
		doc.update(
			{
				"status": "Watching",
				"source": SRC_MANUAL,
				"reason": note or doc.reason,
				"watching_since": now_datetime(),
				"added_by": frappe.session.user,
				"added_on": now_datetime(),
				"last_seen_at": None,
				"last_seen_event": "",
				"last_seen_journey": 0,
				"last_seen_location": "",
				"last_seen_stage": "",
				"closed_by": None,
				"closed_on": None,
				"close_note": "",
			}
		)
		doc.save()
		return {"success": True, "name": name, "action": "reopened"}
	doc = frappe.get_doc(
		{
			"doctype": DT,
			"bucket_id": b,
			"status": "Watching",
			"source": SRC_MANUAL,
			"reason": note or "",
			"farm": known_farm(b),
			"watching_since": now_datetime(),
			"added_by": frappe.session.user,
			"added_on": now_datetime(),
		}
	)
	doc.insert()
	return {"success": True, "name": doc.name, "action": "added"}


@frappe.whitelist(methods=["POST"])
def add_watches(bucket_ids: str | list | None = None, note: str | None = None):
	"""Bulk add_watch (the Inactive tab's 'Add selected to watchlist')."""
	_need("create")
	ids = (
		frappe.parse_json(bucket_ids)
		if isinstance(bucket_ids, str) and bucket_ids.strip().startswith("[")
		else (bucket_ids if isinstance(bucket_ids, list) else str(bucket_ids or "").split(","))
	)
	res = {"added": 0, "already": 0, "failed": []}
	for b in [i for i in dict.fromkeys(act.up(x).strip() for x in ids) if i][:500]:
		r = add_watch(b, note)
		if r.get("success"):
			res["already" if r.get("action") == "already watching" else "added"] += 1
		else:
			res["failed"].append(b)
	return {"success": True, **res}


@frappe.whitelist(methods=["POST"])
def close_watch(name: str | None = None, outcome: str | None = None, note: str | None = None):
	"""outcome: 'Found - resolved' (it turned up and the issue is settled) or 'Confirmed missing' (truly missing)."""
	_need("write")
	if not name or not frappe.db.exists(DT, name):
		return {"success": False, "error": _("Watch not found.")}
	o = str(outcome or "").lower()
	status = CONFIRMED if "missing" in o else RESOLVED if ("found" in o or "resolved" in o) else None
	if not status:
		return {"success": False, "error": _("Outcome must be 'Found - resolved' or 'Confirmed missing'.")}
	doc = frappe.get_doc(DT, name)
	if status == RESOLVED and doc.status == "Watching":
		# 'found' with no appearance on record is still allowed (someone found it by hand), but it is said so
		note = ((note or "") + " [closed as found with no appearance on record]").strip()
	doc.update(
		{
			"status": status,
			"closed_by": frappe.session.user,
			"closed_on": now_datetime(),
			"close_note": note or "",
		}
	)
	doc.save()
	return {"success": True, "name": name, "status": status}


@frappe.whitelist(methods=["POST"])
def reopen_watch(name: str | None = None):
	"""Back to Watching, from now."""
	_need("write")
	if not name or not frappe.db.exists(DT, name):
		return {"success": False, "error": _("Watch not found.")}
	doc = frappe.get_doc(DT, name)
	doc.update(
		{
			"status": "Watching",
			"watching_since": now_datetime(),
			"closed_by": None,
			"closed_on": None,
			"close_note": "",
			"last_seen_at": None,
			"last_seen_event": "",
			"last_seen_journey": 0,
			"last_seen_location": "",
			"last_seen_stage": "",
		}
	)
	doc.save()
	return {"success": True, "name": name, "status": "Watching"}


@frappe.whitelist(methods=["POST"])
def remove_watch(name: str | None = None):
	"""Stop watching: deletes the watch (use close_watch to keep the outcome on record)."""
	_need("write")
	if not name or not frappe.db.exists(DT, name):
		return {"success": False, "error": _("Watch not found.")}
	frappe.delete_doc(DT, name, ignore_permissions=True)
	return {"success": True, "name": name}


def _journey_facts(bucket):
	from upande_packhouse.api.v2 import rt_journey

	r = rt_journey.get_bucket_journeys(bucket, limit=1)
	if not r.get("found"):
		return 0, ""
	return cint((r.get("bucket") or {}).get("journeys")), (r.get("now") or {}).get("label") or ""


@frappe.whitelist(methods=["POST"])
def run_watchlist(search: str | None = None, farm: str | None = None, region: str | None = None):
	"""'Refresh appearances': first picks up any not-found mark no hook saw, then finds each open
	watch's latest appearance and SAVES it (last_seen_*, status Reappeared). For buckets whose latest
	appearance changed it also runs the journey builder (journey number + current stage), within a
	time budget; buckets over budget are filled by the next run."""
	_need("write")
	t0 = time.time()
	sync = sync_not_found()
	rows, kpi, ev, allrows = _compute(search, farm, region)
	now = now_datetime()
	changed, journeys, skipped = 0, 0, 0
	by = {w["name"]: w for w in allrows}
	for r in rows:
		w = by[r["name"]]
		if w["status"] not in OPEN:
			continue
		e = ev.get(act.up(w["bucket_id"]))
		vals = {"last_checked_at": now}
		if e:
			new = _s(e["ts"]) != _s(w["last_seen_at"])
			vals.update(
				{
					"status": "Reappeared",
					"last_seen_at": e["ts"],
					"last_seen_event": e["title"],
					"last_seen_location": " · ".join(x for x in (e["loc"], e["farm"]) if x),
				}
			)
			if new:
				changed += 1
				if time.time() - t0 < RUN_JOURNEY_BUDGET_S:
					try:
						vals["last_seen_journey"], vals["last_seen_stage"] = _journey_facts(w["bucket_id"])
						journeys += 1
					except Exception:
						frappe.log_error(title="Bucket watch journey failed", message=frappe.get_traceback())
				else:
					skipped += 1
					vals["last_seen_stage"] = ""
		frappe.db.set_value(DT, w["name"], vals, update_modified=bool(e))
	rows, kpi, _e, _a = _compute(search, farm, region)
	return {
		"success": True,
		"rows": rows,
		"kpi": kpi,
		"changed": changed,
		"journeys": journeys,
		"journeys_pending": skipped,
		"sync": sync,
		"ms": int((time.time() - t0) * 1000),
	}
