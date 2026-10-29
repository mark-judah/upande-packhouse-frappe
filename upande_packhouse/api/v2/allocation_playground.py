# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Planning v2 (www/sales-allocation-planning-v2.html): confirm a delivery day before allocation.

Spec: docs/allocation-playground/spec.md. This is phase 1: the board, the variety detail, confirming an order
line (Real stock holds buckets, the rest is Virtual from the revised forecast) and releasing it. The holds
are read and released by allocation (allocation_plan.py, availability.py, sales_allocation.py).

The unit on the board is a variety and length for one delivery day (the "pool" in the spec; the page calls
it "variety and length"). For each one:

    ordered       order lines for the delivery date (pipeline.fetch_lines, ordered_stems)
    allocated     already allocated (pipeline.attach_pipeline)
    held          confirmed from stock (shelf or received) and not yet allocated (Real confirmations)
    expected      confirmed from stock still to arrive (Virtual confirmations)
    still to confirm  ordered - allocated - held - expected

What can be confirmed against, most certain first (all by variety and length):

    on the shelf            free stems that can still be confirmed (core/stock.bucket_rows: allocatable, so net of
                            age, cooling and stems held for other lines)
    received, not shelved   buckets received and waiting for a shelf, with their cold room (core/received.py); held
                            bucket by bucket like shelf stock
    harvested, not received the day's production still on its way (core/received.harvested_day)
    still to be cut         the revised forecast for the cutting day less what has been harvested that day. "Safe"
                            takes off the forecast's average error for the variety this year (revised against actual)

    short         still to confirm less everything above that can cover it

Ages come from Production Settings through core/stock (amber time, discard age, max allocation age); no
number of days is written here.
"""

import json
from collections import defaultdict
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from upande_packhouse import allocation_plan as plans
from upande_packhouse import availability
from upande_packhouse.api.v2.core import pipeline, received, stock

PLANNERS = ("System Manager", "Sales Manager", "Sales Master Manager", "Sales User", "Assistant Packhouse Manager")


def _memo(key, fn):
	"""Compute once per request: the board asks for the same variety's forecast for every length."""
	cache = frappe.flags.setdefault("allocation_planning_memo", {})
	if key not in cache:
		cache[key] = fn()
	return cache[key]


def _day(delivery_date):
	return str(getdate(delivery_date or add_days(today(), 1)))


def _farms():
	"""The sales farms: farms marked as a sales shelf in Production Settings (the ones that process orders)."""
	return frappe.get_all(
		"Shelf Locations",
		filters={"parenttype": "Production Settings", "enabled": 1, "sales_shelf": 1},
		fields=["farm", "sales_shelf"],
		order_by="idx",
	)


def _default_farm():
	rows = _farms()
	hub = frappe.db.get_single_value("Production Settings", "transfer_hub_farm")
	if hub and any(r.farm == hub for r in rows):
		return hub
	return rows[0].farm if rows else hub


def _growing_farms(farm):
	"""The farms whose greenhouses supply this stock farm: every farm in the same Production Settings
	location (Kapkolia's location holds Chepsito, Simotwo, Torongo, Kaptumbo and Kapkolia itself)."""
	if not farm:
		return None
	for farms in stock._config().get("farms_by_location", {}).values():
		if farm in farms:
			return list(farms)
	return [farm]


def _stock_farms(sales_farm):
	"""Every farm whose stock can be confirmed for this sales farm's orders: all the farms in its location."""
	return _growing_farms(sales_farm) or ([sales_farm] if sales_farm else [])


def amber_from_days():
	"""The age (days) from which stock counts as getting old: the same value the allocator uses."""
	return stock._config()["amber_time"]


def forecast_error(variety, farm=None):
	"""How far the revised forecast has been off for this variety this year: (error, weeks used).

	Weighted average over the year's finished weeks of |revised - actual| / actual, using the "all lengths" rows
	(actuals are only kept for those). None when no finished week has an actual to judge it by."""
	farms = tuple(_growing_farms(farm) or ())
	return _memo(("error", variety, farms, today()), lambda: _forecast_error(variety, farms))


def _forecast_error(variety, farms):
	today_ = getdate(today())
	year, week, _wd = today_.isocalendar()
	cond, params = "", {"v": variety, "y": year, "w": week}
	if farms:
		cond = " AND wh.custom_farm IN %(farms)s"
		params["farms"] = tuple(farms)
	rows = frappe.db.sql(
		f"""SELECT w.week_number, SUM(w.revised_forecast_stems) AS revised, SUM(w.actual_stems) AS actual
		    FROM `tabProduction Forecast Week` w
		    INNER JOIN `tabProduction Forecast` f ON f.name = w.parent
		    LEFT JOIN `tabWarehouse` wh ON wh.name = f.greenhouse
		    WHERE f.variety = %(v)s AND w.grade = 'all' AND w.iso_year = %(y)s AND w.week_number < %(w)s {cond}
		    GROUP BY w.week_number HAVING SUM(w.actual_stems) > 0""",
		params,
		as_dict=True,
	)  # nosemgrep: the f-string hole is fixed SQL, values are bound
	if not rows:
		return None, 0
	actual = sum(flt(r.actual) for r in rows)
	off = sum(abs(flt(r.revised) - flt(r.actual)) for r in rows)
	return min(1.0, off / actual), len(rows)


