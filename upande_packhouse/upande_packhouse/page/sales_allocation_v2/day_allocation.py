"""Sales Allocation v2: allocate every order of a delivery window in one go, then settle what is short
by moving buckets between orders of the same variety."""

import json
import math
import time
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_packhouse import allocation_plan
from upande_packhouse.api.v2.allocation_teams import team_for, teams_by_crop
from upande_packhouse.box_groups import box_keys, mark_boxes
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
	lengths = {}
	sois = [i["sales_order_item"] for i in items]
	if sois:
		for c in frappe.get_all(
			"Allocation Confirmation",
			filters={"status": ("in", allocation_plan.LIVE), "sales_order_item": ("in", sois)},
			fields=["sales_order_item", "variety", "length"],
		):
			if c.length:
				lengths.setdefault((c.sales_order_item, c.variety), set()).add(c.length)
	for item in items:
		item["plan_lengths"] = lengths.get((item["sales_order_item"], item["item_code"])) or None
	# a spec's mono and mixed bunches are one box
	mark_boxes(items)


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
	"""Lines packed in the same box (box_groups): the key set on the line from its order's lines, else a
	mixed bunch's group, a mixed box's group, or the line on its own."""
	if item.get("_box"):
		return tuple(item["_box"])
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


def _new_boxes(members, picked):
	"""Complete boxes the picks add to a box group: every line of a mix as far as its scarcest line goes."""

	def boxes(extra):
		counts = [
			int((flt(m.get("total_allocated_qty")) + extra(m)) / _per_box(m) + 1e-9)
			for m in members
			if _per_box(m) > 0
		]
		return min(counts) if counts else 0

	return max(0, boxes(lambda m: picked.get(m["sales_order_item"], 0)) - boxes(lambda m: 0))


def _planned(item):
	return flt(item.get("confirmed_stems")) > 0


def _team(item, planned, rules):
	team = item.get("opl_team") or item.get("planning_team") or planned.get(item["sales_order_item"])
	if team or rules["enforce_plan"]:
		return team
	groups = rules.setdefault("item_groups", {})
	if item["item_code"] not in groups:
		groups[item["item_code"]] = frappe.get_cached_value("Item", item["item_code"], "item_group")
	crop = rules["crop_of"].get(groups[item["item_code"]])
	return team_for(item.get("customer"), crop, rules["by_crop"], rules["customer_teams"])


def _orders(location, delivery_start, delivery_end):
	"""Every open order of the window, in allocation order: Allocation Planning's sequence, every team's
	#1 first."""
	return sa.get_pending_sales_orders(
		delivery_start=delivery_start, delivery_end=delivery_end or delivery_start, location=location
	)


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
		# a remote farm's bucket is requested whole: once one order takes it, no other order does
		owner = ("remote_owner", b["bucket_id"])
		remote = item.get("sales_order") and not cint(b.get("is_sales_shelf"))
		if remote and taken.get(owner, item["sales_order"]) != item["sales_order"]:
			continue
		free = flt(b.get("available_qty")) - taken.get(key, 0)
		if free <= 0:
			continue
		# whole bunches only: a bunch is never split, so a bucket gives what fills whole bunches
		per_bunch = flt(item.get("conversion_factor")) or 1
		qty = math.floor(min(free, left) / per_bunch + 1e-9) * per_bunch
		if qty <= 0:
			continue
		taken[key] = taken.get(key, 0) + qty
		if remote:
			taken[owner] = item["sales_order"]
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
	bypass_orders: str | list | None = None,
	box_type: str | None = None,
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
	if isinstance(bypass_orders, str):
		bypass_orders = json.loads(bypass_orders or "[]")
	return _run_day(
		location,
		delivery_start,
		delivery_end,
		_farms(selected_farms),
		frappe.session.user,
		set(bypass_orders or ()),
		box_type if box_type in ("straight", "mixed") else None,
	)


#: Release starts at once, in the request, and goes on there for up to this many seconds (each order posts
#: stock entries and a pick list, a few seconds each); the orders left after that go to the background worker,
#: and so do the closing shortages when the orders alone used the budget up (the request must end in time).
RELEASE_BUDGET = 60


#: A run that has reported nothing for this long is taken as dead (its request was killed, or the server
#: restarted under it): its lock no longer holds the location.
RUN_SILENT_SEC = 300


def _run_dead(location):
	"""The location's run stopped reporting: clear its lock and state so the page and a new run go on."""
	state = frappe.cache.get_value(f"sa2_day_state:{location}") or {}
	if state.get("job_id"):
		return False
	if state.get("at") and time.time() - flt(state["at"]) < RUN_SILENT_SEC:
		return False
	frappe.cache.delete_value(f"sa2_day_allocation:{location}")
	frappe.cache.delete_value(f"sa2_day_state:{location}")
	return True


def _lock(location, phase="propose"):
	lock = f"sa2_day_allocation:{location}"
	if frappe.cache.get_value(lock) and not _run_dead(location):
		frappe.throw(_("Orders at {0} are already being allocated.").format(location))
	frappe.cache.set_value(lock, frappe.session.user, expires_in_sec=1800)
	frappe.cache.delete_value(f"sa2_day_done:{location}")
	# not started yet: the bar shows from the start, also after a refresh
	_set_state(location, {"phase": phase, "progress": 0, "total": 1, "order": ""})


def _set_state(location, state):
	frappe.cache.set_value(f"sa2_day_state:{location}", {**state, "at": time.time()}, expires_in_sec=1800)


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
				"location": location,
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


def _save_pending(location, sales_order, allocations, teams, boxes, source, order=None):
	"""Write (or replace) an order's open Pending Allocation at the location, holding only the rows that complete
	boxes; none left when no box is complete. `order` ({customer, delivery_date}, already read) spares looking the
	links up again."""
	name = frappe.db.get_value(
		PENDING, {"sales_order": sales_order, "location": location, "status": "Pending"}, "name"
	)
	# only complete boxes are held: a straight line's full boxes, a mixed box or bunch once every variety in
	# it fills it. Stems short of a box stay with the planner's page, not on the buckets.
	if allocations:
		allocations, _short, by_box = _split_release(sales_order, allocations)
		boxes = sum(by_box.values())
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
	if order:
		doc.customer = order["customer"]
		doc.delivery_date = order["delivery_date"]
		doc.flags.ignore_links = True
	doc.flags.ignore_permissions = True
	doc.save()
	return doc.name


def _order_location(sales_order):
	"""The location (sales farm) an order is listed under, as the order list puts it (its farm's, else the
	first sales farm of its farm's Farm Location), or "" when its farm has none."""
	from upande_packhouse.api.remote_transfer.transfer_scheduling import _so_farm_expr

	farm = frappe.db.sql(
		"SELECT " + _so_farm_expr("so") + " FROM `tabSales Order` so WHERE so.name = %s", sales_order
	)
	return sa._order_farm_location(farm[0][0] if farm else None)


def _check_location(sales_order, location):
	"""Locations stand alone: an order is allocated only at its own location."""
	own = _order_location(sales_order)
	if own and location and own != location:
		frappe.throw(_("{0} belongs to {1}, not {2}.").format(sales_order, own, location))


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
	if allocations:
		_check_location(sales_order, location)
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


def _release_ok(boxes, full_before, required):
	"""Whether an order's complete boxes go out now. Partial allocation off: only once every box is complete.
	On: once the share of its boxes complete after the release reaches Production Settings' percentage (0:
	any complete box)."""
	if boxes <= 0:
		return False
	required = cint(required)
	if not required:
		return True
	after = min(required, cint(full_before) + cint(boxes))
	ps = frappe.get_cached_doc("Production Settings")
	allow = ps.get("allow_partial_allocation")
	if allow is not None and not cint(allow):
		return after >= required
	return after * 100.0 >= flt(ps.get("partial_allocation_release_percent")) * required - 1e-9


def _drop_gone_buckets(o, location):
	"""Take off an order's allocation the rows whose bucket is no longer on a shelf with that variety and length
	(issued to another pick list, cleared, or the label reused since): the order's other rows still release.
	Saves the trimmed allocation waiting release and returns the buckets taken off."""
	rows = o.get("allocations") or []
	buckets = list({a.get("bucket_id") for a in rows if a.get("bucket_id")})
	if not buckets:
		return []
	on_shelf = {
		(r.bucket_id, r.variety, r.stem_length or "")
		for r in frappe.db.sql(
			"""SELECT si.bucket_id, si.variety, IFNULL(si.stem_length, '') AS stem_length
			   FROM `tabShelf Item` si WHERE si.bucket_id IN %(b)s AND IFNULL(si.stem_qty, 0) > 0""",
			{"b": tuple(buckets)},
			as_dict=True,
		)
	}
	gone = [
		a
		for a in rows
		if (a.get("bucket_id"), a.get("item_code"), a.get("stem_length") or "") not in on_shelf
	]
	if not gone:
		return []
	o["allocations"] = [a for a in rows if a not in gone]
	if o["allocations"]:
		_save_pending(
			location,
			o["sales_order"],
			o["allocations"],
			o.get("teams") or {},
			_pending_boxes(o["allocations"]),
			o.get("source") or "Picked",
		)
	else:
		sa.close_pending(o["sales_order"], location)
	frappe.db.commit()  # nosemgrep -- the trimmed allocation stands whatever the release does next
	return sorted({a["bucket_id"] for a in gone})


def _gate_release(release, keep, boxes, full_before, required):
	"""(release, keep, boxes) as the release sends it out. With partial allocation on (Production Settings), a
	straight line is one variety: its complete boxes always go out, each straight line a pick list of its own.
	The release percentage holds back only the mixed boxes and bunches, until the order reaches it. With partial
	allocation off the order goes out only once every box is complete."""
	if not release or _release_ok(sum(boxes.values()), full_before, required):
		return release, keep, boxes
	# partial allocation off: the order goes out only whole
	allow = frappe.get_cached_doc("Production Settings").get("allow_partial_allocation")
	if allow is not None and not cint(allow):
		return [], keep + release, {}
	straight = {k[1] for k in boxes if k[0] == "line" and boxes[k] > 0}
	out = [a for a in release if a["sales_order_item"] in straight]
	if not out:
		return [], keep + release, {}
	return (
		out,
		keep + [a for a in release if a["sales_order_item"] not in straight],
		{k: v for k, v in boxes.items() if k[0] == "line" and k[1] in straight},
	)


def _summary(p):
	orders = p["orders"] if p else []
	# each order's boxes: how many it needs, and how many will be full once this is released -- counted
	# exactly as the release counts them, so a box it would hold back never shows as full
	rows = [frappe._dict(name=o["sales_order"]) for o in orders]
	sa._attach_boxes(rows)
	for o, r in zip(orders, rows, strict=True):
		o["_release"], _keep, o["_box_groups"] = _gate_release(
			*_split_release(o["sales_order"], o["allocations"]), r.boxes_full, r.boxes_required
		)
		o["boxes"] = sum(o["_box_groups"].values())
		o["order_boxes"] = cint(r.boxes_required)
		o["full_before"] = min(cint(r.boxes_required), cint(r.boxes_full))
		o["full_after"] = min(cint(r.boxes_required), cint(r.boxes_full) + cint(o["boxes"]))
		o["releasable"] = 1 if o["_release"] else 0
		# the order lines (a straight line, a mixed box or bunch) whose complete boxes this release sends
		o["release_lines"] = sum(1 for v in o["_box_groups"].values() if v > 0)
		# the release shows only what goes out: the complete boxes and their stems
		o["release_stems"] = sum(flt(a.get("qty")) for a in o["_release"])
	return {
		"orders": [
			{
				**{k: v for k, v in o.items() if k not in ("_box_groups", "_release")},
				"lines": _line_stems(o["_release"]),
				"team_names": _team_names(o),
				# each line shows the team release gives it: chosen, planned, the order's other pick lists,
				# else the sales farm's only team -- nobody has to add a team where there is just one
				"groups": [
					g
					for g in _release_groups(
						{**o, "allocations": o["_release"], "teams": _fill_teams(o)}, o["_box_groups"]
					)
					if g.get("boxes")
				],
			}
			for o in orders
		],
		"boxes": sum(o["boxes"] for o in orders),
		"stems": sum(o["stems"] for o in orders),
	}


def _split_release(sales_order, allocations):
	"""(release, keep, boxes): the allocation rows that complete boxes -- a straight line's own boxes, a mixed
	box (its mono and mixed bunches, box_groups) only as many as its scarcest line completes, counting what is
	already on pick lists -- the rest, which stays allocated waiting for the stock that completes them, and
	{box group key: boxes the release completes}. Stems a line no longer needs (already on its pick lists)
	are in neither."""
	items = frappe.get_all(
		"Sales Order Item",
		filters={"parent": sales_order},
		fields=[
			"name",
			"parent",
			"stock_qty",
			"qty",
			"conversion_factor",
			"custom_ordered_quantity",
			"custom_number_of_boxes",
			"custom_packrate",
			"custom_packrate_mixed_box",
			"custom_mixed_box",
			"custom_mix_group",
			"custom_mix_name",
			"custom_line",
			"custom_length",
			"custom_mixed_bunch",
			"custom_bunch_group",
		],
	)
	keys = box_keys(items)
	asked = defaultdict(float)
	for a in allocations:
		asked[a["sales_order_item"]] += flt(a.get("qty"))
	per, placed, boxes_of, still = {}, {}, {}, {}
	for it in items:
		n = cint(it.custom_number_of_boxes) or 1
		per[it.name] = sa._line_capacity(it) / n
		placed[it.name] = sa._placed_stems(it.name) if asked.get(it.name) or keys[it.name][0] != "line" else 0
		boxes_of[it.name] = n
		# what the line can still take: its boxes (or what was ordered, when that is more) less its pick lists
		still[it.name] = max(0.0, max(sa._line_capacity(it), flt(it.stock_qty)) - placed[it.name])
	groups = defaultdict(list)
	for it in items:
		groups[keys[it.name]].append(it.name)

	def filled(names, extra):
		return min(
			min(boxes_of[n], int((placed[n] + extra(n)) / per[n] + 1e-9)) if per[n] > 0 else boxes_of[n]
			for n in names
		)

	room, boxes = {}, {}
	for key, names in groups.items():
		if not any(asked.get(n) for n in names):
			continue
		full = filled(names, lambda n: min(asked.get(n, 0), still[n]))
		boxes[key] = max(0, full - filled(names, lambda n: 0))
		for n in names:
			room[n] = max(0.0, full * per[n] - placed[n]) if per[n] > 0 else asked.get(n, 0)
	release, keep = [], []
	for a in allocations:
		soi = a["sales_order_item"]
		qty = flt(a.get("qty"))
		size = flt(a.get("conversion_factor")) or 1
		take = math.floor(min(qty, room.get(soi, 0)) / size + 1e-9) * size
		if take > 0:
			release.append({**a, "qty": take})
			room[soi] = room.get(soi, 0) - take
			still[soi] = still.get(soi, 0) - take
		rest = min(qty - take, max(0.0, still.get(soi, 0)))
		if rest > 1e-9:
			keep.append({**a, "qty": rest})
			still[soi] = still.get(soi, 0) - rest
	return release, keep, boxes


