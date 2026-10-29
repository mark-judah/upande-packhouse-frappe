# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Planning v2: team crops, the team sequence board, the confirmation summary and substitution.

Crops are the Item Groups directly under "Cut Flowers" (Standard Roses, Spray Roses, Chrysanthemums, ...).
A Packing Team lists the crops it packs (Packing Team Crop): one default, the others its fall-back when the
default has no orders that day.

The sequence is per team: every team has its own #1, #2, ... Allocation takes every team's #1, then every
team's #2 (Sales Allocation v2). It is stored as one number per order (Sales Order.custom_allocation_sequence),
written in rounds, so the per-team position and the round order both come back from it.
"""

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_packhouse import allocation_plan as plans
from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.api.v2.allocation_checks import mix_key, per_box

CROP_ROOT = "Cut Flowers"
KEPT = (*plans.LIVE, "Fulfilled")


# ---------------------------------------------------------------- crops


def crops():
	return frappe.get_all(
		"Item Group", filters={"parent_item_group": CROP_ROOT}, pluck="name", order_by="name"
	)


def crop_of_groups():
	"""{item_group: crop} for every group under a crop."""
	out = {}
	for crop in crops():
		b = frappe.db.get_value("Item Group", crop, ["lft", "rgt"], as_dict=True)
		for g in frappe.get_all(
			"Item Group", filters={"lft": (">=", b.lft), "rgt": ("<=", b.rgt)}, pluck="name"
		):
			out[g] = crop
	return out


def team_crops():
	"""{team: {"default": crop | None, "others": [crop, ...]}} for every Packing Team."""
	out = {
		t: {"default": None, "others": []}
		for t in frappe.get_all("Packing Teams", pluck="name", order_by="name")
	}
	for r in frappe.get_all(
		"Packing Team Crop",
		filters={"parenttype": "Packing Teams"},
		fields=["parent", "crop", "is_default"],
		order_by="idx",
	):
		if r.parent not in out:
			continue
		if cint(r.is_default) and not out[r.parent]["default"]:
			out[r.parent]["default"] = r.crop
		else:
			out[r.parent]["others"].append(r.crop)
	return out


def _crop_demand(day, farm):
	"""{crop: stems still to plan} for the day."""
	groups = crop_of_groups()
	out = defaultdict(float)
	for ln in pg._lines_for(day, farm):
		left = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
		out[groups.get(ln.item_group) or ""] += left
	return out


def plans_for(tc, demand):
	"""The crop a team plans today: its default when that has orders, else its first fall-back that has."""
	for crop in [tc["default"], *tc["others"]]:
		if crop and demand.get(crop, 0) > 0:
			return crop
	return None


@frappe.whitelist()
def get_team_crops(delivery_date: str | None = None, farm: str | None = None):
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	demand = _crop_demand(day, farm)
	tc = team_crops()
	rows = []
	for team, c in tc.items():
		today_crop = plans_for(c, demand)
		rows.append(
			{
				"team": team,
				"default": c["default"],
				"others": c["others"],
				"plans_for": today_crop,
				"fallback": bool(today_crop and today_crop != c["default"]),
				"no_crop": not c["default"],
			}
		)
	return {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"crops": crops(),
		"teams": rows,
		"demand": demand,
	}


@frappe.whitelist()
def save_team_crops(team: str, rows: str | list):
	"""Replace a team's crops: [{"crop": ..., "is_default": 0|1}], in fall-back order."""
	frappe.only_for(pg.PLANNERS)
	if isinstance(rows, str):
		rows = json.loads(rows or "[]")
	allowed = set(crops())
	doc = frappe.get_doc("Packing Teams", team)
	doc.set("crops", [])
	for r in rows:
		if not r.get("crop"):
			continue
		if r["crop"] not in allowed:
			frappe.throw(_("{0} is not a crop.").format(r["crop"]))
		doc.append("crops", {"crop": r["crop"], "is_default": cint(r.get("is_default"))})
	doc.save()
	return {"success": True}


@frappe.whitelist()
def teams_by_crop():
	"""{crop: [team, ...]}: the teams that pack it, default teams first. Teams with no crop are left out."""
	out = defaultdict(list)
	tc = team_crops()
	for team, c in tc.items():
		if c["default"]:
			out[c["default"]].append(team)
	for team, c in tc.items():
		for crop in c["others"]:
			if team not in out[crop]:
				out[crop].append(team)
	return {"success": True, "teams": dict(out), "crop_of": crop_of_groups()}


# ---------------------------------------------------------------- the team board