def expected_detail(variety, length, delivery_date, farm=None):
	"""What the farms are forecast to cut for this delivery date, by growing farm.

	Returns {"likely", "sure", "by_farm", "error", "weeks"}. The forecast is kept per greenhouse; a greenhouse
	belongs to a farm (Warehouse > Farm) and only the farm is shown.

	Revised forecast for the cutting day's week, shared over its days, times the length's share of the variety's
	lengths (revised length rows, else the manual budget; the length rows do not sum to the total so only their
	shares are used). `sure` takes off the variety's average forecast error this year (forecast_error); with no
	finished week to judge by, none of it is counted as sure."""
	lead = cint(plans.setting("allocation_harvest_lead_days"))
	cut_day = getdate(delivery_date) - timedelta(days=lead)
	year, week, _wd = cut_day.isocalendar()
	farms = tuple(_growing_farms(farm) or ())
	rows = _memo(("forecast", variety, year, week, farms), lambda: _forecast_rows(variety, year, week, farms))
	total_by_farm = defaultdict(float)
	share_by_farm = defaultdict(lambda: [0.0, 0.0])  # farm -> [this length, all lengths]
	_split_forecast(rows, length, total_by_farm, share_by_farm)
	by_farm = {}
	for f, total in total_by_farm.items():
		mine, allv = share_by_farm[f]
		if total > 0 and allv > 0 and mine > 0:
			by_farm[f] = total / 7.0 * mine / allv
	likely = sum(by_farm.values())
	error, weeks = forecast_error(variety, farm) if likely > 0 else (None, 0)
	sure = likely * (1 - error) if error is not None else 0.0
	return {"likely": likely, "sure": sure, "by_farm": by_farm, "error": error, "weeks": weeks}


def _split_forecast(rows, length, total_by_farm, share_by_farm):
	for r in rows:
		if r.grade == "all":
			total_by_farm[r.farm] += flt(r.revised)
		else:
			v = flt(r.revised) or flt(r.manual)
			share_by_farm[r.farm][1] += v
			if r.grade == (length or ""):
				share_by_farm[r.farm][0] += v


def _forecast_rows(variety, year, week, farms):
	cond, params = "", {"v": variety, "y": year, "w": week}
	if farms:
		cond = " AND wh.custom_farm IN %(farms)s"
		params["farms"] = tuple(farms)
	return frappe.db.sql(
		f"""SELECT w.grade, COALESCE(NULLIF(wh.custom_farm, ''), 'Unknown') AS farm,
		           SUM(w.revised_forecast_stems) AS revised, SUM(w.manual_budget_stems) AS manual
		    FROM `tabProduction Forecast Week` w
		    INNER JOIN `tabProduction Forecast` f ON f.name = w.parent
		    LEFT JOIN `tabWarehouse` wh ON wh.name = f.greenhouse
		    WHERE f.variety = %(v)s AND w.iso_year = %(y)s AND w.week_number = %(w)s {cond}
		    GROUP BY w.grade, farm""",
		params,
		as_dict=True,
	)  # nosemgrep: the f-string hole is fixed SQL, values are bound


def expected_arrivals(variety, length, delivery_date, farm=None):
	"""(likely, sure) stems forecast for the cutting day; see expected_detail."""
	d = expected_detail(variety, length, delivery_date, farm)
	return d["likely"], d["sure"]


def _harvest_room(day, farm, plan_name, variety, length, exclude_soi):
	"""Stems of the day's harvest not yet received that no other customer's expected confirmation has already counted on."""
	on_the_way = flt(_stock_by_pool(farm, {variety}, day).get((variety, length or ""), {}).get("on_the_way"))
	used = sum(
		flt(c.from_harvest)
		for c in (plans.live_confirmations(plan=plan_name, source="Virtual") if plan_name else [])
		if c.variety == variety and (c.length or "") == (length or "") and c.sales_order_item != exclude_soi
	)
	return max(0.0, on_the_way - used)


def _plan_name(day, farm):
	return frappe.db.get_value("Allocation Plan", {"delivery_date": day, "farm": farm})


def _lines(day):
	lines = pipeline.fetch_lines(delivery_from=day, delivery_to=day)
	pipeline.attach_pipeline(lines)
	return lines


def _planned_by_line(day, farm):
	"""{soi: {"real": stems still held, "expected": stems planned from the harvest}}"""
	out = {}
	for c in plans.live_confirmations(plan=_plan_name(day, farm)) if _plan_name(day, farm) else []:
		d = out.setdefault(c.sales_order_item, {"real": 0.0, "expected": 0.0, "status": [], "length": "", "farm": ""})
		left = max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
		d["real" if c.source == "Real" else "expected"] += left
		d["status"].append(c.status)
		d["length"] = c.length or d["length"]
		d["farm"] = c.farm or d["farm"]
	return out


SUPPLY = {
	"free": 0.0,
	"held": 0.0,
	"old": 0.0,
	"oldest": 0,
	"buckets": 0,
	"received": 0.0,
	"rooms": {},
	"on_the_way": 0.0,
	"harvested": 0.0,
	"by_farm": {},
	"rec_by_farm": {},
	"old_by_farm": {},
}


def _confirmed_here(day, farm):
	"""{(variety, length): {"lines": n, "stems": stems}} confirmed against each variety and length (the stock it is
	drawn from, which for a longer-than-ordered confirmation is not the length the customer ordered)."""
	name = _plan_name(day, farm)
	out = {}
	for c in plans.live_confirmations(plan=name) if name else []:
		d = out.setdefault((c.variety, c.length or ""), {"lines": set(), "stems": 0.0, "real": 0.0, "expected": 0.0})
		d["lines"].add(c.sales_order_item)
		left = max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
		d["stems"] += left
		d["real" if c.source == "Real" else "expected"] += left
	return {k: {"lines": len(v["lines"]), "stems": v["stems"], "real": v["real"], "expected": v["expected"]} for k, v in out.items()}


