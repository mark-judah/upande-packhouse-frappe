"""Sales Allocation v2: allocate every order of a delivery window in one go, then settle what is short
by moving buckets between orders of the same variety."""

import json
import math
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_packhouse import allocation_plan
from upande_packhouse.api.v2.allocation_teams import team_for, teams_by_crop
from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa


def _farms(selected_farms):
	if isinstance(selected_farms, str):
		selected_farms = json.loads(selected_farms or "[]")
	return selected_farms or None


def _left(item):
	return flt(item.get("pending_stock_qty")) - flt(item.get("total_allocated_qty"))


def _hold_to_plan(items):
	"""The plan as a guide: the lengths each line was confirmed at, so a longer length the planner chose is
	used without a downgrade reason. Nothing is limited by it."""
	for item in items:
		confs = allocation_plan.live_confirmations(soi=item["sales_order_item"])
		lengths = {c.length or "" for c in confs if c.variety == item["item_code"]}
		item["plan_lengths"] = lengths - {""} or None


def _rules(location):
	"""The v2 switches, plus the teams by crop and by customer, for lines with no team of their own when the
	plan is not enforced."""
	rules = sa._v2_rules(location)
	if not rules["enforce_plan"]:
		by_crop = teams_by_crop(location=location)
		rules["by_crop"] = by_crop["teams"]
		rules["customer_teams"] = by_crop["customer_teams"]
		rules["crop_of"] = by_crop["crop_of"]
	return rules


def _per_box(item):
	"""Stems of this line in one box: its pack rate (the mix's per-box share for a mixed box or bunch), else
	its stems over its boxes -- the rule complete_boxes packs by."""
	mixed = cint(item.get("custom_mixed_box")) or cint(item.get("custom_mixed_bunch"))
	rate = flt(item.get("custom_packrate_mixed_box") if mixed else item.get("custom_packrate"))
	return rate or flt(item.get("original_stock_qty")) / (cint(item.get("custom_number_of_boxes")) or 1)


def _box_key(item):
	"""Lines packed in the same box: a mixed bunch's group, a mixed box's group, or the line on its own."""
	if cint(item.get("custom_mixed_bunch")) and item.get("custom_bunch_group"):
		return ("bunch", str(item["custom_bunch_group"]))
	if cint(item.get("custom_mixed_box")) and item.get("custom_mix_group"):
		return ("box", str(item["custom_mix_group"]))
	return ("line", item["sales_order_item"])


def _free(item, held, usable_only=False):
	"""Stems still free for this line once the proposal (`held`) has taken its buckets, in whole bunches of
	the line from each bucket (a bunch is never split); `usable_only`: without longer buckets still waiting
	for a downgrade reason."""
	per_bunch = flt(item.get("conversion_factor")) or 1
	total = 0
	for b in item.get("batches") or []:
		if usable_only and b.get("length_status") == "downgrade" and not _downgrade_ok(item, b):
			continue
		key = (b["bucket_id"], item["item_code"], b.get("stem_length") or "")
		free = max(0, flt(b.get("available_qty")) - held.get(key, 0))
		total += math.floor(free / per_bunch + 1e-9) * per_bunch
	return total


def _whole_boxes(members, taken, ok):
	"""{sales_order_item: stems to add} so the lines end on whole boxes: a line takes only the boxes its
	stock fills, a mixed box or bunch only as many as its scarcest line fills, and the last part box only
	when it completes the line."""
	boxes, rows = None, []
	for item in members:
		need, have, per_box = (
			flt(item.get("total_allocated_qty")) + _left(item),
			flt(item.get("total_allocated_qty")),
			_per_box(item),
		)
		can = 0
		if ok(item):
			left = need - have
			can = left - _fifo_pick(item, left, dict(taken))[1]
		full = math.ceil(need / per_box - 1e-9) if per_box > 0 else 0
		if per_box > 0:
			fills = full if have + can >= need - 1e-9 else min(full, int((have + can) / per_box + 1e-9))
			boxes = fills if boxes is None else min(boxes, fills)
		rows.append((item, need, have, per_box, full))
	out = {}
	for item, need, have, per_box, full in rows:
		if not ok(item):
			continue
		target = need if per_box <= 0 or boxes >= full else boxes * per_box
		if target - have > 1e-9:
			out[item["sales_order_item"]] = target - have
	return out


def _planned(item):
	return flt(item.get("confirmed_stems")) > 0


def _team(item, planned, rules):
	team = item.get("opl_team") or item.get("planning_team") or planned.get(item["sales_order_item"])
	if team or rules["enforce_plan"]:
		return team
	crop = rules["crop_of"].get(frappe.get_cached_value("Item", item["item_code"], "item_group"))
	return team_for(item.get("customer"), crop, rules["by_crop"], rules["customer_teams"])


def _orders(location, delivery_start, delivery_end):
	"""Every open order of the window, in allocation order: Allocation Planning's sequence, every team's
	#1 first."""
	return sa.get_pending_sales_orders(
		delivery_start=delivery_start, delivery_end=delivery_end or delivery_start, location=location
	)


def _order_lines(location, delivery_start, delivery_end, farms):
	"""(order, its lines with compatible buckets) for every open order of the window."""
	for order in _orders(location, delivery_start, delivery_end):
		items = sa.get_sales_order_items_with_buckets(order.name, location, selected_farms=farms) or []
		if items:
			yield order, items


def _planned_longer(item, b):
	"""The plan confirmed the line at this longer length: the planner chose it, no reason is asked again."""
	return bool(item.get("plan_lengths")) and (b.get("stem_length") or "") in item["plan_lengths"]


def _downgrade_ok(item, b):
	return b.get("downgrade_approval") == "amber_expired" or _planned_longer(item, b)


def _downgrade_reason(item, b):
	if b.get("length_status") != "downgrade":
		return ""
	if _planned_longer(item, b):
		return _("Planned at {0}").format(b.get("stem_length"))
	return _("Amber time expired")