def _order_teams(plan):
	"""{sales_order: team} (the team planned for most of its stems) and {sales_order: stems}."""
	team_of, stems = {}, defaultdict(float)
	for so, team, s in frappe.db.sql(
		"""SELECT sales_order, team, SUM(stems) FROM `tabAllocation Confirmation`
		   WHERE plan = %(p)s AND IFNULL(team, '') != ''
		     AND (status IN %(k)s OR (status = 'Released' AND release_reason = 'Allocated'))
		   GROUP BY sales_order, team ORDER BY SUM(stems) DESC""",
		{"p": plan, "k": KEPT},
	):
		team_of.setdefault(so, team)
		stems[so] += flt(s)
	return team_of, stems


def _queues(plan):
	"""{team: [sales_order, ...]} in sequence order."""
	team_of, _stems = _order_teams(plan)
	seq = dict(
		frappe.db.sql(
			"SELECT name, custom_allocation_sequence FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": tuple(team_of) or ("",)},
		)
	)
	q = defaultdict(list)
	for so in sorted(team_of, key=lambda s: (not cint(seq.get(s)), cint(seq.get(s)), s)):
		q[team_of[so]].append(so)
	return q


def _write_rounds(plan, queues):
	"""Number the plan in rounds: every team's #1 (teams by name), then every team's #2, ..."""
	teams = sorted(queues)
	order = [
		queues[t][i]
		for i in range(max((len(v) for v in queues.values()), default=0))
		for t in teams
		if i < len(queues[t])
	]
	plans.sequence_orders(plan, first=order)


@frappe.whitelist()
def get_team_board(delivery_date: str | None = None, farm: str | None = None):
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	plan = pg._plan_name(day, farm)
	tc = team_crops()
	demand = _crop_demand(day, farm)
	lines = {ln.name: ln for ln in pg._lines_for(day, farm)}
	queues = _queues(plan) if plan else {}
	team_of, stems = _order_teams(plan) if plan else ({}, {})
	info = {
		r.name: r
		for r in frappe.get_all(
			"Sales Order",
			filters={"name": ("in", list(team_of) or [""])},
			fields=["name", "customer", "customer_name", "custom_order_name", "delivery_date"],
		)
	}
	confs = plans.live_confirmations(plan=plan) if plan else []
	short_of = defaultdict(float)
	confirmed_of = defaultdict(float)
	for c in confs:
		confirmed_of[c.sales_order_item] += max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
	boxes_of = defaultdict(float)
	for ln in lines.values():
		if ln.parent in team_of:
			left = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems) - confirmed_of.get(ln.name, 0.0))
			short_of[ln.parent] += left
			size = per_box(ln)
			if size and not mix_key(ln):
				boxes_of[ln.parent] += flt(ln.ordered_stems) / size
	mixes = defaultdict(set)
	for ln in lines.values():
		if mix_key(ln) and ln.parent in team_of:
			mixes[ln.parent].add(mix_key(ln))
	for so, keys in mixes.items():
		for k in keys:
			first = next(x for x in lines.values() if mix_key(x) == k)
			boxes_of[so] += cint(first.custom_number_of_boxes) or 1
	teams = []
	for team in sorted(tc):
		c = tc[team]
		q = queues.get(team, [])
		cards = []
		for i, so in enumerate(q, 1):
			o = info.get(so) or {}
			flag = _("Short {0}").format(int(short_of[so])) if short_of[so] > 0.5 else ""
			cards.append(
				{
					"n": i,
					"sales_order": so,
					"customer": o.get("customer_name") or o.get("customer") or "",
					"order_name": o.get("custom_order_name") or so,
					"stems": stems.get(so, 0.0),
					"boxes": round(boxes_of.get(so, 0.0)),
					"flag": flag,
				}
			)
		teams.append(
			{
				"team": team,
				"default": c["default"],
				"others": c["others"],
				"plans_for": plans_for(c, demand),
				"no_crop": not c["default"],
				"orders": cards,
				"stems": sum(x["stems"] for x in cards),
				"boxes": sum(x["boxes"] for x in cards),
			}
		)
	heaviest = max((t["stems"] for t in teams), default=0.0) or 1.0
	for t in teams:
		t["load_pct"] = round(100.0 * t["stems"] / heaviest)
	rounds = max((len(t["orders"]) for t in teams), default=0)
	flow = [
		{"round": r + 1, "steps": [{"team": t["team"], "n": r + 1} for t in teams if r < len(t["orders"])]}
		for r in range(rounds)
	]
	return {"success": True, "delivery_date": day, "farm": farm, "teams": teams, "flow": flow}