def _stock_by_pool(farm, varieties, day=None, everything=False):
	"""Everything that exists for each (variety, length): on the shelf, received and not shelved (by cold room),
	and the cutting day's harvest that has not been received yet. `old` is the part getting old."""
	amber = amber_from_days()
	out = {}
	if not varieties and not everything:
		return out

	def entry(key):
		return out.setdefault(key, {**SUPPLY, "rooms": {}, "by_farm": {}, "rec_by_farm": {}, "old_by_farm": {}})

	farms = _stock_farms(farm)
	for r in stock.bucket_rows(farms=farms, varieties=sorted(varieties) if varieties else None):
		p = entry((r.variety, r.stem_length or ""))
		p["free"] += r.allocatable
		p["held"] += r.reserved
		if r.allocatable > 0:
			p["buckets"] += 1
			p["oldest"] = max(p["oldest"], r.age_days or 0)
			p["by_farm"][r.shelf_farm] = p["by_farm"].get(r.shelf_farm, 0.0) + r.allocatable
			if (r.age_days or 0) >= amber:
				p["old"] += r.allocatable
				p["old_by_farm"][r.shelf_farm] = p["old_by_farm"].get(r.shelf_farm, 0.0) + r.allocatable
	recv = received.received_unshelved(farms=farms, varieties=sorted(varieties) if varieties else None)
	held_for_lines = availability.reserved_by_bucket(bucket_ids={r.bucket_id for r in recv})
	for r in recv:
		free = max(0.0, r.stems - held_for_lines.get(r.bucket_id, 0.0))
		if free <= 0:
			continue
		p = entry((r.variety, r.stem_length or ""))
		p["received"] += free
		p["rec_by_farm"][r.farm] = p["rec_by_farm"].get(r.farm, 0.0) + free
		room = r.cold_room or _("Cold room")
		p["rooms"][room] = p["rooms"].get(room, 0.0) + free
		p["oldest"] = max(p["oldest"], r.age_days or 0)
		if r.age_days >= amber:
			p["old"] += free
			p["old_by_farm"][r.farm] = p["old_by_farm"].get(r.farm, 0.0) + free
	if day and varieties:
		cut = getdate(day) - timedelta(days=cint(plans.setting("allocation_harvest_lead_days")))
		for key, h in received.harvested_day(cut, _growing_farms(farm), varieties).items():
			p = entry(key)
			p["harvested"] = h["harvested"]
			p["on_the_way"] = h["not_received"]
	return out


def expected_for(variety, length, day, farm, s):
	"""What is still to come for a variety and length, from its supply entry `s`: (likely, sure) in stems.

	The day's harvest not yet received is real; what is still to be cut is the forecast less what was harvested
	that day, and only that part carries the forecast's error."""
	likely_total, sure_total = expected_arrivals(variety, length, day, farm)
	remaining = max(0.0, likely_total - s["harvested"])
	ratio = (sure_total / likely_total) if likely_total > 0 else 0.0
	return s["on_the_way"] + remaining, s["on_the_way"] + remaining * ratio


def _build_pools(day, farm, overrides=None, only_variety=None):
	lines = _lines(day)
	if only_variety:
		lines = [ln for ln in lines if ln.item_code == only_variety]
	planned = overrides if overrides is not None else _planned_by_line(day, farm)
	supply = _stock_by_pool(farm, {ln.item_code for ln in lines}, day)
	confirmed_here = _confirmed_here(day, farm)
	pools = {}
	for ln in lines:
		key = (ln.item_code, ln.custom_length or "")
		p = pools.setdefault(
			key,
			{
				"variety": key[0],
				"length": key[1],
				"lines": 0,
				"ordered": 0.0,
				"allocated": 0.0,
				"held": 0.0,
				"expected_plan": 0.0,
				"lines_done": 0,
			},
		)
		pl = planned.get(ln.name, {"real": 0.0, "expected": 0.0})
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		p["lines"] += 1
		p["ordered"] += ordered
		p["allocated"] += min(allocated, ordered)
		p["held"] += pl["real"]
		p["expected_plan"] += pl["expected"]
		if ordered > 0 and allocated + pl["real"] + pl["expected"] >= ordered - 0.0001:
			p["lines_done"] += 1
	out = []
	for key, p in pools.items():
		s = {**SUPPLY, **supply.get(key, {})}
		likely, sure = expected_for(key[0], key[1], day, farm, s)
		have = s["free"] + s["received"]
		left = max(0.0, p["ordered"] - p["allocated"] - p["held"] - p["expected_plan"])
		likely_left = max(0.0, likely - p["expected_plan"])
		short = max(0.0, left - have - likely_left)
		if short > 0:
			status = "short"
		elif left > 0 and (left > have or (s["old"] > 0 and left > 0)):
			status = "watch"
		else:
			status = "ok"
		# of everything ordered: how much is allocated, confirmed, or can be covered by what we have or expect
		covered = p["allocated"] + p["held"] + p["expected_plan"] + min(left, have + likely_left)
		ch = confirmed_here.get(key, {"lines": 0, "stems": 0.0, "real": 0.0, "expected": 0.0})
		p.update(
			confirmed_lines=ch["lines"],
			confirmed_here=ch["stems"],
			have=have + likely_left,
			covered_pct=int(round(100.0 * min(1.0, covered / p["ordered"]))) if p["ordered"] > 0 else 100,
			still_to_plan=left,
			on_shelf=s["free"],
			received=s["received"],
			on_the_way=s["on_the_way"],
			getting_old=s["old"],
			oldest_days=s["oldest"],
			buckets=s["buckets"],
			expected_likely=likely,
			expected_sure=sure,
			short=short,
			status=status,
		)
		out.append(p)
	return out