def _release_groups(order, boxes=None):
	"""What the order releases, as it is packed: one entry per mixed box or bunch (its varieties together,
	in the complete boxes its scarcest variety fills) and one per straight line. Each: {kind, label, boxes,
	stems, lines: [{variety, length, stems, boxes}]}. `boxes` ({box group key: boxes}, from _split_release)
	are the boxes the release completes."""
	stems = _line_stems(order["allocations"])
	if not stems:
		return []
	lines = frappe.get_all(
		"Sales Order Item",
		filters={"parent": order["sales_order"]},
		fields=[
			"name",
			"parent",
			"idx",
			"item_code",
			"custom_length",
			"stock_qty",
			"custom_number_of_boxes",
			"custom_packrate",
			"custom_packrate_mixed_box",
			"custom_mixed_box",
			"custom_mix_group",
			"custom_mix_name",
			"custom_mixed_bunch",
			"custom_bunch_group",
		],
		order_by="idx",
	)
	keys = box_keys(lines)
	lines = [ln for ln in lines if ln.name in stems]
	groups = {}
	for ln in lines:
		key = keys[ln.name]
		per = _per_box({**ln, "original_stock_qty": ln.stock_qty}) or 0
		if key[0] == "line":
			label = "{0} {1}".format(ln.item_code, ln.custom_length or "").strip()
		elif key[0] == "box":
			label = ln.custom_mix_name or _("Mixed box {0}").format(key[1])
		else:
			label = ln.custom_mix_name or _("Mixed bunch {0}").format(str(key[1]).rsplit("::", 1)[-1])
		g = groups.setdefault(
			key,
			{
				"kind": key[0],
				"label": label,
				"stems": 0.0,
				"lines": [],
				"sois": [],
				"key": key,
				"idx": ln.idx,
				"_fills": [],
			},
		)
		g["sois"].append(ln.name)
		s = flt(stems.get(ln.name))
		# boxes as the proposal counts them: a part box that completes the line counts
		fills = math.ceil(s / per - 1e-9) if per > 0 else 0
		g["stems"] += s
		g["_fills"].append(fills)
		g["lines"].append(
			{"variety": ln.item_code, "length": ln.custom_length or "", "stems": s, "boxes": fills}
		)
	out = []
	for g in sorted(groups.values(), key=lambda x: x["idx"]):
		fills = g.pop("_fills")
		g.pop("idx")
		# every variety of a mix shares the same boxes
		key = g.pop("key")
		g["boxes"] = (boxes or {}).get(key, 0) if boxes is not None else (max(fills) if fills else 0)
		# the group's team: the one its lines go to ("" while any of them has none)
		teams = {(order.get("teams") or {}).get(x) or "" for x in g["sois"]}
		g["team"] = teams.pop() if len(teams) == 1 else ""
		out.append(g)
	return out


def _fill_teams(order, location=None):
	"""{sales_order_item: team} for every line the order releases: the one chosen for the line, else its
	planned team, else the team most of the order's other lines go to (one order is packed by one team),
	else the team packing its other pick lists, else its sales farm's packing team when it has just one.
	A line is left out only when no line of the order has a team."""
	lines = list(dict.fromkeys(a["sales_order_item"] for a in order["allocations"]))
	chosen = {k: v for k, v in (order.get("teams") or {}).items() if v}
	planned = allocation_plan.planned_teams([x for x in lines if not chosen.get(x)])
	teams = {x: chosen.get(x) or planned.get(x) for x in lines if chosen.get(x) or planned.get(x)}
	if not teams and order.get("sales_order"):
		# nothing chosen or planned: the team already packing the order's other pick lists
		from collections import Counter

		had = Counter(
			t
			for t in frappe.get_all(
				"Order Pick List",
				filters={"sales_order": order["sales_order"], "docstatus": ["<", 2]},
				pluck="team",
			)
			if t
		)
		if had:
			teams = {x: had.most_common(1)[0][0] for x in lines}
	if not teams and order.get("sales_order"):
		# still none: the one packing team of the order's sales farm (Packing Teams > Sales Farm)
		from upande_packhouse.api.remote_transfer.transfer_scheduling import order_hub
		from upande_packhouse.api.v2.allocation_teams import team_sales_farms

		farm = order_hub(order["sales_order"], location or order.get("location"))
		here = [t for t, f in team_sales_farms().items() if farm and f == farm]
		if len(here) == 1:
			teams = {x: here[0] for x in lines}
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
LIGHT = ("error", "errors", "saved", "boxes", "stems", "new_orders", "new_boxes", "new_stems", "waiting")


def _light(result):
	return {k: result[k] for k in LIGHT if k in result} | {"orders_count": len(result.get("orders") or [])}


def _publish(user, location, **msg):
	"""Tell the page how the job is going, and keep where it is so a page opened or refreshed while it runs
	picks the bar up again (get_day_state)."""
	key = f"sa2_day_state:{location}"
	if msg.get("done"):
		frappe.cache.delete_value(key)
		# the page also asks (get_day_state) instead of waiting on realtime, which a web page may not have
		frappe.cache.set_value(f"sa2_day_done:{location}", dict(msg), expires_in_sec=600)
	else:
		_set_state(
			location, {k: msg.get(k) for k in ("phase", "stage", "progress", "total", "order", "planned")}
		)
	frappe.publish_realtime("sa2_day_allocation", msg, user=user)


@frappe.whitelist()
def get_day_state(location: str, since: str | None = None):
	"""Where a running Allocate all or Release at this location is ({phase, progress, total, order}), or None.
	With `since` (the page's own run), a run that has finished answers {done, ...counts} instead."""
	if not frappe.cache.get_value(f"sa2_day_allocation:{location}") or _run_dead(location):
		if since:
			done = frappe.cache.get_value(f"sa2_day_done:{location}")
			if done and str(done.get("run") or "") == since:
				return done
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
	# jobs before it in the queue (0: it is next)
	return {"queued": True, "ahead": job.get_position() or 0}


def _is_mixed_line(it):
	return bool(cint(it.get("custom_mixed_box")) or cint(it.get("custom_mixed_bunch")))


def _run_day(location, delivery_start, delivery_end, farms, user, bypass_orders=(), box_type=None):
	"""`box_type` "straight" or "mixed": only those lines are allocated; the other type's rows of earlier
	Allocate all proposals are kept as they were (their buckets stay theirs)."""
	steps = {"total": 1}

	# allocating is the first half of the bar, the shortages check the second
	def plan_progress(done, total, order, planned=None):
		steps["total"] = 2 * total + 1
		_publish(
			user,
			location,
			phase="propose",
			stage="allocate",
			progress=done,
			total=steps["total"],
			order=order,
			planned=planned or {},
		)

	def short_progress(done, total, order):
		_publish(
			user,
			location,
			phase="propose",
			stage="shortages",
			progress=total + 1 + done,
			total=2 * total + 1,
			order=order,
		)

	try:
		_publish(user, location, phase="propose", stage="prepare", progress=0, total=1, order="")
		# a new run replaces the last Allocate all proposal; hand-picked orders keep theirs and are skipped
		stale = frappe.get_all(
			PENDING,
			filters={
				"status": "Pending",
				"location": location,
				"source": "Allocate all",
				"delivery_date": ("between", _window(delivery_start, delivery_end)),
			},
			pluck="name",
		)
		# one box type only: the other type's rows of those proposals are kept, and their buckets are not offered
		kept, kept_taken = defaultdict(list), {}
		if box_type and stale:
			rows = frappe.get_all(
				"Pending Allocation Row",
				filters={"parent": ("in", stale), "parenttype": PENDING},
				fields=[
					"parent",
					"sales_order_item",
					"item_code",
					"bucket_id",
					"stem_length",
					"qty",
					"team",
					"length_status",
					"downgrade_reason",
					"warehouse",
					"uom",
					"stock_uom",
					"conversion_factor",
				],
			)
			flags = {
				r.name: r
				for r in frappe.get_all(
					"Sales Order Item",
					filters={"name": ("in", list({r.sales_order_item for r in rows}) or [""])},
					fields=["name", "parent", "custom_mixed_box", "custom_mixed_bunch"],
				)
			}
			for r in rows:
				line = flags.get(r.sales_order_item)
				if not line or (_is_mixed_line(line) == (box_type == "mixed")):
					continue
				kept[line.parent].append(dict(r))
				k = (r.bucket_id, r.item_code, r.stem_length or "")
				kept_taken[k] = kept_taken.get(k, 0.0) + flt(r.qty)
		if stale:
			# nothing links to a proposal: its rows and itself go in two statements
			frappe.db.delete("Pending Allocation Row", {"parent": ("in", stale), "parenttype": PENDING})
			frappe.db.delete(PENDING, {"name": ("in", stale)})
		# the orders still holding a proposal (picked by hand) -- names only, no need to read them whole
		skip = set(
			frappe.get_all(
				PENDING,
				filters={
					"status": "Pending",
					"location": location,
					"delivery_date": ("between", _window(delivery_start, delivery_end)),
				},
				pluck="sales_order",
			)
		)
		window_orders = _orders(location, delivery_start, delivery_end)
		# the orders the planner confirmed take buckets of any cut stage; the rest only their own
		was_type, frappe.flags.sa2_box_type = frappe.flags.sa2_box_type, box_type
		try:
			orders, errors, lines, taken = _plan_day(
				location,
				delivery_start,
				delivery_end,
				farms,
				plan_progress,
				skip,
				window_orders,
				taken=kept_taken,
				bypass_orders=bypass_orders,
			)
		finally:
			frappe.flags.sa2_box_type = was_type
		_publish(
			user,
			location,
			phase="propose",
			stage="save",
			progress=len(window_orders),
			total=steps["total"],
			order="",
		)
		for o in orders:
			other = kept.pop(o["sales_order"], [])
			_save_pending(
				location,
				o["sales_order"],
				o["allocations"] + other,
				{
					**{r["sales_order_item"]: r.get("team") for r in other if r.get("team")},
					**(o["teams"] or {}),
				},
				o["boxes"],
				"Allocate all",
				order=o,
			)
		# the other type's boxes of orders this run gave nothing to
		for so, rows in kept.items():
			_save_pending(
				location,
				so,
				rows,
				{r["sales_order_item"]: r.get("team") for r in rows if r.get("team")},
				0,
				"Allocate all",
			)
		# the proposal is what the planner reviews next: it must outlive the job
		frappe.db.commit()  # nosemgrep
		p = _proposal(location, delivery_start, delivery_end)
		result = {
			**_summary(p),
			"new_orders": len(orders),
			"new_boxes": sum(o["boxes"] for o in orders),
			"new_stems": sum(o["stems"] for o in orders),
			"shortages": _shortages(
				location,
				delivery_start,
				delivery_end,
				farms,
				errors,
				_pending(p),
				orders=window_orders,
				lines=lines,
				held_all=taken,
				progress=short_progress,
			),
		}
		_keep_shortages(location, delivery_start, delivery_end, farms, result["shortages"])
		# the steps that follow (cut stage, substitutes, downgrades) are read in the background, so this
		# request ends as soon as the allocation is saved and the steps open at once when they are ready
		frappe.enqueue(
			f"{__name__}._precompute_job",
			queue="short",
			enqueue_after_commit=True,
			location=location,
			delivery_start=delivery_start,
			delivery_end=delivery_end,
			farms=farms,
		)
	except Exception as e:
		frappe.log_error("Sales Allocation v2: auto-allocate all failed", frappe.get_traceback())
		result = {"error": str(e)}
	finally:
		frappe.cache.delete_value(f"sa2_day_allocation:{location}")
	_publish(user, location, phase="propose", done=True, **_light(result))
	return {"phase": "propose", "done": True, **result}


