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
from frappe.utils import cint, flt, getdate

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
	"""{team: {"default": crop | None, "others": [crop, ...], "customers": [customer, ...]}} for every
	Packing Team."""
	out = {
		t: {"default": None, "others": [], "customers": [], "locations": [], "start_time": "07:00"}
		for t in frappe.get_all("Packing Teams", pluck="name", order_by="name")
	}
	for t, loc, start in frappe.get_all(
		"Packing Teams", fields=["name", "location", "start_time"], as_list=True
	):
		if t not in out:
			continue
		if loc:
			out[t]["locations"] = [loc]
		if start:
			out[t]["start_time"] = _hhmm(start)
	if frappe.db.table_exists("Packing Team Customer"):
		for r in frappe.get_all(
			"Packing Team Customer",
			filters={"parenttype": "Packing Teams"},
			fields=["parent", "customer"],
			order_by="idx",
		):
			if r.parent in out:
				out[r.parent]["customers"].append(r.customer)
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
	location = location_of(farm)
	# only the teams that work at the sales farm's location
	tc = {t: c for t, c in team_crops().items() if works_at(c, location)}
	rows = []
	for team, c in tc.items():
		today_crop = plans_for(c, demand)
		rows.append(
			{
				"team": team,
				"default": c["default"],
				"others": c["others"],
				"customers": c["customers"],
				"locations": c["locations"],
				"start_time": c["start_time"],
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
		"customers": flower_customers({x for c in tc.values() for x in c["customers"]}),
		"locations": sales_locations(),
		"teams": rows,
		"demand": demand,
		"rates": rates(),
	}


def sales_locations():
	"""The locations farms process orders at (Farm.farm_location), e.g. Karen and Ravine."""
	return sorted(
		set(frappe.get_all("Farm", filters={"farm_location": ("is", "set")}, pluck="farm_location"))
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def sales_location_query(
	doctype: str, txt: str, searchfield: str, start: int, page_len: int, filters: dict | None = None
):
	"""Link search for a team's Location: the sales locations only."""
	txt = (txt or "").lower()
	names = [n for n in sales_locations() if txt in n.lower()]
	return [[n] for n in names[cint(start) : cint(start) + cint(page_len)]]


def sales_farm_names():
	"""The enabled Sales Shelf farms (Production Settings > Shelf Locations): the farms teams pack at."""
	ps = frappe.get_cached_doc("Production Settings")
	return sorted({r.farm for r in ps.get("shelf_locations") or [] if r.enabled and r.sales_shelf and r.farm})


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def sales_farm_query(
	doctype: str, txt: str, searchfield: str, start: int, page_len: int, filters: dict | None = None
):
	"""Link search for a team's Sales Farm: the Sales Shelf farms only."""
	txt = (txt or "").lower()
	names = [n for n in sales_farm_names() if txt in n.lower()]
	return [[n] for n in names[cint(start) : cint(start) + cint(page_len)]]


def team_sales_farms():
	"""{team: its sales farm} (a team with none: its location's farm)."""
	out = {}
	for r in frappe.get_all("Packing Teams", fields=["name", "sales_farm", "location"]):
		out[r.name] = r.sales_farm or r.location or ""
	return out


def check_team_farm(team, sales_order):
	"""Refuse a team of another sales farm than the order's: an order is packed by its own farm's teams."""
	from upande_packhouse.api.remote_transfer.transfer_scheduling import order_hub

	home = team_sales_farms().get(team) if team else None
	farm = order_hub(sales_order) if sales_order else None
	if home and farm and home != farm:
		frappe.throw(_("{0} packs at {1}, not at {2}.").format(team, home, farm))


def location_of(farm):
	return frappe.db.get_value("Farm", farm, "farm_location") if farm else None


def works_at(c, location):
	"""A team with no location set works at every location; one with a location only there."""
	return not location or not c["locations"] or location in c["locations"]


def teams_at(farm):
	"""The Packing Teams of this sales farm (a team with no sales farm or location works everywhere)."""
	location = location_of(farm)
	return [
		r.name
		for r in frappe.get_all("Packing Teams", fields=["name", "sales_farm", "location"], order_by="name asc")
		if not farm
		or (r.sales_farm == farm if r.sales_farm else (not location or not r.location or r.location == location))
	]


def flower_customers(keep=()):
	"""Customers who buy flowers (a submitted Sales Order for an item under Cut Flowers), plus `keep` (already
	on a team), by name."""
	groups = list(crop_of_groups())
	names = set(keep)
	if groups:
		names.update(
			frappe.db.sql_list(
				"""SELECT DISTINCT so.customer FROM `tabSales Order` so
				   JOIN `tabSales Order Item` soi ON soi.parent = so.name
				   JOIN `tabItem` i ON i.name = soi.item_code
				   WHERE so.docstatus = 1 AND i.item_group IN %(g)s""",
				{"g": tuple(groups)},
			)
		)
	if not names:
		return []
	return frappe.get_all(
		"Customer",
		filters={"name": ("in", list(names)), "disabled": 0},
		fields=["name", "customer_name"],
		order_by="customer_name",
	)


@frappe.whitelist()
def save_team_crops(
	team: str,
	rows: str | list,
	customers: str | list | None = None,
	location: str | None = None,
	start_time: str | None = None,
):
	"""Replace a team's crops: [{"crop": ..., "is_default": 0|1}], in fall-back order, and the customers it
	packs for, the location it works at ("" for every location) and the time it starts when those are sent."""
	frappe.only_for(pg.PLANNERS)
	if isinstance(rows, str):
		rows = json.loads(rows or "[]")
	if isinstance(customers, str):
		customers = json.loads(customers or "[]")
	allowed = set(crops())
	doc = frappe.get_doc("Packing Teams", team)
	doc.set("crops", [])
	for r in rows:
		if not r.get("crop"):
			continue
		if r["crop"] not in allowed:
			frappe.throw(_("{0} is not a crop.").format(r["crop"]))
		doc.append("crops", {"crop": r["crop"], "is_default": cint(r.get("is_default"))})
	if customers is not None:
		doc.set("customers", [])
		for c in dict.fromkeys(c for c in customers if c):
			if not frappe.db.exists("Customer", c):
				frappe.throw(_("{0} is not a customer.").format(c))
			doc.append("customers", {"customer": c})
	if location is not None:
		if location and location not in sales_locations():
			frappe.throw(_("{0} is not a sales location.").format(location))
		doc.location = location or None
	if start_time:
		doc.start_time = start_time
	doc.save()
	return {"success": True}


@frappe.whitelist()
def teams_by_crop(location: str | None = None, farm: str | None = None):
	"""{crop: [team, ...]}: the teams that pack it, default teams first, among the teams working at the
	location (or the farm's location). Teams with no crop are left out."""
	out = defaultdict(list)
	location = location or location_of(farm)
	tc = {t: c for t, c in team_crops().items() if works_at(c, location)}
	for team, c in tc.items():
		if c["default"]:
			out[c["default"]].append(team)
	for team, c in tc.items():
		for crop in c["others"]:
			if team not in out[crop]:
				out[crop].append(team)
	by_customer = defaultdict(list)
	for team, c in tc.items():
		for customer in c["customers"]:
			by_customer[customer].append(team)
	return {
		"success": True,
		"teams": dict(out),
		"crop_of": crop_of_groups(),
		"customer_teams": dict(by_customer),
	}


def team_for(customer, crop, by_crop, customer_teams):
	"""The team for a line: one that packs for its customer and packs its crop, else the crop's first team
	(its default team first)."""
	packs = by_crop.get(crop) or []
	served = [t for t in customer_teams.get(customer) or [] if t in packs]
	return (served or packs or [None])[0]


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
	crop_boxes = defaultdict(lambda: defaultdict(float))  # order -> crop -> boxes
	groups = crop_of_groups()
	for ln in lines.values():
		if ln.parent in team_of:
			left = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems) - confirmed_of.get(ln.name, 0.0))
			short_of[ln.parent] += left
			size = per_box(ln)
			if size and not mix_key(ln):
				boxes_of[ln.parent] += flt(ln.ordered_stems) / size
				crop_boxes[ln.parent][groups.get(ln.item_group) or ""] += flt(ln.ordered_stems) / size
	mixes = defaultdict(set)
	for ln in lines.values():
		if mix_key(ln) and ln.parent in team_of:
			mixes[ln.parent].add(mix_key(ln))
	for so, keys in mixes.items():
		for k in keys:
			first = next(x for x in lines.values() if mix_key(x) == k)
			boxes_of[so] += cint(first.custom_number_of_boxes) or 1
			crop_boxes[so][groups.get(first.item_group) or ""] += cint(first.custom_number_of_boxes) or 1
	# The teams working at this farm's location, and any other team that already has orders here.
	location = location_of(farm)
	tc = {t: c for t, c in team_crops().items() if works_at(c, location) or queues.get(t)}
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
	_add_workload(teams, day, {so: crop_boxes[so] for t in teams for so in queues.get(t["team"], [])}, queues)
	heaviest = max((t["stems"] for t in teams), default=0.0) or 1.0
	for t in teams:
		t["load_pct"] = round(100.0 * t["stems"] / heaviest)
	rounds = max((len(t["orders"]) for t in teams), default=0)
	flow = [
		{"round": r + 1, "steps": [{"team": t["team"], "n": r + 1} for t in teams if r < len(t["orders"])]}
		for r in range(rounds)
	]
	return {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"teams": teams,
		"flow": flow,
		"workload": _day_workload(teams),
	}