def _fifo_pick(item, left, taken):
	"""Oldest compatible buckets first (the order the page lists them in). A longer bucket is used only
	when its amber time is up; one that still needs a downgrade reason is left for a person."""
	picks = []
	for b in item.get("batches") or []:
		if left <= 0:
			break
		if b.get("length_status") == "downgrade" and not _downgrade_ok(item, b):
			continue
		key = (b["bucket_id"], item["item_code"], b.get("stem_length") or "")
		free = flt(b.get("available_qty")) - taken.get(key, 0)
		if free <= 0:
			continue
		# whole bunches only: a bunch is never split, so a bucket gives what fills whole bunches
		per_bunch = flt(item.get("conversion_factor")) or 1
		qty = math.floor(min(free, left) / per_bunch + 1e-9) * per_bunch
		if qty <= 0:
			continue
		taken[key] = taken.get(key, 0) + qty
		left -= qty
		picks.append(
			{
				"item_code": item["item_code"],
				"bucket_id": b["bucket_id"],
				"qty": qty,
				"sales_order_item": item["sales_order_item"],
				"stem_length": b.get("stem_length") or "",
				"warehouse": b.get("warehouse") or "",
				"uom": item.get("uom") or "",
				"stock_uom": item.get("stock_uom") or "",
				"conversion_factor": item.get("conversion_factor") or 1,
				"length_status": b.get("length_status") or "exact",
				"downgrade_reason": _downgrade_reason(item, b),
				"available_exact_stems": 0,
			}
		)
	return picks, left


@frappe.whitelist(methods=["POST"])
def auto_allocate_day(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	selected_farms: str | list | None = None,
):
	"""Propose an allocation for every open order delivering in the window, FIFO, in whole boxes, in
	Allocation Planning's sequence. Nothing is saved: the proposal is kept for this user until it is
	confirmed (confirm_day_allocation). Progress and the result reach the user over realtime as
	`sa2_day_allocation`."""
	if not location or not delivery_start:
		frappe.throw(_("Location and delivery date are required."))
	_lock(location)
	# Proposing is quick (no stock moves): it runs now, in this request, so it never waits behind the
	# background worker. Progress still reaches the page over realtime while it runs.
	# the result also comes back with the request, so the page finishes even if a realtime message is missed
	return _run_day(location, delivery_start, delivery_end, _farms(selected_farms), frappe.session.user)


#: A release of up to this many orders runs at once, in the request; a bigger one goes to the background
#: worker (each order posts stock entries and a pick list, a few seconds each).
RELEASE_NOW = 5


def _lock(location, phase="propose"):
	lock = f"sa2_day_allocation:{location}"
	if frappe.cache.get_value(lock):
		frappe.throw(_("Orders at {0} are already being allocated.").format(location))
	frappe.cache.set_value(lock, frappe.session.user, expires_in_sec=1800)
	# not started yet: the bar shows from the start, also after a refresh
	_set_state(location, {"phase": phase, "progress": 0, "total": 1, "order": ""})


def _set_state(location, state):
	frappe.cache.set_value(f"sa2_day_state:{location}", state, expires_in_sec=1800)


PENDING = "Pending Allocation"


def _window(delivery_start, delivery_end, farms=None):
	return [delivery_start, delivery_end or delivery_start]


def _proposal(location, delivery_start=None, delivery_end=None, farms=None, user=None):
	"""Every open Pending Allocation (allocated, waiting confirmation) at this location for the window, as
	{"orders": [{sales_order, ..., allocations, teams}]}, or None when there is none."""
	filters = {"status": "Pending", "location": location}
	if delivery_start:
		filters["delivery_date"] = ("between", _window(delivery_start, delivery_end))
	names = frappe.get_all(PENDING, filters=filters, pluck="name", order_by="creation")
	orders = []
	for name in names:
		doc = frappe.get_doc(PENDING, name)
		rows = [
			{
				"item_code": r.item_code,
				"bucket_id": r.bucket_id,
				"qty": flt(r.qty),
				"sales_order_item": r.sales_order_item,
				"stem_length": r.stem_length or "",
				"warehouse": r.warehouse or "",
				"uom": r.uom or "",
				"stock_uom": r.stock_uom or "",
				"conversion_factor": flt(r.conversion_factor) or 1,
				"length_status": r.length_status or "exact",
				"downgrade_reason": r.downgrade_reason or "",
				"available_exact_stems": 0,
			}
			for r in doc.allocations
		]
		orders.append(
			{
				"name": doc.name,
				"sales_order": doc.sales_order,
				"order_name": frappe.db.get_value("Sales Order", doc.sales_order, "custom_order_name") or "",
				"customer": doc.customer,
				"delivery_date": str(doc.delivery_date or ""),
				"sequence": 0,
				"source": doc.source,
				"boxes": cint(doc.boxes),
				"stems": flt(doc.stems),
				"allocations": rows,
				"teams": {r.sales_order_item: r.team for r in doc.allocations if r.team},
			}
		)
	return {"orders": orders} if orders else None


def _save_pending(location, sales_order, allocations, teams, boxes, source):
	"""Write (or replace) an order's open Pending Allocation at the location; none left when it has no rows."""
	name = frappe.db.get_value(
		PENDING, {"sales_order": sales_order, "location": location, "status": "Pending"}, "name"
	)
	if not allocations:
		if name:
			frappe.delete_doc(PENDING, name, ignore_permissions=True, force=True)
		return None
	doc = frappe.get_doc(PENDING, name) if name else frappe.new_doc(PENDING)
	doc.update(
		{
			"sales_order": sales_order,
			"location": location,
			"source": source,
			"status": "Pending",
			"boxes": cint(boxes),
			"stems": sum(flt(a.get("qty")) for a in allocations),
		}
	)
	doc.set("allocations", [])
	for a in allocations:
		doc.append(
			"allocations",
			{
				"sales_order_item": a["sales_order_item"],
				"item_code": a["item_code"],
				"bucket_id": a["bucket_id"],
				"stem_length": a.get("stem_length") or "",
				"qty": flt(a.get("qty")),
				"team": (teams or {}).get(a["sales_order_item"]),
				"length_status": a.get("length_status") or "exact",
				"downgrade_reason": a.get("downgrade_reason") or "",
				"warehouse": a.get("warehouse") or "",
				"uom": a.get("uom") or "",
				"stock_uom": a.get("stock_uom") or "",
				"conversion_factor": flt(a.get("conversion_factor")) or 1,
			},
		)
	doc.flags.ignore_permissions = True
	doc.save()
	return doc.name