def _downgrade_options(lines, proposed, taken):
	"""Every line still short after both passes that longer stems on the shelves could fill: [{sales_order,
	order_name, customer, sales_order_item, variety, ordered_length, length, stems, buckets: [{bucket_id,
	qty}], shorts: [{order_name, stems}]}]. `shorts`: the orders that ordered that longer length themselves
	and would be left short if it went to this line (what the longer length has beyond their own need is
	given first)."""
	free, need_at = {}, defaultdict(float)
	for _so, (order, items) in lines.items():
		for item in items:
			left = _left(item) - proposed.get(item["sales_order_item"], 0)
			if left > 1e-9:
				need_at[(item["item_code"], item.get("required_length") or "")] += left
			for b in item.get("batches") or []:
				key = (b["bucket_id"], item["item_code"], b.get("stem_length") or "")
				if key not in free:
					free[key] = max(0.0, flt(b.get("available_qty")) - taken.get(key, 0))
	# what each longer length has left once the orders that ordered it are covered
	stock_at = defaultdict(float)
	for (_b, v, length), q in free.items():
		stock_at[(v, length)] += q
	spare = {k: max(0.0, q - need_at.get(k, 0)) for k, q in stock_at.items()}
	who_at = defaultdict(list)
	for _so, (order, items) in lines.items():
		for item in items:
			left = _left(item) - proposed.get(item["sales_order_item"], 0)
			if left > 1e-9:
				who_at[(item["item_code"], item.get("required_length") or "")].append(
					(order.get("custom_order_name") or order.name, left)
				)
	out, used = [], defaultdict(float)
	for _so, (order, items) in lines.items():
		for item in items:
			left = _left(item) - proposed.get(item["sales_order_item"], 0)
			if left <= 1e-9:
				continue
			per_bunch = flt(item.get("conversion_factor")) or 1
			lengths = sorted(
				{
					b.get("stem_length") or ""
					for b in item.get("batches") or []
					if b.get("length_status") == "downgrade"
					and b.get("downgrade_approval") != "amber_expired"
				},
				key=lambda x: sa._parse_cm(x) or 0,
			)
			for length in lengths:
				if left <= 1e-9:
					break
				buckets, got = [], 0.0
				for b in item.get("batches") or []:
					if b.get("length_status") != "downgrade" or (b.get("stem_length") or "") != length:
						continue
					key = (b["bucket_id"], item["item_code"], length)
					avail = free.get(key, 0) - used[key]
					take = min(avail, left - got)
					if take <= 0:
						continue
					buckets.append({"bucket_id": b["bucket_id"], "qty": take, "_key": key})
					got += take
					if got >= left - 1e-9:
						break
				stems = math.floor(got / per_bunch + 1e-9) * per_bunch
				if stems <= 0:
					continue
				# trim the buckets to whole bunches
				over = got - stems
				while over > 1e-9 and buckets:
					cut = min(over, buckets[-1]["qty"])
					buckets[-1]["qty"] -= cut
					over -= cut
					if buckets[-1]["qty"] <= 1e-9:
						buckets.pop()
				for x in buckets:
					used[x.pop("_key")] += x["qty"]
				k = (item["item_code"], length)
				short_by = max(0.0, stems - spare.get(k, 0))
				spare[k] = max(0.0, spare.get(k, 0) - stems)
				shorts = []
				if short_by > 0:
					rest = short_by
					for name, n in who_at.get(k, []):
						if rest <= 0:
							break
						shorts.append({"order_name": name, "stems": min(n, rest)})
						rest -= min(n, rest)
				out.append(
					{
						"sales_order": order.name,
						"order_name": order.get("custom_order_name") or order.name,
						"customer": order.customer,
						"sales_order_item": item["sales_order_item"],
						"variety": item["item_code"],
						"ordered_length": item.get("required_length") or "",
						"length": length,
						"stems": stems,
						"buckets": buckets,
						"shorts": shorts,
					}
				)
				left -= stems
	return out


def _cut_stage_options(more, lines):
	"""The orders the cut-stage pass could fill, each line with the stems it would take and the cut stages
	of those buckets against the one its specification asks for: [{sales_order, order_name, customer, boxes,
	stems, lines: [{variety, length, cut_stage, stages, stems}], allocations}]."""
	stage_of = {}
	for _so, (_order, items) in lines.items():
		for item in items:
			for b in item.get("batches") or []:
				stage_of[b["bucket_id"]] = b.get("cut_stage") or ""
	out = []
	for o in more:
		order, items = lines.get(o["sales_order"], (None, []))
		by_line = {i["sales_order_item"]: i for i in items}
		per = defaultdict(lambda: {"stems": 0.0, "stages": set()})
		for a in o["allocations"]:
			x = per[a["sales_order_item"]]
			x["stems"] += flt(a["qty"])
			if stage_of.get(a["bucket_id"]):
				x["stages"].add(stage_of[a["bucket_id"]])
		out.append(
			{
				"sales_order": o["sales_order"],
				"order_name": (order or {}).get("custom_order_name") or o["sales_order"],
				"customer": o["customer"],
				"boxes": o["boxes"],
				"stems": o["stems"],
				"lines": [
					{
						"sales_order_item": soi,
						"variety": (by_line.get(soi) or {}).get("item_code") or "",
						"length": (by_line.get(soi) or {}).get("required_length") or "",
						"cut_stage": (by_line.get(soi) or {}).get("custom_cut_stage") or "",
						"stages": sorted(x["stages"]),
						"stems": x["stems"],
					}
					for soi, x in per.items()
				],
				"allocations": o["allocations"],
			}
		)
	return out


def _free_for(location, sales_order, bypass=True):
	"""{(bucket, variety, length): stems still free for this order's lines} as allocation reads it now."""
	return {
		(b["bucket_id"], i["item_code"], b.get("stem_length") or ""): flt(b.get("available_qty"))
		for i in sa.get_sales_order_items_with_buckets(
			sales_order, location, bypass_cut_stage=1 if bypass else 0
		)
		or []
		for b in i.get("batches") or []
	}


def _left_on_buckets(keys):
	"""{(bucket, variety, length): stems no one holds}: on the shelf, less outstanding allocations and every
	allocation waiting release -- this order's own included. The order's own view (get_sales_order_items_
	with_buckets) leaves its own waiting rows in, so a step adding to an order counted them twice."""
	keys = {k for k in keys if k and k[0]}
	if not keys:
		return {}
	buckets = tuple({k[0] for k in keys})
	shelf = defaultdict(float)
	for b, v, ln, q in frappe.db.sql(
		"""SELECT si.bucket_id, si.variety, IFNULL(si.stem_length, ''), SUM(si.stem_qty) FROM `tabShelf Item` si
		WHERE si.parenttype = 'Shelf' AND si.bucket_id IN %(b)s GROUP BY si.bucket_id, si.variety, IFNULL(si.stem_length, '')""",
		{"b": buckets},
	):
		shelf[(b, v, ln)] += flt(q)
	held = defaultdict(float)
	for b, v, ln, q in frappe.db.sql(
		"""SELECT bas.bucket_id, bas.item_code, IFNULL(bas.stem_length, ''), SUM(ba.quantity_allocated)
		FROM `tabBucket Allocations` ba JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		WHERE bas.bucket_id IN %(b)s AND ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0
		GROUP BY bas.bucket_id, bas.item_code, IFNULL(bas.stem_length, '')""",
		{"b": buckets},
	):
		held[(b, v, ln)] += flt(q)
	for key, rows in sa.pending_holds(set(buckets)).items():
		held[key] += sum(flt(q) for _so, q in rows)
	return {k: max(0.0, shelf.get(k, 0) - held.get(k, 0)) for k in keys}


def _add_checked(location, sales_order, rows):
	"""Add allocation rows to an order's Allocate all proposal, each cut to what its bucket still has free
	(something taken since is not taken twice). Returns the stems added."""
	free, keep = _free_for(location, sales_order), []
	left = _left_on_buckets(free.keys())
	free = {k: min(v, left.get(k, 0)) for k, v in free.items()}
	for r in rows:
		key = (r["bucket_id"], r["item_code"], r.get("stem_length") or "")
		size = flt(r.get("conversion_factor")) or 1
		qty = math.floor(min(flt(r.get("qty")), free.get(key, 0)) / size + 1e-9) * size
		if qty <= 0:
			continue
		free[key] -= qty
		keep.append({**r, "qty": qty})
	if keep:
		_add_pending(location, sales_order, keep, {}, source="Allocate all")
	return sum(flt(r["qty"]) for r in keep)


def _bypass_plan(location, delivery_start, delivery_end, farms, only=None):
	"""After Allocate all is saved: what buckets of any cut stage add to the window's orders on top of their
	proposal (`only`: just these orders). Orders holding picks are left alone."""
	skip = set(
		frappe.get_all(
			PENDING,
			filters={
				"status": "Pending",
				"location": location,
				"source": "Picked",
				"delivery_date": ("between", _window(delivery_start, delivery_end)),
			},
			pluck="sales_order",
		)
	)
	orders = [
		o
		for o in _orders(location, delivery_start, delivery_end)
		if o.name not in skip and (only is None or o.name in only)
	]
	prior = defaultdict(float)
	for o in orders:
		for soi in frappe.get_all("Sales Order Item", filters={"parent": o.name}, pluck="name"):
			prior[soi] = _pending_stems(soi)
	out, _errors, lines, _taken = _plan_day(
		location, delivery_start, delivery_end, farms, None, (), orders, bypass=True, prior=prior
	)
	# only buckets of another cut stage than the line asks for: stock at the right cut stage is the normal
	# allocation's, never something to bypass for
	stage_of, want = {}, {}
	for _so, (_order, items) in lines.items():
		for item in items:
			# spec_cut_stage: the line's cut stage where it applies (Standard Roses only)
			want[item["sales_order_item"]] = sa._norm_cut_stage(item.get("spec_cut_stage") or "")
			for b in item.get("batches") or []:
				stage_of[b["bucket_id"]] = sa._norm_cut_stage(b.get("cut_stage") or "")
	kept = []
	for o in out:
		o["allocations"] = [
			a
			for a in o["allocations"]
			if want.get(a["sales_order_item"])
			and stage_of.get(a["bucket_id"], want[a["sales_order_item"]]) != want[a["sales_order_item"]]
		]
		if o["allocations"]:
			o["stems"] = sum(flt(a["qty"]) for a in o["allocations"])
			kept.append(o)
	return kept, lines


@frappe.whitelist()
def get_cut_stage_candidates(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	selected_farms: str | list | None = None,
	sales_orders: str | list | None = None,
):
	"""After Allocate all: the orders still short that buckets of another cut stage would fill, for the
	planner to confirm. Nothing is saved. `sales_orders`: just these (Auto-allocate order)."""
	farms = _farms(selected_farms)
	only = _only(sales_orders)
	if only:
		return _cut_stage_candidates(location, delivery_start, delivery_end, farms, only)
	return _kept_step(
		"cutstage",
		location,
		delivery_start,
		delivery_end,
		farms,
		lambda: _cut_stage_candidates(location, delivery_start, delivery_end, farms),
	)


def _kept_step(step, location, delivery_start, delivery_end, farms, compute):
	"""A step of Allocate all, read by the Allocate all job itself once it has saved and kept until anything
	it reads changes (the same stamp as the shortages): the step opens at once."""
	key = _shortages_key(location, delivery_start, delivery_end, farms).replace(
		"sa2_shortages:", f"sa2_step_{step}:", 1
	)
	kept = frappe.cache.get_value(key)
	if kept is not None:
		return kept
	out = compute()
	frappe.cache.set_value(key, out, expires_in_sec=SHORTAGES_KEPT)
	return out


def _precompute_job(location, delivery_start, delivery_end, farms=None):
	_precompute_steps(location, delivery_start, delivery_end, farms, lambda _stage: None)


def _precompute_steps(location, delivery_start, delivery_end, farms, publish):
	"""Read the steps that follow Allocate all (cut stage, substitutes, downgrades) while its job still
	runs, so each opens at once. A step that cannot be read here is read when it opens."""
	try:
		_read_steps(location, delivery_start, delivery_end, farms, publish)
	except Exception:
		frappe.log_error("Sales Allocation v2: reading the Allocate all steps failed", frappe.get_traceback())


def _read_steps(location, delivery_start, delivery_end, farms, publish):
	publish("cutcheck")
	_kept_step(
		"cutstage",
		location,
		delivery_start,
		delivery_end,
		farms,
		lambda: _cut_stage_candidates(location, delivery_start, delivery_end, farms),
	)
	publish("substitutes")
	lines = _window_lines(location, delivery_start, delivery_end)
	_kept_step(
		"substitutes",
		location,
		delivery_start,
		delivery_end,
		None,
		lambda: _substitute_candidates(location, lines),
	)
	publish("downgrades")
	_kept_step(
		"downgrades",
		location,
		delivery_start,
		delivery_end,
		None,
		lambda: _downgrade_options(lines, {}, {}),
	)


def _only(sales_orders):
	"""The orders a step is scoped to, or None for the whole window."""
	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders or "[]")
	return set(sales_orders) if sales_orders else None


def _cut_stage_candidates(location, delivery_start, delivery_end, farms, only=None):
	more, lines = _bypass_plan(location, delivery_start, delivery_end, farms, only)

	def other_stage(line):
		want = line["cut_stage"] and sa._norm_cut_stage(line["cut_stage"])
		return want and any(sa._norm_cut_stage(st) != want for st in line["stages"])

	return [
		{k: v for k, v in o.items() if k != "allocations"}
		for o in _cut_stage_options(more, lines)
		if any(other_stage(line) for line in o["lines"])
	]


@frappe.whitelist(methods=["POST"])
def apply_cut_stage(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	sales_orders: str | list | None = None,
	selected_farms: str | list | None = None,
):
	"""Allocate buckets of any cut stage to the orders the planner confirmed, on top of their Allocate all
	proposal."""
	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders or "[]")
	want = set(sales_orders or [])
	if not want:
		return {"stems": 0, "orders": 0}
	more, _lines = _bypass_plan(location, delivery_start, delivery_end, _farms(selected_farms), want)
	added, n = 0.0, 0
	for o in more:
		got = _add_checked(location, o["sales_order"], o["allocations"])
		added += got
		n += 1 if got else 0
	frappe.db.commit()  # nosemgrep
	return {"stems": added, "orders": n}