@frappe.whitelist()
def move_order(delivery_date: str, farm: str, sales_order: str, team: str, position: int):
	"""Drag on the team board: put an order at `position` (1-based) in `team`'s queue. Moving it to another team
	gives every line of the order that team."""
	frappe.only_for(pg.PLANNERS)
	day = pg._day(delivery_date)
	plan = pg._plan_name(day, farm)
	if not plan:
		frappe.throw(_("Nothing is planned for {0} yet.").format(day))
	if not frappe.db.exists("Packing Teams", team):
		frappe.throw(_("{0} is not a team we know.").format(team))
	tc = team_crops().get(team) or {}
	if not tc.get("default"):
		frappe.throw(_("{0} has no crop set. Set its crop first.").format(team))
	frappe.db.sql(
		"""UPDATE `tabAllocation Confirmation` SET team = %(t)s
		   WHERE plan = %(p)s AND sales_order = %(so)s AND status IN %(k)s""",
		{"t": team, "p": plan, "so": sales_order, "k": KEPT},
	)
	queues = _queues(plan)
	for t in queues:
		if sales_order in queues[t]:
			queues[t].remove(sales_order)
	q = queues.setdefault(team, [])
	q.insert(max(0, min(len(q), cint(position) - 1)), sales_order)
	_write_rounds(plan, queues)
	return {"success": True}


@frappe.whitelist()
def balance_teams(delivery_date: str, farm: str):
	"""Re-sequence in rounds without moving any order between teams (after a confirm or undo)."""
	frappe.only_for(pg.PLANNERS)
	plan = pg._plan_name(pg._day(delivery_date), farm)
	if plan:
		_write_rounds(plan, _queues(plan))
	return {"success": True}


# ---------------------------------------------------------------- confirmation summary


@frappe.whitelist()
def get_confirmation_summary(delivery_date: str | None = None, farm: str | None = None):
	"""Every confirmed order with its lines: what each counts on, what the system did, and the result."""
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	plan = pg._plan_name(day, farm)
	lines = {ln.name: ln for ln in pg._lines_for(day, farm)}
	empty = {"confirmed": 0, "net": 0, "rolling": 0, "changed": 0, "left": 0, "lines_left": 0}
	if not plan:
		return {"success": True, "delivery_date": day, "farm": farm, "orders": [], "totals": empty}
	confs = frappe.get_all(
		"Allocation Confirmation",
		filters={"plan": plan, "status": ("in", KEPT)},
		fields=[
			"sales_order",
			"sales_order_item",
			"source",
			"stems",
			"fulfilled_stems",
			"variety",
			"length",
			"ordered_variety",
			"ordered_length",
			"team",
			"farm",
		],
	)
	by_line = defaultdict(list)
	for c in confs:
		by_line[c.sales_order_item].append(c)
	team_of, _stems = _order_teams(plan)
	queues = _queues(plan)
	pos = {so: i for q in queues.values() for i, so in enumerate(q, 1)}
	orders = {}
	totals = dict(empty)
	for soi, cs in by_line.items():
		ln = lines.get(soi)
		if not ln:
			continue
		ordered = flt(ln.ordered_stems)
		held = sum(flt(c.stems) for c in cs if c.source == "Real")
		expected = sum(flt(c.stems) for c in cs if c.source != "Real")
		confirmed = held + expected
		left = max(0.0, ordered - flt(ln.allocated_stems) - confirmed)
		c0 = cs[0]
		done = []
		if c0.ordered_variety and c0.variety != c0.ordered_variety:
			done.append(_("Substituted for {0}").format(c0.ordered_variety))
		if (c0.length or "") != (c0.ordered_length or "") and c0.length:
			done.append(_("Downgraded {0} for {1}").format(c0.length, c0.ordered_length))
		if left > 0.5:
			size = per_box(ln)
			done.append(
				_("Part: {0} of {1} boxes").format(
					int((confirmed + flt(ln.allocated_stems)) / size + 1e-9),
					cint(ln.custom_number_of_boxes) or 1,
				)
				if size and not mix_key(ln)
				else _("Part")
			)
		if expected and not held:
			done.append(_("Confirmed on forecast"))
		result = (_("{0} open").format(int(left)), "warn") if left > 0.5 else (_("Full"), "ok")
		if done and done[0].startswith(_("Downgraded")) and left <= 0.5:
			result = (_("Full · downgrade reason"), "warn")
		if expected and not held and left <= 0.5:
			result = (_("Full · on forecast"), "blue")
		o = orders.setdefault(
			ln.parent,
			{
				"sales_order": ln.parent,
				"customer": ln.customer_name or ln.customer,
				"order_name": "",
				"team": team_of.get(ln.parent) or c0.team or "",
				"seq": pos.get(ln.parent, 0),
				"delivery": str(ln.delivery_date),
				"ordered": 0.0,
				"confirmed": 0.0,
				"lines_done": 0,
				"lines_total": sum(1 for x in lines.values() if x.parent == ln.parent),
				"items": [],
			},
		)
		o["ordered"] += ordered
		o["confirmed"] += confirmed
		o["lines_done"] += 1
		o["items"].append(
			{
				"soi": soi,
				"variety": "{0} {1}".format(c0.variety, c0.length or "").strip(),
				"note": _("ordered {0}").format(c0.ordered_variety)
				if c0.ordered_variety and c0.ordered_variety != c0.variety
				else _("ordered {0}").format(c0.ordered_length)
				if c0.length and c0.ordered_length and c0.length != c0.ordered_length
				else (ln.custom_mix_name if mix_key(ln) else ""),
				"ordered": ordered,
				"confirmed": confirmed,
				"held": held,
				"expected": expected,
				"done": done,
				"result": result[0],
				"kind": result[1],
			}
		)
		totals["confirmed"] += confirmed
		totals["net"] += held
		totals["rolling"] += expected
		totals["changed"] += (
			confirmed
			if (
				c0.variety != (c0.ordered_variety or c0.variety)
				or (c0.length or "") != (c0.ordered_length or c0.length or "")
			)
			else 0.0
		)
		totals["left"] += left
		totals["lines_left"] += 1 if left > 0.5 else 0
	names = dict(
		frappe.db.sql(
			"SELECT name, custom_order_name FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": tuple(orders) or ("",)},
		)
	)
	for o in orders.values():
		o["order_name"] = names.get(o["sales_order"]) or o["sales_order"]
		short = o["ordered"] - o["confirmed"]
		o["result"], o["kind"] = (
			(_("Full"), "ok") if short < 0.5 else (_("{0} open").format(int(short)), "warn")
		)
	out = sorted(orders.values(), key=lambda o: (not o["seq"], o["seq"], o["team"], o["customer"] or ""))
	totals["orders"] = len(out)
	totals["lines"] = sum(len(o["items"]) for o in out)
	totals["all_lines"] = len(lines)
	return {"success": True, "delivery_date": day, "farm": farm, "orders": out, "totals": totals}