# ---------------------------------------------------------------- workforce and processing time


def team_people(day, teams):
	"""{team: [employee, ...]} at work on the day: the team's members and anyone moved into it for the day,
	less those moved out, off (weekly off or holiday) or on leave."""
	from upande_packhouse.api.v2.allocation_people import _employees, _leave, _off_days
	from upande_packhouse.upande_packhouse.doctype.packing_team_move.packing_team_move import moves_on

	day = getdate(day)
	members = defaultdict(set)
	for parent, emp in frappe.get_all(
		"Packing Team Member",
		filters={"parenttype": "Packing Teams", "parent": ("in", list(teams) or [""])},
		fields=["parent", "employee"],
		as_list=True,
	):
		if emp:
			members[parent].add(emp)
	moves = moves_on(day)
	for emp, m in moves.items():
		for t in members.values():
			t.discard(emp)
		if m.to_team in teams:
			members[m.to_team].add(emp)
	ids = sorted({e for t in members.values() for e in t})
	if not ids:
		return {t: [] for t in teams}
	staff = _employees(day, ids)
	working = {e.employee for e in staff}
	off, leave = _off_days(staff, day), _leave(ids, day)
	away = set(off) | set(leave)
	return {t: sorted(e for e in members.get(t, ()) if e in working and e not in away) for t in teams}