def _window_lines(location, delivery_start, delivery_end, bypass=True):
	"""{sales_order: (order, its lines)} of the window with what is waiting release counted as allocated:
	the state after Allocate all, for the steps that follow it."""
	skip = set(
		frappe.get_all(
			PENDING,
			filters={
				"status": "Pending",
				"location": location,
				"source": "Picked",
				"delivery_date": ("between", _window(delivery_start, delivery_end)),
			},
			pluck="sales_order",
		)
	)
	out = {}
	# shelf stock only, as Allocate all reads it: what is still arriving and downgrade conflicts are the
	# page's own view, and no step reads them
	was_planning, frappe.flags.sa2_planning = frappe.flags.sa2_planning, True
	try:
		orders = [o for o in _orders(location, delivery_start, delivery_end) if o.name not in skip]
		read = {
			o.name: sa.get_sales_order_items_with_buckets(
				o.name, location, bypass_cut_stage=1 if bypass else 0
			)
			or []
			for o in orders
		}
	finally:
		frappe.flags.sa2_planning = was_planning
	for order in orders:
		items = read[order.name]
		for item in items:
			item["customer"] = order.customer
			item["total_allocated_qty"] = flt(item.get("total_allocated_qty")) + _pending_stems(
				item["sales_order_item"]
			)
		_hold_to_plan(items)
		out[order.name] = (order, items)
	return out


@frappe.whitelist()
def get_substitute_candidates(location: str, delivery_start: str, delivery_end: str | None = None):
	"""Every line of the window still short once Allocate all's proposal is counted, that another variety
	(the specification's, or the same colour) has free stock for: [{sales_order, order_name, sales_order_item,
	variety, length, short, mixed, options: [{variety, free, color}], ...the line's numbers}]."""
	return _kept_step(
		"substitutes",
		location,
		delivery_start,
		delivery_end,
		None,
		lambda: _substitute_candidates(location, _window_lines(location, delivery_start, delivery_end)),
	)


@frappe.whitelist()
def get_order_substitute_candidates(location: str, sales_order: str):
	"""The substitutes step for one order, read again after one of its lines changed."""
	order = frappe.db.get_value(
		"Sales Order", sales_order, ["name", "customer", "custom_order_name"], as_dict=True
	)
	if not order:
		return []
	was_planning, frappe.flags.sa2_planning = frappe.flags.sa2_planning, True
	try:
		items = sa.get_sales_order_items_with_buckets(sales_order, location, bypass_cut_stage=1) or []
	finally:
		frappe.flags.sa2_planning = was_planning
	for item in items:
		item["customer"] = order.customer
		item["total_allocated_qty"] = flt(item.get("total_allocated_qty")) + _pending_stems(
			item["sales_order_item"]
		)
	_hold_to_plan(items)
	return _substitute_candidates(location, {sales_order: (order, items)})


def _sub_memo():
	"""What reading many lines' substitutes at once reads only once (stock, crops, allocated stems), or None
	outside such a read."""
	return getattr(frappe.local, "sa2_sub_memo", None)


def _stock_of(farms, varieties):
	"""Stock rows of these varieties on these farms: once per farm set while a memo is open."""
	from upande_packhouse.api.v2.core import stock

	memo = _sub_memo()
	if memo is None:
		return stock.bucket_rows(farms=farms, varieties=varieties) if varieties else []
	key = ("stock", tuple(sorted(farms or [])))
	if key not in memo:
		by = defaultdict(list)
		for row in stock.bucket_rows(farms=farms):
			by[row.variety].append(row)
		memo[key] = by
	return [row for v in varieties or [] for row in memo[key].get(v, [])]


def _waiting_release():
	"""{(bucket, variety, length): stems} that allocations waiting release hold: still free on the shelf, but
	no other line can take them."""
	memo = _sub_memo()
	if memo is not None and "waiting" in memo:
		return memo["waiting"]
	out = defaultdict(float)
	for bucket, variety, length, qty in frappe.db.sql(
		"""SELECT r.bucket_id, r.item_code, IFNULL(r.stem_length, ''), SUM(r.qty)
		FROM `tabPending Allocation Row` r
		JOIN `tabPending Allocation` p ON p.name = r.parent AND r.parenttype = 'Pending Allocation'
		WHERE p.status = 'Pending'
		GROUP BY r.bucket_id, r.item_code, IFNULL(r.stem_length, '')"""
	):
		out[(bucket, variety, length)] += flt(qty)
	if memo is not None:
		memo["waiting"] = out
	return out


def _free_now(row, owned=(), waiting=None):
	"""A stock row's allocatable stems less what allocations waiting release hold of it; none of a remote
	bucket another order holds (`owned`: it is requested whole). `waiting`: _waiting_release() read once
	by a caller going over many rows."""
	if row.bucket_id in owned:
		return 0.0
	if waiting is None:
		waiting = _waiting_release()
	return max(
		0.0, flt(row.allocatable) - waiting.get((row.bucket_id, row.variety, row.stem_length or ""), 0)
	)


def _owned_elsewhere(rows, sales_order):
	"""Remote farm buckets among `rows` that another order holds: requested whole, nothing of them is free."""
	farm_config = sa._get_production_config()["farm_config"]
	remote = {r.bucket_id for r in rows if not farm_config.get(r.shelf_farm, {}).get("sales_shelf")}
	memo = _sub_memo()
	if memo is None:
		owners = sa.remote_bucket_owners(remote)
	else:
		# each bucket's owners read once over all the lines
		known = memo.setdefault("owners", {})
		ask = [b for b in remote if b not in known]
		if ask:
			found = sa.remote_bucket_owners(ask)
			for b in ask:
				known[b] = found.get(b) or set()
		owners = {b: known[b] for b in remote}
	return {b for b, sos in owners.items() if sos - {sales_order}}


def _crops():
	from upande_packhouse.api.v2.allocation_teams import crop_of_groups

	memo = _sub_memo()
	if memo is None:
		return crop_of_groups()
	if "crops" not in memo:
		memo["crops"] = crop_of_groups()
	return memo["crops"]


def _held_stems(soi):
	"""Released plus waiting release for a line."""
	memo = _sub_memo()
	if memo is not None and ("held", soi) in memo:
		return memo[("held", soi)]
	v = flt(sa._cumulative_allocated_stems(soi)) + _pending_stems(soi)
	if memo is not None:
		memo[("held", soi)] = v
	return v


def _substitute_candidates(location, lines):
	frappe.local.sa2_sub_memo = {}
	try:
		return _substitute_candidates_now(location, lines)
	finally:
		frappe.local.sa2_sub_memo = None


def _substitute_candidates_now(location, lines):
	out = []
	for so, (order, items) in lines.items():
		for item in items:
			short = _left(item)
			if short <= 1e-9:
				continue
			sub = get_shortage_substitutes(item["sales_order_item"], location)
			opts = [o for o in sub["options"] if o["free"] > 0]
			if not opts:
				continue
			out.append(
				{
					"sales_order": so,
					"order_name": order.get("custom_order_name") or so,
					"customer": order.customer,
					"sales_order_item": item["sales_order_item"],
					"short": short,
					# a line in a mix: which mix and how many boxes it has, so two mixes of one order read apart
					"mix": (item.get("custom_mix_name") or _("Mix {0}").format(item.get("custom_mix_group")))
					if cint(item.get("custom_mixed_box")) and item.get("custom_mix_group")
					else (
						_("Bunch mix {0}").format(item.get("custom_bunch_group"))
						if cint(item.get("custom_mixed_bunch")) and item.get("custom_bunch_group")
						else ""
					),
					"boxes": cint(item.get("custom_number_of_boxes")),
					"mix_group": (
						f"box:{item.get('custom_mix_group')}"
						if cint(item.get("custom_mixed_box")) and item.get("custom_mix_group")
						else f"bunch:{item.get('custom_bunch_group')}"
						if cint(item.get("custom_mixed_bunch")) and item.get("custom_bunch_group")
						else ""
					),
					**{
						k: sub[k]
						for k in (
							"variety",
							"length",
							"needed",
							"allocated",
							"held",
							"own_free",
							"per_box",
							"splittable",
							# the specification(s) the options are approved by, shown on the line
							"source",
							"spec",
							"specs",
						)
					},
					"options": opts,
					"add_stems": sub["add_stems"],
				}
			)
	return out


@frappe.whitelist(methods=["POST"])
def allocate_orders(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	sales_orders: str | list | None = None,
	selected_farms: str | list | None = None,
	bypass_cut_stage: int | str | None = None,
):
	"""Allocate these orders again (after their varieties changed), adding to their Allocate all proposal.
	bypass_cut_stage: their buckets are taken whatever their cut stage (the page's Bypass cut stage)."""
	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders or "[]")
	want = set(sales_orders or [])
	orders = [o for o in _orders(location, delivery_start, delivery_end) if o.name in want]
	# an order opened from outside the dates shown: read on its own delivery day
	for so in want - {o.name for o in orders}:
		day = str(frappe.db.get_value("Sales Order", so, "delivery_date") or "")
		orders += [o for o in _orders(location, day, day) if o.name == so]
	prior = defaultdict(float)
	for o in orders:
		for soi in frappe.get_all("Sales Order Item", filters={"parent": o.name}, pluck="name"):
			prior[soi] = _pending_stems(soi)
	out, _errors, _lines, _taken = _plan_day(
		location,
		delivery_start,
		delivery_end,
		_farms(selected_farms),
		None,
		(),
		orders,
		prior=prior,
		bypass_orders=want if frappe.utils.cint(bypass_cut_stage) else (),
	)
	added = 0.0
	for o in out:
		added += _add_checked(location, o["sales_order"], o["allocations"])
	frappe.db.commit()  # nosemgrep
	return {"stems": added, "orders": len(out)}


@frappe.whitelist()
def get_downgrade_options(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	sales_orders: str | list | None = None,
):
	"""The downgrade step, on the proposal as it stands now (after the cut stage and substitutes).
	`sales_orders`: just these (Auto-allocate order)."""
	only = _only(sales_orders)
	if only:
		lines = {k: v for k, v in _window_lines(location, delivery_start, delivery_end).items() if k in only}
		return {
			"options": _downgrade_options(lines, {}, {}),
			"reasons": frappe.get_all("Downgrade Reason", pluck="name", order_by="reason asc"),
		}
	return {
		"options": _kept_step(
			"downgrades",
			location,
			delivery_start,
			delivery_end,
			None,
			lambda: _downgrade_options(_window_lines(location, delivery_start, delivery_end), {}, {}),
		),
		"reasons": frappe.get_all("Downgrade Reason", pluck="name", order_by="reason asc"),
	}


@frappe.whitelist(methods=["POST"])
def apply_downgrades(
	location: str, delivery_start: str, delivery_end: str | None = None, picks: str | list | None = None
):
	"""Add the downgrades the planner chose ([{sales_order_item, bucket_id, length, qty}]) to their orders'
	allocations waiting release, as longer stems with their reason. A bucket another line has taken since is
	given only what it still has free."""
	if isinstance(picks, str):
		picks = json.loads(picks or "[]")
	by_order, added = defaultdict(list), 0.0
	free = {}
	reasons = set(frappe.get_all("Downgrade Reason", pluck="name"))
	for pk in picks or []:
		if pk.get("reason") and pk["reason"] not in reasons:
			frappe.throw(_("{0} is not a downgrade reason.").format(pk["reason"]))
		line = frappe.db.get_value(
			"Sales Order Item",
			pk["sales_order_item"],
			["parent", "item_code", "uom", "stock_uom", "conversion_factor"],
			as_dict=True,
		)
		if not line:
			continue
		if line.parent not in free:
			view = {
				(b["bucket_id"], i["item_code"], b.get("stem_length") or ""): flt(b.get("available_qty"))
				for i in sa.get_sales_order_items_with_buckets(line.parent, location) or []
				for b in i.get("batches") or []
			}
			left = _left_on_buckets(view.keys())
			free[line.parent] = {k: min(v, left.get(k, 0)) for k, v in view.items()}
		key = (pk["bucket_id"], line.item_code, pk.get("length") or "")
		size = flt(line.conversion_factor) or 1
		qty = math.floor(min(flt(pk.get("qty")), free[line.parent].get(key, 0)) / size + 1e-9) * size
		if qty <= 0:
			continue
		free[line.parent][key] -= qty
		by_order[line.parent].append(
			{
				"item_code": line.item_code,
				"bucket_id": pk["bucket_id"],
				"qty": qty,
				"sales_order_item": pk["sales_order_item"],
				"stem_length": pk.get("length") or "",
				"warehouse": "",
				"uom": line.uom,
				"stock_uom": line.stock_uom,
				"conversion_factor": size,
				"length_status": "downgrade",
				"downgrade_reason": pk.get("reason") or _("Downgraded at Allocate all"),
				"available_exact_stems": 0,
			}
		)
		added += qty
	for so, rows in by_order.items():
		_add_pending(location, so, rows, {}, source="Allocate all")
	frappe.db.commit()  # nosemgrep
	p = _proposal(location, delivery_start, delivery_end)
	return {"stems": added, "orders": len(by_order), "proposal": _summary(p)}


def _plan_day(
	location,
	delivery_start,
	delivery_end,
	farms,
	progress=None,
	skip=(),
	orders=None,
	bypass=False,
	prior=None,
	taken=None,
	bypass_orders=(),
):
	"""([{sales_order, ..., allocations, teams, boxes, stems}] for every order that can take whole boxes,
	{sales_order_item: why} for lines whose box the plan refuses, {sales_order: (order, its lines)} as read,
	{(bucket, variety, length): stems} taken). Buckets taken for one order are not offered to the next. A line
	with no packing team is allocated all the same: the team is chosen when it is released."""
	rules = _rules(location)
	if orders is None:
		orders = _orders(location, delivery_start, delivery_end)
	was_planning, frappe.flags.sa2_planning = frappe.flags.sa2_planning, True
	try:
		return _plan_orders(
			location, farms, progress, skip, orders, bypass, prior, taken, bypass_orders, rules
		)
	finally:
		frappe.flags.sa2_planning = was_planning


