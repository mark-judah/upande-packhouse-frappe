# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Offline issuing — finishing an OPL whose allocated bucket never reached the
# issuing scan. Either it could not be found, or it turned out to be the wrong
# variety and another bucket went out in its place. Without this the OPL stays
# short (packed 100%, issued 89%) because nothing marks those stems issued.
#
# Two callers:
#   - Quality app, Shelf Operations → Issue Offline: pick the OPL, pick the
#     allocated bucket, say why, scan the bucket that actually went out.
#     `issue_offline` swaps the allocation onto the scanned bucket and issues it
#     exactly as the issuing scan would, so stock and the OPL's progress match.
#   - Packhouse app, Issuing: a bucket that cannot be found offers a Replace,
#     the same way remote transfers do (`replacement_options`,
#     `replace_for_issuing`); the replacement is then issued by scanning it.
#
# The swap is sales_allocation._replace_requested_bucket (remote transfers'
# replacement), allowed here for buckets already shelved at the packhouse.

import time

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from upande_packhouse.upande_packhouse.page.sales_allocation import sales_allocation as sa

# App reason -> Bucket Replacement reason (bucket_replacement.REASONS).
REASONS = {"not_found": "Missing", "wrong_variety": "Wrong variety"}

# How far back an OPL's delivery date may be and still be offered.
OPL_LOOKBACK_DAYS = 7


# ── Shared lookups ────────────────────────────────────────────────────────────


def _anchor_row(opl_name, bucket, sale_order_item=None):
	"""The Pick List Item that stands for `bucket` on `opl_name`: an unissued
	one, on `sale_order_item` when given."""
	rows = frappe.get_all(
		"Pick List Item",
		filters={"parent": opl_name, "parenttype": "Order Pick List", "bucket": bucket},
		fields=["name", "issued", "sales_order_item", "custom_sale_order_item"],
		order_by="idx asc",
	)
	if not rows:
		frappe.throw(_("Bucket {0} is not on {1}.").format(bucket, opl_name))
	unissued = [r for r in rows if not cint(r.issued)]
	if not unissued:
		frappe.throw(_("Bucket {0} is already issued on {1}.").format(bucket, opl_name))
	if sale_order_item:
		for r in unissued:
			if sale_order_item in (r.sales_order_item, r.custom_sale_order_item):
				return r.name
	return unissued[0].name


def _describe(c):
	return {
		"new_bucket": c.bucket_id,
		"shelf": c.shelf,
		"variety": c.variety,
		"stem_length": c.stem_length,
		"available_qty": c.available_qty,
		"harvest_date": str(c.harvest_date)[:10] if c.harvest_date else None,
	}


def _candidates(pick_list_item, limit, farm=None):
	"""Replacements for the bucket on `pick_list_item`. With `farm` (the issuing
	station) they come from there, and a remote-transfer bucket that left its farm
	but never arrived can be replaced too."""
	anchor, rows, farm = sa._requested_bucket_rows(
		pick_list_item, allow_shelved=True, allow_left=bool(farm), farm=farm or None
	)
	needed = sum(flt(r.stock_qty) for r in rows)
	return anchor, farm, needed, sa._replacement_candidates(anchor, farm, needed, limit=limit)


def _why_not(bucket, anchor, farm, needed):
	"""Why `bucket` cannot stand in for `anchor`, in the operator's terms."""
	items = frappe.get_all(
		"Shelf Item",
		filters={"bucket_id": bucket},
		fields=["parent", "variety", "stem_length", "stem_qty"],
	)
	if not items:
		return _("Bucket {0} is not on any shelf.").format(bucket)
	same = [i for i in items if i.variety == anchor.item_code]
	if not same:
		return _("Bucket {0} holds {1}, not {2}.").format(bucket, items[0].variety, anchor.item_code)
	if not any(frappe.db.get_value("Shelf", i.parent, "farm") == farm for i in same):
		return _("Bucket {0} is not shelved at {1}.").format(bucket, farm)
	min_cm = sa._length_cm(anchor.stem_length)
	if min_cm is not None and all((sa._length_cm(i.stem_length) or 0) < min_cm for i in same):
		return _("Bucket {0} is {1}, shorter than the {2} needed.").format(
			bucket, same[0].stem_length or "?", anchor.stem_length
		)
	if all(flt(i.stem_qty) < needed for i in same):
		return _("Bucket {0} holds {1} stems; {2} are needed.").format(
			bucket, int(max(flt(i.stem_qty) for i in same)), int(needed)
		)
	allocated = flt(
		frappe.db.sql(
			"SELECT COALESCE(SUM(allocated_quantity), 0) FROM `tabBucket Allocation Status` WHERE bucket_id = %s",
			bucket,
		)[0][0]
	)
	if allocated:
		return _("Bucket {0} is allocated to another order.").format(bucket)
	return _("Bucket {0} cannot replace {1} (too old, in transit, or already picked).").format(
		bucket, anchor.bucket
	)