def _hhmm(v):
	"""A Time value ("7:00:00", timedelta or None) as "07:00"."""
	if v is None or v == "":
		return "07:00"
	if hasattr(v, "total_seconds"):
		m = int(v.total_seconds() // 60)
		return "%02d:%02d" % (m // 60 % 24, m % 60)
	h, m = (str(v).split(":") + ["0"])[:2]
	return "%02d:%02d" % (int(h), int(m))


def rates():
	"""{crop: boxes one person packs in an hour}."""
	return {
		r.crop: flt(r.boxes_per_person_hour)
		for r in frappe.get_all("Crop Processing Rate", fields=["crop", "boxes_per_person_hour"])
	}


def _add_workload(teams, day, crop_boxes, queues):
	"""Per team: the planned boxes by crop, the people at work, the hours the boxes take them and the time the
	last box is packed, from the team's start time."""
	from datetime import datetime, timedelta

	people = team_people(day, [t["team"] for t in teams])
	rate = rates()
	starts = {
		name: _hhmm(start)
		for name, start in frappe.get_all("Packing Teams", fields=["name", "start_time"], as_list=True)
	}
	for t in teams:
		by_crop = defaultdict(float)
		for so in queues.get(t["team"], []):
			for crop, boxes in crop_boxes.get(so, {}).items():
				by_crop[crop] += boxes
		n = len(people.get(t["team"], []))
		rows, hours, missing = [], 0.0, []
		for crop, boxes in sorted(by_crop.items()):
			r = rate.get(crop, 0.0)
			h = boxes / (r * n) if r and n else None
			if r:
				hours += h or 0.0
			else:
				missing.append(crop or _("No crop"))
			rows.append({"crop": crop, "boxes": round(boxes), "rate": r, "hours": h})
		start_dt = datetime.strptime(starts.get(t["team"]) or "07:00", "%H:%M")
		done = bool(by_crop) and n > 0 and not missing
		t["workload"] = {
			"people": n,
			"by_crop": rows,
			"missing_rate": missing,
			"start": start_dt.strftime("%H:%M"),
			"hours": hours if done else None,
			"end": (start_dt + timedelta(hours=hours)).strftime("%H:%M") if done else None,
			"end_minutes": start_dt.hour * 60 + start_dt.minute + round(hours * 60) if done else None,
		}


def _day_workload(teams):
	"""The day across the teams: boxes, people, and the last team to finish."""
	w = [t["workload"] for t in teams]
	last = max((x for x in w if x["end_minutes"] is not None), key=lambda x: x["end_minutes"], default=None)
	return {
		"boxes": sum(t["boxes"] for t in teams),
		"people": sum(x["people"] for x in w),
		"end": last["end"] if last else None,
		"last_team": next((t["team"] for t in teams if t["workload"] is last), None) if last else None,
		"unknown": [t["team"] for t in teams if t["orders"] and t["workload"]["end"] is None],
	}


@frappe.whitelist()
def get_rates():
	"""Every crop with its processing rate (0 when none is set)."""
	have = rates()
	return {"success": True, "rates": [{"crop": c, "rate": have.get(c, 0.0)} for c in crops()]}


@frappe.whitelist(methods=["POST"])
def save_rates(rows: str | list):
	"""Set the crops' processing rates: [{crop, rate}]; a rate of 0 removes it."""
	frappe.only_for(pg.PLANNERS)
	if isinstance(rows, str):
		rows = json.loads(rows or "[]")
	allowed = set(crops())
	for r in rows:
		crop, rate = r.get("crop"), flt(r.get("rate"))
		if crop not in allowed:
			frappe.throw(_("{0} is not a crop.").format(crop))
		if rate < 0:
			frappe.throw(_("A rate cannot be negative."))
		if rate == 0:
			frappe.delete_doc("Crop Processing Rate", crop, ignore_permissions=True, ignore_missing=True)
		elif frappe.db.exists("Crop Processing Rate", crop):
			frappe.db.set_value("Crop Processing Rate", crop, "boxes_per_person_hour", rate)
		else:
			frappe.get_doc(
				{"doctype": "Crop Processing Rate", "crop": crop, "boxes_per_person_hour": rate}
			).insert(ignore_permissions=True)
	return {"success": True}


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
	if not works_at(tc, location_of(farm)):
		frappe.throw(_("{0} does not work at {1}.").format(team, location_of(farm)))
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
def get_confirmation_summary(
	delivery_date: str | None = None, farm: str | None = None, week: str | None = None
):
	"""Every confirmed order with its lines: what each counts on, what the system did, and the result. With
	`week`, every delivery day of it, day by day."""
	if week:
		week, days = pg._week(week)
		out = {
			"success": True,
			"delivery_date": days[0],
			"week": week,
			"days": days,
			"orders": [],
			"totals": {},
		}
		for d in days:
			r = get_confirmation_summary(d, farm)
			out["farm"] = r["farm"]
			out["orders"] += r["orders"]
			for k, v in r["totals"].items():
				out["totals"][k] = out["totals"].get(k, 0) + v
		return out
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	plan = pg._plan_name(day, farm)
	lines = {ln.name: ln for ln in pg._lines_for(day, farm)}
	empty = {"confirmed": 0, "rolling": 0, "production": 0, "changed": 0, "left": 0, "lines_left": 0}
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
			"forecast_basis",
		],
	)
	by_line = defaultdict(list)
	for c in confs:
		by_line[c.sales_order_item].append(c)
	first = _first_confirmed(plan)
	# a line revised out of the plan still shows: confirmed before, 0 now
	for soi, cs in first.items():
		if soi not in by_line and soi in lines:
			by_line[soi] = [frappe._dict({**cs[0], "stems": 0.0, "source": "Virtual", "forecast_basis": ""})]
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
		revised = soi in first
		before = sum(flt(c.stems) for c in first[soi]) if revised else confirmed
		production = sum(flt(c.stems) for c in cs if (c.forecast_basis or "").startswith("production"))
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
		if production:
			done.append(_("Confirmed on production forecast"))
		result = (_("{0} open").format(int(left)), "warn") if left > 0.5 else (_("Full"), "ok")
		if done and done[0].startswith(_("Downgraded")) and left <= 0.5:
			result = (_("Full · downgrade reason"), "warn")
		if production and left <= 0.5:
			result = (_("Full · on production forecast"), "blue")
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
				"revised": None,
				"lines_done": 0,
				"lines_total": sum(1 for x in lines.values() if x.parent == ln.parent),
				"items": [],
			},
		)
		o["ordered"] += ordered
		o["confirmed"] += before
		o["now"] = o.get("now", 0.0) + confirmed
		if revised:
			o["revised"] = True
		o["lines_done"] += 1 if confirmed > 0 else 0
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
				"confirmed": before,
				"revised": confirmed if revised else None,
				"held": held,
				"expected": expected,
				"rolling": confirmed - production,
				"production": production,
				"done": done,
				"result": result[0],
				"kind": result[1],
			}
		)
		totals["confirmed"] += confirmed
		totals["rolling"] += confirmed - production
		totals["production"] += production
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
	# every line of an order the plan has started: the ones not planned yet show too, to be planned
	for ln in lines.values():
		o = orders.get(ln.parent)
		if not o or ln.name in by_line or flt(ln.ordered_stems) <= flt(ln.allocated_stems):
			continue
		o["ordered"] += flt(ln.ordered_stems)
		o["items"].append(
			{
				"soi": ln.name,
				"variety": "{0} {1}".format(ln.item_code, ln.custom_length or "").strip(),
				"pool_variety": ln.item_code,
				"pool_length": ln.custom_length or "",
				"note": ln.custom_mix_name if mix_key(ln) else "",
				"ordered": flt(ln.ordered_stems),
				"confirmed": 0.0,
				"revised": None,
				"held": 0.0,
				"expected": 0.0,
				"rolling": 0.0,
				"production": 0.0,
				"done": [],
				"result": _("Not planned"),
				"kind": "red",
				"not_planned": 1,
			}
		)
		totals["left"] += flt(ln.ordered_stems) - flt(ln.allocated_stems)
		totals["lines_left"] += 1
	names = dict(
		frappe.db.sql(
			"SELECT name, custom_order_name FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": tuple(orders) or ("",)},
		)
	)
	for o in orders.values():
		o["order_name"] = names.get(o["sales_order"]) or o["sales_order"]
		o["revised"] = o.pop("now") if o["revised"] else None
		short = o["ordered"] - (o["revised"] if o["revised"] is not None else o["confirmed"])
		o["result"], o["kind"] = (
			(_("Full"), "ok") if short < 0.5 else (_("{0} open").format(int(short)), "warn")
		)
	out = sorted(orders.values(), key=lambda o: (not o["seq"], o["seq"], o["team"], o["customer"] or ""))
	totals["orders"] = len(out)
	totals["lines"] = sum(len(o["items"]) for o in out)
	totals["all_lines"] = len(lines)
	return {"success": True, "delivery_date": day, "farm": farm, "orders": out, "totals": totals}