def _row_key(r):
	return (r["sales_order_item"], r["bucket_id"], r.get("stem_length") or "", round(flt(r["qty"]), 3))


@frappe.whitelist(methods=["POST"])
def save_pending(
	location: str,
	sales_order: str,
	allocations: str | list | None = None,
	teams: str | dict | None = None,
	boxes: int = 0,
):
	"""Hold an order's hand-picked stems as a Pending Allocation (the buckets are held for it) until confirmed;
	no stems cancels it."""
	if isinstance(allocations, str):
		allocations = json.loads(allocations or "[]")
	if isinstance(teams, str):
		teams = json.loads(teams or "{}")
	# stems another order holds or has been allocated since must not be held twice
	if allocations:
		holds = sa.pending_holds({a["bucket_id"] for a in allocations})
		for a in allocations:
			other = sum(
				q
				for so, q in holds.get((a["bucket_id"], a["item_code"], a.get("stem_length") or ""), [])
				if so != sales_order
			)
			if other:
				free = flt(
					frappe.db.sql(
						"""SELECT GREATEST(0, COALESCE(SUM(si.stem_qty), 0)) FROM `tabShelf Item` si
						   WHERE si.bucket_id = %s AND si.variety = %s AND IFNULL(si.stem_length, '') = %s""",
						(a["bucket_id"], a["item_code"], a.get("stem_length") or ""),
					)[0][0]
				)
				if flt(a.get("qty")) > free - other + 1e-6:
					frappe.throw(
						_("Bucket {0} is held for another order. Please refresh.").format(a["bucket_id"])
					)
	# an Allocate all proposal reopened and saved unchanged stays Allocate all; any change makes it Picked
	source = "Picked"
	name = frappe.db.get_value(
		PENDING, {"sales_order": sales_order, "location": location, "status": "Pending"}, "name"
	)
	if name and allocations:
		before = sorted(
			_row_key(r)
			for r in frappe.get_all(
				"Pending Allocation Row",
				filters={"parent": name, "parenttype": PENDING},
				fields=["sales_order_item", "bucket_id", "stem_length", "qty"],
			)
		)
		if before == sorted(_row_key(a) for a in allocations):
			source = frappe.db.get_value(PENDING, name, "source") or source
	return {"name": _save_pending(location, sales_order, allocations or [], teams, boxes, source)}


def _summary(p):
	orders = p["orders"] if p else []
	# each order's boxes: how many it needs, and how many will be full once this is released
	rows = [frappe._dict(name=o["sales_order"]) for o in orders]
	sa._attach_boxes(rows)
	for o, r in zip(orders, rows, strict=True):
		o["order_boxes"] = cint(r.boxes_required)
		o["full_after"] = min(cint(r.boxes_required), cint(r.boxes_full) + cint(o["boxes"]))
	return {
		"orders": [
			{**o, "lines": _line_stems(o["allocations"]), "team_names": _team_names(o)} for o in orders
		],
		"boxes": sum(o["boxes"] for o in orders),
		"stems": sum(o["stems"] for o in orders),
	}


def _fill_teams(order):
	"""{sales_order_item: team} for every line the order releases: the one chosen for the line, else its
	planned team, else the team most of the order's other lines go to (one order is packed by one team).
	A line is left out only when no line of the order has a team."""
	lines = list(dict.fromkeys(a["sales_order_item"] for a in order["allocations"]))
	chosen = {k: v for k, v in (order.get("teams") or {}).items() if v}
	planned = allocation_plan.planned_teams([x for x in lines if not chosen.get(x)])
	teams = {x: chosen.get(x) or planned.get(x) for x in lines if chosen.get(x) or planned.get(x)}
	if teams and len(teams) < len(lines):
		from collections import Counter

		main = Counter(teams.values()).most_common(1)[0][0]
		teams.update({x: main for x in lines if x not in teams})
	return teams


def _team_names(order):
	"""The teams the order's lines are released to (_fill_teams); "" at the end when a line has none."""
	teams = _fill_teams(order)
	lines = {a["sales_order_item"] for a in order["allocations"]}
	return sorted(set(teams.values())) + ([""] if len(teams) < len(lines) else [])


def _line_stems(allocations):
	out = {}
	for a in allocations:
		out[a["sales_order_item"]] = out.get(a["sales_order_item"], 0) + flt(a["qty"])
	return out


def _pending(p):
	"""({sales_order_item: stems}, {sales_order: {(bucket, variety, length): stems}}) the pending allocations
	hold. Other orders' holds are already taken off the buckets each order sees, so only its own count here."""
	lines, held = {}, {}
	for o in p["orders"] if p else []:
		mine = held.setdefault(o["sales_order"], {})
		for a in o["allocations"]:
			lines[a["sales_order_item"]] = lines.get(a["sales_order_item"], 0) + flt(a["qty"])
			key = (a["bucket_id"], a["item_code"], a.get("stem_length") or "")
			mine[key] = mine.get(key, 0) + flt(a["qty"])
	return lines, held


#: What the realtime "done" message carries: the counts. The proposal and the shortages (hundreds of
#: kilobytes on a busy day) come back with the request, or the page reloads them.
LIGHT = ("error", "errors", "saved", "boxes", "stems", "new_orders", "new_boxes", "new_stems")


def _light(result):
	return {k: result[k] for k in LIGHT if k in result} | {"orders_count": len(result.get("orders") or [])}


def _publish(user, location, **msg):
	"""Tell the page how the job is going, and keep where it is so a page opened or refreshed while it runs
	picks the bar up again (get_day_state)."""
	key = f"sa2_day_state:{location}"
	if msg.get("done"):
		frappe.cache.delete_value(key)
	else:
		_set_state(location, {k: msg.get(k) for k in ("phase", "progress", "total", "order")})
	frappe.publish_realtime("sa2_day_allocation", msg, user=user)


