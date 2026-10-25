# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""The ONE definition of 'a bucket did something' used by the Bucket Watchlist and the
Inactive buckets tab (Bucket Journey v2).

ACTIVITY = the most recent of any bucket event from these sources (same ones the journey
builder api/v2/rt_journey.py reads):

  harvest / grading / receiving / quarantine / release / reject / discard / issue ledger legs
                              Stock Entry by custom_bucket_id (docstatus < 2)
  shelving, shelf move, off the shelf (issued, offline, transfer)
                              Shelving Log shelved_on / removed_on, Shelf Item date_added
  transfer steps              Pick List Item loaded_in_trolley_at, loaded_on_truck_at, left_farm_at,
                              arrived_at, shelved_at, issued_at; Bucket Transfer Event
  replacement                 Bucket Replacement (as the old or the new bucket), Stem Replacement Log
  count / packing reject      Cold Store Stock Take Bucket, Bucket Count Bucket, Packing Reject Log
  allocation (SYSTEM)         Pick List Item creation, Bucket Allocations creation

Two flavours:
  * physical  = evidence that someone handled / scanned the bucket. The WATCHLIST uses only
    these, so a missing bucket that the allocator keeps allocating, or the clean-up written
    by the not-found flow itself ('Shelf Cleared'), never makes it 'reappear'.
  * all       = physical + allocation. The INACTIVE tab uses these: an allocated bucket is
    not idle.

