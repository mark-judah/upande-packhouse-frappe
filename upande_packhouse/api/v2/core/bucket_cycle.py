# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""When was a bucket last harvested / received / discarded?

Bucket ids are reused: the same id is harvested again and again. A bucket that
was harvested again has been emptied and refilled, so whatever the system still
holds against its OLD contents (a quarantine balance nobody closed, an
allocation nobody released) is stock that no longer exists. Stock Visibility
uses this to tell the books from the floor ("Filter out reharvested"), and to
replace its slowest per-row "any later cycle?" subqueries.

Reading it straight from Stock Entry means touching every bucket's whole
history (6-20 s on the live table), so the answer is an index:

    {BUCKET (upper, trimmed): {stock entry type: latest creation}}

built once from the (type, docstatus, posting_date) index, then topped up from
the `creation` index (new entries only, ~60 ms) at most every REFRESH_SECONDS,
and rebuilt in the background every REBUILD_SECONDS so a cancelled entry ages
out. Until a rebuild lands, a cancelled harvest can still mark its bucket as
harvested again; that lag is at most REBUILD_SECONDS.
"""

import frappe
from frappe.utils import add_to_date, add_days, get_datetime, now_datetime, today

# a bucket's cycle: cut, received, and every way it can leave the cold room afterwards (cold_room.ONWARD_TYPES)
TYPES = (
	"Harvesting",
	"Receiving",
	"Late Receipt",
	"Discard",
	"Issuing From Cold Store",
	"Issue From The Cold Store",
	"Offline Issuing",
	"Move To Graded Sold",
	"Packing",
	"Dispatch",
)
LOOKBACK_DAYS = 120  # an anchor older than this is compared with the bucket's latest entry inside the window
OVERLAP_MINUTES = 10  # a top-up re-reads this much, for entries committed after a later one was stamped
REFRESH_SECONDS = 30
REBUILD_SECONDS = 6 * 3600
KEY = "bucket_cycle_index_v2"  # v2: more entry types and a posting date per type
POSTED = "posted:"  # "posted:<type>": the latest posting date of that type, for rules written in posting dates
HARVEST_DATE = "harvest_date"  # alongside the entry types in a bucket's record: posting date of its latest harvest


def _read(where, params):
	"""[(bucket, type, latest creation, latest posting date)] for submitted entries of TYPES matching `where`."""
	return frappe.db.sql(
		f"""SELECT UPPER(TRIM(custom_bucket_id)), stock_entry_type, MAX(creation), MAX(posting_date)
		    FROM `tabStock Entry` {where[0]}
		    WHERE stock_entry_type IN %(types)s AND docstatus = 1 AND IFNULL(custom_bucket_id, '') != ''{where[1]}
		    GROUP BY UPPER(TRIM(custom_bucket_id)), stock_entry_type""",
		dict(params, types=TYPES),
	)  # nosemgrep: fixed SQL fragments, bound values


def _merge(data, rows):
	for bucket, typ, at, *posted in rows:
		cur = data.setdefault(bucket, {})
		if typ not in cur or at > cur[typ]:
			cur[typ] = at
			if typ == "Harvesting" and posted:
				cur[HARVEST_DATE] = posted[0]  # the date the bucket was last cut
		if posted:
			k = POSTED + typ
			if k not in cur or posted[0] > cur[k]:
				cur[k] = posted[0]


def rebuild():
	"""Read the whole window and store it. Also the background job."""
	started = now_datetime()
	rows = _read(
		("FORCE INDEX (stock_entry_type_docstatus_posting_date_index)", " AND posting_date >= %(since)s"),
		{"since": add_days(today(), -LOOKBACK_DAYS)},
	)
	data = {}
	_merge(data, rows)
	idx = {"data": data, "built": started, "mark": started, "refreshed": started}
	frappe.cache.set_value(KEY, idx)
	return idx


def _top_up(idx, now):
	rows = _read(
		("FORCE INDEX (creation)", " AND creation >= %(since)s"),
		{"since": add_to_date(idx["mark"], minutes=-OVERLAP_MINUTES)},
	)
	_merge(idx["data"], rows)
	idx["mark"] = now
	idx["refreshed"] = now
	frappe.cache.set_value(KEY, idx)


def index():
	"""The current index. Built on first use; kept fresh within REFRESH_SECONDS."""
	idx = frappe.cache.get_value(KEY)
	now = now_datetime()
	if not idx:
		return rebuild()["data"]
	if (now - idx["refreshed"]).total_seconds() >= REFRESH_SECONDS:
		_top_up(idx, now)
	if (now - idx["built"]).total_seconds() >= REBUILD_SECONDS:
		# serve what we have; a worker replaces it (one job at a time)
		frappe.enqueue(
			"upande_packhouse.api.v2.core.bucket_cycle.rebuild",
			queue="long",
			job_id="bucket_cycle_rebuild",
			deduplicate=True,
		)
	return idx["data"]


def key(bucket):
	return str(bucket or "").strip().upper()


def latest(idx, bucket, types=TYPES):
	"""Latest creation of any of `types` for the bucket, or None."""
	ev = idx.get(key(bucket))
	if not ev:
		return None
	found = [ev[t] for t in types if t in ev]
	return max(found) if found else None


def harvested_after(idx, bucket, since):
	"""True when the bucket has a submitted Harvesting entry created after `since`: it was emptied and refilled."""
	at = latest(idx, bucket, ("Harvesting",))
	return bool(at and since and at > get_datetime(since))


def harvest_date(idx, bucket):
	"""The posting date of the bucket's latest harvest, or None."""
	return (idx.get(key(bucket)) or {}).get(HARVEST_DATE)


def posted_since(idx, bucket, types, day):
	"""True when any of `types` was posted for the bucket on or after the date `day`."""
	ev = idx.get(key(bucket))
	if not ev or not day:
		return False
	return any(ev.get(POSTED + t) is not None and ev[POSTED + t] >= day for t in types)


def later_entry(idx, bucket, types, since):
	"""True when any of `types` was created for the bucket after `since`."""
	at = latest(idx, bucket, types)
	return bool(at and since and at > get_datetime(since))