@frappe.whitelist()
def get_day_state(location: str):
	"""Where a running Allocate all or Release at this location is ({phase, progress, total, order}), or None."""
	if not frappe.cache.get_value(f"sa2_day_allocation:{location}"):
		return None
	state = frappe.cache.get_value(f"sa2_day_state:{location}") or {"progress": 0, "total": 1, "order": ""}
	if state.get("job_id"):
		state.update(_queue_place(state["job_id"]))
	return state


def _queue_place(job_id):
	"""{"queued": True, "ahead": jobs before it} while the background job has not started, else {}."""
	from frappe.utils.background_jobs import create_job_id, get_redis_conn
	from rq.job import Job

	try:
		job = Job.fetch(create_job_id(job_id), connection=get_redis_conn())
	except Exception:
		return {}
	if job.get_status() != "queued":
		return {}
	# the worker finishes the job it is on first
	return {"queued": True, "ahead": (job.get_position() or 0) + 1}


def _run_day(location, delivery_start, delivery_end, farms, user):
	def progress(done, total, order):
		_publish(user, location, phase="propose", progress=done, total=total, order=order)

	try:
		# a new run replaces the last Allocate all proposal; hand-picked orders keep theirs and are skipped
		for name in frappe.get_all(
			PENDING,
			filters={
				"status": "Pending",
				"location": location,
				"source": "Allocate all",
				"delivery_date": ("between", _window(delivery_start, delivery_end)),
			},
			pluck="name",
		):
			frappe.delete_doc(PENDING, name, ignore_permissions=True, force=True)
		kept = _proposal(location, delivery_start, delivery_end)
		skip = {o["sales_order"] for o in (kept or {}).get("orders", [])}
		orders, errors = _plan_day(location, delivery_start, delivery_end, farms, progress, skip)
		for o in orders:
			_save_pending(
				location, o["sales_order"], o["allocations"], o["teams"], o["boxes"], "Allocate all"
			)
		# the proposal is what the planner reviews next: it must outlive the job
		frappe.db.commit()  # nosemgrep
		p = _proposal(location, delivery_start, delivery_end)
		result = {
			**_summary(p),
			"new_orders": len(orders),
			"new_boxes": sum(o["boxes"] for o in orders),
			"new_stems": sum(o["stems"] for o in orders),
			"shortages": _shortages(location, delivery_start, delivery_end, farms, errors, _pending(p)),
		}
	except Exception as e:
		frappe.log_error("Sales Allocation v2: auto-allocate all failed", frappe.get_traceback())
		result = {"error": str(e)}
	finally:
		frappe.cache.delete_value(f"sa2_day_allocation:{location}")
	_publish(user, location, phase="propose", done=True, **_light(result))
	return {"phase": "propose", "done": True, **result}


def _plan_day(location, delivery_start, delivery_end, farms, progress=None, skip=()):
	"""[{sales_order, ..., allocations, teams, boxes, stems}] for every order that can take whole boxes, and
	{sales_order_item: why} for lines whose box the plan refuses. Buckets taken for one order are not offered to the next."""
	rules = _rules(location)
	orders = _orders(location, delivery_start, delivery_end)
	taken, out, errors = {}, [], {}

	for n, order in enumerate(orders, 1):
		if progress:
			progress(n - 1, len(orders), order.name)
		if order.name in skip:
			continue
		items = sa.get_sales_order_items_with_buckets(order.name, location, selected_farms=farms) or []
		for item in items:
			item["customer"] = order.customer
		_hold_to_plan(items)
		planned = allocation_plan.planned_teams([i["sales_order_item"] for i in items]) if items else {}
		mine, allocations, teams, order_boxes = dict(taken), [], {}, 0

		def ok(item):
			return (
				_left(item) > 0
				and _team(item, planned, rules)
				and not (rules["enforce_plan"] and not _planned(item))
			)

		groups = {}
		for item in items:
			groups.setdefault(_box_key(item), []).append(item)
		for members in groups.values():
			adds = _whole_boxes(members, mine, ok)
			if not rules["partial_allocation"] and any(
				adds.get(it["sales_order_item"], 0) < _left(it) - 1e-9 for it in members if ok(it)
			):
				continue
			group_boxes, group_taken, group_picks, group_teams = 0, dict(mine), [], {}
			for item in members:
				add = adds.get(item["sales_order_item"], 0)
				if add <= 0:
					continue
				picks, _short = _fifo_pick(item, add, group_taken)
				if picks:
					group_picks += picks
					group_teams[item["sales_order_item"]] = _team(item, planned, rules)
					per_box = _per_box(item)
					if per_box > 0:
						group_boxes = max(group_boxes, math.ceil(add / per_box - 1e-9))
			if not group_picks:
				continue
			# the plan's checks run per box: a box the plan refuses stays out, the rest of the order goes on
			if rules["enforce_plan"]:
				try:
					sa._check_against_plan(group_picks, group_teams)
				except Exception as e:
					frappe.clear_messages()
					errors.update({it["sales_order_item"]: str(e) for it in members})
					continue
			mine = group_taken
			allocations += group_picks
			teams.update(group_teams)
			order_boxes += group_boxes
		if not allocations:
			continue
		taken = mine
		out.append(
			{
				"sales_order": order.name,
				"customer": order.customer,
				"delivery_date": str(order.delivery_date),
				"sequence": cint(order.get("planning_sequence")),
				"boxes": order_boxes,
				"stems": sum(flt(a["qty"]) for a in allocations),
				"allocations": allocations,
				"teams": teams,
			}
		)

	if progress:
		progress(len(orders), len(orders), "")
	return out, errors


@frappe.whitelist()
def get_day_proposal(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	selected_farms: str | list | None = None,
):
	return _summary(_proposal(location, delivery_start, delivery_end, _farms(selected_farms)))