@frappe.whitelist()
def get_board(delivery_date: str | None = None, farm: str | None = None, view: str = "attention"):
	"""The four numbers and the list of varieties and lengths, most urgent first."""
	day = _day(delivery_date)
	farm = farm or _default_farm()
	# Stock that has reached the shelf since the last visit takes over from the expected harvest.
	plan_name = _plan_name(day, farm)
	if plan_name and frappe.db.exists("Allocation Confirmation", {"plan": plan_name, "source": "Virtual", "status": "Planned"}):
		plans.convert_virtual(day, farm)
	pools = _build_pools(day, farm)
	kpis = {
		"lines": sum(p["lines"] for p in pools),
		"lines_planned": sum(p["lines_done"] for p in pools),
		"stems_left": sum(p["still_to_plan"] for p in pools),
		"short": sum(p["short"] for p in pools),
		"use_first": sum(p["getting_old"] for p in pools if p["still_to_plan"] > 0),
		"non_moving": sum(p["getting_old"] for p in pools),
	}
	_name = _plan_name(day, farm)
	_confs = plans.live_confirmations(plan=_name) if _name else []
	kpis["confirmed_orders"] = len({c.sales_order for c in _confs})
	kpis["confirmed_lines"] = len({c.sales_order_item for c in _confs})
	kpis["planned_pct"] = round(100.0 * kpis["lines_planned"] / kpis["lines"]) if kpis["lines"] else None
	rank = {"short": 0, "watch": 1, "ok": 2}
	pools.sort(key=lambda p: (rank[p["status"]], -p["short"], -p["still_to_plan"], p["variety"], p["length"]))
	if view == "confirmed":
		pools = [p for p in pools if p["confirmed_lines"] or p["held"] or p["expected_plan"]]
	elif view != "all":
		pools = [p for p in pools if p["status"] != "ok"]
	return {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"farms": [r.farm for r in _farms()],
		"stock_farms": _stock_farms(farm),
		"amber_from_days": amber_from_days(),
		"teams": frappe.get_all("Packing Teams", pluck="name", order_by="name asc"),
		"days_ahead": cint(plans.setting("allocation_horizon_days")),
		"kpis": kpis,
		"pools": pools,
	}


def _group_of(ln):
	"""What kind of box a line is in, and which lines belong together: a straight box line stands alone; a mixed box is
	every line with the same mix group; a mixed bunch is every line with the same bunch group."""
	if ln.custom_mixed_box:
		g = ln.custom_mix_group or ln.custom_box_id or ""
		return {"kind": "mixed_box", "group_key": "box|%s" % g, "group_label": _("Mixed box {0}").format(g) if g else _("Mixed box")}
	if ln.custom_mixed_bunch:
		g = ln.custom_bunch_group or ""
		num = g.rsplit("::", 1)[-1] if "::" in g else g
		return {"kind": "mixed_bunch", "group_key": "bunch|%s" % g, "group_label": _("Mixed bunch {0}").format(num) if num else _("Mixed bunch")}
	return {"kind": "straight", "group_key": "line|%s" % ln.name, "group_label": _("Straight box")}


def _held_sources(conf_names):
	"""{confirmation: {"shelf": stems, "received": stems}} for what a Real confirmation holds: a held bucket that is on a shelf
	counts as shelf, one still waiting for a shelf as received."""
	if not conf_names:
		return {}
	rows = frappe.get_all(
		"Bucket Reservation",
		filters={"confirmation": ("in", list(conf_names)), "status": "Active"},
		fields=["confirmation", "bucket_id", "stems"],
	)
	if not rows:
		return {}
	on_shelf = {
		r[0]
		for r in frappe.db.sql(
			"SELECT DISTINCT bucket_id FROM `tabShelf Item` WHERE stem_qty > 0 AND bucket_id IN %(b)s",
			{"b": tuple({r.bucket_id for r in rows})},
		)
	}
	out = {}
	for r in rows:
		d = out.setdefault(r.confirmation, {"shelf": 0.0, "received": 0.0})
		d["shelf" if r.bucket_id in on_shelf else "received"] += flt(r.stems)
	return out


def _pool_totals(day, farm, variety, length, supply, ch):
	"""For a variety and length: everything available (on the shelf and received, what is held, and what is expected), what
	is already taken by confirmations, and what is left."""
	s = {**SUPPLY, **supply.get((variety, length or ""), {})}
	likely, _sure = expected_for(variety, length or "", day, farm, s)
	ch = ch.get((variety, length or ""), {"real": 0.0, "expected": 0.0})
	total = s["free"] + s["received"] + ch["real"] + likely
	taken = ch["real"] + ch["expected"]
	return {
		"variety": variety,
		"length": length or "",
		"total": total,
		"taken": taken,
		"left": max(0.0, total - taken),
	}


@frappe.whitelist()
def get_team_plan(delivery_date: str | None = None, farm: str | None = None):
	"""The plan per team: what each team has been given to pack for the delivery day, so the load can be seen to be even.

	Every team (also one with nothing yet) with its stems, boxes (the line's boxes in proportion to what is confirmed),
	order lines, orders and varieties, and its share against an even share. A team without a team name goes under
	"No team"; stems not yet confirmed are reported once as `unplanned`."""
	day = _day(delivery_date)
	farm = farm or _default_farm()
	name = _plan_name(day, farm)
	confs = plans.live_confirmations(plan=name) if name else []
	lines = {ln.name: ln for ln in _lines(day)}
	rows = {t: _team_row(t) for t in frappe.get_all("Packing Teams", pluck="name", order_by="name asc")}
	per_line = {}
	for c in confs:
		per_line.setdefault((c.team or "", c.sales_order_item), 0.0)
		per_line[(c.team or "", c.sales_order_item)] += max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
	for (team, soi), stems in per_line.items():
		ln = lines.get(soi)
		r = rows.setdefault(team, _team_row(team or _("No team")))
		r["stems"] += stems
		if ln is None:
			continue
		r["lines"] += 1
		r["orders"].add(ln.parent)
		r["varieties"][ln.item_code] = r["varieties"].get(ln.item_code, 0.0) + stems
		mk = _mix_key(ln)
		if mk and mk in r["boxes_of"]:
			pass  # a mixed box is one set of boxes however many lines it has
		elif flt(ln.ordered_stems) > 0:
			r["boxes"] += flt(ln.boxes) * min(1.0, stems / flt(ln.ordered_stems))
			if mk:
				r["boxes_of"].add(mk)
		r["customers"].add(ln.customer_name or ln.customer)
	teams = list(rows.values())
	total = sum(r["stems"] for r in teams)
	working = [r for r in teams if r["name"] != _("No team")]
	even = total / len(working) if working else 0.0
	out = []
	for r in sorted(teams, key=lambda x: -x["stems"]):
		out.append(
			{
				"team": r["name"],
				"stems": r["stems"],
				"boxes": round(r["boxes"], 1),
				"lines": r["lines"],
				"orders": len(r["orders"]),
				"customers": len(r["customers"]),
				"varieties": len(r["varieties"]),
				"top": [{"variety": v, "stems": s} for v, s in sorted(r["varieties"].items(), key=lambda kv: -kv[1])[:3]],
				"share": (r["stems"] / total) if total else 0.0,
				"vs_even": ((r["stems"] - even) / even) if even else 0.0,
			}
		)
	stems_list = [r["stems"] for r in working]
	spread = (max(stems_list) - min(stems_list)) / even if even and stems_list else 0.0
	planned_lines = {soi for (_t, soi) in per_line}
	unplanned = sum(max(0.0, flt(l.ordered_stems) - flt(l.allocated_stems)) for l in lines.values() if l.name not in planned_lines)
	return {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"teams": out,
		"total": total,
		"even": even,
		"spread": spread,
		"even_enough": spread <= 0.25,
		"unplanned": unplanned,
	}