def _swap(pick_list_item, new_bucket_id, reason, notes, keep_old_on_shelf=False, farm=None):
	"""sa.replace_requested_bucket with shelved buckets allowed: same lock wait
	and retries, since a swap can lose a lock race to another stock posting."""
	previous = frappe.db.sql("SELECT @@SESSION.innodb_lock_wait_timeout")[0][0]
	frappe.db.sql("SET SESSION innodb_lock_wait_timeout = %s", (int(sa.REPLACE_LOCK_WAIT_S),))
	try:
		res = {}
		for attempt in range(sa.REPLACE_ATTEMPTS):
			res = sa._replace_requested_bucket(
				pick_list_item,
				new_bucket_id,
				reason=reason,
				notes=notes,
				allow_shelved=True,
				keep_old_on_shelf=keep_old_on_shelf,
				allow_left=bool(farm),
				farm=farm or None,
			)
			if not res.pop("_lock_conflict", False):
				return res
			if attempt + 1 < sa.REPLACE_ATTEMPTS:
				time.sleep(0.5 * (attempt + 1))
		return {
			"success": False,
			"message": _("Another stock posting is holding the records this swap needs. Try again."),
		}
	finally:
		frappe.db.sql("SET SESSION innodb_lock_wait_timeout = %s", (int(previous),))


class _Request:
	"""Stands in for frappe.request so an endpoint that reads its JSON body can
	be called from here with the same logic the apps hit."""

	def __init__(self, payload):
		self.json = payload

	def get_json(self, *args, **kwargs):
		return self.json


_RESPONSE_KEYS = ("message", "http_status_code", "data")


def _call(endpoint, payload):
	"""Run a request-reading endpoint and return (status, message, data),
	leaving this request's own response untouched."""
	response = frappe.local.response
	saved = {k: response[k] for k in _RESPONSE_KEYS if k in response}
	request = getattr(frappe.local, "request", None)
	frappe.local.request = _Request(payload)
	for k in _RESPONSE_KEYS:
		response.pop(k, None)
	try:
		endpoint()
		return (
			cint(response.get("http_status_code") or 200),
			response.get("message"),
			response.get("data"),
		)
	finally:
		frappe.local.request = request
		for k in _RESPONSE_KEYS:
			response.pop(k, None)
		response.update(saved)


def _issue(bucket, opl_name):
	"""Issue every unissued line `bucket` holds on `opl_name`, through the
	issuing scan's own endpoint: same stock movement, same pick-row, shelf and
	allocation updates."""
	from upande_packhouse.mobile import api as mobile_api

	lines = frappe.get_all(
		"Pick List Item",
		filters={"parent": opl_name, "parenttype": "Order Pick List", "bucket": bucket, "issued": 0},
		fields=["custom_sale_order_item", "sales_order_item"],
	)
	so_items = sorted({r.custom_sale_order_item or r.sales_order_item for r in lines} - {None, ""})
	results = []
	for so_item in so_items:
		status, message, data = _call(
			mobile_api.issueBucketToSaleOrderItem,
			{"bucket": bucket, "sale_order_item": so_item, "opl_name": opl_name},
		)
		results.append({"sale_order_item": so_item, "ok": status == 200, "message": message, "data": data})
	return results