def _plan_orders(location, farms, progress, skip, orders, bypass, prior, taken, bypass_orders, rules):
	"""_plan_day's work, with frappe.flags.sa2_planning set."""
	# `bypass`: a second pass that takes buckets of any cut stage, on top of the first pass's picks (`prior`
	# {sales_order_item: stems}, its buckets in `taken`)
	taken, out, errors, lines = dict(taken or {}), [], {}, {}
	prior = prior or {}

	for n, order in enumerate(orders, 1):
		if progress:
			# the boxes planned so far, so the page's order cards fill as the run goes
			progress(
				n - 1,
				len(orders),
				order.get("custom_order_name") or order.name,
				{o["sales_order"]: o["boxes"] for o in out},
			)
		if order.name in skip:
			continue
		items = (
			sa.get_sales_order_items_with_buckets(
				order.name,
				location,
				selected_farms=farms,
				bypass_cut_stage=1 if bypass or order.name in bypass_orders else 0,
			)
			or []
		)
		# Allocate all for one box type: the other type's lines are left as they are
		if frappe.flags.sa2_box_type:
			items = [i for i in items if _is_mixed_line(i) == (frappe.flags.sa2_box_type == "mixed")]
		for item in items:
			item["customer"] = order.customer
			item["sales_order"] = order.name
			if prior.get(item["sales_order_item"]):
				item["total_allocated_qty"] = (
					flt(item.get("total_allocated_qty")) + prior[item["sales_order_item"]]
				)
		_hold_to_plan(items)
		lines[order.name] = (order, items)
		planned = allocation_plan.planned_teams([i["sales_order_item"] for i in items]) if items else {}
		mine, allocations, teams, order_boxes = dict(taken), [], {}, 0

		def ok(item):
			if _left(item) <= 0:
				return False
			if not rules["enforce_plan"]:
				return True
			return bool(_planned(item) and _team(item, planned, rules))

		groups = {}
		for item in items:
			groups.setdefault(_box_key(item), []).append(item)
		for members in groups.values():
			if rules["partial_allocation"]:
				# everything the shelves have for each line, complete box or not: it waits release, and the
				# release takes only the complete boxes (the rest stays allocated for the next one)
				adds = {it["sales_order_item"]: _left(it) for it in members if ok(it)}
			else:
				adds = _whole_boxes(members, mine, ok)
				if any(adds.get(it["sales_order_item"], 0) < _left(it) - 1e-9 for it in members if ok(it)):
					continue
			group_boxes, group_taken, group_picks, group_teams = 0, dict(mine), [], {}
			picked = defaultdict(float)
			for item in members:
				add = adds.get(item["sales_order_item"], 0)
				if add <= 0:
					continue
				picks, _short = _fifo_pick(item, add, group_taken)
				if picks:
					group_picks += picks
					picked[item["sales_order_item"]] += sum(flt(p["qty"]) for p in picks)
					# no team is guessed: it is chosen when the order is released (the plan's, when it is enforced)
					team = _team(item, planned, rules) if rules["enforce_plan"] else None
					if team:
						group_teams[item["sales_order_item"]] = team
			if not group_picks:
				continue
			group_boxes = _new_boxes(members, picked)
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
		progress(len(orders), len(orders), "", {o["sales_order"]: o["boxes"] for o in out})
	return out, errors, lines, taken


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
	"""Save the proposal of these orders (all of it when none are named), each order on its own, reporting
	over `sa2_day_allocation`: at once in this request, the rest in the background past RELEASE_BUDGET."""
	farms = _farms(selected_farms)
	p = _proposal(location, delivery_start, delivery_end)
	if not p:
		frappe.throw(_("Nothing to release."))
	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders or "[]")
	_lock(location, "confirm")
	args = {
		"location": location,
		"delivery_start": delivery_start,
		"delivery_end": delivery_end,
		"farms": farms,
		"sales_orders": sales_orders or None,
		"user": frappe.session.user,
	}
	# never queued behind other jobs: the release starts now, and only what is left past the budget goes on
	# in the background
	return _run_confirm(**args, budget=RELEASE_BUDGET)


def _continue_release(args, chosen, n, offset, total, carried):
	"""The orders of a release left after its time budget: on in the background worker, the bar following."""
	job_id = f"sa2_release:{args['location']}:{frappe.generate_hash(length=8)}"
	_set_state(
		args["location"],
		{"phase": "confirm", "progress": offset + n, "total": total, "order": "", "job_id": job_id},
	)
	frappe.enqueue(
		_run_confirm,
		queue="short",
		at_front=True,
		timeout=1800,
		enqueue_after_commit=True,
		job_id=job_id,
		run=job_id,
		offset=offset + n,
		total=total,
		carried=carried,
		# every order released: the job only reads the shortages and reports the run done
		finish_only=n >= len(chosen),
		**{**args, "sales_orders": [o["sales_order"] for o in chosen[n:]]},
	)
	return {"queued": True, "run": job_id}


def _run_confirm(
	location,
	delivery_start,
	delivery_end,
	farms,
	sales_orders,
	user,
	run=None,
	budget=None,
	offset=0,
	total=None,
	carried=None,
	finish_only=False,
):
	import time

	started = time.monotonic()
	handed_on = None
	carried = carried or {}
	args = {
		"location": location,
		"delivery_start": delivery_start,
		"delivery_end": delivery_end,
		"farms": farms,
		"sales_orders": sales_orders,
		"user": user,
	}

	def hand_on(chosen, n):
		queued = _continue_release(
			args,
			chosen,
			n,
			offset,
			total,
			{
				"saved": cint(carried.get("saved")) + len(saved),
				"boxes": flt(carried.get("boxes")) + sum(x["boxes"] for x in saved),
				"stems": flt(carried.get("stems")) + sum(x["stems"] for x in saved),
				"errors": errors,
				"waiting": waiting,
				"taken_off": taken_off,
				"freed": freed,
			},
		)
		frappe.db.commit()  # nosemgrep -- the job is queued after commit
		# only once queued: a failed commit leaves the lock to be cleared below
		return queued

	try:
		p = _proposal(location, delivery_start, delivery_end) or {"orders": []}
		chosen = (
			[]
			if finish_only
			else [o for o in p["orders"] if not sales_orders or o["sales_order"] in sales_orders]
		)
		total = total or len(chosen)
		rules = sa._v2_rules(location)
		saved, errors, waiting = [], dict(carried.get("errors") or {}), list(carried.get("waiting") or [])
		taken_off = dict(carried.get("taken_off") or {})
		freed = dict(carried.get("freed") or {})

		def clear(o, stems):
			"""A release leaves nothing waiting: the order's allocation waiting release is closed whatever went
			out, and the stems that did not go out are free on the shelf again."""
			sa.close_pending(o["sales_order"], location)
			frappe.db.commit()  # nosemgrep -- each order stands on its own
			if stems > 1e-9:
				freed[o["sales_order"]] = flt(freed.get(o["sales_order"])) + stems

		for n, o in enumerate(chosen):
			if budget and n and time.monotonic() - started > budget:
				handed_on = hand_on(chosen, n)
				return handed_on
			_publish(
				user,
				location,
				phase="confirm",
				stage="release",
				progress=offset + n,
				total=total,
				order=o.get("order_name") or o["sales_order"],
			)
			# a bucket gone from the shelves since it was allocated comes off; the rest of the order goes on
			gone = _drop_gone_buckets(o, location)
			if gone:
				taken_off[o["sales_order"]] = gone
				if not o["allocations"]:
					clear(o, 0)
					waiting.append(o["sales_order"])
					continue
			held = sum(flt(a.get("qty")) for a in o["allocations"])
			frappe.db.savepoint("confirm_day_order")
			try:
				_check_location(o["sales_order"], location)
				if rules["enforce_plan"]:
					sa._check_against_plan(o["allocations"], o["teams"])
				o["teams"] = _fill_teams(o, location)
				# a pick list with no packing team can't be scheduled, so its buckets never get a trip:
				# such an order waits until its team is chosen (the page asks for it)
				if not o["teams"]:
					frappe.db.rollback(save_point="confirm_day_order")
					clear(o, held)
					waiting.append(o["sales_order"])
					continue
				# only complete boxes go out; what does not complete a box goes back to the shelf
				row = frappe._dict(name=o["sales_order"])
				sa._attach_boxes([row])
				# below the release percentage only the straight lines' complete boxes go out
				release, keep, boxes = _gate_release(
					*_split_release(o["sales_order"], o["allocations"]), row.boxes_full, row.boxes_required
				)
				o["boxes"] = sum(boxes.values())
				if not release:
					frappe.db.rollback(save_point="confirm_day_order")
					clear(o, held)
					waiting.append(o["sales_order"])
					continue
				sa._allocate_stock_with_buckets_impl(o["sales_order"], release, location, o["teams"])
			except Exception as e:
				frappe.db.rollback(save_point="confirm_day_order")
				frappe.clear_messages()
				errors[o["sales_order"]] = str(e)
				clear(o, held)
				continue
			# each order stands on its own: a later failure must not roll back the ones already saved
			clear(o, sum(flt(a.get("qty")) for a in keep))
			saved.append(o)
		# the orders used the budget up: the shortages (seconds more) are read in the background
		if budget and chosen and time.monotonic() - started > budget:
			handed_on = hand_on(chosen, len(chosen))
			return handed_on
		_publish(user, location, phase="confirm", progress=total, total=total, order="")
		p = _proposal(location, delivery_start, delivery_end)
		result = {
			"saved": cint(carried.get("saved")) + len(saved),
			"boxes": flt(carried.get("boxes")) + sum(o["boxes"] for o in saved),
			"stems": flt(carried.get("stems")) + sum(o["stems"] for o in saved),
			"errors": errors,
			"waiting": waiting,
			"taken_off": taken_off,
			"freed": freed,
			"proposal": _summary(p),
			"shortages": _shortages(location, delivery_start, delivery_end, farms, errors, _pending(p)),
		}
		_keep_shortages(location, delivery_start, delivery_end, farms, result["shortages"])
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("Sales Allocation v2: confirm allocated orders failed", frappe.get_traceback())
		result = {"error": str(e)}
	finally:
		# handed on: the lock stays with the run, which goes on in the background
		if not handed_on:
			frappe.cache.delete_value(f"sa2_day_allocation:{location}")
	_publish(user, location, phase="confirm", done=True, run=run or "", **_light(result))
	return {"phase": "confirm", "done": True, **result}


@frappe.whitelist()
def get_day_shortages(
	location: str,
	delivery_start: str,
	delivery_end: str | None = None,
	selected_farms: str | list | None = None,
):
	"""The window's shortages. Reading them takes seconds, so they are kept until anything they read changes
	(proposals, allocations, shelves, pick lists, orders, stock entries, plans) or for ten minutes."""
	farms = _farms(selected_farms)
	key = _shortages_key(location, delivery_start, delivery_end, farms)
	kept = frappe.cache.get_value(key)
	if kept is not None:
		return kept
	p = _proposal(location, delivery_start, delivery_end, farms)
	out = _shortages(location, delivery_start, delivery_end, farms, pending=_pending(p))
	frappe.cache.set_value(key, out, expires_in_sec=SHORTAGES_KEPT)
	return out


SHORTAGES_KEPT = 600


def _shortages_key(location, delivery_start, delivery_end, farms):
	"""Cache key for a window's shortages: the window plus a stamp of the state they are read from, so any
	change there makes a new key."""
	import hashlib

	stamp = frappe.db.sql(
		"""SELECT
			(SELECT MAX(modified) FROM `tabPending Allocation`), (SELECT COUNT(*) FROM `tabPending Allocation Row`),
			(SELECT MAX(modified) FROM `tabBucket Allocation Status`), (SELECT MAX(modified) FROM `tabBucket Allocations`),
			(SELECT COUNT(*) FROM `tabBucket Allocations`), (SELECT SUM(quantity_allocated) FROM `tabBucket Allocations`),
			(SELECT MAX(modified) FROM `tabShelf Item`), (SELECT COUNT(*) FROM `tabShelf Item`),
			(SELECT SUM(stem_qty) FROM `tabShelf Item`),
			(SELECT MAX(modified) FROM `tabPick List Item`), (SELECT COUNT(*) FROM `tabPick List Item`),
			(SELECT MAX(modified) FROM `tabSales Order`), (SELECT MAX(creation) FROM `tabStock Entry`),
			(SELECT MAX(modified) FROM `tabAllocation Confirmation`)"""
	)
	mark = hashlib.md5(repr(stamp).encode(), usedforsecurity=False).hexdigest()
	return "sa2_shortages:{0}:{1}:{2}:{3}:{4}".format(
		location, delivery_start, delivery_end or delivery_start, ",".join(sorted(farms or [])), mark
	)


def _keep_shortages(location, delivery_start, delivery_end, farms, shortages):
	"""Keep shortages just read (by Allocate all or a release) for the Shortages tab to open with."""
	frappe.cache.set_value(
		_shortages_key(location, delivery_start, delivery_end, farms),
		shortages,
		expires_in_sec=SHORTAGES_KEPT,
	)


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


def _shortage_lines(location, delivery_start, delivery_end, farms, orders, lines):
	"""(order, its lines, read by the plan) for every open order of the window: the lines Allocate all just
	read are used again, the others are read now."""
	if orders is None:
		orders = _orders(location, delivery_start, delivery_end)
	for order in orders:
		if lines is not None and order.name in lines:
			items = lines[order.name][1]
			if items:
				yield order, items, True
			continue
		items = sa.get_sales_order_items_with_buckets(order.name, location, selected_farms=farms) or []
		if items:
			yield order, items, False