def _team_row(name):
	return {"name": name, "stems": 0.0, "boxes": 0.0, "lines": 0, "orders": set(), "customers": set(), "varieties": {}, "boxes_of": set()}


@frappe.whitelist()
def get_confirmed_orders(delivery_date: str | None = None, farm: str | None = None):
	"""The Confirmed page: every order with something confirmed, with ALL its lines (confirmed or not), so the whole order stays in
	view. For each line: what was ordered and confirmed, where the stems come from, and, for the variety and length it is
	confirmed against, how much was available and how much is already taken."""
	day = _day(delivery_date)
	farm = farm or _default_farm()
	name = _plan_name(day, farm)
	confs = plans.live_confirmations(plan=name) if name else []
	if not confs:
		return {"success": True, "delivery_date": day, "farm": farm, "orders": [], "totals": {"orders": 0, "lines": 0, "lines_confirmed": 0}}
	by_soi = {}
	for c in confs:
		by_soi.setdefault(c.sales_order_item, []).append(c)
	orders_in = {c.sales_order for c in confs}
	lines = [ln for ln in _lines(day) if ln.parent in orders_in]
	supply = _stock_by_pool(farm, {ln.item_code for ln in lines} | {c.variety for c in confs}, day)
	ch = _confirmed_here(day, farm)
	held = _held_sources([c.name for c in confs if c.source == "Real"])
	names = dict(frappe.db.sql("SELECT name, custom_order_name FROM `tabSales Order` WHERE name IN %(s)s", {"s": tuple(orders_in)}))
	out = {}
	for ln in lines:
		cs = by_soi.get(ln.name, [])
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		sources, total_conf = [], 0.0
		for c in cs:
			left = max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
			total_conf += left
			if c.source == "Real":
				h = held.get(c.name, {"shelf": 0.0, "received": 0.0})
				if h["shelf"]:
					sources.append({"kind": "shelf", "stems": h["shelf"], "farm": c.farm})
				if h["received"]:
					sources.append({"kind": "received", "stems": h["received"], "farm": c.farm})
				rest = left - h["shelf"] - h["received"]
				if rest > 0.0001:  # held stock already partly allocated
					sources.append({"kind": "held", "stems": rest, "farm": c.farm})
			else:
				hv = min(left, flt(c.from_harvest))
				if hv > 0.0001:
					sources.append({"kind": "harvested", "stems": hv, "farm": c.farm})
				if left - hv > 0.0001:
					sources.append({"kind": "forecast", "stems": left - hv, "farm": c.farm})
		key_v = cs[0].variety if cs else ln.item_code
		key_l = (cs[0].length if cs else ln.custom_length) or ""
		room = max(0.0, ordered - allocated)
		state = "none"
		if total_conf > 0:
			state = "confirmed" if total_conf >= room - 0.0001 else "part"
		elif room <= 0.0001 and allocated > 0:
			state = "allocated"
		o = out.setdefault(
			ln.parent,
			{
				"sales_order": ln.parent,
				"customer": ln.customer_name or ln.customer,
				"order_name": names.get(ln.parent) or "",
				"ordered": 0.0,
				"allocated": 0.0,
				"confirmed": 0.0,
				"lines_total": 0,
				"lines_confirmed": 0,
				"lines": [],
			},
		)
		o["ordered"] += ordered
		o["allocated"] += min(allocated, ordered)
		o["confirmed"] += total_conf
		o["lines_total"] += 1
		o["lines_confirmed"] += 1 if state in ("confirmed", "part") else 0
		o["lines"].append(
			{
				"soi": ln.name,
				"line_no": ln.idx,
				"variety": ln.item_code,
				"length": ln.custom_length or "",
				"ordered": ordered,
				"allocated": min(allocated, ordered),
				"mixed": bool(ln.custom_mixed_box or ln.custom_mixed_bunch),
				"mix_name": ln.custom_mix_name or "",
				**_group_of(ln),
				"state": state,
				"confirmed": total_conf,
				"confirmed_variety": key_v if cs else "",
				"confirmed_length": key_l if cs else "",
				"farm": cs[0].farm if cs else "",
				"team": plans.team_for_line(ln.name) or plans.group_team(_mix_key(ln)) or "",
				"sources": sources,
				"missing": any(c.status == "Broken" for c in cs),
				"pool": _pool_totals(day, farm, key_v, key_l, supply, ch),
			}
		)
	orders = sorted(out.values(), key=lambda o: ((o["customer"] or "").lower(), o["sales_order"]))
	for o in orders:
		# lines of one mixed box or bunch sit together; everything else stays in the order's own line order
		first = {}
		for x in sorted(o["lines"], key=lambda x: x["line_no"] or 0):
			first.setdefault(x["group_key"], x["line_no"] or 0)
		o["lines"].sort(key=lambda x: (first[x["group_key"]], x["line_no"] or 0))
	return {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"orders": orders,
		"totals": {
			"orders": len(orders),
			"lines": sum(o["lines_total"] for o in orders),
			"lines_confirmed": sum(o["lines_confirmed"] for o in orders),
		},
	}