# ---------------------------------------------------------------- substitution


@frappe.whitelist()
def substitute_and_confirm(delivery_date: str, farm: str, sales_order_item: str, variety: str, team: str):
	"""Substitute an order line's variety with one its specification approves, then confirm the line in full
	on the new variety. The line itself changes (as Sales Allocation's Substitute does), so allocation follows."""
	frappe.only_for(pg.PLANNERS)
	from upande_packhouse.api.v2 import allocation_guards as guards
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
		substitute_variety,
	)

	day = pg._day(delivery_date)
	ln = pg._line(sales_order_item)
	if str(ln.delivery_date) != day:
		frappe.throw(_("That order line is for {0}, not {1}.").format(ln.delivery_date, day))
	approved = guards._spec_varieties([ln]).get(ln.custom_line) or set()
	if variety not in approved:
		frappe.throw(
			_("{0}'s specification does not approve {1}.").format(ln.customer_name or ln.customer, variety)
		)
	ordered_variety = ln.item_code
	for c in plans.live_confirmations(soi=sales_order_item):
		plans.release_confirmation(c.name, "Line changed")
	res = substitute_variety(ln.parent, sales_order_item, variety) or {}
	if not res.get("success"):
		frappe.throw(res.get("message") or _("Could not substitute."))
	ln = pg._line(sales_order_item)
	stems = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
	r = pg.confirm_lines(day, farm, [{"sales_order_item": sales_order_item, "stems": stems, "team": team}])
	# the plan remembers what was ordered, so the summary can say "Substituted for ..."
	frappe.db.sql(
		"""UPDATE `tabAllocation Confirmation` SET ordered_variety = %(v)s, flag = 'Other variety'
		   WHERE sales_order_item = %(soi)s AND status IN %(live)s""",
		{"v": ordered_variety, "soi": sales_order_item, "live": plans.LIVE},
	)
	return {"success": True, "substituted": variety, **r}