def _shortages(
	location,
	delivery_start,
	delivery_end,
	farms,
	errors=None,
	pending=None,
	orders=None,
	lines=None,
	held_all=None,
	progress=None,
):
	"""Every line of the window still short once the proposal (`pending`) is counted in, with why, and the
	buckets other orders hold of its variety that would fit it. `lines` (with `held_all`, every stem Allocate
	all took) are the lines it read before proposing: their buckets do not have the proposal taken off yet."""
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
	if orders is None:
		orders = _orders(location, delivery_start, delivery_end)
	for n, (order, items, read_before) in enumerate(
		_shortage_lines(location, delivery_start, delivery_end, farms, orders, lines), 1
	):
		if progress and n % 5 == 0:
			progress(n, len(orders), order.get("custom_order_name") or order.name)
		planned = allocation_plan.planned_teams([i["sales_order_item"] for i in items])
		if read_before:
			mine = held_all or {}
		else:
			_hold_to_plan(items)
			mine = held.get(order.name, {})
		boxes, order_boxes = _box_cover(items, proposed, mine)
		for item in items:
			item["customer"] = order.customer
			short = _left(item) - proposed.get(item["sales_order_item"], 0)
			if short <= 1e-9:
				continue
			# the line's own team only (its pick list's or the plan's): none is guessed for it
			team = item.get("opl_team") or item.get("planning_team") or planned.get(item["sales_order_item"])
			free = _free(item, mine)
			usable = _free(item, mine, usable_only=True)
			per_box = _per_box(item)
			if errors.get(item["sales_order_item"]) or errors.get(order.name):
				why = errors.get(item["sales_order_item"]) or errors[order.name]
			elif rules["enforce_plan"] and not _planned(item):
				why = _("Not in the allocation plan")
			elif rules["enforce_plan"] and not team:
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
						for r in _donors(item, order.name, location_farms, location)
					],
				}
			)
	_attach_incoming(out, location_farms)
	return out


def _attach_incoming(lines, location_farms):
	"""Each short line's stock still on its way to the shelf, at its variety and length: received and not
	shelved, in quarantine, and harvested and not received yet ({received, quarantine, harvested}), and where
	it is: [{stage, farm, warehouse, stems, buckets}], most stems first."""
	from upande_packhouse.api.v2 import allocation_playground as pg
	from upande_packhouse.api.v2.allocation_revise import stock_stages

	sales_farm = next((r.farm for r in pg._farms() if r.farm in location_farms), None)
	places = {}
	stages = (
		stock_stages(sales_farm, {x["item_code"] for x in lines}, places=places)
		if sales_farm and lines
		else {}
	)
	for x in lines:
		key = (x["item_code"], x["length"] or "")
		st = stages.get(key) or {}
		x["incoming"] = {k: flt(st.get(k)) for k in ("received", "quarantine", "harvested")}
		x["incoming_places"] = sorted(
			(
				{"stage": stage, "farm": farm, "warehouse": wh, "stems": v[0], "buckets": len(v[1])}
				for (stage, farm, wh), v in (places.get(key) or {}).items()
			),
			key=lambda r: -r["stems"],
		)


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
			# released plus waiting release: a move takes from what waits release
			"line_allocated": flt(sa._cumulative_allocated_stems(soi)) + _pending_stems(soi),
			"line_per_box": _per_box(row) if row else 0,
			"line_boxes": cint(row.get("custom_number_of_boxes")),
			# a mixed box or bunch: the line fills only its own share of each box
			"line_mixed": cint(row.get("custom_mixed_box")) or cint(row.get("custom_mixed_bunch")),
		}
	return memo[soi]


def _donors(item, sales_order, location_farms, location=None):
	"""Buckets of this variety allocated to another order's line and still waiting release (no pick list
	yet), on this location's shelves, at the line's length or longer (and its cut stage when the spec names
	one). A bucket already on a pick list, submitted or waiting transfer, is never taken. Latest delivery and
	last in the sequence first: those are the ones to take from."""
	if not location_farms:
		return []
	rows = frappe.db.sql(
		"""
		SELECT r.sales_order_item, pa.sales_order, IFNULL(so.custom_order_name, '') AS order_name,
			so.customer, so.delivery_date,
			0 AS sequence,
			r.bucket_id, IFNULL(r.stem_length, '') AS stem_length,
			SUM(r.qty) AS qty,
			(SELECT MIN(si.cut_stage) FROM `tabShelf Item` si
				WHERE si.bucket_id = r.bucket_id AND si.variety = r.item_code) AS cut_stage
		FROM `tabPending Allocation Row` r
		INNER JOIN `tabPending Allocation` pa ON pa.name = r.parent AND r.parenttype = 'Pending Allocation'
		INNER JOIN `tabSales Order` so ON so.name = pa.sales_order
		WHERE r.item_code = %(item)s
		  AND pa.status = 'Pending' AND pa.sales_order != %(so)s
		  AND (%(loc)s = '' OR pa.location = %(loc)s)
		  AND so.docstatus = 1 AND so.status NOT IN ('Completed', 'Closed', 'Cancelled')
		  AND EXISTS (SELECT 1 FROM `tabShelf Item` si INNER JOIN `tabShelf` s ON s.name = si.parent
			WHERE si.bucket_id = r.bucket_id AND si.variety = r.item_code AND s.farm IN %(farms)s)
		GROUP BY r.sales_order_item, r.bucket_id, IFNULL(r.stem_length, '')
		HAVING qty > 0
		""",
		{"item": item["item_code"], "so": sales_order, "farms": tuple(location_farms), "loc": location or ""},
		as_dict=True,
	)
	want_cm = sa._parse_cm(item.get("required_length"))
	stage = (
		sa._norm_cut_stage(item.get("custom_cut_stage"))
		if item.get("custom_cut_stage") and sa._cut_stage_applies(item.get("item_code"))
		else None
	)
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
	"""Take a bucket off one order's line and give it (up to what is short, and no more than `stems`) to another
	order's line of the same variety; what the move does not take goes back to the first line. Nothing is
	released here: both sides wait in their orders' allocations to release (no pick list is made or changed
	beyond taking the bucket off the first order's)."""
	src = frappe.db.get_value("Sales Order Item", from_item, ["parent", "item_code"], as_dict=True)
	dst = frappe.db.get_value(
		"Sales Order Item", to_item, ["parent", "item_code", "custom_length", "custom_opl"], as_dict=True
	)
	if not src or not dst:
		frappe.throw(_("Order line not found."))
	if src.item_code != dst.item_code:
		frappe.throw(_("Only a bucket of the same variety can be moved."))

	# only an allocation still waiting release is moved: a bucket on a pick list (submitted, or a draft
	# waiting transfer) stays where it is
	src_rows = frappe.db.sql(
		"""SELECT r.name, r.qty FROM `tabPending Allocation Row` r
		INNER JOIN `tabPending Allocation` pa ON pa.name = r.parent AND r.parenttype = 'Pending Allocation'
		WHERE pa.status = 'Pending' AND pa.location = %s AND r.sales_order_item = %s AND r.bucket_id = %s
		  AND IFNULL(r.stem_length, '') = %s""",
		(location, from_item, bucket_id, stem_length or ""),
		as_dict=True,
	)
	held = sum(flt(r.qty) for r in src_rows)
	if held <= 0:
		frappe.throw(
			_("{0} is not waiting release on {1}: a bucket on a pick list is not moved.").format(
				bucket_id, src.parent
			)
		)

	# up to what was ordered (less what is already waiting release for it): the plan does not limit a move
	need = (
		sa._required_stems_for_so_item(to_item)
		- flt(sa._cumulative_allocated_stems(to_item))
		- _pending_stems(to_item)
	)
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

	# no team is set on a move: it is chosen when the order is released
	team = None
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

	# off the order it came from (it was never on a pick list): what is not moved goes back on below
	_drop_pending_rows(src.parent, location, [r.name for r in src_rows])

	# both sides wait to be released: the moved stems on the order they go to, the rest (in whole bunches of
	# its line) on the order they came from
	src_size = flt(frappe.db.get_value("Sales Order Item", from_item, "conversion_factor")) or 1
	rest = math.floor((held - qty) / src_size + 1e-9) * src_size
	src_len = frappe.db.get_value("Sales Order Item", from_item, "custom_length")
	src_longer = sa._parse_cm(stem_length) > sa._parse_cm(src_len)
	back = [
		_alloc(
			from_item,
			rest,
			"downgrade" if src_longer else "exact",
			_("Planned at {0}").format(stem_length) if src_longer else "",
		)
	]
	try:
		_add_pending(location, dst.parent, target, {to_item: team})
		if rest > 0:
			_add_pending(location, src.parent, back, {from_item: src_team} if src_team else {})
		frappe.db.commit()  # nosemgrep
	except Exception as e:
		# nothing was committed: the rollback leaves the bucket on the order it came from
		frappe.db.rollback()
		frappe.clear_messages()
		frappe.throw(_("Could not move {0} to {1}: {2}").format(bucket_id, dst.parent, str(e)))

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


def _drop_pending_rows(sales_order, location, rows):
	"""Take these rows off an order's allocation waiting release, its boxes and stems recounted; an
	allocation left with no rows goes."""
	frappe.db.delete("Pending Allocation Row", {"name": ("in", rows)})
	name = frappe.db.get_value(
		PENDING, {"sales_order": sales_order, "location": location, "status": "Pending"}, "name"
	)
	if not name:
		return
	doc = frappe.get_doc(PENDING, name)
	if not doc.allocations:
		frappe.delete_doc(PENDING, name, ignore_permissions=True, force=True)
		return
	allocs = [{"sales_order_item": r.sales_order_item, "qty": flt(r.qty)} for r in doc.allocations]
	frappe.db.set_value(
		PENDING,
		name,
		{"boxes": _pending_boxes(allocs), "stems": sum(flt(r.qty) for r in doc.allocations)},
		update_modified=False,
	)


def _pending_stems(sales_order_item):
	"""Stems waiting release for a line (any location)."""
	return flt(
		frappe.db.sql(
			"""SELECT SUM(r.qty) FROM `tabPending Allocation Row` r
			JOIN `tabPending Allocation` p ON p.name = r.parent AND r.parenttype = %s
			WHERE p.status = 'Pending' AND r.sales_order_item = %s""",
			(PENDING, sales_order_item),
		)[0][0]
	)


def _pending_boxes(allocations):
	"""Whole boxes a set of allocation rows fills: per box (a mix once, by its scarcest line), each line's
	stems over its share of a box."""
	stems = defaultdict(float)
	for a in allocations:
		stems[a["sales_order_item"]] += flt(a.get("qty"))
	if not stems:
		return 0
	lines = frappe.get_all(
		"Sales Order Item",
		filters={
			"parent": (
				"in",
				list(
					set(
						frappe.get_all(
							"Sales Order Item", filters={"name": ("in", list(stems))}, pluck="parent"
						)
					)
				),
			)
		},
		fields=[
			"name AS sales_order_item",
			"parent",
			"custom_mix_name",
			"stock_qty AS original_stock_qty",
			"custom_number_of_boxes",
			"custom_mixed_box",
			"custom_mixed_bunch",
			"custom_packrate",
			"custom_packrate_mixed_box",
			"custom_mix_group",
			"custom_bunch_group",
		],
	)
	keys = box_keys(lines)
	# every line of a box counts, not only those these rows feed: a mix is as full as its scarcest line, and a
	# line with nothing waiting holds only what it was allocated before
	groups = defaultdict(list)
	for ln in lines:
		ln["_box"] = keys[ln.sales_order_item]
		groups[_box_key(ln)].append(ln)
	fed = {_box_key(ln) for ln in lines if ln.sales_order_item in stems}

	def boxes(members, extra):
		counts = []
		for ln in members:
			pb = _per_box(ln)
			if pb > 0:
				have = flt(sa._cumulative_allocated_stems(ln.sales_order_item)) + extra(ln)
				counts.append(int(have / pb + 1e-9))
		return min(counts) if counts else 0

	return sum(
		max(0, boxes(m, lambda ln: stems.get(ln.sales_order_item, 0)) - boxes(m, lambda ln: 0))
		for key, m in groups.items()
		if key in fed
	)


def _add_pending(location, sales_order, rows, teams, source="Picked"):
	"""Add allocation rows to an order's allocation waiting release at the location (made if it has none),
	the same bucket and line adding up. Kept by Allocate all: it is the planner's own pick now."""
	name = frappe.db.get_value(
		PENDING, {"sales_order": sales_order, "location": location, "status": "Pending"}, "name"
	)
	kept, all_teams = [], {}
	if name:
		doc = frappe.get_doc(PENDING, name)
		for r in doc.allocations:
			kept.append(
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
				}
			)
			if r.team:
				all_teams[r.sales_order_item] = r.team
	all_teams.update({k: v for k, v in (teams or {}).items() if v})
	merged = {}
	for a in kept + [r for r in rows if flt(r.get("qty")) > 0]:
		key = (a["sales_order_item"], a["bucket_id"], a.get("stem_length") or "")
		if key in merged:
			merged[key]["qty"] += flt(a["qty"])
		else:
			merged[key] = dict(a)
	allocations = list(merged.values())
	return _save_pending(location, sales_order, allocations, all_teams, _pending_boxes(allocations), source)


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
	buckets = [
		r for r in _donors(line, dst.parent, location_farms, location) if r.sales_order_item == from_item
	]
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
def set_pending_team(location: str, sales_order: str, team: str, lines: str | list | None = None):
	"""Give the lines of an order waiting release this team: every line, or only `lines` (one mix or one
	straight line), so one order can be packed by several teams."""
	if isinstance(lines, str):
		lines = json.loads(lines or "[]")
	if not frappe.db.exists("Packing Teams", team):
		frappe.throw(_("{0} is not a packing team.").format(team))
	from upande_packhouse.api.v2.allocation_teams import check_team_farm

	check_team_farm(team, sales_order, location)
	names = frappe.get_all(
		PENDING, filters={"status": "Pending", "location": location, "sales_order": sales_order}, pluck="name"
	)
	if not names:
		frappe.throw(_("{0} has nothing waiting to be released.").format(sales_order))
	only = " AND sales_order_item IN %(l)s" if lines else ""
	# the f-string hole is fixed SQL; values are bound
	frappe.db.sql(  # nosemgrep
		f"""UPDATE `tabPending Allocation Row` SET team = %(team)s
		WHERE parent IN %(p)s AND parenttype = %(dt)s{only}""",
		{"team": team, "p": tuple(names), "dt": PENDING, "l": tuple(lines or ("",))},
	)
	return {"success": True}