@frappe.whitelist()
def get_pool(delivery_date: str | None = None, farm: str | None = None, variety: str | None = None, length: str | None = None):
	"""One variety and length: its customer lines with what is planned for each."""
	day = _day(delivery_date)
	farm = farm or _default_farm()
	length = length or ""
	pool = next(
		(p for p in _build_pools(day, farm, only_variety=variety) if p["variety"] == variety and p["length"] == length), None
	)
	if pool is None:
		frappe.throw(_("There are no orders for {0} {1} on {2}.").format(variety, length, day))
	planned = _planned_by_line(day, farm)
	rows, longer = [], []
	pool_cm = availability.length_cm(length)
	for ln in _lines(day):
		if ln.item_code != variety:
			continue
		pl = planned.get(ln.name, {"real": 0.0, "expected": 0.0, "length": ""})
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		row = {
			"soi": ln.name,
			"sales_order": ln.parent,
			"customer": ln.customer_name or ln.customer,
			"ordered": ordered,
			"allocated": min(allocated, ordered),
			"held": pl["real"],
			"expected": pl["expected"],
			"left": max(0.0, ordered - allocated - pl["real"] - pl["expected"]),
			"max_plan": max(0.0, ordered - allocated),
			"mixed": bool(ln.custom_mixed_box or ln.custom_mixed_bunch),
			"mix_name": ln.custom_mix_name or "",
			"mix_key": _mix_key(ln),
			"line_no": ln.idx,
			"stock_farm": pl.get("farm") or "",
			"ordered_length": ln.custom_length or "",
			"confirmed_length": pl["length"] or "",
			"team": plans.team_for_line(ln.name) or plans.group_team(_mix_key(ln)) or "",
		}
		if (ln.custom_length or "") == length:
			rows.append(row)
		elif 0 < availability.length_cm(ln.custom_length) < pool_cm:
			# Ordered shorter than this length: this stock would give them a longer stem than they ordered.
			# Shown when there is still something to confirm, or when it is already confirmed at this length.
			at_here = pl["length"] == length
			row["held"] = pl["real"] if at_here else 0.0
			row["expected"] = pl["expected"] if at_here else 0.0
			row["left"] = max(0.0, ordered - allocated - pl["real"] - pl["expected"]) if not at_here else row["left"]
			if row["left"] > 0 or at_here:
				longer.append(row)
	rows.sort(key=lambda r: (-r["left"], r["customer"] or ""))
	longer.sort(key=lambda r: (-r["left"], r["customer"] or ""))
	confirmed = _confirmed_lines(day, farm, variety, length)
	detail = expected_detail(variety, length, day, farm)
	sup = {**SUPPLY, **_stock_by_pool(farm, {variety}, day).get((variety, length), {})}
	remaining = max(0.0, detail["likely"] - sup["harvested"])
	scale = (remaining / detail["likely"]) if detail["likely"] > 0 else 0.0
	return {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"pool": pool,
		"lines": rows,
		"longer_lines": longer,
		"confirmed_lines": confirmed,
		# what exists for this variety and length, most certain first
		"stock_farms": [
			{"farm": f, "on_shelf": sup["by_farm"].get(f, 0.0), "received": sup["rec_by_farm"].get(f, 0.0)}
			for f in _stock_farms(farm)
		],
		"best_farm": _best_farm(farm, sup),
		"supply": {
			"on_shelf": sup["free"],
			"received": sup["received"],
			"rooms": [{"room": r, "stems": v} for r, v in sorted(sup["rooms"].items(), key=lambda kv: -kv[1])],
			"on_the_way": sup["on_the_way"],
			"to_cut": remaining,
			"to_cut_sure": remaining * (detail["sure"] / detail["likely"]) if detail["likely"] > 0 else 0.0,
			"to_cut_by_farm": [
				{"farm": f, "stems": v * scale} for f, v in sorted(detail["by_farm"].items(), key=lambda kv: -kv[1]) if v * scale > 0
			],
			"error_pct": None if detail["error"] is None else round(100.0 * detail["error"]),
			"error_weeks": detail["weeks"],
		},
	}


def _best_farm(sales_farm, sup):
	"""The farm with the most stock for a variety and length (shelf and received); the sales farm when there is none."""
	best, most = sales_farm, 0.0
	for f in _stock_farms(sales_farm):
		have = sup["by_farm"].get(f, 0.0) + sup["rec_by_farm"].get(f, 0.0)
		if have > most + 0.0001:
			best, most = f, have
	return best


def _confirmed_lines(day, farm, variety, length):
	"""What is confirmed against this variety and length, one row per order line, so it can be seen and taken out."""
	name = _plan_name(day, farm)
	if not name:
		return []
	by_line = {ln.name: ln for ln in _lines(day)}
	rows = {}
	for c in plans.live_confirmations(plan=name):
		if c.variety != variety or (c.length or "") != length:
			continue
		ln = by_line.get(c.sales_order_item)
		d = rows.setdefault(
			c.sales_order_item,
			{
				"soi": c.sales_order_item,
				"sales_order": c.sales_order,
				"customer": (ln.customer_name or ln.customer) if ln else (c.customer or ""),
				"ordered": flt(ln.ordered_stems) if ln else 0.0,
				"ordered_length": c.ordered_length or "",
				"held": 0.0,
				"buckets": 0,
				"expected": 0.0,
				"missing": False,
				"team": c.team or "",
				"farm": c.farm or "",
				"mixed": bool(ln.custom_mixed_box or ln.custom_mixed_bunch) if ln else bool(c.mix_key),
			},
		)
		left = max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
		if c.source == "Real":
			d["held"] += left
			d["buckets"] += len(plans.held_buckets(c.name))
		else:
			d["expected"] += left
		d["missing"] = d["missing"] or c.status == "Broken"
	return sorted(rows.values(), key=lambda r: (r["customer"] or "", r["sales_order"]))