def _correct(bucket, variety, stem_length):
	"""Put a mislabelled bucket's real variety/length on its record, with the
	Quality app's own correction, then
	re-key its now-unallocated Bucket Allocation Status to match."""
	try:
		from upande_quality.mobile import api as quality_api
	except ImportError:
		return {"ok": False, "message": _("Bucket correction is not installed on this site.")}

	old = frappe.get_all(
		"Bucket Allocation Status",
		filters={"bucket_id": bucket},
		fields=["name", "item_code", "stem_length", "allocated_quantity"],
	)
	status, _message, data = _call(
		quality_api.correctDetails,
		{"kind": "bucket", "id": bucket, "variety": variety or "", "stem_length": stem_length or ""},
	)
	data = data or {}
	if status != 200 or data.get("status") != "success":
		return {"ok": False, "message": data.get("error") or data.get("message") or _("Correction failed.")}

	for bas in old:
		if flt(bas.allocated_quantity):
			continue  # still counted by another order; leave its key alone
		target = {"item_code": variety or bas.item_code, "stem_length": stem_length or bas.stem_length}
		clash = frappe.db.get_value(
			"Bucket Allocation Status", {"bucket_id": bucket, **target, "name": ["!=", bas.name]}, "name"
		)
		if not clash:
			frappe.db.set_value("Bucket Allocation Status", bas.name, target)
	frappe.db.commit()
	return {"ok": True, "message": data.get("message")}


def _off_trips(bucket, opl_name):
	"""A missing remote bucket replaced at issuing is not on any truck: take it off
	the trips (not yet received) that still list it for `opl_name`."""
	from upande_packhouse.api import transfer_control as tc

	where = [w for w in tc._bucket_trip_rows([bucket.upper()]).get(bucket.upper(), []) if w.opl == opl_name]
	if where:
		try:
			tc._drop_bucket_from_trips(bucket.upper(), where, "not found; replaced at offline issuing")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- after the committed swap
		except Exception:
			frappe.log_error(title="Offline issue: trip clean-up failed", message=frappe.get_traceback())


def _fail(message, **extra):
	return {"success": False, "message": str(message), **extra}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@frappe.whitelist()
def offline_issue_opls(
	days: int = OPL_LOOKBACK_DAYS, delivery_date: str | None = None, farm: str | None = None
):
	"""Submitted OPLs, delivering from `days` ago to tomorrow, that still have a
	bucket to issue. Newest first, with how far issuing has got.

	`delivery_date` (YYYY-MM-DD) narrows it to that one day, so the cold store
	works on tomorrow's orders without older ones mixed in.

	`farm` is the station's farm. A remote farm sees only the OPLs with buckets
	coming from it (its remote transfers), counted on those buckets alone; the
	sales farm (the transfer hub) — or no farm — sees every OPL."""
	from upande_packhouse.api.transfer_control import FARM_EXPR, transfer_hub

	hub = transfer_hub(required=False) or ""
	farm_cond = ""
	if farm and farm != hub:
		farm_cond = "AND " + FARM_EXPR + " = %(farm)s"
	if delivery_date:
		since = until = getdate(delivery_date)
	else:
		since = add_days(today(), -max(0, min(cint(days), 60)))
		until = add_days(today(), 1)
	rows = frappe.db.sql(
		"""
		SELECT opl.name AS opl_name, opl.order_name, opl.customer, opl.team, opl.farm,
		       so.delivery_date,
		       SUM(pli.stock_qty) AS total_stems,
		       SUM(CASE WHEN pli.issued = 1 THEN pli.stock_qty ELSE 0 END) AS issued_stems,
		       COUNT(DISTINCT CASE WHEN COALESCE(pli.issued, 0) = 0 THEN pli.bucket END) AS open_buckets,
		       GROUP_CONCAT(DISTINCT pli.item_code ORDER BY pli.item_code SEPARATOR '||') AS varieties
		FROM `tabOrder Pick List` opl
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
		WHERE opl.docstatus = 1
		  AND so.delivery_date BETWEEN %(since)s AND %(until)s
		  AND COALESCE(pli.bucket, '') != ''
		  {farm_cond}
		GROUP BY opl.name
		HAVING open_buckets > 0
		ORDER BY so.delivery_date DESC, opl.order_name ASC
		""".format(farm_cond=farm_cond),
		{"since": since, "until": until, "farm": farm},
		as_dict=True,
	)
	for r in rows:
		total = flt(r.total_stems)
		r["delivery_date"] = str(getdate(r.delivery_date)) if r.delivery_date else None
		r["issued_pct"] = round(flt(r.issued_stems) / total * 100) if total else 0
		# The varieties on the OPL, so the list shows them before it is opened.
		r["varieties"] = [v for v in (r.varieties or "").split("||") if v]
	return {"opls": rows}