def _option_specs(spec, source, customer, varieties):
	"""[{name, varieties}] the line's own specification: per line, never the customer's others."""
	return [{"name": spec, "varieties": _spec_varieties(spec)}] if spec else []


def _spec_varieties(spec):
	"""[{variety, color, primary}] every variety a specification approves, its primary first."""
	if not spec:
		return []
	rows = frappe.get_all(
		"Spec Approved Variety",
		filters={"parent": spec, "parenttype": "Specifications"},
		fields=["variety", "is_primary", "idx"],
		order_by="is_primary desc, idx asc",
	)
	return [
		{
			"variety": r.variety,
			"color": frappe.get_cached_value("Item", r.variety, "custom_color") or "",
			"primary": cint(r.is_primary),
		}
		for r in rows
		if r.variety
	]


@frappe.whitelist()
def get_shortage_substitutes(sales_order_item: str, location: str):
	"""Varieties a short line could be changed to, with the stems free for it on this location's shelves at
	its length (exact) or longer (a downgrade), always of the ordered variety's colour. A line with a
	specification: only the varieties it approves, its primary first. A line without one: varieties of the
	same colour in the same crop. Free stock counts
	whether it is moving or not; non-moving stock (past the amber time) is shown apart and goes first."""
	from upande_packhouse.api.v2 import allocation_playground as pg

	ln = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		[
			"parent",
			"item_code",
			"custom_length",
			"custom_line",
			"item_group",
			"stock_qty",
			"custom_number_of_boxes",
			"conversion_factor",
			"custom_mixed_box",
			"custom_mixed_bunch",
		],
		as_dict=True,
	)
	if not ln:
		frappe.throw(_("Order line not found."))
	farms = sa._get_production_config()["farms_by_location"].get(location) or []
	want = sa._parse_cm(ln.custom_length) or 0
	allocated = flt(sa._cumulative_allocated_stems(sales_order_item))
	color = frappe.get_cached_value("Item", ln.item_code, "custom_color") or ""
	# the approved varieties: the line's specification's, else the customer's, else every specification's
	# -- the customer's and everyone's of the ordered variety's colour and crop
	groups = _crops()
	crop = groups.get(ln.item_group)
	same_crop = [g for g, c in groups.items() if c == crop] if crop else [ln.item_group]
	customer = frappe.db.get_value("Sales Order", ln.parent, "customer")
	source, approved = sa.approved_varieties_for(
		ln.custom_line,
		customer,
		ln.item_code,
		color,
		same_crop,
	)
	varieties = [r.variety for r in approved]
	# the specification's varieties of the ordered variety's colour only
	if source == "spec" and color and varieties:
		same = set(
			frappe.get_all("Item", filters={"name": ("in", varieties), "custom_color": color}, pluck="name")
		)
		varieties = [v for v in varieties if v in same]
	amber = pg.amber_from_days()
	agg = defaultdict(lambda: {"free": 0.0, "exact": 0.0, "longer": 0.0, "non_moving": 0.0, "oldest": 0})
	sales_order = ln.parent
	stock_rows = _stock_of(farms, varieties)
	owned = _owned_elsewhere(stock_rows, sales_order)
	waiting = _waiting_release()
	for r in stock_rows:
		cm = sa._parse_cm(r.stem_length) or 0
		free = _free_now(r, owned, waiting)
		if cm < want or free <= 0:
			continue
		a = agg[r.variety]
		a["free"] += free
		a["exact" if cm == want else "longer"] += free
		if cint(r.age_days) >= amber:
			a["non_moving"] += free
		a["oldest"] = max(a["oldest"], cint(r.age_days))
	primary = {r.variety for r in approved if cint(r.is_primary)}
	rank = {v: i for i, v in enumerate(varieties)}
	options = [
		{
			"variety": v,
			"primary": v in primary,
			"color": frappe.get_cached_value("Item", v, "custom_color"),
			**a,
		}
		for v, a in agg.items()
	]
	# the spec's own order; without a spec, non-moving stock first, then the most stems
	options.sort(
		key=lambda o: (
			(rank.get(o["variety"], 0), -o["free"])
			if source in ("spec", "customer")
			else (-o["non_moving"], -o["free"])
		)
	)
	# Add variety takes no more than the variety has free, in whole boxes of the line: the rest stays on the
	# line's own variety
	box = flt(ln.stock_qty) / (cint(ln.custom_number_of_boxes) or 1) or 1
	# the line's own variety: what it holds and what is free for it, for splitting the line across varieties
	own_rows = _stock_of(farms, [ln.item_code])
	own_owned = _owned_elsewhere(own_rows, sales_order)
	own_free = sum(
		_free_now(r, own_owned, waiting) for r in own_rows if (sa._parse_cm(r.stem_length) or 0) >= want
	)
	add_stems = _add_stems(sales_order_item, ln, allocated, own_free)
	add_mode, step = "boxes", box
	held = allocated + _pending_stems(sales_order_item)
	gap = flt(ln.stock_qty) - held - own_free
	# a line of a mix can also take another variety in the same boxes, beside it, for as many stems as that
	# variety has (whole bunches in every box) -- only while none of its boxes is on a pick list yet, so no
	# packed box changes. Each variety takes the way that uses more of its stems: whole later boxes, or a
	# share of every box.
	share_step = 0
	if (
		(cint(ln.custom_mixed_box) or cint(ln.custom_mixed_bunch))
		and gap > 1e-9
		and not sa._placed_stems(sales_order_item)
	):
		# whole bunches: spread over the boxes as evenly as they go (box_groups keeps each box whole)
		share_step = flt(ln.conversion_factor) or 1
	share_room = (
		math.floor(max(0, flt(ln.stock_qty) - held) / share_step + 1e-9) * share_step if share_step else 0
	)
	for o in options:
		by_boxes = min(add_stems, math.floor(o["free"] / box + 1e-9) * box)
		by_share = (
			math.floor(min(o["free"], gap, share_room) / share_step + 1e-9) * share_step if share_step else 0
		)
		if by_share > by_boxes:
			o["adds"], o["mode"] = by_share, "share"
		else:
			o["adds"], o["mode"] = by_boxes, "boxes"
	if share_step:
		add_stems = max(add_stems, min(gap, share_room))
		add_mode, step = "share", share_step
	return {
		"source": source,
		# the line's specification, shown on the substitute dialog, with every variety it approves
		"spec": ln.custom_line or "",
		"spec_varieties": _spec_varieties(ln.custom_line),
		# the specifications the options come from: the line's own, else the customer's, else the
		# active ones approving an offered variety -- each with every variety it approves
		"specs": _option_specs(ln.custom_line, source, customer, [o["variety"] for o in options]),
		"customer": customer,
		"color": color,
		"variety": ln.item_code,
		"length": ln.custom_length,
		"needed": flt(ln.stock_qty),
		"allocated": allocated,
		"held": held,
		"own_free": own_free,
		"per_box": flt(ln.stock_qty) / (cint(ln.custom_number_of_boxes) or 1),
		"per_bunch": flt(ln.conversion_factor) or 1,
		"add_mode": add_mode,
		"add_step": step,
		"splittable": not (cint(ln.custom_mixed_box) or cint(ln.custom_mixed_bunch)),
		"add_stems": add_stems,
		"options": options,
	}


def _mix_members(row):
	"""The lines packed in the same mixed box or bunch as this one, in order, with the group's field."""
	field = "custom_bunch_group" if cint(row.get("custom_mixed_bunch")) else "custom_mix_group"
	return (
		field,
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- the columns are the table's own, values are bound
			f"SELECT * FROM `tabSales Order Item` WHERE parent = %s AND `{field}` = %s ORDER BY idx",  # nosemgrep
			(row["parent"], row[field]),
			as_dict=True,
		),
	)


def _mix_keep_boxes(sales_order_item, row, allocated, own_free):
	"""(boxes the mix keeps as ordered, its boxes): the most boxes any variety of the mix already holds, or
	this line can fill with its own variety. The rest of the mix's boxes can take another variety."""
	_field, members = _mix_members(row)
	boxes = cint(row.get("custom_number_of_boxes")) or 1
	keep = 0
	for m in members:
		share = flt(m.stock_qty) / (cint(m.custom_number_of_boxes) or 1)
		held = _held_stems(m.name)
		if share:
			keep = max(keep, math.ceil(held / share - 1e-9))
	share = flt(row.get("stock_qty")) / boxes
	if share:
		keep = max(keep, math.floor((allocated + own_free) / share + 1e-9))
	return min(keep, boxes), boxes


def _add_stems(sales_order_item, ln, allocated, own_free):
	"""Stems another variety takes when added to this line: the boxes the line cannot fill itself."""
	stems = flt(ln.stock_qty)
	boxes = cint(ln.custom_number_of_boxes) or 1
	box = stems / boxes or 1
	if cint(ln.custom_mixed_box) or cint(ln.custom_mixed_bunch):
		row = frappe.db.sql(
			"SELECT * FROM `tabSales Order Item` WHERE name = %s", sales_order_item, as_dict=True
		)
		keep, boxes = _mix_keep_boxes(sales_order_item, row[0], allocated, own_free)
		return (boxes - keep) * box
	held = allocated + _pending_stems(sales_order_item)
	keep = min(
		stems,
		max(math.ceil(held / box - 1e-9) * box, math.floor((allocated + own_free) / box + 1e-9) * box),
	)
	return stems - keep


@frappe.whitelist(methods=["POST"])
def add_variety(sales_order_item: str, location: str, variety: str):
	"""Fill what a short line cannot fill itself with another variety. A straight line: the variety takes the
	line's remaining boxes as a line of its own. A line in a mix: the mix keeps the boxes its varieties
	already hold or can fill as ordered, and its later boxes become a mix of their own with this variety in
	the line's place."""
	sub = get_shortage_substitutes(sales_order_item, location)
	opt = next((o for o in sub["options"] if o["variety"] == variety), None)
	if not opt:
		frappe.throw(_("{0} is not a substitute for {1} here.").format(variety, sub["variety"]))
	if sub["add_stems"] <= 1e-9:
		frappe.throw(_("{0} can fill this line itself.").format(sub["variety"]))
	# only what the variety has on the shelves, in whole boxes: the rest stays on the line's own variety
	give = opt["adds"]
	if give <= 1e-9:
		frappe.throw(
			_("{0} has {1} stems free, less than the {2} it needs.").format(
				variety, int(opt["free"]), int(sub.get("add_step") or sub["per_box"])
			)
		)
	if opt.get("mode") == "share":
		return _share_spread(sales_order_item, location, variety, give)
	if sub["splittable"]:
		return split_substitute(sales_order_item, location, [{"variety": variety, "stems": give}])
	return _split_mix(sales_order_item, location, variety, round(give / (sub["per_box"] or 1)))


def _share_spread(sales_order_item, location, variety, give):
	"""Put `give` stems of another variety beside a line of a mix, spread over its boxes in whole bunches: an
	even share in every box, and when the bunches do not divide evenly, the first boxes take one bunch more
	(the mix is split into those boxes and the rest, each with its own share)."""
	row = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		["parent", "custom_number_of_boxes", "conversion_factor"],
		as_dict=True,
	)
	boxes = cint(row.custom_number_of_boxes) or 1
	bunch = flt(row.conversion_factor) or 1
	q, rem = divmod(int(give / bunch + 1e-9), boxes)
	if not rem:
		return _share_split(sales_order_item, variety, q * boxes * bunch)
	# a spec's mixed bunches share this box: it is not split by boxes, so every box takes the even share
	lines = frappe.get_all(
		"Sales Order Item",
		filters={"parent": row.parent},
		fields=[
			"name",
			"parent",
			"custom_number_of_boxes",
			"custom_mixed_box",
			"custom_mix_group",
			"custom_mix_name",
			"custom_line",
			"custom_length",
			"custom_mixed_bunch",
			"custom_bunch_group",
		],
	)
	keys = box_keys(lines)
	if any(keys[ln.name] == keys[sales_order_item] and cint(ln.custom_mixed_bunch) for ln in lines):
		if not q:
			frappe.throw(
				_("{0} stems do not reach one bunch in each of the {1} boxes.").format(int(give), boxes)
			)
		return _share_split(sales_order_item, variety, q * boxes * bunch)
	# the first `rem` boxes take q + 1 bunches, the other boxes q
	split = _split_mix(sales_order_item, location, None, boxes - rem, plain=True)
	later = next(p["sales_order_item"] for p in split["parts"] if p["from"] == sales_order_item)
	parts = _share_split(sales_order_item, variety, (q + 1) * rem * bunch)["parts"]
	if q:
		parts += _share_split(later, variety, q * (boxes - rem) * bunch)["parts"]
	return {
		"kept": flt(frappe.db.get_value("Sales Order Item", sales_order_item, "stock_qty")),
		"parts": parts,
	}