def _mix_key(ln):
	from upande_packhouse.api.v2 import allocation_guards as guards

	return guards._mix_key(ln)


def _line(soi):
	"""One order line with ordered and allocated stems, the way the board reads it."""
	parent = frappe.db.get_value("Sales Order Item", soi, "parent")
	if not parent:
		frappe.throw(_("That order line no longer exists."))
	lines = pipeline.fetch_lines(sales_orders=[parent])
	pipeline.attach_pipeline(lines)
	ln = next((x for x in lines if x.name == soi), None)
	if ln is None:
		frappe.throw(_("That order line is not open for planning."))
	return ln


@frappe.whitelist()
def confirm_lines(delivery_date: str, farm: str, lines: str | list, team: str | None = None):
	"""Confirm stems for order lines: `lines` = [{"sales_order_item": ..., "stems": ..., "length": ...}], the TOTAL
	confirmed for each line (0 takes the line out of the plan; `length` is optional and only ever a longer one than
	ordered). Stock on the shelf is held first, oldest buckets first; anything beyond it is confirmed from the
	expected harvest.

	Every confirmation has a team, and the team belongs to the order line: each item can carry its own `team`
	(`team` is only the fallback for items that do not), or the line (or its mixed box) must already have one. A mixed box is
	packed by one team, so the team given for one of its lines is saved on every line of the box; allocation can then
	autofill it.

	The page shows the heads-ups (allocation_guards.preview_confirmation) before it calls this; going ahead is the planner's call."""
	frappe.only_for(PLANNERS)
	from upande_packhouse.api.v2 import allocation_guards as guards

	day = _day(delivery_date)
	if not farm:
		frappe.throw(_("Choose a farm."))
	if isinstance(lines, str):
		lines = json.loads(lines)
	plan = plans.get_plan(day, farm)
	if plan.status == "Closed":
		frappe.throw(_("The plan for {0} is closed.").format(day))
	stock_farms = _stock_farms(farm)
	for f in {i.get("stock_farm") for i in lines} - {None, ""}:
		if f not in stock_farms:
			frappe.throw(_("{0} is not one of the farms for {1}.").format(f, farm))
	for t in {team, *(i.get("team") for i in lines)} - {None, ""}:
		if not frappe.db.exists("Packing Teams", t):
			frappe.throw(_("{0} is not a team we know.").format(t))
	result = guards.check(day, farm, lines)
	basis = "revised forecast, cutting day %s" % (getdate(day) - timedelta(days=cint(plans.setting("allocation_harvest_lead_days"))))
	done = []
	for item in lines:
		soi, want = item["sales_order_item"], flt(item.get("stems"))
		if want < 0:
			frappe.throw(_("Stems cannot be less than 0."))
		ln = _line(soi)
		length = item.get("length") or ln.custom_length
		if length != ln.custom_length and not frappe.db.exists("Stem Length", length):
			frappe.throw(_("{0} is not a length we know.").format(length))
		if str(ln.delivery_date) != day:
			frappe.throw(_("That order line is for {0}, not {1}.").format(ln.delivery_date, day))
		room = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
		if want > room + 0.0001:
			frappe.throw(
				_("Only {0} stems of this line are left to confirm, and you asked for {1}.").format(int(room), int(want))
			)
		info = {
			"sales_order": ln.parent,
			"sales_order_item": soi,
			"customer": ln.customer,
			"ordered_variety": ln.item_code,
			"ordered_length": ln.custom_length,
			"mix_key": guards._mix_key(ln),
		}
		line_team = item.get("team") or team or plans.team_for_line(soi) or plans.group_team(info["mix_key"])
		# the farm whose stock this is confirmed against: chosen on the row, else where it was confirmed before, else the sales farm
		stock_farm = item.get("stock_farm") or plans.farm_for_line(soi) or farm
		if want > 0 and not line_team:
			frappe.throw(_("Choose a team for this confirmation."))
		held = 0.0
		if want > 0:
			_conf, held = plans.reserve_real(
				plan=plan,
				line=info,
				farm=stock_farm,
				variety=ln.item_code,
				length=length,
				stems=want,
				team=line_team,
			)
		else:
			for c in plans.live_confirmations(soi=soi):
				plans.release_confirmation(c.name, "Manual")
		rest = max(0.0, want - held)
		hv = min(rest, _harvest_room(day, farm, plan.name, ln.item_code, length, soi)) if rest > 0 else 0.0
		plans.plan_virtual(
			plan=plan,
			line=info,
			harvest_stems=hv,
			farm=stock_farm,
			variety=ln.item_code,
			length=length,
			stems=rest,
			forecast_basis=basis,
			team=line_team,
		)
		if want > 0:
			plans.apply_team_to_group(info["mix_key"], line_team)
		done.append({"soi": soi, "held": held, "expected": rest, "team": line_team, "farm": stock_farm})
	return {"success": True, "plan": plan.name, "lines": done, "quiet": result["quiet"]}


def _confirmed_at(soi, variety, length):
	"""A line's live confirmations: (stems still to be allocated, team, True when they are all at this variety and length)."""
	confs = plans.live_confirmations(soi=soi)
	total = sum(max(0.0, flt(c.stems) - flt(c.fulfilled_stems)) for c in confs)
	here = all(c.variety == variety and (c.length or "") == (length or "") for c in confs)
	return total, plans.team_for_line(soi), here