Reads only. Bucket ids are compared upper-case (catalog: Stock Entry ids are mixed case).
"""

from datetime import datetime, timedelta

import frappe

#: pick-row timestamp column -> (kind, label)
PLI_STEPS = (
	("loaded_in_trolley_at", "transfer", "Loaded on trolley"),
	("loaded_on_truck_at", "transfer", "Loaded on truck"),
	("left_farm_at", "transfer", "Truck left the farm"),
	("arrived_at", "transfer", "Arrived at packhouse"),
	("shelved_at", "shelve", "Shelved at the packhouse"),
	("issued_at", "issue", "Issued"),
)
#: Shelving Log removal reasons that are clean-up, not somebody handling the bucket
NOT_PHYSICAL_REMOVALS = ("Shelf Cleared",)


def up(v):
	return str(v or "").upper()


def _dt(v):
	if v is None or v == "":
		return None
	if isinstance(v, datetime):
		return v
	try:
		return frappe.utils.get_datetime(v)
	except Exception:
		return None


def _ev(b, ts, kind, title, loc="", farm="", physical=True, ref=""):
	ts = _dt(ts)
	return {"b": up(b), "ts": ts, "kind": kind, "title": title, "loc": loc or "", "farm": farm or "", "physical": physical, "ref": ref or ""} if ts else None


def _exists(doctype):
	return frappe.db.exists("DocType", doctype)


def events(ids, since=None, physical_only=True, include_se=True):
	"""Every event of `ids` newer than `since` (datetime, or None). Sorted by time. For a few
	hundred buckets; use last_activity() for the whole site."""
	ids = tuple({i for i in (ids or []) if i})
	if not ids:
		return []
	since = _dt(since) or datetime(2000, 1, 1)
	P = {"ids": ids, "since": since, "d": since.date()}
	out = []

	def add(e):
		if e and e["ts"] > since and (e["physical"] or not physical_only):
			out.append(e)

	for r in frappe.db.sql(
		"""SELECT se.name, se.custom_bucket_id AS b, se.stock_entry_type AS t, se.farm,
		       IF(se.stock_entry_type = 'Discard', se.creation, TIMESTAMP(se.posting_date, se.posting_time)) AS ts
		FROM `tabStock Entry` se
		WHERE se.custom_bucket_id IN %(ids)s AND se.docstatus < 2 AND se.posting_date >= %(d)s""", P, as_dict=True) if include_se else []:
		add(_ev(r.b, r.ts, "stock_entry", r.t, "", r.farm, ref="Stock Entry/" + r.name))
	for r in frappe.db.sql(
		"""SELECT name, bucket_id AS b, shelf, farm, shelved_on, removed_on, reason FROM `tabShelving Log`
		WHERE bucket_id IN %(ids)s AND (shelved_on > %(since)s OR removed_on > %(since)s)""", P, as_dict=True):
		add(_ev(r.b, r.shelved_on, "shelve", "Shelved on " + (r.shelf or "?"), r.shelf, r.farm, ref="Shelving Log/" + r.name))
		if r.removed_on and r.reason not in NOT_PHYSICAL_REMOVALS:
			add(_ev(r.b, r.removed_on, "off_shelf", "Taken off shelf %s (%s)" % (r.shelf or "?", r.reason or "removed"), r.shelf, r.farm, ref="Shelving Log/" + r.name))
	farm_col = "COALESCE(NULLIF(si.farm, ''), sh.farm)" if frappe.db.has_column("Shelf Item", "farm") else "sh.farm"
	for r in frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- fixed expression
		f"""SELECT si.name, si.bucket_id AS b, si.parent AS shelf, {farm_col} AS farm, si.date_added
		FROM `tabShelf Item` si LEFT JOIN `tabShelf` sh ON sh.name = si.parent
		WHERE si.bucket_id IN %(ids)s AND si.date_added > %(since)s""", P, as_dict=True):
		add(_ev(r.b, r.date_added, "shelve", "On shelf " + (r.shelf or "?"), r.shelf, r.farm, ref="Shelf Item/" + r.name))
	cols = ", ".join("pli.`%s`" % c for c, _k, _l in PLI_STEPS if frappe.db.has_column("Pick List Item", c))
	for r in frappe.db.sql(  # nosemgrep
		f"""SELECT pli.name, pli.bucket AS b, pli.parent AS opl, pli.farm, pli.shelf, pli.creation, {cols}
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket IN %(ids)s AND o.docstatus < 2""", P, as_dict=True):
		for c, kind, label in PLI_STEPS:
			if r.get(c):
				add(_ev(r.b, r[c], kind, label, r.shelf, r.farm, ref="Order Pick List/" + r.opl))
		add(_ev(r.b, r.creation, "allocate", "Allocated to " + r.opl, r.shelf, r.farm, physical=False, ref="Order Pick List/" + r.opl))
	for r in frappe.db.sql(
		"""SELECT a.creation, a.parent, b.bucket_id AS b, b.shelf_location, b.shelf_farm FROM `tabBucket Allocations` a
		JOIN `tabBucket Allocation Status` b ON b.name = a.parent
		WHERE b.bucket_id IN %(ids)s AND a.creation > %(since)s AND IFNULL(a.cancelled, 0) = 0""", P, as_dict=True):
		add(_ev(r.b, r.creation, "allocate", "Allocated", r.shelf_location, r.shelf_farm, physical=False, ref="Bucket Allocation Status/" + r.parent))
	if _exists("Bucket Transfer Event"):
		for r in frappe.db.sql(
			"""SELECT name, bucket AS b, stage, event_time, farm, shelf FROM `tabBucket Transfer Event`
			WHERE bucket IN %(ids)s AND event_time > %(since)s""", P, as_dict=True):
			add(_ev(r.b, r.event_time, "transfer", r.stage, r.shelf, r.farm, ref="Bucket Transfer Event/" + r.name))
	for r in frappe.db.sql(
		"""SELECT name, old_bucket, new_bucket, reported_at, resolved_at, status, farm, new_shelf, expected_shelf, order_name
		FROM `tabBucket Replacement`
		WHERE (old_bucket IN %(ids)s OR new_bucket IN %(ids)s) AND (reported_at > %(since)s OR resolved_at > %(since)s)""", P, as_dict=True):
		if up(r.new_bucket) in ids:  # the bucket was found and used as the replacement
			add(_ev(r.new_bucket, r.reported_at, "replace", "Used as the replacement for %s" % r.old_bucket, r.new_shelf, r.farm, ref="Bucket Replacement/" + r.name))
		if up(r.old_bucket) in ids:
			add(_ev(r.old_bucket, r.reported_at, "replaced_out", "Replaced by %s" % (r.new_bucket or "?"), r.expected_shelf, r.farm, physical=False, ref="Bucket Replacement/" + r.name))
			if r.status == "Found":
				add(_ev(r.old_bucket, r.resolved_at, "found", "Replacement closed as found", r.expected_shelf, r.farm, ref="Bucket Replacement/" + r.name))
	if _exists("Stem Replacement Log"):
		for r in frappe.db.sql(
			"""SELECT name, destination_bucket, donor_bucket, donor_shelf, replaced_at FROM `tabStem Replacement Log`
			WHERE (destination_bucket IN %(ids)s OR donor_bucket IN %(ids)s) AND replaced_at > %(since)s""", P, as_dict=True):
			for b, t in ((r.donor_bucket, "Gave stems to " + (r.destination_bucket or "?")), (r.destination_bucket, "Received stems from " + (r.donor_bucket or "?"))):
				if up(b) in ids:
					add(_ev(b, r.replaced_at, "replace", t, r.donor_shelf, "", ref="Stem Replacement Log/" + r.name))
	if _exists("Packing Reject Log"):
		for r in frappe.db.sql(
			"""SELECT name, bucket, donor_bucket, creation FROM `tabPacking Reject Log`
			WHERE (bucket IN %(ids)s OR donor_bucket IN %(ids)s) AND creation > %(since)s""", P, as_dict=True):
			for b in (r.bucket, r.donor_bucket):
				if up(b) in ids:
					add(_ev(b, r.creation, "reject", "Packing reject", "", "", ref="Packing Reject Log/" + r.name))
	if _exists("Cold Store Stock Take Bucket"):
		for r in frappe.db.sql(
			"""SELECT name, parent, bucket_id AS b, shelf, scanned_at FROM `tabCold Store Stock Take Bucket`
			WHERE bucket_id IN %(ids)s AND scanned_at > %(since)s""", P, as_dict=True):
			add(_ev(r.b, r.scanned_at, "count", "Counted in a stock take", r.shelf, "", ref="Cold Store Stock Take/" + r.parent))
	if _exists("Bucket Count Bucket"):
		for r in frappe.db.sql(
			"""SELECT name, parent, bucket_id AS b, location, scanned_at FROM `tabBucket Count Bucket`
			WHERE bucket_id IN %(ids)s AND scanned_at > %(since)s""", P, as_dict=True):
			add(_ev(r.b, r.scanned_at, "count", "Scanned in a bucket count", r.location, "", ref="Bucket Count/" + r.parent))
	out.sort(key=lambda e: e["ts"])
	return out


def last_activity(ids, cutoff, se_lookback_days=3):
	"""{BUCKET: (ts, label)} newest activity of ALL kinds (physical + allocation) for `ids`.

	The Stock Entry table is huge (3.5 M rows, ~95% harvest / grading scans), so it is read through the
	(type, posting_date) index from `cutoff - se_lookback_days`, WITHOUT harvest and grading scans: for a
	bucket already received or shelved those happened before its receiving / shelving record and are never
	its newest activity (a re-harvest of a bucket still on a shelf is the one case missed: the bucket then
	shows on the Reuse Anomaly list). This decides 'idle since `cutoff`' EXACTLY (any entry
	newer than the cutoff is inside the window); for a bucket whose newest entry is older than
	the window the other sources (shelving, pick rows ...) give the displayed date."""
	ids = tuple({i for i in (ids or []) if i})
	best = {}
	if not ids:
		return best

	def put(b, ts, label):
		ts = _dt(ts)
		if ts and (b not in best or ts > best[b][0]):
			best[b] = (ts, label)

	P = {"ids": ids, "d": (_dt(cutoff) - timedelta(days=se_lookback_days)).date()}
	for r in frappe.db.sql(
		"""SELECT custom_bucket_id AS b, stock_entry_type AS t,
		       MAX(IF(stock_entry_type = 'Discard', creation, TIMESTAMP(posting_date, posting_time))) AS ts
		FROM `tabStock Entry` WHERE posting_date >= %(d)s AND docstatus < 2 AND IFNULL(custom_bucket_id, '') != ''
		  AND stock_entry_type NOT IN ('Harvesting', 'Grading')
		GROUP BY custom_bucket_id, stock_entry_type""", P, as_dict=True):
		if up(r.b) in ids:
			put(up(r.b), r.ts, r.t)
	# the other sources are small: reuse events() for the exact same definitions
	for e in events(ids, since=None, physical_only=False, include_se=False):
		put(e["b"], e["ts"], e["title"])
	return best