def _share_split(sales_order_item, variety, give):
	"""A line of a mix gives `give` stems of its share of every box to another variety: a new line of the
	same mix, the same boxes, beside it in each box. The box still holds what it was ordered to."""
	row = frappe.db.sql("SELECT * FROM `tabSales Order Item` WHERE name = %s", sales_order_item, as_dict=True)
	if not row:
		frappe.throw(_("Order line not found."))
	row = row[0]
	if frappe.db.get_value("Sales Order", row.parent, "docstatus") != 1:
		frappe.throw(_("The order is not submitted."))
	stems = flt(row.stock_qty)
	boxes = cint(row.custom_number_of_boxes) or 1
	held = flt(sa._cumulative_allocated_stems(sales_order_item)) + _pending_stems(sales_order_item)
	if give <= 0 or stems - give < held - 1e-6:
		frappe.throw(
			_("{0} already has {1} stems allocated; it can give no more than {2}.").format(
				row.item_code, int(held), int(max(0, stems - held))
			)
		)
	share = give / stems
	cols = set(frappe.db.get_table_columns("Sales Order Item"))
	item = frappe.db.get_value("Item", variety, ["item_name", "description"], as_dict=True)
	new = dict(row)
	for f in SPLIT_SCALED:
		if f in new and new[f] is not None:
			new[f] = flt(flt(row[f]) * share, 6)
	new.update(
		name=frappe.generate_hash(length=10),
		idx=cint(row.idx) + 1,
		item_code=variety,
		item_name=item.item_name,
		description=item.description or item.item_name,
		custom_number_of_boxes=boxes,
		custom_packrate_mixed_box=give / boxes,
		custom_packrate=str(int(give / boxes)) if row.get("custom_packrate") else row.get("custom_packrate"),
		custom_opl=None,
		custom_fully_allocated=0,
		delivered_qty=0,
		picked_qty=0,
		creation=frappe.utils.now(),
		modified=frappe.utils.now(),
	)
	# the new line sits right after the one it shares the box with
	frappe.db.sql(
		"UPDATE `tabSales Order Item` SET idx = idx + 1 WHERE parent = %s AND idx > %s", (row.parent, row.idx)
	)
	keep_cols = {k: v for k, v in new.items() if k in cols}
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- the columns are the table's own, values are bound
		"INSERT INTO `tabSales Order Item` ({0}) VALUES ({1})".format(  # nosemgrep
			", ".join(f"`{k}`" for k in keep_cols), ", ".join(["%s"] * len(keep_cols))
		),
		list(keep_cols.values()),
	)
	sets = {
		f: flt(flt(row[f]) * (1 - share), 6) for f in SPLIT_SCALED if f in cols and row.get(f) is not None
	}
	sets["custom_packrate_mixed_box"] = (stems - give) / boxes
	if row.get("custom_packrate"):
		sets["custom_packrate"] = str(int((stems - give) / boxes))
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- the columns are the table's own, values are bound
		"UPDATE `tabSales Order Item` SET {0}, modified = NOW() WHERE name = %(name)s".format(  # nosemgrep
			", ".join(f"`{k}` = %({k})s" for k in sets)
		),
		{**sets, "name": sales_order_item},
	)
	frappe.db.sql("UPDATE `tabSales Order` SET modified = NOW() WHERE name = %s", row.parent)
	frappe.db.commit()  # nosemgrep
	frappe.log_error(
		title="Variety split",
		message="{0} {1}: {2} gives {3} stems of each box's share to {4} ({5} boxes)".format(
			row.parent, sales_order_item, row.item_code, int(give), variety, boxes
		),
	)
	return {
		"kept": stems - give,
		"parts": [{"variety": variety, "stems": give, "sales_order_item": new["name"]}],
	}


def _split_mix(sales_order_item, location, variety, new_boxes=None, plain=False):
	"""Split a mix by boxes: its first boxes stay as they are, the rest become a mix of their own (the same
	varieties; with `variety` in this line's place unless `plain`)."""
	row = frappe.db.sql("SELECT * FROM `tabSales Order Item` WHERE name = %s", sales_order_item, as_dict=True)
	if not row:
		frappe.throw(_("Order line not found."))
	row = row[0]
	so = row.parent
	if frappe.db.get_value("Sales Order", so, "docstatus") != 1:
		frappe.throw(_("The order is not submitted."))
	if plain:
		boxes = cint(row.custom_number_of_boxes) or 1
		keep = boxes - cint(new_boxes)
		if not 0 < keep < boxes:
			frappe.throw(_("A mix of {0} boxes cannot keep {1}.").format(boxes, keep))
	else:
		sub = get_shortage_substitutes(sales_order_item, location)
		keep, boxes = _mix_keep_boxes(sales_order_item, row, sub["allocated"], sub["own_free"])
		# the new variety takes only the boxes its free stems fill; the mix keeps the rest as ordered
		if new_boxes is not None:
			keep = max(keep, boxes - max(0, cint(new_boxes)))
		if keep >= boxes:
			frappe.throw(_("{0} can fill this line itself.").format(row.item_code))
		# nothing of the mix held yet: the line's variety is simply replaced
		if keep <= 0:
			return sa.substitute_variety(so, sales_order_item, variety)
	field, members = _mix_members(row)
	cols = set(frappe.db.get_table_columns("Sales Order Item"))
	used = [
		cint(v)
		for v in frappe.get_all("Sales Order Item", filters={"parent": so}, pluck=field, distinct=True)
		if str(v or "").isdigit()
	]
	group = str(max(used or [0]) + 1)
	last = max(cint(m.idx) for m in members)
	later = len(members)
	# the new mix's boxes come straight after the mix's own: every later box keeps its number
	frappe.db.sql(
		"UPDATE `tabSales Order Item` SET idx = idx + %s WHERE parent = %s AND idx > %s", (later, so, last)
	)
	item = (
		frappe.db.get_value("Item", variety, ["item_name", "description"], as_dict=True)
		if not plain
		else None
	)
	made = []
	for i, m in enumerate(members, 1):
		n = cint(m.custom_number_of_boxes) or boxes
		share = keep / n
		new = dict(m)
		for f in SPLIT_SCALED:
			if f in new and new[f] is not None:
				new[f] = flt(flt(m[f]) * (1 - share), 6)
		new.update(
			name=frappe.generate_hash(length=10),
			idx=last + i,
			custom_number_of_boxes=n - keep,
			custom_opl=None,
			custom_fully_allocated=0,
			delivered_qty=0,
			picked_qty=0,
			creation=frappe.utils.now(),
			modified=frappe.utils.now(),
		)
		new[field] = group
		if m.name == sales_order_item and not plain:
			new.update(
				item_code=variety,
				item_name=item.item_name,
				description=item.description or item.item_name,
			)
		keep_cols = {k: v for k, v in new.items() if k in cols}
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- the columns are the table's own, values are bound
			"INSERT INTO `tabSales Order Item` ({0}) VALUES ({1})".format(  # nosemgrep
				", ".join(f"`{k}`" for k in keep_cols), ", ".join(["%s"] * len(keep_cols))
			),
			list(keep_cols.values()),
		)
		sets = {f: flt(flt(m[f]) * share, 6) for f in SPLIT_SCALED if f in cols and m.get(f) is not None}
		sets["custom_number_of_boxes"] = keep
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- the columns are the table's own, values are bound
			"UPDATE `tabSales Order Item` SET {0}, modified = NOW() WHERE name = %(name)s".format(  # nosemgrep
				", ".join(f"`{k}` = %({k})s" for k in sets)
			),
			{**sets, "name": m.name},
		)
		made.append(
			{
				"variety": new["item_code"],
				"stems": flt(new.get("stock_qty")),
				"sales_order_item": new["name"],
				"from": m.name,
			}
		)
	frappe.db.sql("UPDATE `tabSales Order` SET modified = NOW() WHERE name = %s", so)
	frappe.db.commit()  # nosemgrep
	frappe.log_error(
		title="Variety split",
		message="{0} {1}: mix {2} keeps {3} of {4} boxes; boxes after take {5} for {6} as mix {7}".format(
			so, sales_order_item, row[field], keep, boxes, variety, row.item_code, group
		),
	)
	return {"kept": flt(row.stock_qty) * keep / boxes, "boxes": keep, "parts": made}


#: Sales Order Item amounts that follow the stems when a line is split.
SPLIT_SCALED = (
	"qty",
	"stock_qty",
	"amount",
	"base_amount",
	"net_amount",
	"base_net_amount",
	"custom_ordered_quantity",
)


@frappe.whitelist(methods=["POST"])
def split_substitute(sales_order_item: str, location: str, parts: str | list):
	"""Fill a short straight line with more than one variety of the same colour (or that its specification
	approves): `parts` [{variety, stems}] are the stems other varieties take, in whole boxes; the line keeps
	its own variety for the rest. Each part becomes a line of its own on the order, the same length, box,
	rate and specification, its boxes and amounts in proportion."""
	if isinstance(parts, str):
		parts = json.loads(parts or "[]")
	row = frappe.db.sql("SELECT * FROM `tabSales Order Item` WHERE name = %s", sales_order_item, as_dict=True)
	if not row:
		frappe.throw(_("Order line not found."))
	row = row[0]
	if cint(row.custom_mixed_box) or cint(row.custom_mixed_bunch):
		frappe.throw(_("A line in a mix is substituted whole, not split."))
	if frappe.db.get_value("Sales Order", row.parent, "docstatus") != 1:
		frappe.throw(_("The order is not submitted."))
	opts = {o["variety"] for o in get_shortage_substitutes(sales_order_item, location)["options"]}
	stems = flt(row.stock_qty)
	boxes = cint(row.custom_number_of_boxes) or 1
	per_box = stems / boxes
	per_bunch = flt(row.conversion_factor) or 1
	parts = [p for p in parts or [] if flt(p.get("stems")) > 0]
	if not parts:
		frappe.throw(_("Give stems to at least one other variety."))
	for p in parts:
		if p["variety"] not in opts:
			frappe.throw(_("{0} is not a substitute for {1} here.").format(p["variety"], row.item_code))
		n = flt(p["stems"]) / per_box
		if abs(n - round(n)) > 1e-6:
			frappe.throw(_("{0}: give whole boxes of {1} stems.").format(p["variety"], int(per_box)))
	moved = sum(flt(p["stems"]) for p in parts)
	keep = stems - moved
	held = flt(sa._cumulative_allocated_stems(sales_order_item)) + _pending_stems(sales_order_item)
	if keep < held - 1e-6:
		frappe.throw(
			_("{0} already has {1} stems allocated; it can keep no less than that.").format(
				row.item_code, int(held)
			)
		)
	if keep < 0:
		frappe.throw(_("More stems were given away than the line has."))
	cols = set(frappe.db.get_table_columns("Sales Order Item"))

	def scaled(base, share):
		out = dict(base)
		for f in SPLIT_SCALED:
			if f in out and out[f] is not None:
				out[f] = flt(base[f]) * share
		out["custom_number_of_boxes"] = round(boxes * share)
		return out

	last_idx = cint(
		frappe.db.sql("SELECT MAX(idx) FROM `tabSales Order Item` WHERE parent = %s", row.parent)[0][0]
	)
	made = []
	# nothing left for the line's own variety: the line itself becomes the first substitute
	first = None
	if keep <= 1e-6:
		first, parts = parts[0], parts[1:]
		keep = flt(first["stems"])
	for i, p in enumerate(parts, 1):
		item = frappe.db.get_value("Item", p["variety"], ["item_name", "description"], as_dict=True)
		new = scaled(row, flt(p["stems"]) / stems)
		new.update(
			name=frappe.generate_hash(length=10),
			idx=last_idx + i,
			item_code=p["variety"],
			item_name=item.item_name,
			description=item.description or item.item_name,
			custom_opl=None,
			custom_fully_allocated=0,
			delivered_qty=0,
			picked_qty=0,
			creation=frappe.utils.now(),
			modified=frappe.utils.now(),
		)
		keep_cols = {k: v for k, v in new.items() if k in cols}
		names = ", ".join(f"`{k}`" for k in keep_cols)
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- the columns are the table's own, values are bound
			f"INSERT INTO `tabSales Order Item` ({names}) VALUES ({', '.join(['%s'] * len(keep_cols))})",  # nosemgrep
			list(keep_cols.values()),
		)
		made.append({"variety": p["variety"], "stems": flt(p["stems"]), "sales_order_item": new["name"]})
	# the line keeps its own variety for the rest
	mine = scaled(row, keep / stems)
	sets = {f: mine[f] for f in (*SPLIT_SCALED, "custom_number_of_boxes") if f in cols}
	if first:
		item = frappe.db.get_value("Item", first["variety"], ["item_name", "description"], as_dict=True)
		sets.update(
			item_code=first["variety"],
			item_name=item.item_name,
			description=item.description or item.item_name,
		)
		made.insert(0, {"variety": first["variety"], "stems": keep, "sales_order_item": sales_order_item})
		keep = 0.0
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- the columns are the table's own, values are bound
		"UPDATE `tabSales Order Item` SET {0}, modified = NOW() WHERE name = %(name)s".format(  # nosemgrep
			", ".join(f"`{k}` = %({k})s" for k in sets)
		),
		{**sets, "name": sales_order_item},
	)
	frappe.db.sql("UPDATE `tabSales Order` SET modified = NOW() WHERE name = %s", row.parent)
	frappe.db.commit()  # nosemgrep
	frappe.log_error(
		title="Variety split",
		message="{0} {1}: {2} keeps {3} stems; {4}".format(
			row.parent,
			sales_order_item,
			row.item_code,
			int(keep),
			", ".join("{0} {1}".format(m["variety"], int(m["stems"])) for m in made),
		),
	)
	return {"kept": keep, "parts": made}
