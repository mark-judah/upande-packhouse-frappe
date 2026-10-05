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

# Reasons the packhouse app's Issuing replace may record (Bucket Replacement.reason).
ISSUING_REASONS = ("Missing", "Damaged", "Wrong variety", "Issued offline")

# How far back an OPL's delivery date may be and still be offered.
OPL_LOOKBACK_DAYS = 7


# ── Shared lookups ────────────────────────────────────────────────────────────


def _issued_where(bucket):
	"""Where `bucket` was already issued: [{opl, order_name, team, offline}], newest
	first -- offline = it went out through Issue Offline."""
	return frappe.db.sql(
		"""SELECT pli.parent AS opl, opl.order_name, IFNULL(opl.team, '') AS team,
		       MAX(IFNULL(pli.issued_offline, 0)) AS offline
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND pli.issued = 1 AND pli.bucket = %s
		GROUP BY pli.parent, opl.order_name, opl.team
		ORDER BY MAX(pli.modified) DESC""",
		(bucket,),
		as_dict=True,
	)


def _already_issued_message(bucket, where=None):
	"""'{bucket} was already issued offline to Team A (ORDER-1)' -- or None when it
	was never issued."""
	where = where if where is not None else _issued_where(bucket)
	if not where:
		return None
	to = ", ".join("{0} ({1})".format(r.team or _("no team"), r.order_name or r.opl) for r in where)
	if any(cint(r.offline) for r in where):
		return _("{0} was already issued offline to {1}.").format(bucket, to)
	return _("{0} was already issued to {1}.").format(bucket, to)


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
		frappe.throw(
			_already_issued_message(bucket) or _("Bucket {0} is already issued on {1}.").format(bucket, opl_name)
		)
	if sale_order_item:
		for r in unissued:
			if sale_order_item in (r.sales_order_item, r.custom_sale_order_item):
				return r.name
	return unissued[0].name