@frappe.whitelist(methods=["POST"])
def discard_day_proposal(
	location: str,
	delivery_start: str | None = None,
	delivery_end: str | None = None,
	sales_orders: str | list | None = None,
):
	"""Cancel these orders' Pending Allocations (every open one in the window when none are named): their
	buckets are free again."""
	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders or "[]")
	filters = {"status": "Pending", "location": location}
	if sales_orders:
		filters["sales_order"] = ("in", sales_orders)
	elif delivery_start:
		filters["delivery_date"] = ("between", _window(delivery_start, delivery_end))
	names = frappe.get_all(PENDING, filters=filters, pluck="name")
	for name in names:
		frappe.delete_doc(PENDING, name, ignore_permissions=True, force=True)
	return {"discarded": len(names)}


@frappe.whitelist(methods=["POST"])
def confirm_day_allocation(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	selected_farms: str | list | None = None,
	sales_orders: str | list | None = None,
):
	"""Save the proposal of these orders (all of it when none are named), each order on its own, as a
	background job reporting over `sa2_day_allocation`."""
	farms = _farms(selected_farms)
	if not _proposal(location, delivery_start, delivery_end):
		frappe.throw(_("Nothing to release."))
	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders or "[]")
	p = _proposal(location, delivery_start, delivery_end) or {"orders": []}
	count = len([o for o in p["orders"] if not sales_orders or o["sales_order"] in sales_orders])
	_lock(location, "confirm")
	args = {
		"location": location,
		"delivery_start": delivery_start,
		"delivery_end": delivery_end,
		"farms": farms,
		"sales_orders": sales_orders or None,
		"user": frappe.session.user,
	}
	if count <= RELEASE_NOW:
		return _run_confirm(**args)
	job_id = f"sa2_release:{location}:{frappe.generate_hash(length=8)}"
	_set_state(location, {"phase": "confirm", "progress": 0, "total": count, "order": "", "job_id": job_id})
	frappe.enqueue(
		_run_confirm,
		# someone is waiting on the bar: ahead of the scheduler's hourly backlog in the long and default queues
		queue="short",
		at_front=True,
		timeout=1800,
		enqueue_after_commit=True,
		job_id=job_id,
		**args,
	)
	return {"queued": True}


def _run_confirm(location, delivery_start, delivery_end, farms, sales_orders, user):
	try:
		p = _proposal(location, delivery_start, delivery_end) or {"orders": []}
		chosen = [o for o in p["orders"] if not sales_orders or o["sales_order"] in sales_orders]
		rules = sa._v2_rules(location)
		saved, errors = [], {}
		for n, o in enumerate(chosen):
			_publish(user, location, phase="confirm", progress=n, total=len(chosen), order=o["sales_order"])
			frappe.db.savepoint("confirm_day_order")
			try:
				if rules["enforce_plan"]:
					sa._check_against_plan(o["allocations"], o["teams"])
				o["teams"] = _fill_teams(o)
				sa._allocate_stock_with_buckets_impl(o["sales_order"], o["allocations"], location, o["teams"])
			except Exception as e:
				frappe.db.rollback(save_point="confirm_day_order")
				frappe.clear_messages()
				errors[o["sales_order"]] = str(e)
				continue
			sa.close_pending(o["sales_order"], location)
			# each order stands on its own: a later failure must not roll back the ones already saved
			frappe.db.commit()  # nosemgrep
			saved.append(o)
		_publish(user, location, phase="confirm", progress=len(chosen), total=len(chosen), order="")
		p = _proposal(location, delivery_start, delivery_end)
		result = {
			"saved": len(saved),
			"boxes": sum(o["boxes"] for o in saved),
			"stems": sum(o["stems"] for o in saved),
			"errors": errors,
			"proposal": _summary(p),
			"shortages": _shortages(location, delivery_start, delivery_end, farms, errors, _pending(p)),
		}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("Sales Allocation v2: confirm allocated orders failed", frappe.get_traceback())
		result = {"error": str(e)}
	finally:
		frappe.cache.delete_value(f"sa2_day_allocation:{location}")
	_publish(user, location, phase="confirm", done=True, **_light(result))
	return {"phase": "confirm", "done": True, **result}


@frappe.whitelist()
def get_day_shortages(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	selected_farms: str | list | None = None,
):
	farms = _farms(selected_farms)
	p = _proposal(location, delivery_start, delivery_end, farms)
	return _shortages(location, delivery_start, delivery_end, farms, pending=_pending(p))


def _box_cover(items, proposed, held):
	"""({box key: {"needed", "covered", "coverable"}}, the same summed for the order): boxes each line (a mix
	once) needs, the whole boxes its allocated and proposed stems cover, and the whole boxes it could reach
	with the buckets still free for it -- a mix only as many as its scarcest line reaches."""
	groups = {}
	for item in items:
		groups.setdefault(_box_key(item), []).append(item)
	out = {}
	for key, members in groups.items():
		needed = max(
			math.ceil(flt(m.get("pending_stock_qty")) / _per_box(m) - 1e-9) if _per_box(m) > 0 else 0
			for m in members
		)
		covered = coverable = needed
		for m in members:
			per_box = _per_box(m)
			if per_box > 0:
				have = flt(m.get("total_allocated_qty")) + proposed.get(m["sales_order_item"], 0)
				covered = min(covered, int(have / per_box + 1e-9))
				reach = have + _free(m, held, usable_only=True)
				if reach < flt(m.get("pending_stock_qty")) - 1e-9:
					coverable = min(coverable, int(reach / per_box + 1e-9))
		out[key] = {"needed": needed, "covered": covered, "coverable": max(covered, coverable)}
	return out, {k: sum(g[k] for g in out.values()) for k in ("needed", "covered", "coverable")}