def _first_confirmed(plan):
	"""{sales_order_item: [confirmations]} as they stood before the line's first revision: the ones released as
	"Revised" at that first revision."""
	out = defaultdict(list)
	rows = frappe.get_all(
		"Allocation Confirmation",
		filters={"plan": plan, "status": "Released", "release_reason": "Revised"},
		fields=[
			"sales_order",
			"sales_order_item",
			"stems",
			"variety",
			"length",
			"ordered_variety",
			"ordered_length",
			"team",
			"farm",
			"released_on",
		],
		order_by="released_on asc",
	)
	for r in rows:
		cs = out[r.sales_order_item]
		# one revision releases a line's confirmations within a moment of each other
		if not cs or (r.released_on - cs[0].released_on).total_seconds() < 5:
			cs.append(r)
	return out


# ---------------------------------------------------------------- substitution


def _mix_varieties(ln):
	"""Varieties of the other lines packed in the same mixed box or bunch as this line."""
	key = mix_key(ln)
	if not key:
		return set()
	_parent, kind, group = key
	field, flag = (
		("custom_bunch_group", "custom_mixed_bunch")
		if kind == "bunch"
		else ("custom_mix_group", "custom_mixed_box")
	)
	return set(
		frappe.get_all(
			"Sales Order Item",
			filters={"parent": ln.parent, field: group, flag: 1, "name": ("!=", ln.name)},
			pluck="item_code",
		)
	)


@frappe.whitelist()
def substitute_and_confirm(
	delivery_date: str,
	farm: str,
	sales_order_item: str,
	variety: str,
	team: str,
	same_as_mix: int = 0,
):
	"""Substitute an order line's variety with one its specification approves, then confirm the line in full
	on the new variety. The line itself changes (as Sales Allocation's Substitute does), so allocation follows.
	A variety already in the line's mix is refused unless the planner said to go ahead (`same_as_mix`)."""
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
	if not cint(same_as_mix) and variety in _mix_varieties(ln):
		frappe.throw(
			_("{0} is already in this mix. Confirm to use it again.").format(variety),
			title=_("Same variety in the mix"),
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
