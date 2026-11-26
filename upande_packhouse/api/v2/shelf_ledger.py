# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Stem Movement v2 (www/stem-movement-v2.html): the shelf ledger.

For one farm and one day: the stems on the shelves at 00:00, every way stems came on, every way they left, and the
stems on the shelves at the end, as a stock ledger with a running balance. A report: it reads the Shelving Log,
the issued pick rows and a few stock entries, and writes nothing.

A LOT is the stems of one bucket, one variety and length, on one shelf, from `shelved_on` until they leave.

    in      a Shelving Log row shelved that day. One that follows a clear of the same bucket within a day is a
            reshelving; one that follows a shelf-to-shelf move within seconds, an arrival; one that follows a trolley
            or truck transfer, an arrival from another farm (or a return, on the same farm).
    issues  an issue takes stems off the shelf a few at a time (the Shelf Item's quantity falls) but the Shelving Log
            closes the lot only after the last draw, at the lot's full original size. So every issue is read from its
            issued pick row (its own time, its own stems, its order) and placed on the lot it came off, first shelved
            first. A lot's own exit then carries only what was left on it.
    out     the reason the Shelving Log recorded, for the stems left. Many lots are never closed in the log. For those
            the exit is dated by the first thing that shows the lot is gone: a Discard or Issuing entry on the bucket,
            the same bucket and variety shelved again, the bucket harvested again, or the discard age passing. Those
            rows are marked inferred, with the reason in `why`.

Opening plus in minus out equals closing for any day, and a day's opening is the previous day's closing. Today the
closing is also set against the live shelf (core/stock.bucket_rows). Times are the stored server times."""

from collections import defaultdict
from datetime import datetime, time, timedelta

import frappe
from frappe.utils import getdate, now_datetime, today

from upande_packhouse.api.v2.core import stock

OUT_LABEL = {
	"Issued to Sales Order": "Issued to an order",
	"Offline Issuing": "Removed offline (farm app)",
	"Issued from Coldstore": "Issued from the cold store",
	"Transferred (Trolley/Truck)": "Taken for transfer (trolley or truck)",
	"Transferred (Shelf-to-Shelf)": "Moved to another shelf",
	"Replaced": "Used as a replacement bucket",
	"Shelf Cleared": "Shelf cleared",
	"Discarded": "Discarded",
	"Aged Out": "Aged out",
	"Stale Allocation": "Clean-up (stale allocation)",
}
ISSUED = "Issued to an order"
ISSUED_OFFLINE = "Issued to an order (offline)"
ISSUED_NO_PICK = "Issued to an order (no pick row)"
# stock entries that show a bucket's stems left the shelf, and what to call it
EXIT_ENTRIES = {
	"Discard": "Discarded",
	"Issuing From Cold Store": "Issued from the cold store",
	"Issue From The Cold Store": "Issued from the cold store",
	"Offline Issuing": "Removed offline",
}
CHUNK = 500
S2S_SECONDS = 30
RESHELVE_HOURS = 24
PULL_TOLERANCE = timedelta(seconds=3)  # a lot is closed a moment after its last draw


def _guard():
	"""The same audience as Stock Visibility."""
	frappe.has_permission("Sales Order", "read", throw=True)


def _rows(sql, args=None):
	return frappe.db.sql(sql, args or (), as_dict=True)


def _chunks(items, n=CHUNK):
	items = list(items)
	for i in range(0, len(items), n):
		yield items[i : i + n]


def _stamp(t):
	return t.strftime("%Y-%m-%d %H:%M")


@frappe.whitelist(methods=["GET"])
def get_ledger(farm: str | None = None, day: str | None = None):
	"""One farm's shelf ledger for one day (default: the first sales-shelf farm, today)."""
	_guard()
	try:
		cfg = stock._config()
		farm_cfg = cfg["farm_config"]
		farms = sorted(farm_cfg, key=lambda f: (not farm_cfg[f].get("sales_shelf"), f))
		farm = farm if farm in farm_cfg else (farms[0] if farms else None)
		now = now_datetime()
		d = min(getdate(day or today()), getdate(today()))
		out = {"success": True, "farms": farms, "farm": farm, "day": str(d), "today": str(getdate(today()))}
		if not farm:
			return dict(out, open=[], moves=[], left_out={"stems": 0, "issues": 0}, phantom={"lots": 0, "stems": 0}, live=None)
		out.update(build(farm, d, now, float(cfg.get("discard_age") or 6)))
		out["live"] = None
		if d == getdate(today()):
			live = sum(float(r.stems or 0) for r in stock.bucket_rows(farms=[farm]))
			out["live"] = {"stems": live}
		return out
	except frappe.PermissionError:
		raise
	except Exception:
		frappe.log_error("v2 shelf_ledger.get_ledger")
		return {"success": False, "error": "Could not load the shelf ledger; the error was logged."}


def build(farm, day, now, discard_days):
	"""{open, moves, left_out, phantom, as_of} for `farm` on `day`, as of `now`. Pure reads."""
	start = datetime.combine(day, time.min)
	end = min(start + timedelta(days=1), now)
	age = timedelta(days=discard_days)
	lots = _read_lots(farm, start, end, age)
	# the same buckets' earlier lots take issues first-shelved-first too: needed to place issues, never shown
	lots += _read_context(farm, lots, end)
	for l in lots:
		l["pulls"] = []
		l["exit_at"], l["exit_label"], l["inferred"], l["why"] = (
			l["removed_on"],
			OUT_LABEL.get(l["reason"], l["reason"] or "Left the shelf"),
			0,
			"",
		)
	_date_open_lots(lots, end, age)
	left_out, order_of = _place_pulls(lots, end)
	for l in lots:
		l["rem"] = l["q"] - sum(p[1] for p in l["pulls"])
		if l["removed_on"] and l["reason"] == "Issued to Sales Order" and l["rem"] > 1e-9:
			l["exit_label"] = ISSUED_NO_PICK
			l["why"] = "closed in the log as issued, but no issued pick row accounts for these stems"
		if l["inferred"] and l["rem"] <= 1e-9:
			l["exit_at"] = None  # issued down to nothing: it ended at its last issue
	types = _in_types([l for l in lots if start <= l["shelved_on"] < end], farm, start, end)
	opening, moves = [], []
	for l in lots:
		if l.get("context"):
			continue
		before = sum(p[1] for p in l["pulls"] if p[0] < start)
		if l["shelved_on"] < start and (l["exit_at"] is None or l["exit_at"] >= start) and l["q"] - before > 1e-9:
			opening.append({"s": l["shelf"], "b": l["bucket_id"], "v": l["variety"], "l": l["len"], "q": l["q"] - before})
		if start <= l["shelved_on"] < end:
			moves.append(_move(l, "i", l["shelved_on"], types.get(l["name"], "Shelved from receiving"), l["q"]))
		for t, take, pr in l["pulls"]:
			if start <= t < end:
				so = order_of.get(pr["opl"]) or ""
				moves.append(
					_move(l, "x", t, ISSUED_OFFLINE if pr["off"] else ISSUED, take, ref=(so + " · " if so else "") + pr["opl"], so=so)
				)
		if l["exit_at"] and start <= l["exit_at"] < end and l["rem"] > 1e-9:
			moves.append(_move(l, "x", l["exit_at"], l["exit_label"], l["rem"], inferred=l["inferred"], why=l["why"]))
	moves.sort(key=lambda m: (m["t"], m["k"] != "i"))
	lo = left_out.get(day, [0.0, 0])
	return {
		"open": opening,
		"moves": moves,
		"left_out": {"stems": lo[0], "issues": lo[1]},
		"phantom": _phantom(farm, start, end),
		"as_of": str(end),
		"discard_days": discard_days,
	}


def _move(l, kind, t, label, qty, ref="", so="", inferred=0, why=""):
	return {
		"t": t.strftime("%H:%M:%S"),
		"k": kind,
		"s": l["shelf"],
		"b": l["bucket_id"],
		"v": l["variety"],
		"l": l["len"],
		"m": label,
		"q": qty,
		"inf": inferred,
		"w": why,
		"ref": ref,
		"so": so,
	}


def _read_lots(farm, start, end, age):
	"""Lots on the farm's shelves at any time in the day. A lot the log never closed and shelved before `age` ago was
	over before the day (the discard age), so those are not read at all."""
	lots = _rows(
		"""SELECT name, bucket_id, shelf, variety, stem_length, stem_qty, shelved_on, removed_on, reason
		   FROM `tabShelving Log`
		   WHERE farm = %(f)s AND shelved_on IS NOT NULL AND shelved_on < %(end)s
		     AND ((removed_on IS NOT NULL AND removed_on >= %(start)s) OR (removed_on IS NULL AND shelved_on >= %(floor)s))""",
		{"f": farm, "end": end, "start": start, "floor": start - age},
	)
	for l in lots:
		l["q"] = float(l["stem_qty"] or 0)
		l["shelf"] = l["shelf"] or "(no shelf)"
		l["len"] = l["stem_length"] or ""
	return lots


def _read_context(farm, lots, end):
	"""Lots on this farm of the day's buckets that are not in `lots`: shelved before the day's and closed before it.
	An issue made while one of them was still on the shelf comes off it first, so without them it would be placed on
	a later lot of the bucket, and that lot would look emptier than it was."""
	have = {l["name"] for l in lots}
	buckets = sorted({l["bucket_id"] for l in lots if l["bucket_id"]})
	if not buckets:
		return []
	since = min(l["shelved_on"] for l in lots)
	extra = []
	for c in _chunks(buckets):
		for l in _rows(
			"""SELECT name, bucket_id, shelf, variety, stem_length, stem_qty, shelved_on, removed_on, reason
			   FROM `tabShelving Log`
			   WHERE farm = %s AND bucket_id IN %s AND shelved_on IS NOT NULL AND shelved_on < %s
			     AND (removed_on IS NULL OR removed_on >= %s)""",
			(farm, c, end, since),
		):
			if l["name"] in have:
				continue
			l["q"] = float(l["stem_qty"] or 0)
			l["shelf"] = l["shelf"] or "(no shelf)"
			l["len"] = l["stem_length"] or ""
			l["context"] = True
			extra.append(l)
	return extra


def _date_open_lots(lots, end, age):
	"""When a lot the log never closed was really over, and why: the first of the evidence below that is before `end`."""
	never = [l for l in lots if not l["removed_on"]]
	buckets = sorted({l["bucket_id"] for l in never if l["bucket_id"]})
	if not buckets:
		return
	since = min(l["shelved_on"] for l in never)
	again, harvests, entries = defaultdict(list), defaultdict(list), defaultdict(list)
	for c in _chunks(buckets):
		for r in _rows(
			"SELECT bucket_id, variety, shelved_on FROM `tabShelving Log` WHERE bucket_id IN %s AND shelved_on IS NOT NULL AND shelved_on >= %s",
			(c, since),
		):
			again[(r["bucket_id"], r["variety"])].append(r["shelved_on"])
		for r in _rows(
			"""SELECT custom_bucket_id AS b, stock_entry_type AS t, creation FROM `tabStock Entry`
			   WHERE custom_bucket_id IN %s AND docstatus = 1 AND creation >= %s AND creation < %s
			     AND stock_entry_type IN ('Harvesting', 'Discard', 'Issuing From Cold Store', 'Issue From The Cold Store', 'Offline Issuing')""",
			(c, since, end),
		):
			(harvests if r["t"] == "Harvesting" else entries)[r["b"]].append((r["creation"], r["t"]))
	for l in never:
		t0 = l["shelved_on"]
		cand = [(t, EXIT_ENTRIES[k], f"{k} entry on the bucket, {_stamp(t)}") for t, k in entries[l["bucket_id"]] if t > t0]
		cand += [
			(t, "Shelved again", f"same bucket and variety shelved again at {_stamp(t)}, with no exit logged")
			for t in again[(l["bucket_id"], l["variety"])]
			if t > t0
		]
		cand += [
			(t, "Emptied: bucket harvested again", f"Harvesting entry on the bucket at {_stamp(t)}")
			for t, _k in harvests[l["bucket_id"]]
			if t > t0
		]
		cand.append((t0 + age, "Aged out", f"still open {int(age.total_seconds() // 86400)} days after shelving, past the discard age"))
		t, label, why = min(cand, key=lambda x: x[0])
		if t < end:
			l["exit_at"], l["exit_label"], l["inferred"], l["why"] = t, label, 1, why
		else:
			l["exit_at"] = None  # still on the shelf


def _place_pulls(lots, end):
	"""Put each issue (an issued pick row) on the lot it came off: of its bucket and variety, shelved before the issue
	and not yet over, first shelved first, up to what the lot has left. Returns ({day: [stems, issues]} for issues no
	lot could take, {pick list: sales order})."""
	buckets = sorted({l["bucket_id"] for l in lots if l["bucket_id"]})
	if not buckets:
		return {}, {}
	since = min(l["shelved_on"] for l in lots)
	pulls = []
	for c in _chunks(buckets):
		pulls += _rows(
			"""SELECT p.bucket AS b, p.item_code AS v, p.stock_qty AS q, p.issued_at AS t, p.parent AS opl,
			          IFNULL(p.issued_offline, 0) AS off
			   FROM `tabPick List Item` p
			   WHERE p.bucket IN %s AND IFNULL(p.issued, 0) = 1 AND p.issued_at IS NOT NULL AND p.issued_at >= %s AND p.issued_at < %s""",
			(c, since, end),
		)
	order_of = {}
	opls = sorted({p["opl"] for p in pulls if p["opl"]})
	for c in _chunks(opls):
		order_of.update({r["name"]: r["sales_order"] for r in _rows("SELECT name, sales_order FROM `tabOrder Pick List` WHERE name IN %s", (c,))})
	by_bucket = defaultdict(list)
	for l in lots:
		l["cap"] = l["q"]
		by_bucket[(l["bucket_id"], l["variety"])].append(l)
	for group in by_bucket.values():
		# first shelved first; of two shelved at the same instant (a bucket shelved as several rows), the one the log
		# closed first, so an issue lands on the lot the log says it emptied
		group.sort(key=lambda x: (x["shelved_on"], x["removed_on"] is None, x["removed_on"] or datetime.max, x["name"]))
	left_out = defaultdict(lambda: [0.0, 0])
	for p in sorted(pulls, key=lambda x: x["t"]):
		left = float(p["q"] or 0)
		for l in by_bucket.get((p["b"], p["v"]), []):
			if left <= 1e-9:
				break
			over = l["exit_at"] is not None and l["exit_at"] + PULL_TOLERANCE < p["t"]
			if l["shelved_on"] > p["t"] or over or l["cap"] <= 1e-9:
				continue
			take = min(left, l["cap"])
			l["cap"] -= take
			l["pulls"].append((p["t"], take, p))
			left -= take
		if left > 1e-9:
			x = left_out[getdate(p["t"])]
			x[0] += left
			x[1] += 1
	return left_out, order_of


def _in_types(ins, farm, start, end):
	"""What each lot shelved in the day is: from receiving, a reshelving, an arrival (by what its bucket last left on)."""
	if not ins:
		return {}
	buckets = sorted({l["bucket_id"] for l in ins if l["bucket_id"]})
	exits = defaultdict(list)
	for c in _chunks(buckets):
		for r in _rows(
			"""SELECT bucket_id, variety, removed_on, reason, farm FROM `tabShelving Log`
			   WHERE bucket_id IN %s AND removed_on IS NOT NULL AND removed_on >= %s AND removed_on <= %s""",
			(c, start - timedelta(days=1), end),
		):
			exits[r["bucket_id"]].append(r)
	out = {}
	for l in ins:
		prior = [
			e
			for e in exits[l["bucket_id"]]
			if e["variety"] == l["variety"]
			and e["removed_on"] <= l["shelved_on"]
			and l["shelved_on"] - e["removed_on"] <= timedelta(hours=RESHELVE_HOURS)
		]
		label = "Shelved from receiving"
		if prior:
			e = max(prior, key=lambda x: x["removed_on"])
			gap = l["shelved_on"] - e["removed_on"]
			if e["reason"] == "Transferred (Shelf-to-Shelf)" and gap <= timedelta(seconds=S2S_SECONDS):
				label = "Arrived from another shelf"
			elif e["reason"] == "Shelf Cleared":
				label = "Reshelved after a clear"
			elif e["reason"] == "Transferred (Trolley/Truck)":
				label = "Returned from trolley or truck" if e["farm"] == farm else "Arrived from another farm (trolley or truck)"
		out[l["name"]] = label
	return out


def _phantom(farm, start, end):
	"""Exits the log recorded for stems it never recorded shelving: they cannot be on the ledger."""
	r = _rows(
		"SELECT COUNT(*) AS n, COALESCE(SUM(stem_qty), 0) AS q FROM `tabShelving Log` WHERE farm = %s AND shelved_on IS NULL AND removed_on >= %s AND removed_on < %s",
		(farm, start, end),
	)[0]
	return {"lots": int(r["n"] or 0), "stems": float(r["q"] or 0)}