def _shortages(location, delivery_start, delivery_end, farms, errors=None, pending=None):
	"""Every line of the window still short once the proposal (`pending`) is counted in, with why, and the
	buckets other orders hold of its variety that would fit it."""
	errors = errors or {}
	proposed, held = pending or ({}, {})
	rules = _rules(location)
	location_farms = sa._get_production_config()["farms_by_location"].get(location) or []
	order_names = dict(
		frappe.db.sql(
			"""SELECT name, IFNULL(custom_order_name, '') FROM `tabSales Order`
			WHERE delivery_date BETWEEN %s AND %s""",
			(delivery_start, delivery_end or delivery_start),
		)
	)
	out, box_memo = [], {}
	for order, items in _order_lines(location, delivery_start, delivery_end, farms):
		planned = allocation_plan.planned_teams([i["sales_order_item"] for i in items])
		_hold_to_plan(items)
		mine = held.get(order.name, {})
		boxes, order_boxes = _box_cover(items, proposed, mine)
		for item in items:
			item["customer"] = order.customer
			short = _left(item) - proposed.get(item["sales_order_item"], 0)
			if short <= 1e-9:
				continue
			team = _team(item, planned, rules)
			free = _free(item, mine)
			usable = _free(item, mine, usable_only=True)
			per_box = _per_box(item)
			if errors.get(item["sales_order_item"]) or errors.get(order.name):
				why = errors.get(item["sales_order_item"]) or errors[order.name]
			elif rules["enforce_plan"] and not _planned(item):
				why = _("Not in the allocation plan")
			elif not team:
				why = _("No packing team")
			elif (
				usable >= short - 1e-9
				and boxes[_box_key(item)]["coverable"] >= boxes[_box_key(item)]["needed"]
			):
				why = _("Ready to allocate")
			elif usable > 0 and not rules["partial_allocation"] and usable < short:
				why = _("Not enough for the whole line")
			elif usable > 0 and _box_key(item)[0] != "line":
				why = _("Rest of the mix is short")
			elif usable > 0:
				why = _("Partly in stock")
			elif free > 0:
				why = _("Longer stems need a downgrade reason")
			else:
				why = _("No stock")
			out.append(
				{
					"sales_order": order.name,
					"order_name": order.get("custom_order_name") or order_names.get(order.name) or "",
					"per_bunch": flt(item.get("conversion_factor")) or 1,
					"customer": order.customer,
					"delivery_date": order.delivery_date,
					"sequence": cint(order.get("planning_sequence")),
					"sales_order_item": item["sales_order_item"],
					"item_code": item["item_code"],
					"item_name": item.get("item_name") or item["item_code"],
					"length": item.get("required_length") or "",
					"team": team or "",
					"needed": flt(item.get("pending_stock_qty")),
					"allocated": flt(item.get("total_allocated_qty"))
					+ proposed.get(item["sales_order_item"], 0),
					"short": short,
					"per_box": per_box,
					"boxes_short": math.ceil(short / per_box - 1e-9) if per_box > 0 else 0,
					# the line's box (its mix's, for a mixed box or bunch): boxes needed and whole boxes covered
					"group_boxes": boxes[_box_key(item)],
					"order_boxes": order_boxes,
					# a mixed box or bunch is one box across its varieties: count it once per order
					"box_group": "{0}:{1}".format(*_box_key(item)),
					"free": free,
					"usable": usable,
					"why": why,
					"donors": [
						# a longer length the plan confirmed for this line needs no downgrade reason
						{
							**r,
							**_line_boxes(r.sales_order_item, box_memo),
							"longer": r.longer and not _planned_longer(item, r),
						}
						for r in _donors(item, order.name, location_farms)
					],
				}
			)
	return out


def _line_boxes(soi, memo):
	"""{line_allocated, line_per_box, line_boxes}: a donor line's allocated stems and its box, so moving from
	it can say which boxes it leaves open."""
	if soi not in memo:
		row = (
			frappe.db.get_value(
				"Sales Order Item",
				soi,
				[
					"stock_qty AS original_stock_qty",
					"custom_number_of_boxes",
					"custom_mixed_box",
					"custom_mixed_bunch",
					"custom_packrate",
					"custom_packrate_mixed_box",
				],
				as_dict=True,
			)
			or {}
		)
		memo[soi] = {
			"line_allocated": flt(sa._cumulative_allocated_stems(soi)),
			"line_per_box": _per_box(row) if row else 0,
			"line_boxes": cint(row.get("custom_number_of_boxes")),
		}
	return memo[soi]


def _donors(item, sales_order, location_farms):
	"""Buckets of this variety allocated to another order's line, not issued yet, on this location's
	shelves, at the line's length or longer (and its cut stage when the spec names one). Latest delivery
	and last in the sequence first: those are the ones to take from."""
	if not location_farms:
		return []
	rows = frappe.db.sql(
		"""
		SELECT ba.sales_order_item, soi.parent AS sales_order, IFNULL(so.custom_order_name, '') AS order_name,
			so.customer, so.delivery_date,
			0 AS sequence,
			bas.bucket_id, IFNULL(bas.stem_length, '') AS stem_length,
			SUM(ba.quantity_allocated) AS qty,
			(SELECT MIN(si.cut_stage) FROM `tabShelf Item` si
				WHERE si.bucket_id = bas.bucket_id AND si.variety = bas.item_code) AS cut_stage
		FROM `tabBucket Allocations` ba
		INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		INNER JOIN `tabSales Order Item` soi ON soi.name = ba.sales_order_item
		INNER JOIN `tabSales Order` so ON so.name = soi.parent
		WHERE bas.item_code = %(item)s
		  AND soi.parent != %(so)s
		  AND ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0
		  AND so.docstatus = 1 AND so.status NOT IN ('Completed', 'Closed', 'Cancelled')
		  AND EXISTS (SELECT 1 FROM `tabShelf Item` si INNER JOIN `tabShelf` s ON s.name = si.parent
			WHERE si.bucket_id = bas.bucket_id AND si.variety = bas.item_code AND s.farm IN %(farms)s)
		GROUP BY ba.sales_order_item, bas.bucket_id, IFNULL(bas.stem_length, '')
		HAVING qty > 0
		""",
		{"item": item["item_code"], "so": sales_order, "farms": tuple(location_farms)},
		as_dict=True,
	)
	want_cm = sa._parse_cm(item.get("required_length"))
	stage = sa._norm_cut_stage(item.get("custom_cut_stage")) if item.get("custom_cut_stage") else None
	out = []
	for r in rows:
		cm = sa._parse_cm(r.stem_length)
		if not cm or cm < want_cm or (stage and sa._norm_cut_stage(r.cut_stage) != stage):
			continue
		r.longer = cm > want_cm
		r.qty = flt(r.qty)
		out.append(r)
	out.sort(key=lambda r: (str(r.delivery_date), r.sequence), reverse=True)
	return out