@frappe.whitelist()
def offline_issue_buckets(opl_name: str, farm: str | None = None):
	"""The buckets `opl_name` is still waiting on, one entry per bucket. A remote
	`farm` (not the transfer hub) gets only the buckets coming from it."""
	from upande_packhouse.api.transfer_control import transfer_hub

	hub = transfer_hub(required=False) or ""
	only_farm = farm if farm and farm != hub else None
	rows = frappe.get_all(
		"Pick List Item",
		filters={"parent": opl_name, "parenttype": "Order Pick List", "issued": 0, "bucket": ["is", "set"]},
		fields=[
			"bucket",
			"item_code",
			"stem_length",
			"stock_qty",
			"shelf",
			"not_found",
			"in_transit",
			"loaded_in_trolley",
			"source_warehouse",
			"warehouse",
			"farm",
		],
		order_by="idx asc",
	)
	buckets = {}
	for r in rows:
		if only_farm:
			# Same source-farm rule as FARM_EXPR: source warehouse, then warehouse,
			# then the row's farm.
			wh = r.source_warehouse or r.warehouse or ""
			source = wh.split(" ", 1)[0] if wh else (r.farm or "")
			if source != only_farm:
				continue
		b = buckets.get(r.bucket)
		if not b:
			shelf = frappe.db.get_value("Shelf Item", {"bucket_id": r.bucket}, "parent")
			b = buckets[r.bucket] = {
				"bucket": r.bucket,
				"variety": r.item_code,
				"stem_length": r.stem_length,
				"stems": 0,
				"shelf": shelf or r.shelf,
				"on_shelf": bool(shelf),
				"not_found": bool(cint(r.not_found)),
				"in_transit": bool(cint(r.in_transit) or cint(r.loaded_in_trolley)),
			}
		b["stems"] += flt(r.stock_qty)
	return {"opl_name": opl_name, "buckets": list(buckets.values())}


def _offline_history(bucket):
	"""Earlier reports of `bucket` as not found or the wrong variety (Bucket
	Replacements, newest first): whether it already went through offline issuing."""
	rows = frappe.get_all(
		"Bucket Replacement",
		filters={"old_bucket": bucket, "reason": ["in", ["Missing", "Wrong variety"]], "docstatus": ["<", 2]},
		fields=["name", "reason", "status", "new_bucket", "order_pick_list", "order_name", "reported_by", "reported_at"],
		order_by="reported_at desc, creation desc",
		limit=5,
	)
	return [
		{
			"replacement": r.name,
			"reason": "not_found" if r.reason == "Missing" else "wrong_variety",
			"status": r.status,
			"new_bucket": r.new_bucket,
			"opl_name": r.order_pick_list,
			"order_name": r.order_name,
			"reported_by": frappe.utils.get_fullname(r.reported_by) if r.reported_by else "",
			"reported_at": str(r.reported_at)[:16] if r.reported_at else "",
		}
		for r in rows
	]


@frappe.whitelist()
def replacement_options(
	opl_name: str, bucket: str, sale_order_item: str | None = None, limit: int = 20, farm: str | None = None
):
	"""Buckets that can stand in for `bucket` on `opl_name` -- remote transfers'
	rules (same variety, same or longer length, enough stems, unallocated, at the
	farm the bucket is shelved at), best match first."""
	history = _offline_history(bucket)
	try:
		pli = _anchor_row(opl_name, bucket, sale_order_item)
		anchor, farm, needed, found = _candidates(pli, max(1, min(cint(limit) or 20, 100)), farm=farm)
	except frappe.ValidationError as e:
		return {"found": False, "message": str(e), "candidates": [], "history": history}
	if not found:
		return {
			"found": False,
			"message": sa._no_replacement_message(anchor, farm, needed),
			"candidates": [],
			"history": history,
		}
	return {
		"found": True,
		"history": history,
		"old_bucket": anchor.bucket,
		"variety": anchor.item_code,
		"stem_length": anchor.stem_length,
		"needed_qty": needed,
		"farm": farm,
		"candidates": [_describe(c) for c in found],
	}


@frappe.whitelist(methods=["POST"])
def replace_for_issuing(
	opl_name: str,
	bucket: str,
	new_bucket_id: str,
	reason: str | None = None,
	notes: str | None = None,
	sale_order_item: str | None = None,
):
	"""Swap `bucket` on `opl_name` for `new_bucket_id` (one of
	`replacement_options`). The replacement is then issued by scanning it."""
	try:
		pli = _anchor_row(opl_name, bucket, sale_order_item)
	except frappe.ValidationError as e:
		return _fail(e)
	return _swap(pli, new_bucket_id, reason if reason in REASONS.values() else "Missing", notes)