def _describe(c):
	return {
		"new_bucket": c.bucket_id,
		# Set only for a bucket at a remote farm: it comes on a truck.
		"farm": c.get("farm"),
		"remote": bool(c.get("farm")),
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


def _remote_farms(opl_name, searched):
	"""The remote farms that truck to `opl_name`'s sales farm (its Sales Order's farm,
	else its own, else the transfer hub), less `searched` -- the farm already looked at."""
	sales_farm = sa._order_sales_farm(opl_name) or searched
	return [f for f in sa._remote_farms_of(sales_farm) if f != searched]


def _remote_candidates(anchor, opl_name, farm, needed, limit):
	"""Nothing matching at `farm`: the same rules at each remote farm that trucks to
	the OPL's sales farm, each candidate tagged with its farm, FIFO order kept per
	farm and farms in configured order."""
	out = []
	for remote in _remote_farms(opl_name, farm):
		for c in sa._replacement_candidates(anchor, remote, needed, limit=limit):
			c["farm"] = remote
			out.append(c)
			if len(out) >= limit:
				return out
	return out


def _delivers_today(opl_name):
	"""A remote bucket has to be trucked in: for a same-day order it may not make it."""
	so = frappe.db.get_value("Order Pick List", opl_name, "sales_order")
	dd = frappe.db.get_value("Sales Order", so, "delivery_date") if so else None
	return bool(dd) and getdate(dd) <= getdate(today())


def _why_not(bucket, anchor, farm, needed):
	"""Why `bucket` cannot stand in for `anchor`, in the operator's terms."""
	issued = _already_issued_message(bucket)
	if issued:
		return issued
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


def _swap(pick_list_item, new_bucket_id, reason, notes, keep_old_on_shelf=False, farm=None, to_remote=False):
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
				to_remote=to_remote,
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


def _trip_truck(opl_name, farm):
	"""The truck of the open trip planned to carry `opl_name`'s buckets from `farm`."""
	row = frappe.db.sql(
		"""SELECT t.vehicle FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name AND o.parenttype = 'Bucket Request Trip'
		WHERE o.order_pick_list = %(opl)s AND o.farm = %(farm)s AND IFNULL(o.unscheduled, 0) = 0
		  AND t.status IN ('Draft', 'Scheduled', 'Dispatched') AND IFNULL(t.vehicle, '') != ''
		ORDER BY t.trip_date DESC, t.run ASC LIMIT 1""",
		{"opl": opl_name, "farm": farm},
	)
	return row[0][0] if row else None


def _load_on_truck(pli_name, truck):
	"""Load the bucket on `pli_name` onto `truck` and put it in transit through the
	Bucket Requests app's own endpoint (setOfflineTrolleyFlags): sibling rows,
	transit_truck, its farm shelf cleared, the trip's load recorded, the transfer log
	and the OPL's version history -- exactly as if the farm had loaded it."""
	try:
		from upande_quality.mobile import api as quality_api
	except ImportError:
		quality_api = None
	if quality_api is None:
		from upande_packhouse.api.transfer_control import record_truck_load

		frappe.db.set_value(
			"Pick List Item",
			pli_name,
			{"loaded_in_trolley": 1, "in_transit": 1, **({"transit_truck": truck} if truck else {})},
		)
		record_truck_load([pli_name])
		return None
	for flag in ("loaded", "transit"):
		_status, message, _data = _call(
			quality_api.setOfflineTrolleyFlags,
			{"data": {"pli_ids": [pli_name], "flag": flag, "truck": truck or ""}},
		)
		message = message or {}
		if message.get("status") != "success":
			return message.get("message") or _("Could not load it on the truck.")
		if message.get("conflicts"):
			c = message["conflicts"][0]
			return (
				_("It is already on {0}.").format(c.get("truck"))
				if c.get("reason") == "on_truck"
				else _("It has already arrived at the packhouse.")
			)
	return None


def _send_in_transit(bucket, opl_name):
	"""A remote-transfer bucket still at its farm, issued offline on `opl_name` (a
	draft OPL waiting on its transfer): it is not issued here. It goes onto the truck
	of the trip planned for it and in transit (_load_on_truck -- the trip and the OPL
	show it on the truck), and the transfer finishes the usual way: shelving it at the
	hub moves its stock and marks it arrived, then the issuing scan issues it to the
	line that asked for it. Returns {"farm", "truck"}, or None when the bucket is not
	waiting on a transfer for this OPL."""
	from upande_packhouse import stock_movement as sm

	opl = frappe.get_doc("Order Pick List", opl_name)
	waiting = [
		r
		for r in sm.opl_rows(opl)
		if (r.bucket or "").upper() == bucket.upper()
		and cint(r.awaiting_transfer)
		and not cint(r.shelved)
		and not cint(r.issued)
	]
	if not waiting:
		return None
	farm = waiting[0].get("farm") or (sm._row_warehouse(waiting[0]) or "").split(" ")[0]
	truck = waiting[0].get("transit_truck") or _trip_truck(opl_name, farm)
	if not cint(waiting[0].in_transit):
		refused = _load_on_truck(waiting[0].name, truck)
		if refused:
			frappe.throw(_("{0} can't go on the truck: {1}").format(bucket, refused))
	return {"farm": farm, "truck": truck}


def _issue_remote_aware(bucket, opl_name):
	"""_issue -- unless `bucket` is a remote-transfer bucket still at its farm: then it
	is only put in transit (_send_in_transit) and issued once it is shelved at the hub.
	Returns (issue results, None) or ([], {"farm", "truck"}) for one sent in transit."""
	try:
		sent = _send_in_transit(bucket, opl_name)
	except frappe.ValidationError as e:
		frappe.db.rollback()
		return [{"sale_order_item": None, "ok": False, "message": str(e), "data": None}], None
	if sent:
		return [], sent
	return _issue(bucket, opl_name), None


def _transit_note(bucket, sent, line):
	hub = frappe.db.get_single_value("Production Settings", "transfer_hub_farm") or _("the packhouse")
	return _(
		"{0} is coming from {1}{2}: in transit to {3}, not issued yet. Shelve it at {3} when it arrives, then issue it to {4}."
	).format(bucket, sent["farm"], _(" on {0}").format(sent["truck"]) if sent.get("truck") else "", hub, line)


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
	# correctDetails commits its own corrections; the re-key belongs with them, and
	# must not be lost if the caller's request fails after this point.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
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
	"""OPLs (draft or submitted), delivering from `days` ago to tomorrow, that still
	have a bucket to issue. Newest first, with how far issuing has got. Drafts are
	included -- an order waiting on a remote transfer stays a draft until its buckets
	arrive, and issuing works on it all the same. A draft waiting on a remote transfer
	shows only once Bucket Requests works on it: scheduled (on a Packhouse Schedule)
	and planned on a trip. A sales farm's own draft (nothing to transfer) shows as is.

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
	# nosemgrep: frappe-sql-format-injection -- farm_cond is FARM_EXPR (a fixed column expression); values are bound
	rows = frappe.db.sql(
		"""
		SELECT opl.name AS opl_name, opl.order_name, opl.customer, opl.team, opl.farm,
		       so.delivery_date,
		       SUM(pli.stock_qty) AS total_stems,
		       SUM(CASE WHEN pli.issued = 1 THEN pli.stock_qty ELSE 0 END) AS issued_stems,
		       COUNT(DISTINCT CASE WHEN COALESCE(pli.issued, 0) = 0 THEN pli.bucket END) AS open_buckets,
		       COUNT(DISTINCT CASE WHEN pli.issued = 1 THEN pli.bucket END) AS issued_buckets,
		       GROUP_CONCAT(DISTINCT pli.item_code ORDER BY pli.item_code SEPARATOR '||') AS varieties
		FROM `tabOrder Pick List` opl
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
		WHERE (opl.docstatus = 1 OR (opl.docstatus = 0 AND (
		    NOT EXISTS (SELECT 1 FROM `tabPick List Item` x WHERE x.parent = opl.name
		      AND x.parenttype = 'Order Pick List'
		      AND (x.awaiting_transfer = 1 OR x.loaded_in_trolley = 1 OR x.in_transit = 1 OR x.shelved = 1))
		    OR (EXISTS (SELECT 1 FROM `tabPackhouse Schedule Order` pso WHERE pso.order_pick_list = opl.name)
		      AND EXISTS (SELECT 1 FROM `tabBucket Request Trip Order` tro
		        JOIN `tabBucket Request Trip` t ON t.name = tro.parent
		        WHERE tro.order_pick_list = opl.name AND IFNULL(tro.unscheduled, 0) = 0
		          AND t.status IN ('Draft', 'Scheduled', 'Dispatched', 'Received'))))))
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
def offline_issue_issued(opl_name: str):
	"""The buckets already issued to `opl_name`'s line, one entry per bucket (a mixed
	bucket lists each variety): variety, stem length, stems, and whether it went out
	through Issue Offline. Newest first."""
	rows = frappe.db.sql(
		"""SELECT pli.bucket, pli.item_code AS variety, pli.stem_length, SUM(pli.stock_qty) AS stems,
		       MAX(IFNULL(pli.issued_offline, 0)) AS offline, MAX(pli.modified) AS at
		FROM `tabPick List Item` pli
		WHERE pli.parent = %s AND pli.parenttype = 'Order Pick List' AND pli.issued = 1
		  AND COALESCE(pli.bucket, '') != ''
		GROUP BY pli.bucket, pli.item_code, pli.stem_length
		ORDER BY at DESC, pli.bucket""",
		(opl_name,),
		as_dict=True,
	)
	out = {}
	for r in rows:
		b = out.setdefault(
			r.bucket, {"bucket": r.bucket, "issued_offline": False, "at": str(r.at or ""), "contents": []}
		)
		b["issued_offline"] = b["issued_offline"] or bool(cint(r.offline))
		b["contents"].append({"variety": r.variety, "stem_length": r.stem_length, "stems": flt(r.stems)})
	return {"opl_name": opl_name, "buckets": list(out.values())}


@frappe.whitelist()
def offline_issue_buckets(opl_name: str, farm: str | None = None):
	"""The buckets `opl_name` is still waiting on, one entry per bucket. A remote
	`farm` (not the transfer hub) gets only the buckets coming from it."""
	from upande_packhouse.api.transfer_control import transfer_hub

	hub = transfer_hub(required=False) or ""
	only_farm = farm if farm and farm != hub else None
	rows = frappe.get_all(
		"Pick List Item",
		filters={"parent": opl_name, "parenttype": "Order Pick List", "bucket": ["is", "set"]},
		fields=[
			"bucket",
			"issued",
			"issued_offline",
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
				"issued": True,
				"issued_offline": False,
			}
		b["stems"] += flt(r.stock_qty)
		# Issued once every row of it is; issued offline when any row went out that way.
		b["issued"] = b["issued"] and bool(cint(r.issued))
		b["issued_offline"] = b["issued_offline"] or bool(cint(r.issued_offline))
	# Still to issue first; the ones already issued offline stay listed (marked) so
	# the operator sees they are done. Ones issued by the normal scan drop out.
	out = [b for b in buckets.values() if not b["issued"]]
	out += [b for b in buckets.values() if b["issued"] and b["issued_offline"]]
	return {"opl_name": opl_name, "buckets": out}


def _offline_history(bucket):
	"""Earlier reports of `bucket` as not found or the wrong variety (Bucket
	Replacements, newest first): whether it already went through offline issuing."""
	rows = frappe.get_all(
		"Bucket Replacement",
		filters={"old_bucket": bucket, "reason": ["in", ["Missing", "Wrong variety"]], "docstatus": ["<", 2]},
		fields=[
			"name",
			"reason",
			"status",
			"new_bucket",
			"order_pick_list",
			"order_name",
			"reported_by",
			"reported_at",
		],
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
	source = "local"
	warning = None
	if not found:
		found = _remote_candidates(anchor, opl_name, farm, needed, max(1, min(cint(limit) or 20, 100)))
		source = "remote"
		if found and _delivers_today(opl_name):
			warning = _(
				"This order is delivered today. A bucket from a remote farm comes on the next truck and may not arrive in time."
			)
	if not found:
		return {
			"found": False,
			"message": sa._no_replacement_message(anchor, farm, needed),
			"candidates": [],
			"history": history,
		}
	return {
		"found": True,
		"source": source,
		"message": _("No matching bucket at {0}; available at remote farms.").format(farm)
		if source == "remote"
		else None,
		"warning": warning,
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
		anchor, _rows, farm = sa._requested_bucket_rows(pli, allow_shelved=True)
	except frappe.ValidationError as e:
		return _fail(e)
	reason = reason if reason in ISSUING_REASONS else "Missing"
	# Mislabelled, not gone: the old bucket stays on its shelf for its record to be corrected.
	keep_old = reason == "Wrong variety"
	# Where the replacement sits is the server's to decide, never the app's.
	new_farm = frappe.db.sql(
		"""SELECT s.farm FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		WHERE si.bucket_id = %s LIMIT 1""",
		new_bucket_id,
	)
	new_farm = new_farm[0][0] if new_farm else None
	if not new_farm or new_farm == farm:
		return _swap(pli, new_bucket_id, reason, notes, keep_old_on_shelf=keep_old)
	if new_farm not in _remote_farms(opl_name, farm):
		return _fail(
			_("Bucket {0} is at {1}, which does not supply {2}.").format(new_bucket_id, new_farm, farm)
		)
	res = _swap(pli, new_bucket_id, reason, notes, keep_old_on_shelf=keep_old, farm=new_farm, to_remote=True)
	if res.get("success"):
		_off_trips(anchor.bucket, opl_name)
		res["message"] = _(
			"{0} replaced with {1}, requested from {2}. Issue it once the truck brings it to {3}."
		).format(anchor.bucket, new_bucket_id, new_farm, farm)
	return res


def _issued_offline(opl_name, bucket, sale_order_item=None):
	"""The OPLs `bucket` was already issued on, each with its line (team), and
	whether that is `opl_name`'s own line: the same OPL, or another on its team."""
	pli = _anchor_row(opl_name, bucket, sale_order_item)
	line = frappe.db.get_value("Order Pick List", opl_name, "team") or ""
	issued = frappe.db.sql(
		"""SELECT DISTINCT pli.parent AS opl, opl.order_name, IFNULL(opl.team, '') AS team
		FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		WHERE pli.parenttype = 'Order Pick List' AND pli.issued = 1 AND pli.bucket = %s""",
		(bucket,),
		as_dict=True,
	)
	for r in issued:
		r["same_line"] = r.opl == opl_name or (bool(line) and r.team == line)
	return pli, line, issued


@frappe.whitelist()
def issued_offline_info(opl_name: str, bucket: str, sale_order_item: str | None = None):
	"""Replace reason "Issued offline": which line(s) `bucket` was issued to, and
	whether one is this OPL's own line -- then it is marked issued, not replaced."""
	try:
		_pli, line, issued = _issued_offline(opl_name, bucket, sale_order_item)
	except frappe.ValidationError as e:
		return _fail(e)
	return {
		"success": True,
		"bucket": bucket,
		"line": line,
		"issued_to": issued,
		"same_line": any(r["same_line"] for r in issued),
	}


@frappe.whitelist(methods=["POST"])
def mark_issued_offline(opl_name: str, bucket: str, sale_order_item: str | None = None):
	"""`bucket` already went out to this OPL's own line without the issuing scan:
	mark it issued here (the scan's own logic, so stock moves the same way), with
	no replacement. Issued to another line, or nowhere: refused -- replace it."""
	try:
		_pli, line, issued = _issued_offline(opl_name, bucket, sale_order_item)
	except frappe.ValidationError as e:
		return _fail(e)
	if not any(r["same_line"] for r in issued):
		where = ", ".join("{0} ({1})".format(r.team or _("no team"), r.order_name or r.opl) for r in issued)
		return _fail(
			_("{0} was issued to {1}, not this line. Replace it instead.").format(bucket, where)
			if where
			else _("{0} has not been issued to any line. Replace it instead.").format(bucket)
		)
	results, came_from = _issue_remote_aware(bucket, opl_name)
	if came_from:
		return {"success": True, "in_transit": True, "message": _transit_note(bucket, came_from, line or opl_name)}
	ok = bool(results) and all(r["ok"] for r in results)
	if not ok:
		return _fail(
			_("Could not mark {0} issued: {1}").format(
				bucket, "; ".join(str(r["message"]) for r in results if not r["ok"]) or _("nothing to issue")
			)
		)
	frappe.db.sql(
		"""UPDATE `tabPick List Item` SET issued_offline = 1
		WHERE parent = %s AND parenttype = 'Order Pick List' AND bucket = %s AND issued = 1""",
		(opl_name, bucket),
	)
	return {"success": True, "message": _("{0} marked issued to {1}.").format(bucket, line or opl_name)}


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

	issued, came_from = _issue_remote_aware(target, opl_name)
	line = frappe.db.get_value("Order Pick List", opl_name, "team") or opl_name
	issue_ok = bool(came_from) or (bool(issued) and all(r["ok"] for r in issued))
	if issue_ok and not came_from:
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
		(_transit_note(target, came_from, line) if came_from else _("{0} issued to {1}.").format(target, opl_name))
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
		# A remote bucket still at its farm: sent in transit, issued once shelved at the hub.
		"in_transit": bool(came_from),
		"replacement": (swapped or {}).get("replacement"),
		"issued": issued,
		"correction": correction,
	}