@frappe.whitelist(methods=["POST"])
def move_bucket(
	from_item: str,
	bucket_id: str,
	stem_length: str,
	to_item: str,
	location: str,
	downgrade_reason: str | None = None,
	stems: float | None = None,
):
	"""Take a bucket off one order's line and allocate it (up to what is short, and no more than `stems`) to
	another order's line of the same variety; what the move does not take goes back to the first line. If the
	second allocation fails, the bucket goes back to the first line."""
	src = frappe.db.get_value("Sales Order Item", from_item, ["parent", "item_code"], as_dict=True)
	dst = frappe.db.get_value(
		"Sales Order Item", to_item, ["parent", "item_code", "custom_length", "custom_opl"], as_dict=True
	)
	if not src or not dst:
		frappe.throw(_("Order line not found."))
	if src.item_code != dst.item_code:
		frappe.throw(_("Only a bucket of the same variety can be moved."))

	held = flt(
		frappe.db.sql(
			"""SELECT SUM(ba.quantity_allocated) FROM `tabBucket Allocations` ba
			INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
			WHERE ba.sales_order_item = %s AND bas.bucket_id = %s AND IFNULL(bas.stem_length, '') = %s
			  AND ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0""",
			(from_item, bucket_id, stem_length or ""),
		)[0][0]
	)
	if held <= 0:
		frappe.throw(_("{0} is no longer allocated to {1}.").format(bucket_id, src.parent))

	# up to what was ordered: the plan does not limit a move
	need = sa._required_stems_for_so_item(to_item) - flt(sa._cumulative_allocated_stems(to_item))
	if need <= 0:
		frappe.throw(_("{0} is no longer short.").format(dst.parent))

	longer = sa._parse_cm(stem_length) > sa._parse_cm(dst.custom_length)
	if (
		longer
		and not (downgrade_reason or "").strip()
		and any(
			(c.length or "") == (stem_length or "") for c in allocation_plan.live_confirmations(soi=to_item)
		)
	):
		downgrade_reason = _("Planned at {0}").format(stem_length)
	if longer and not (downgrade_reason or "").strip():
		frappe.throw(_("A downgrade reason is required for longer stems."))

	team = (
		frappe.db.get_value("Order Pick List", dst.custom_opl, "team") if dst.custom_opl else None
	) or allocation_plan.planned_teams([to_item]).get(to_item)
	if not team:
		frappe.throw(_("Set a packing team for {0} first.").format(dst.parent))
	src_team = frappe.db.get_value(
		"Order Pick List", frappe.db.get_value("Sales Order Item", from_item, "custom_opl"), "team"
	) or allocation_plan.planned_teams([from_item]).get(from_item)

	def _alloc(soi, qty, length_status, reason):
		row = frappe.db.get_value(
			"Sales Order Item", soi, ["uom", "stock_uom", "conversion_factor"], as_dict=True
		)
		return {
			"item_code": dst.item_code,
			"bucket_id": bucket_id,
			"qty": qty,
			"sales_order_item": soi,
			"stem_length": stem_length or "",
			"warehouse": "",
			"uom": row.uom,
			"stock_uom": row.stock_uom,
			"conversion_factor": row.conversion_factor or 1,
			"length_status": length_status,
			"downgrade_reason": reason,
			"available_exact_stems": 0,
		}

	# whole bunches of the line it goes to
	size = flt(frappe.db.get_value("Sales Order Item", to_item, "conversion_factor")) or 1
	qty = math.floor(min(held, need, flt(stems) or held) / size + 1e-9) * size
	if qty <= 0:
		frappe.throw(_("{0} needs less than one bunch of {1}.").format(dst.parent, int(size)))
	target = [_alloc(to_item, qty, "downgrade" if longer else "exact", (downgrade_reason or "").strip())]
	rules = sa._v2_rules(location)
	if not rules["partial_allocation"]:
		sa._check_whole_lines(dst.parent, target)
	if rules["enforce_plan"]:
		sa._check_against_plan(target, {to_item: team})

	out = sa.unallocate_bucket_from_opl(from_item, bucket_id, stem_length)
	if not out.get("success"):
		frappe.throw(out.get("message") or _("Could not unallocate {0}.").format(bucket_id))

	try:
		sa._allocate_stock_with_buckets_impl(dst.parent, target, location, {to_item: team})
		frappe.db.commit()
		# the rest of the bucket stays with the line it came from, in whole bunches of that line
		src_size = flt(frappe.db.get_value("Sales Order Item", from_item, "conversion_factor")) or 1
		rest = math.floor((held - qty) / src_size + 1e-9) * src_size
		if rest > 0 and src_team:
			src_len = frappe.db.get_value("Sales Order Item", from_item, "custom_length")
			src_longer = sa._parse_cm(stem_length) > sa._parse_cm(src_len)
			sa._allocate_stock_with_buckets_impl(
				src.parent,
				[
					_alloc(
						from_item,
						rest,
						"downgrade" if src_longer else "exact",
						_("Planned at {0}").format(stem_length) if src_longer else "",
					)
				],
				location,
				{from_item: src_team},
			)
			frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		frappe.clear_messages()
		if src_team:
			src_len = frappe.db.get_value("Sales Order Item", from_item, "custom_length")
			src_longer = sa._parse_cm(stem_length) > sa._parse_cm(src_len)
			sa._allocate_stock_with_buckets_impl(
				src.parent,
				[
					_alloc(
						from_item,
						held,
						"downgrade" if src_longer else "exact",
						_("Returned after a failed move") if src_longer else "",
					)
				],
				location,
				{from_item: src_team},
			)
			frappe.db.commit()
		frappe.throw(_("Could not allocate {0} to {1}: {2}").format(bucket_id, dst.parent, str(e)))

	names = dict(
		frappe.db.sql(
			"SELECT name, IFNULL(custom_order_name, '') FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": (src.parent, dst.parent)},
		)
	)
	return {
		"moved": target[0]["qty"],
		"from": names.get(src.parent) or src.parent,
		"to": names.get(dst.parent) or dst.parent,
	}