@frappe.whitelist()
def move_stems(delivery_date: str, farm: str, from_soi: str, to_soi: str, stems: float, team: str | None = None):
	"""Give stems that are confirmed for one customer to another customer who still needs them.

	Giving all of them is a swap (the first customer goes back to "still to confirm"); giving part is a split. The stems
	keep their variety and length, so a customer who ordered a shorter length receives the longer stem (the heads-up for that
	is on the page). Stock held on the shelf moves with them, then what was confirmed from the expected harvest.
	Both lines are confirmed again with their new totals, so the holds, the Confirmed Stems rows, the teams and the
	buckets stay consistent. A mixed box can receive stems (it is confirmed like any other line, and its team applies to the
	whole box), but it cannot give them away: its lines are packed together."""
	frappe.only_for(PLANNERS)
	day, stems = _day(delivery_date), flt(stems)
	if from_soi == to_soi:
		frappe.throw(_("Choose another customer to give the stems to."))
	if stems <= 0:
		frappe.throw(_("Choose how many stems to give."))
	src, dst = _line(from_soi), _line(to_soi)
	for ln in (src, dst):
		if str(ln.delivery_date) != day:
			frappe.throw(_("{0} is for {1}, not {2}.").format(ln.customer_name or ln.customer, ln.delivery_date, day))
	if src.custom_mixed_box or src.custom_mixed_bunch:
		frappe.throw(_("A mixed box is packed together, so its stems cannot be given to another customer."))
	if src.item_code != dst.item_code:
		frappe.throw(_("Stems can only go to a customer who ordered the same variety."))
	src_confs = plans.live_confirmations(soi=from_soi)
	if not src_confs:
		frappe.throw(_("{0} has nothing confirmed to give.").format(src.customer_name or src.customer))
	length = src_confs[0].length or ""
	src_farm = src_confs[0].farm
	have, src_team, src_here = _confirmed_at(from_soi, src.item_code, length)
	if not src_here:
		frappe.throw(_("{0} is confirmed at more than one length. Unconfirm it first.").format(src.customer_name or src.customer))
	if stems > have + 0.0001:
		frappe.throw(_("{0} has only {1} stems confirmed.").format(src.customer_name or src.customer, int(have)))
	dst_have, dst_team, dst_here = _confirmed_at(to_soi, dst.item_code, length)
	if plans.live_confirmations(soi=to_soi) and not dst_here:
		frappe.throw(_("{0} is already confirmed at another length. Unconfirm it first.").format(dst.customer_name or dst.customer))
	if any(c.farm != src_farm for c in plans.live_confirmations(soi=to_soi)):
		frappe.throw(_("{0} is already confirmed from another farm. Unconfirm it first.").format(dst.customer_name or dst.customer))
	room = max(0.0, flt(dst.ordered_stems) - flt(dst.allocated_stems) - dst_have)
	if stems > room + 0.0001:
		frappe.throw(_("{0} only needs {1} more stems.").format(dst.customer_name or dst.customer, int(room)))
	dst_team = team or dst_team
	if not dst_team:
		frappe.throw(_("Choose a team for {0}.").format(dst.customer_name or dst.customer))
	left = have - stems
	# the giver first, so what it lets go is free for the receiver
	confirm_lines(day, farm, [{"sales_order_item": from_soi, "stems": left, "stock_farm": src_farm, **({"team": src_team} if left > 0 and src_team else {})}])
	item = {"sales_order_item": to_soi, "stems": dst_have + stems, "team": dst_team, "stock_farm": src_farm}
	if length and length != (dst.custom_length or ""):
		item["length"] = length
	confirm_lines(day, farm, [item])
	return {"success": True, "moved": stems, "kept": left, "received": dst_have + stems}


@frappe.whitelist()
def split_stems(delivery_date: str, farm: str, from_soi: str, parts: str | list):
	"""Split one customer's confirmed stems between several customers at once.

	`parts` = [{"to_soi": ..., "stems": ..., "team": ...}]. Each part is a move_stems; together they cannot give away
	more than the customer has, and a customer can only appear once. Giving everything to one customer is a swap."""
	frappe.only_for(PLANNERS)
	if isinstance(parts, str):
		parts = json.loads(parts)
	parts = [x for x in (parts or []) if flt(x.get("stems")) > 0]
	if not parts:
		frappe.throw(_("Choose how many stems to give."))
	targets = [x["to_soi"] for x in parts]
	if len(set(targets)) != len(targets):
		frappe.throw(_("Each customer can only be chosen once."))
	have, _team, _here = _confirmed_at(from_soi, *_pool_of(from_soi))
	total = sum(flt(x["stems"]) for x in parts)
	if total > have + 0.0001:
		frappe.throw(_("You are giving {0} stems and only {1} are confirmed.").format(int(total), int(have)))
	moved = []
	for x in parts:
		moved.append(move_stems(delivery_date, farm, from_soi, x["to_soi"], flt(x["stems"]), x.get("team")))
	return {"success": True, "moved": total, "kept": max(0.0, have - total), "parts": moved}


def _pool_of(soi):
	"""(variety, length) a line is confirmed against."""
	confs = plans.live_confirmations(soi=soi)
	if not confs:
		frappe.throw(_("That customer has nothing confirmed to give."))
	return confs[0].variety, confs[0].length or ""


@frappe.whitelist()
def unconfirm_line(sales_order_item: str):
	"""Take an order line out of the plan: what it held on the shelf goes back for everyone, and what was confirmed
	from the expected harvest is dropped."""
	frappe.only_for(PLANNERS)
	live = plans.live_confirmations(soi=sales_order_item)
	for c in live:
		plans.release_confirmation(c.name, "Manual")
	return {"success": True, "released": len(live)}


@frappe.whitelist()
def unconfirm(confirmation: str):
	"""Take one confirmation out; whatever it held goes back to the shelf for everyone."""
	frappe.only_for(PLANNERS)
	if not frappe.db.exists("Allocation Confirmation", confirmation):
		frappe.throw(_("That confirmation no longer exists."))
	plans.release_confirmation(confirmation, "Manual")
	return {"success": True}