@frappe.whitelist(methods=["POST"])
def issue_offline(
	opl_name: str,
	allocated_bucket: str,
	scanned_bucket: str,
	reason: str,
	notes: str | None = None,
	variety: str | None = None,
	stem_length: str | None = None,
	farm: str | None = None,
):
	"""Record that `scanned_bucket` went out for `opl_name` in place of
	`allocated_bucket`, which was not found or was the wrong variety, and issue it.
	reason "found": the allocated bucket is there after all — scanning it issues it
	to its line, with no substitute and nothing reported.

	1. Swap the allocation onto the scanned bucket (skipped when the allocated
	   bucket turned up after all and was scanned itself).
	2. Issue it exactly as the issuing scan does -- the OPL's issued % moves.
	3. Wrong variety: correct the allocated bucket's record to `variety` /
	   `stem_length`; it stays on its shelf. Not found: it leaves its shelf and
	   its Bucket Replacement stays open until it is shelved again.
	"""
	allocated_bucket = (allocated_bucket or "").strip()
	scanned_bucket = (scanned_bucket or "").strip()
	if reason == "found":
		if scanned_bucket.upper() != allocated_bucket.upper():
			return _fail(
				_(
					"That is {0}, not {1}. Scan {1} itself, or report it not found or the wrong variety / stem length."
				).format(scanned_bucket, allocated_bucket),
				reason="not_the_allocated_bucket",
			)
		scanned_bucket = allocated_bucket
	elif reason not in REASONS:
		return _fail(_("Reason must be 'found', 'not_found' or 'wrong_variety'."))
	if not allocated_bucket or not scanned_bucket:
		return _fail(_("Pick the allocated bucket and scan the bucket that went out."))
	wrong_variety = reason == "wrong_variety"
	if wrong_variety and not (variety or stem_length):
		return _fail(_("Enter the allocated bucket's real variety or stem length."))

	try:
		pli = _anchor_row(opl_name, allocated_bucket)
	except frappe.ValidationError as e:
		return _fail(e)

	swapped = None
	target = allocated_bucket
	if scanned_bucket != allocated_bucket:
		try:
			anchor, from_farm, needed, found = _candidates(pli, 500, farm=farm)
		except frappe.ValidationError as e:
			return _fail(e)
		if scanned_bucket not in {c.bucket_id for c in found}:
			return _fail(
				_why_not(scanned_bucket, anchor, from_farm, needed),
				reason="not_a_replacement",
				candidates=[_describe(c) for c in found[:5]],
			)
		swapped = _swap(
			pli,
			scanned_bucket,
			REASONS[reason],
			notes,
			keep_old_on_shelf=wrong_variety,
			farm=farm,
		)
		if not swapped.get("success"):
			return _fail(swapped.get("message") or _("The swap failed."))
		target = scanned_bucket
		_off_trips(allocated_bucket, opl_name)
	elif wrong_variety:
		return _fail(_("That is the allocated bucket. Scan the bucket that actually went out instead."))

	issued = _issue(target, opl_name)
	issue_ok = bool(issued) and all(r["ok"] for r in issued)
	if issue_ok:
		# Marks the line so Bucket Logistics can show it went out through Issue Offline.
		frappe.db.sql(
			"""UPDATE `tabPick List Item` SET issued_offline = 1
			WHERE parent = %s AND parenttype = 'Order Pick List' AND bucket = %s AND issued = 1""",
			(opl_name, target),
		)

	correction = None
	if wrong_variety:
		correction = _correct(allocated_bucket, variety, stem_length)

	parts = []
	if swapped:
		parts.append(_("{0} replaced {1}.").format(target, allocated_bucket))
	parts.append(
		_("{0} issued to {1}.").format(target, opl_name)
		if issue_ok
		else _("Issuing {0} failed: {1}").format(
			target, "; ".join(str(r["message"]) for r in issued if not r["ok"]) or _("nothing to issue")
		)
	)
	if correction:
		parts.append(
			_("{0} corrected.").format(allocated_bucket)
			if correction["ok"]
			else _("Correcting {0} failed: {1}").format(allocated_bucket, correction["message"])
		)

	return {
		"success": issue_ok,
		"message": " ".join(str(p) for p in parts),
		"opl_name": opl_name,
		"allocated_bucket": allocated_bucket,
		"issued_bucket": target,
		"replacement": (swapped or {}).get("replacement"),
		"issued": issued,
		"correction": correction,
	}