@frappe.whitelist(methods=["POST"])
def move_from_order(
	from_item: str,
	to_item: str,
	location: str,
	stems: float | None = None,
	downgrade_reason: str | None = None,
):
	"""Move what a short line needs (up to `stems`) from another order's line, bucket by bucket: the
	buckets of that line that fit the short one, as the Shortages tab lists them."""
	dst = frappe.db.get_value(
		"Sales Order Item",
		to_item,
		["parent", "item_code", "custom_length", "custom_cut_stage"],
		as_dict=True,
	)
	if not dst:
		frappe.throw(_("Order line not found."))
	location_farms = sa._get_production_config()["farms_by_location"].get(location) or []
	line = {
		"item_code": dst.item_code,
		"required_length": dst.custom_length,
		"custom_cut_stage": dst.custom_cut_stage,
	}
	buckets = [r for r in _donors(line, dst.parent, location_farms) if r.sales_order_item == from_item]
	if not buckets:
		frappe.throw(_("That order holds no bucket that fits this line any more."))
	left = flt(stems) or None
	moved, names = 0.0, {}
	for b in buckets:
		if left is not None and left <= 0:
			break
		try:
			r = move_bucket(from_item, b.bucket_id, b.stem_length, to_item, location, downgrade_reason, left)
		except frappe.ValidationError:
			# the line is filled, or this bucket no longer fits: what moved so far stays
			frappe.clear_messages()
			if moved:
				break
			raise
		moved += flt(r["moved"])
		if left is not None:
			left -= flt(r["moved"])
		names = r
	return {"moved": moved, "from": names.get("from"), "to": names.get("to"), "buckets": len(buckets)}


@frappe.whitelist(methods=["POST"])
def set_pending_team(location: str, sales_order: str, team: str):
	"""Give every line of an order waiting release that has no team (never planned, none chosen) this team."""
	if not frappe.db.exists("Packing Teams", team):
		frappe.throw(_("{0} is not a packing team.").format(team))
	names = frappe.get_all(
		PENDING, filters={"status": "Pending", "location": location, "sales_order": sales_order}, pluck="name"
	)
	if not names:
		frappe.throw(_("{0} has nothing waiting to be released.").format(sales_order))
	frappe.db.sql(
		"""UPDATE `tabPending Allocation Row` SET team = %(team)s
		WHERE parent IN %(p)s AND parenttype = %(dt)s AND IFNULL(team, '') = ''""",
		{"team": team, "p": tuple(names), "dt": PENDING},
	)
	return {"success": True}


@frappe.whitelist()
def get_shortage_substitutes(sales_order_item: str, location: str):
	"""Varieties a short line could be changed to, with the stems free for it at its length or longer on this
	location's shelves. A line with a specification: the varieties it approves (its primary first). A line
	without one: varieties of the same crop whose stock is past the amber time (non-moving), oldest first."""
	from upande_packhouse.api.v2 import allocation_playground as pg
	from upande_packhouse.api.v2.allocation_teams import crop_of_groups
	from upande_packhouse.api.v2.core import stock

	ln = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		["parent", "item_code", "custom_length", "custom_line", "item_group", "stock_qty"],
		as_dict=True,
	)
	if not ln:
		frappe.throw(_("Order line not found."))
	farms = sa._get_production_config()["farms_by_location"].get(location) or []
	want = sa._parse_cm(ln.custom_length)

	def fits(r):
		return (sa._parse_cm(r.stem_length) or 0) >= (want or 0)

	allocated = flt(sa._cumulative_allocated_stems(sales_order_item))
	approved = []
	if ln.custom_line:
		approved = frappe.db.sql(
			"""SELECT variety, IFNULL(is_primary, 0) AS is_primary FROM `tabSpec Approved Variety`
			WHERE parenttype = 'Specifications' AND parent = %s AND IFNULL(variety, '') != ''
			ORDER BY is_primary DESC, idx""",
			ln.custom_line,
			as_dict=True,
		)
	if approved:
		varieties = [r.variety for r in approved if r.variety != ln.item_code]
		free = defaultdict(float)
		for r in stock.bucket_rows(farms=farms, varieties=varieties or [""]):
			if fits(r):
				free[r.variety] += flt(r.allocatable)
		primary = {r.variety for r in approved if cint(r.is_primary)}
		options = [
			{"variety": v, "free": free[v], "primary": v in primary, "oldest": None}
			for v in dict.fromkeys(varieties)
		]
		source = "spec"
	else:
		amber = pg.amber_from_days()
		groups = crop_of_groups()
		crop = groups.get(ln.item_group)
		free, oldest = defaultdict(float), defaultdict(int)
		for r in stock.bucket_rows(farms=farms):
			if r.variety == ln.item_code or not fits(r) or (r.age_days or 0) < amber:
				continue
			if crop and groups.get(frappe.get_cached_value("Item", r.variety, "item_group")) != crop:
				continue
			free[r.variety] += flt(r.allocatable)
			oldest[r.variety] = max(oldest[r.variety], cint(r.age_days))
		options = [
			{"variety": v, "free": s, "primary": False, "oldest": oldest[v]}
			for v, s in sorted(free.items(), key=lambda kv: (-oldest[kv[0]], -kv[1]))
			if s > 0
		]
		source = "non_moving"
	return {
		"source": source,
		"variety": ln.item_code,
		"length": ln.custom_length,
		"needed": flt(ln.stock_qty),
		"allocated": allocated,
		"options": options,
	}
