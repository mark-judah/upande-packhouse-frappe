# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Planning v2 (www/sales-allocation-planning-v2.html): confirm a delivery day before allocation.

Spec: docs/allocation-playground/spec.md. This is phase 1: the board, the variety detail, confirming an order
line on the cutting day's rolling or production forecast (Virtual; it becomes Real as that harvest reaches the
shelf) and releasing it. Allocation uses the confirmations up (allocation_plan.py, sales_allocation.py).

The unit on the board is a variety and length for one delivery day (the "pool" in the spec; the page calls
it "variety and length"). For each one:

    ordered       order lines for the delivery date (pipeline.fetch_lines, ordered_stems)
    allocated     already allocated (pipeline.attach_pipeline)
    held          confirmed from stock (shelf or received) and not yet allocated (Real confirmations)
    expected      confirmed from stock still to arrive (Virtual confirmations)
    still to confirm  ordered - allocated - held - expected

What can be confirmed against, most certain first (all by variety and length):

    on the shelf            free stems that can still be confirmed (core/stock.bucket_rows: allocatable, so net of
                            age and cooling), less what other lines confirmed from stock and have not allocated
    received, not shelved   buckets received and waiting for a shelf, with their cold room (core/received.py)
    harvested, not received the day's production still on its way (core/received.harvested_day)
    still to be cut         the revised forecast for the cutting day less what has been harvested that day. "Safe"
                            takes off the forecast's average error for the variety this year (revised against actual)

    short         still to confirm less everything above that can cover it

Ages come from Production Settings through core/stock (amber time, discard age, max allocation age); no
number of days is written here.
"""

import json
import math
from collections import defaultdict
from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from upande_packhouse import allocation_plan as plans
from upande_packhouse import availability, quality_b
from upande_packhouse.api.v2 import allocation_forecast as forecasts
from upande_packhouse.api.v2.core import pipeline, received, stock

PLANNERS = (
	"System Manager",
	"Sales Manager",
	"Sales Master Manager",
	"Sales User",
	"Assistant Packhouse Manager",
)


def _memo(key, fn):
	"""Compute once per request: the board asks for the same variety's forecast for every length."""
	cache = frappe.flags.setdefault("allocation_planning_memo", {})
	if key not in cache:
		cache[key] = fn()
	return cache[key]


#: Seconds the page's calls share what they read of the floor (the day's lines, the shelf).
SHARED_SECONDS = 60


def _changed():
	"""A planner changed something: what the pages share is read again (now, and once it is committed)."""

	def bump():
		frappe.cache.set_value("allocation_planning_gen", frappe.generate_hash(length=10))

	frappe.flags.allocation_planning_memo = {}
	bump()
	frappe.db.after_commit.add(bump)


def _shared(key, fn):
	"""Read once a minute across requests: the board, the variety, the checks and the drawer all ask for the
	same lines and shelf when the page opens. A planner's change starts a new read (_changed)."""
	gen = frappe.cache.get_value("allocation_planning_gen") or ""
	ck = "allocation_planning:" + gen + ":" + ":".join(str(k) for k in key)
	got = frappe.cache.get_value(ck)
	if got is None:
		got = fn()
		frappe.cache.set_value(ck, got, expires_in_sec=SHARED_SECONDS)
	return got


def _day(delivery_date):
	"""The delivery day planned: the day after tomorrow unless asked (tomorrow is already being allocated)."""
	return str(getdate(delivery_date or add_days(today(), 2)))


def _week(week=None):
	"""The ISO week planned ("2026-W46"), next week unless asked: (week, [its days, Monday to Sunday])."""
	if week:
		year, num = str(week).upper().split("-W")
		monday = date.fromisocalendar(int(year), int(num), 1)
	else:
		now = getdate(today())
		monday = now - timedelta(days=now.weekday()) + timedelta(days=7)
	year, num, _wd = monday.isocalendar()
	return "%d-W%02d" % (year, num), [str(monday + timedelta(days=i)) for i in range(7)]


def _cut_of(day):
	"""The cutting day for a delivery day."""
	return getdate(day) - timedelta(days=plans.HARVEST_LEAD_DAYS)


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
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	hub = transfer_hub(required=False)
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
	# the better of the two forecasts' errors: confirmations use whichever forecast has more that is safe to promise
	rev = _memo(("error", variety, farms, today()), lambda: _forecast_error(variety, farms))
	roll = _memo(("rerror", variety, farms, today()), lambda: forecasts.rolling_error(variety, farms))
	have = [x for x in (rev, roll) if x[0] is not None]
	return min(have, key=lambda x: x[0]) if have else (None, 0)


def _forecast_error(variety, farms):
	return _error_of(_error_rows((variety,), farms).get(variety, []))


def _error_rows(varieties, farms):
	"""{variety: [{week_number, revised, actual}]} of this year's finished weeks that have an actual."""
	year, week, _wd = getdate(today()).isocalendar()
	cond, params = "", {"v": tuple(varieties), "y": year, "w": week}
	if farms:
		cond = " AND wh.custom_farm IN %(farms)s"
		params["farms"] = tuple(farms)
	out = defaultdict(list)
	# the f-string hole is fixed SQL; values are bound
	for r in frappe.db.sql(  # nosemgrep
		f"""SELECT f.variety, w.week_number, SUM(w.revised_forecast_stems) AS revised, SUM(w.actual_stems) AS actual
		    FROM `tabProduction Forecast Week` w
		    INNER JOIN `tabProduction Forecast` f ON f.name = w.parent
		    LEFT JOIN `tabWarehouse` wh ON wh.name = f.greenhouse
		    WHERE f.variety IN %(v)s AND w.grade = 'all' AND w.iso_year = %(y)s AND w.week_number < %(w)s {cond}
		    GROUP BY f.variety, w.week_number HAVING SUM(w.actual_stems) > 0""",
		params,
		as_dict=True,
	):
		out[r.variety].append(r)
	return out


def _error_of(rows):
	if not rows:
		return None, 0
	actual = sum(flt(r.actual) for r in rows)
	off = sum(abs(flt(r.revised) - flt(r.actual)) for r in rows)
	return min(1.0, off / actual), len(rows)


def expected_detail(variety, length, delivery_date, farm=None):
	"""What the farms are forecast to cut for this delivery date, by growing farm.

	Returns {"likely", "sure", "by_farm", "error", "weeks"} for the forecast being confirmed against ("basis"), with both
	forecasts ("revised", "rolling") and their variance. The forecast is kept per greenhouse; a greenhouse
	belongs to a farm (Warehouse > Farm) and only the farm is shown.

	Revised forecast for the cutting day's week, shared over its days, times the length's share of the variety's
	lengths (revised length rows, else the manual budget; the length rows do not sum to the total so only their
	shares are used). `sure` takes off the variety's average forecast error this year (forecast_error); with no
	finished week to judge by, none of it is counted as sure."""
	lead = plans.HARVEST_LEAD_DAYS
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
	revised_error, revised_weeks = (
		_memo(("error", variety, farms, today()), lambda: _forecast_error(variety, farms))
		if likely > 0
		else (None, 0)
	)
	revised = {
		"likely": likely,
		"sure": likely * (1 - revised_error) if revised_error is not None else 0.0,
		"by_farm": by_farm,
		"error": revised_error,
		"weeks": revised_weeks,
	}
	# the rolling forecast: what was cut one cut-to-cut cycle before. Length shares fall back to the revised rows' own.
	mine = sum(v[0] for v in share_by_farm.values())
	allv = sum(v[1] for v in share_by_farm.values())
	roll_farms = forecasts.rolling_by_farm(
		variety, length, cut_day, farms, (mine / allv) if allv > 0 else None
	)
	rolling = None
	if roll_farms is not None:
		r_likely = sum(roll_farms.values())
		r_err, r_weeks = (
			_memo(("rerror", variety, farms, today()), lambda: forecasts.rolling_error(variety, farms))
			if r_likely > 0
			else (None, 0)
		)
		rolling = {
			"likely": r_likely,
			"sure": r_likely * (1 - r_err) if r_err is not None else 0.0,
			"by_farm": roll_farms,
			"error": r_err,
			"weeks": r_weeks,
		}
	# Confirm against either: the forecast with more that is safe to promise covers (the production forecast on a tie).
	used = (
		forecasts.ROLLING
		if (rolling is not None and rolling["sure"] > revised["sure"])
		else forecasts.REVISED
	)
	chosen = rolling if used == forecasts.ROLLING else revised
	diff = (rolling["likely"] - revised["likely"]) if rolling is not None else None
	return {
		**chosen,
		"basis": used,
		"revised": revised,
		"rolling": rolling,
		"variance": diff,
		"variance_pct": (diff / revised["likely"]) if diff is not None and revised["likely"] > 0 else None,
	}


def _split_forecast(rows, length, total_by_farm, share_by_farm):
	for r in rows:
		if r.grade == "all":
			total_by_farm[r.farm] += flt(r.revised)
		else:
			v = flt(r.revised) or flt(r.manual)
			share_by_farm[r.farm][1] += v
			if r.grade == (length or ""):
				share_by_farm[r.farm][0] += v


def _spec_rank(specs):
	"""{specification: {variety: (0 for its primary variety else 1, row)}}."""
	out = defaultdict(dict)
	if specs:
		for spec, variety, primary, idx in frappe.db.sql(
			"""SELECT parent, variety, IFNULL(is_primary, 0), idx FROM `tabSpec Approved Variety`
			   WHERE parenttype = 'Specifications' AND parent IN %(s)s AND IFNULL(variety, '') != ''""",
			{"s": tuple(specs)},
		):
			key = (0 if primary else 1, idx)
			out[spec][variety] = min(out[spec].get(variety, key), key)
	return out


def _forecast_rows(variety, year, week, farms):
	return _forecast_rows_for((variety,), year, week, farms).get(variety, [])


def _forecast_rows_for(varieties, year, week, farms):
	"""{variety: [{grade, farm, revised, manual}]} of the week's forecast, by grade and growing farm. `varieties`
	None: every variety forecast that week. Quality B (the Orders filter) is planned from the shelf only: none."""
	if _order_kind() == "qualityb":
		return defaultdict(list)
	conds, params = [], {"y": year, "w": week}
	if varieties is not None:
		conds.append("f.variety IN %(v)s")
		params["v"] = tuple(varieties) or ("",)
	if farms:
		conds.append("wh.custom_farm IN %(farms)s")
		params["farms"] = tuple(farms)
	cond = "".join(" AND " + c for c in conds)
	out = defaultdict(list)
	# the f-string hole is fixed SQL; values are bound
	for r in frappe.db.sql(  # nosemgrep
		f"""SELECT f.variety, w.grade, COALESCE(NULLIF(wh.custom_farm, ''), 'Unknown') AS farm,
		           SUM(w.revised_forecast_stems) AS revised, SUM(w.manual_budget_stems) AS manual
		    FROM `tabProduction Forecast Week` w
		    INNER JOIN `tabProduction Forecast` f ON f.name = w.parent
		    LEFT JOIN `tabWarehouse` wh ON wh.name = f.greenhouse
		    WHERE w.iso_year = %(y)s AND w.week_number = %(w)s{cond}
		    GROUP BY f.variety, w.grade, farm""",
		params,
		as_dict=True,
	):
		out[r.variety].append(r)
	return out


def _prefetch_forecasts(varieties, delivery_date, farm):
	"""Read every variety's cutting-day forecast and forecast error in two queries, into the request memo that
	expected_detail and forecast_error read, instead of two queries per variety."""
	if not varieties:
		return
	cut_day = getdate(delivery_date) - timedelta(days=plans.HARVEST_LEAD_DAYS)
	year, week, _wd = cut_day.isocalendar()
	farms = tuple(_growing_farms(farm) or ())
	memo = frappe.flags.setdefault("allocation_planning_memo", {})
	todo = [v for v in varieties if ("forecast", v, year, week, farms) not in memo]
	if todo:
		rows = _forecast_rows_for(todo, year, week, farms)
		for v in todo:
			memo[("forecast", v, year, week, farms)] = rows.get(v, [])
	todo = [v for v in varieties if ("error", v, farms, today()) not in memo]
	if todo:
		rows = _error_rows(todo, farms)
		for v in todo:
			memo[("error", v, farms, today())] = _error_of(rows.get(v, []))


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
	# read once per request; a plan made in this request (plans.ensure_plan) is read again
	cache = frappe.flags.setdefault("allocation_planning_memo", {})
	key = ("plan_name", str(day), farm)
	if not cache.get(key):
		got = frappe.db.sql(
			"SELECT name FROM `tabAllocation Plan` WHERE delivery_date = %s AND farm = %s LIMIT 1",
			(str(day), farm),
		)
		cache[key] = got[0][0] if got else None
	return cache[key]


def _with_greenhouses(fn):
	"""Sea freight lines carry their greenhouses (sea_freight_plan.with_greenhouses); imported late, the module
	imports this one."""
	import functools

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		from upande_packhouse.api.v2.sea_freight_plan import with_greenhouses

		return with_greenhouses(fn)(*args, **kwargs)

	return wrapper


def _order_kind():
	"""Allocation Planning's Orders filter, sent by the page as `order_kind`: Quality B (webstore, Shopping Cart)
	orders, sea freight orders (harvested for the order, planned by greenhouse: sea_freight_plan), else export --
	every other order. Any other caller plans export."""
	kind = (frappe.form_dict or {}).get("order_kind")
	return kind if kind in ("qualityb", "seafreight") else "export"


def _kind_sql():
	"""The orders the Orders filter shows, as SQL on `so`."""
	kind = _order_kind()
	sea = (
		"IFNULL(so.custom_mode_of_transport, '') = 'Sea Freight'"
		if frappe.db.has_column("Sales Order", "custom_mode_of_transport")
		else "0"
	)
	if kind == "qualityb":
		return "AND IFNULL(so.order_type, '') = 'Shopping Cart'"
	if kind == "seafreight":
		return "AND " + sea
	return "AND IFNULL(so.order_type, '') != 'Shopping Cart' AND NOT (" + sea + ")"


def _lines(day, to=None):
	"""The lines for a delivery day, or for every day from `day` to `to` (read once per request), of the orders
	the Orders filter shows (_order_kind)."""
	kind = _order_kind()

	def read():
		lines = pipeline.fetch_lines(delivery_from=day, delivery_to=to or day, extra_where=_kind_sql())
		pipeline.attach_pipeline(lines)
		return lines

	key = ("lines", str(day), str(to or day), kind)
	return list(_memo(key, lambda: _shared(key, read)))


def _lines_for(day, farm, to=None):
	"""The day's lines this sales farm plans: orders of a farm in its location, and, for the default sales
	farm, orders whose farm is in no sales farm's location (or not set). Each order is planned by one farm only."""
	mine = set(_stock_farms(farm))
	if farm == _default_farm():
		covered = {f for r in _farms() for f in _stock_farms(r.farm)}
		return [ln for ln in _lines(day, to) if ln.so_farm in mine or ln.so_farm not in covered]
	return [ln for ln in _lines(day, to) if ln.so_farm in mine]


def _planned_week(days, farm):
	"""_planned_by_line for every delivery day of the week (a line belongs to one day's plan)."""
	out = {}
	for d in days:
		out.update(_planned_by_line(d, farm))
	return out


def _planned_by_line(day, farm):
	"""{soi: {"real": stems still held, "expected": stems planned from the harvest}}"""
	out = {}
	for c in plans.live_confirmations(plan=_plan_name(day, farm)) if _plan_name(day, farm) else []:
		d = out.setdefault(
			c.sales_order_item, {"real": 0.0, "expected": 0.0, "status": [], "length": "", "farm": ""}
		)
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
		d = out.setdefault(
			(c.variety, c.length or ""), {"lines": set(), "stems": 0.0, "real": 0.0, "expected": 0.0}
		)
		d["lines"].add(c.sales_order_item)
		left = max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
		d["stems"] += left
		d["real" if c.source == "Real" else "expected"] += left
	return {
		k: {"lines": len(v["lines"]), "stems": v["stems"], "real": v["real"], "expected": v["expected"]}
		for k, v in out.items()
	}


def _stock_by_pool(farm, varieties, day=None, everything=False):
	"""Everything that exists for each (variety, length): on the shelf, received and not shelved (by cold room),
	and the cutting day's harvest that has not been received yet. `old` is the part getting old."""
	amber = amber_from_days()
	out = {}
	if not varieties and not everything:
		return out

	def entry(key):
		return out.setdefault(
			key, {**SUPPLY, "rooms": {}, "by_farm": {}, "rec_by_farm": {}, "old_by_farm": {}}
		)

	farms = _stock_farms(farm)
	wanted = set(varieties) if varieties else None
	if _order_kind() == "qualityb":
		# Quality B is packed from the shelf stock at or past its discard age on the processing day: no
		# received-not-shelved stock, no harvest, no forecast
		key = ("qb_rows", tuple(farms) if farms is not None else None, str(day or ""))
		for r in _memo(key, lambda: _shared(key, lambda: quality_b.aged_stock(farms, day))):
			if wanted is not None and r.variety not in wanted:
				continue
			p = entry((r.variety, r.stem_length or ""))
			p["free"] += r.qb_free
			p["buckets"] += 1
			p["oldest"] = max(p["oldest"], r.age_days or 0)
			p["by_farm"][r.shelf_farm] = p["by_farm"].get(r.shelf_farm, 0.0) + r.qb_free
		_take_outstanding(out, farms)
		return out
	if _order_kind() == "seafreight":
		# harvested for the order: nothing from the shelf or the cold room, only the cutting day's harvest (the
		# forecast is added by the board as for export)
		if day and varieties:
			cut = getdate(day) - timedelta(days=plans.HARVEST_LEAD_DAYS)
			for key, h in received.harvested_day(cut, _growing_farms(farm), varieties).items():
				p = entry(key)
				p["harvested"] = h["harvested"]
				p["on_the_way"] = h["not_received"]
		return out
	# every variety's stock rows once per request: the board, the checks and each day of a week read the same shelf
	every = _memo(
		("bucket_rows", tuple(farms) if farms is not None else None),
		lambda: _shared(
			("bucket_rows", tuple(farms) if farms is not None else None),
			lambda: stock.bucket_rows(farms=farms),
		),
	)
	for r in every if wanted is None else (r for r in every if r.variety in wanted):
		p = entry((r.variety, r.stem_length or ""))
		p["free"] += r.allocatable
		if r.allocatable > 0:
			p["buckets"] += 1
			p["oldest"] = max(p["oldest"], r.age_days or 0)
			p["by_farm"][r.shelf_farm] = p["by_farm"].get(r.shelf_farm, 0.0) + r.allocatable
			if (r.age_days or 0) >= amber:
				p["old"] += r.allocatable
				p["old_by_farm"][r.shelf_farm] = p["old_by_farm"].get(r.shelf_farm, 0.0) + r.allocatable
	recv = received.received_unshelved(farms=farms, varieties=sorted(varieties) if varieties else None)
	for r in recv:
		free = max(0.0, flt(r.stems))
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
	_take_outstanding(out, farms)
	if day and varieties:
		cut = getdate(day) - timedelta(days=plans.HARVEST_LEAD_DAYS)
		for key, h in received.harvested_day(cut, _growing_farms(farm), varieties).items():
			p = entry(key)
			p["harvested"] = h["harvested"]
			p["on_the_way"] = h["not_received"]
	return out


def _take_outstanding(out, farms):
	"""Stems the plan confirmed from stock and not allocated yet are no longer free: off the shelf first, then
	off what is received and waiting for a shelf."""
	for (f, variety, length), stems in plans.real_outstanding(farms).items():
		p = out.get((variety, length))
		if not p or stems <= 0:
			continue
		take = min(stems, p["free"])
		p["free"] -= take
		p["by_farm"][f] = max(0.0, p["by_farm"].get(f, 0.0) - take)
		rest = min(stems - take, p["received"])
		p["received"] -= rest
		p["rec_by_farm"][f] = max(0.0, p["rec_by_farm"].get(f, 0.0) - rest)
		p["held"] += take + rest


def expected_for(variety, length, day, farm, s):
	"""What is still to come for a variety and length, from its supply entry `s`: (likely, sure) in stems.

	The day's harvest not yet received is real; what is still to be cut is the forecast less what was harvested
	that day, and only that part carries the forecast's error."""
	likely_total, sure_total = expected_arrivals(variety, length, day, farm)
	remaining = max(0.0, likely_total - s["harvested"])
	ratio = (sure_total / likely_total) if likely_total > 0 else 0.0
	return s["on_the_way"] + remaining, s["on_the_way"] + remaining * ratio


def cut_day_total(s, forecast):
	"""Stems the cutting day brings for a variety and length: the forecast, or what was harvested once that is more.
	Confirmations count on this, never on stock already on the shelf."""
	return max(flt(forecast), s["harvested"])


def _build_pools(day, farm, overrides=None, only_variety=None):
	lines = _lines_for(day, farm)
	if only_variety:
		lines = [ln for ln in lines if ln.item_code == only_variety]
	planned = overrides if overrides is not None else _planned_by_line(day, farm)
	_prefetch_forecasts(sorted({ln.item_code for ln in lines}), day, farm)
	supply = _stock_by_pool(farm, {ln.item_code for ln in lines}, day)
	rolling = _rolling_by_pool(day, farm, {ln.item_code for ln in lines})
	confirmed_here = _confirmed_here(day, farm)
	pools = {}
	# stems confirmed, by the variety and length they were confirmed at: a 50cm line confirmed at 60cm takes
	# from the 60cm harvest, never from the 50cm one
	counted_at = defaultdict(float)
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
				"lines_planned": 0,
			},
		)
		pl = planned.get(ln.name, {"real": 0.0, "expected": 0.0})
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		p["lines"] += 1
		p["ordered"] += ordered
		p["allocated"] += min(allocated, ordered)
		p["held"] += pl["real"]
		p["expected_plan"] += pl["expected"]
		counted_at[(ln.item_code, pl.get("length") or key[1])] += pl["real"] + pl["expected"]
		if ordered > 0 and allocated + pl["real"] + pl["expected"] >= ordered - 0.0001:
			p["lines_done"] += 1
		# planned: anything allocated or confirmed for it, even short of what was ordered
		if allocated + pl["real"] + pl["expected"] > 0.0001:
			p["lines_planned"] += 1
	out = []
	for key, p in pools.items():
		s = {**SUPPLY, **supply.get(key, {})}
		likely, sure = expected_for(key[0], key[1], day, farm, s)
		left = max(0.0, p["ordered"] - p["allocated"] - p["held"] - p["expected_plan"])
		# what the rolling forecast for the cutting day still has for lines not confirmed yet
		counted = counted_at.get(key, 0.0)
		have = max(0.0, cut_day_total(s, rolling.get(key, 0.0)) - counted)
		harvested_left = max(0.0, s["harvested"] - counted)
		short = max(0.0, left - have)
		if short > 0:
			status = "short"
		elif left > 0 and (left > harvested_left or s["old"] > 0):
			status = "watch"
		else:
			status = "ok"
		# of everything ordered: how much is allocated, confirmed, or can be covered by the forecast
		covered = p["allocated"] + counted + min(left, have)
		ch = confirmed_here.get(key, {"lines": 0, "stems": 0.0, "real": 0.0, "expected": 0.0})
		p.update(
			confirmed_lines=ch["lines"],
			confirmed_here=ch["stems"],
			have=have,
			harvested_left=harvested_left,
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


def _build_pools_week(days, farm, only_variety=None):
	"""The week's varieties and lengths, running by delivery day: the lines of each day count on the stock there is
	now plus every cutting day's forecast up to their own, less what the lines of earlier days took. Both forecasts
	(rolling and production) carry their own balance; `days` holds each delivery day's share."""
	return _memo(
		("week_pools", farm, tuple(days), only_variety), lambda: _week_pools(days, farm, only_variety)
	)


def _week_lines(days, farm):
	"""The week's lines for the sales farm, read once per request."""
	return _memo(("week_lines", farm, tuple(days)), lambda: _lines_for(days[0], farm, days[-1]))


def _week_pools(days, farm, only_variety):
	lines = _week_lines(days, farm)
	if only_variety:
		wanted = {only_variety} if isinstance(only_variety, str) else set(only_variety)
		lines = [ln for ln in lines if ln.item_code in wanted]
	if not lines:
		return []
	varieties = {ln.item_code for ln in lines}
	planned = _planned_week(days, farm)
	here = {}
	for d in days:
		for k, v in _confirmed_here(d, farm).items():
			h = here.setdefault(k, {"lines": 0, "stems": 0.0, "real": 0.0, "expected": 0.0})
			for f in h:
				h[f] += v[f]
	by_day = {}
	# per delivery day, stems confirmed at the variety and length they were confirmed at (see _build_pools)
	counted_at = defaultdict(lambda: defaultdict(float))
	for ln in lines:
		key = (ln.item_code, ln.custom_length or "")
		q = by_day.setdefault(str(ln.delivery_date), {}).setdefault(
			key,
			{
				"lines": 0,
				"ordered": 0.0,
				"allocated": 0.0,
				"held": 0.0,
				"expected_plan": 0.0,
				"lines_done": 0,
				"lines_planned": 0,
			},
		)
		pl = planned.get(ln.name, {"real": 0.0, "expected": 0.0})
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		q["lines"] += 1
		q["ordered"] += ordered
		q["allocated"] += min(allocated, ordered)
		q["held"] += pl["real"]
		q["expected_plan"] += pl["expected"]
		counted_at[str(ln.delivery_date)][(ln.item_code, pl.get("length") or key[1])] += (
			pl["real"] + pl["expected"]
		)
		if ordered > 0 and allocated + pl["real"] + pl["expected"] >= ordered - 0.0001:
			q["lines_done"] += 1
		# planned: anything allocated or confirmed for it, even short of what was ordered
		if allocated + pl["real"] + pl["expected"] > 0.0001:
			q["lines_planned"] += 1
	now = _stock_by_pool(farm, varieties)
	keys = sorted({k for d in by_day.values() for k in d})
	sums = (
		"lines",
		"ordered",
		"allocated",
		"held",
		"expected_plan",
		"lines_done",
		"lines_planned",
		"still_to_plan",
		"short",
		"short_production",
		"forecast",
		"expected_likely",
		"expected_sure",
		"on_the_way",
		"harvested",
	)
	pools, bal = {}, {}
	for key in keys:
		s = {**SUPPLY, **now.get(key, {})}
		pools[key] = {"variety": key[0], "length": key[1], "days": [], **{f: 0.0 for f in sums}}
		start = flt(s["free"]) + flt(s["received"])
		bal[key] = {"rolling": start, "production": start, "start": start}
	growing = _growing_farms(farm)
	for d in days:
		_prefetch_forecasts(sorted(varieties), d, farm)
		harvest = received.harvested_day(_cut_of(d), growing, varieties)
		rolling = _rolling_by_pool(d, farm, varieties)
		# a cutting day of today (or before): what it cut and was already received is in the stock the week
		# starts from, so it is not counted again with the day's cut
		cut_done = _cut_of(d) <= getdate(today())
		for key in keys:
			p, q, b = pools[key], by_day.get(d, {}).get(key), bal[key]
			h = harvest.get(key) or {}
			s = {**SUPPLY, "harvested": flt(h.get("harvested")), "on_the_way": flt(h.get("not_received"))}
			likely, sure = expected_for(key[0], key[1], d, farm, s)
			counted = counted_at.get(d, {}).get(key, 0.0)
			in_stock = max(0.0, s["harvested"] - s["on_the_way"]) if cut_done else 0.0
			roll_in = max(0.0, cut_day_total(s, rolling.get(key, 0.0)) - in_stock - counted)
			b["rolling"] += roll_in
			b["production"] += max(0.0, s["harvested"] - s["on_the_way"] - in_stock + likely - counted)
			p["forecast"] += roll_in
			p["expected_likely"] += likely
			p["expected_sure"] += sure
			p["on_the_way"] += s["on_the_way"]
			p["harvested"] += s["harvested"]
			if not q:
				continue
			left = max(0.0, q["ordered"] - q["allocated"] - q["held"] - q["expected_plan"])
			row = {
				"date": d,
				"ordered": q["ordered"],
				"left": left,
				"avail": b["rolling"],
				"avail_production": b["production"],
			}
			for basis, short_key in (("rolling", "short"), ("production", "short_production")):
				take = min(left, b[basis])
				b[basis] -= take
				row[short_key] = left - take
				p[short_key] += left - take
			p["days"].append(row)
			p["still_to_plan"] += left
			for f in (
				"lines",
				"ordered",
				"allocated",
				"held",
				"expected_plan",
				"lines_done",
				"lines_planned",
			):
				p[f] += q[f]
	out = []
	for key, p in pools.items():
		s = {**SUPPLY, **now.get(key, {})}
		counted = p["held"] + p["expected_plan"]
		left, short = p["still_to_plan"], p["short"]
		harvested_left = max(0.0, p["harvested"] - counted)
		if short > 0:
			status = "short"
		elif left > 0 and (left > harvested_left + bal[key]["start"] or s["old"] > 0):
			status = "watch"
		else:
			status = "ok"
		covered = p["allocated"] + counted + left - short
		ch = here.get(key, {"lines": 0, "stems": 0.0})
		p.update(
			lines=int(p["lines"]),
			lines_done=int(p["lines_done"]),
			lines_planned=int(p["lines_planned"]),
			confirmed_lines=ch["lines"],
			confirmed_here=ch["stems"],
			have=bal[key]["start"] + p["forecast"],
			harvested_left=harvested_left,
			covered_pct=int(round(100.0 * min(1.0, covered / p["ordered"]))) if p["ordered"] > 0 else 100,
			on_shelf=s["free"],
			received=s["received"],
			getting_old=s["old"],
			oldest_days=s["oldest"],
			buckets=s["buckets"],
			status=status,
		)
		out.append(p)
	return out


@frappe.whitelist()
@_with_greenhouses
def get_board(
	delivery_date: str | None = None,
	farm: str | None = None,
	view: str = "attention",
	week: str | None = None,
):
	"""The four numbers and the list of varieties and lengths, most urgent first. With `week`, every delivery day of
	that week (see _build_pools_week)."""
	farm = farm or _default_farm()
	if week:
		week, days = _week(week)
	else:
		days = [_day(delivery_date)]
	# a passed or closed delivery day takes no more confirmations: its orders and lines leave the lists
	closed = _closed_days(days, farm)
	all_days = days
	days = [d for d in days if d not in closed] or days
	nothing_open = all(d in closed for d in days)
	day = days[0]
	# Stock that has reached the shelf since the last visit takes over from the expected harvest -- held for the
	# lines, so only when a planner opens the board, never for anyone else who can read it.
	for d in days if set(PLANNERS) & set(frappe.get_roles()) else []:
		plan_name = _plan_name(d, farm)
		if plan_name and frappe.db.exists(
			"Allocation Confirmation", {"plan": plan_name, "source": "Virtual", "status": "Planned"}
		):
			plans.convert_virtual(d, farm)
	pools = _build_pools_week(days, farm) if week else _build_pools(day, farm)
	kpis = {
		"lines": sum(p["lines"] for p in pools),
		"lines_planned": sum(p["lines_planned"] for p in pools),
		"stems_left": sum(p["still_to_plan"] for p in pools),
		"short": sum(p["short"] for p in pools),
		"use_first": sum(p["getting_old"] for p in pools if p["still_to_plan"] > 0),
		"non_moving": sum(p["getting_old"] for p in pools),
	}
	_confs = []
	for d in days:
		_name = _plan_name(d, farm)
		_confs += plans.live_confirmations(plan=_name) if _name else []
	kpis["confirmed_orders"] = len({c.sales_order for c in _confs})
	from upande_packhouse.api.v2.allocation_teams import crop_of_groups, team_sales_farms, teams_at

	crop_of, group_of = crop_of_groups(), {}
	for row in frappe.get_all(
		"Item",
		filters={"name": ("in", list({x["variety"] for x in pools}) or [""])},
		fields=["name", "item_group"],
	):
		group_of[row.name] = row.item_group
	# whether a variety's lines are in mixes, straight boxes or both: the page filters on it
	from upande_packhouse.api.v2.allocation_checks import mix_key

	kinds = defaultdict(set)
	for ln in _week_lines(days, farm) if week else _lines_for(day, farm):
		kinds[(ln.item_code, ln.custom_length or "")].add("mixed" if mix_key(ln) else "straight")
	for x in pools:
		x["crop"] = crop_of.get(group_of.get(x["variety"])) or ""
		x["kinds"] = sorted(kinds.get((x["variety"], x["length"])) or [])
	kpis["confirmed_lines"] = len({c.sales_order_item for c in _confs})
	ordered = {(x["variety"], x["length"]): x for x in pools}
	forecast, harvest_days = _forecast_supply(days, farm)
	# stems already confirmed on the harvest are taken off the forecast: the next order of the variety sees what is left
	promised = _expected_confirmed(farm, max(days))
	for f in forecast:
		took = promised.get((f["variety"], f["length"]), 0.0)
		if took:
			# promised from the harvest; what the harvest left to come cannot cover (it was cut and is on the
			# shelf already) comes off the shelf
			shelf = f["on_shelf"]
			f["on_shelf"] = round(max(0.0, shelf - max(0.0, took - f["rolling"])))
			shelf_production = round(max(0.0, shelf - max(0.0, took - f["production"])))
			f["rolling"] = round(max(0.0, f["rolling"] - took))
			f["production"] = round(max(0.0, f["production"] - took))
			f["total"] = f["on_shelf"] + f["rolling"]
			f["total_production"] = shelf_production + f["production"]
	for row in frappe.get_all(
		"Item",
		filters={"name": ("in", list({f["variety"] for f in forecast} - set(group_of)) or [""])},
		fields=["name", "item_group"],
	):
		group_of[row.name] = row.item_group
	for f in forecast:
		p = ordered.get((f["variety"], f["length"]))
		f["ordered"] = p["still_to_plan"] if p else 0.0
		f["crop"] = crop_of.get(group_of.get(f["variety"])) or ""
	forecast.sort(key=lambda f: (-f["total"], f["variety"], f["length"]))
	kpis["planned_pct"] = round(100.0 * kpis["lines_planned"] / kpis["lines"]) if kpis["lines"] else None
	rank = {"short": 0, "watch": 1, "ok": 2}
	pools.sort(key=lambda p: (rank[p["status"]], -p["short"], -p["still_to_plan"], p["variety"], p["length"]))
	if view == "confirmed":
		pools = [p for p in pools if p["confirmed_lines"] or p["held"] or p["expected_plan"]]
	else:
		# a variety whose every line is planned is done: it leaves the list
		pools = [p for p in pools if p["lines_planned"] < p["lines"]]
		if view != "all":
			pools = [p for p in pools if p["status"] != "ok"]
	if nothing_open:
		pools = []
		kpis.update(
			{k: 0 for k in ("lines", "lines_planned", "stems_left", "short", "use_first", "non_moving")}
		)
	lines_now = [] if nothing_open else (_week_lines(days, farm) if week else _lines_for(day, farm))
	return {
		"success": True,
		"delivery_date": day,
		"week": week or "",
		"days": all_days,
		"closed_days": sorted(closed),
		"farm": farm,
		"farms": [r.farm for r in _farms()],
		"stock_farms": _stock_farms(farm),
		"amber_from_days": amber_from_days(),
		"teams": teams_at(farm),
		# every team with the sales farm it packs at: a team of another sales farm sends the order there
		"all_teams": [{"team": t, "sales_farm": f} for t, f in sorted(team_sales_farms().items())],
		"days_ahead": cint(plans.setting("allocation_horizon_days")),
		"kpis": kpis,
		"pools": pools,
		"forecast": forecast,
		"mixes": _mixes(lines_now, days, farm),
		"orders": _orders_view(lines_now, days, farm, crop_of),
		"harvest_days": harvest_days,
	}


def _closed_days(days, farm):
	"""The days among `days` that take no more confirmations: a delivery day already passed, or one whose
	Allocation Plan for `farm` is closed."""
	now = getdate(today())
	return {str(d) for d in days if getdate(d) < now} | {
		str(d)
		for d in frappe.get_all(
			"Allocation Plan",
			filters={"farm": farm, "delivery_date": ("in", list(days)), "status": "Closed"},
			pluck="delivery_date",
		)
	}


def _orders_view(lines, days, farm, crop_of):
	"""The day's (or week's) orders with their lines, a mix's lines together (box_groups, as Sales Allocation
	packs them): [{sales_order, order_name, customer, delivery_date, boxes, lines: [{soi, variety, length,
	ordered, allocated, planned, left, per_box, boxes, mixed, group, group_name, team, crop}]}]. A whole order,
	or any of its straight lines or mixes on its own, can be confirmed for a team."""
	from upande_packhouse.api.v2.allocation_checks import per_box
	from upande_packhouse.box_groups import box_keys

	planned = _planned_week(days, farm) if len(days) > 1 else _planned_by_line(days[0], farm)
	keys = box_keys(lines)
	teams = plans.teams_for_lines([ln.name for ln in lines])
	groups = dict(
		frappe.db.sql(
			"SELECT name, item_group FROM `tabItem` WHERE name IN %(i)s",
			{"i": tuple({ln.item_code for ln in lines}) or ("",)},
		)
	)
	names = dict(
		frappe.db.sql(
			"SELECT name, IFNULL(custom_order_name, '') FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": tuple({ln.parent for ln in lines}) or ("",)},
		)
	)
	orders = {}
	for ln in lines:
		o = orders.setdefault(
			ln.parent,
			{
				"sales_order": ln.parent,
				"order_name": names.get(ln.parent) or "",
				"customer": ln.customer_name or ln.customer,
				"delivery_date": str(ln.delivery_date),
				"lines": [],
			},
		)
		key = keys.get(ln.name) or ("line", ln.name)
		pl = planned.get(ln.name, {"real": 0.0, "expected": 0.0})
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		have = pl["real"] + pl["expected"]
		o["lines"].append(
			{
				"soi": ln.name,
				"idx": ln.idx,
				"variety": ln.item_code,
				"length": ln.custom_length or "",
				"ordered": ordered,
				"allocated": min(allocated, ordered),
				"planned": have,
				"left": max(0.0, ordered - allocated - have),
				"per_box": per_box(ln),
				"boxes": cint(ln.custom_number_of_boxes),
				"mixed": key[0] != "line",
				"group": "" if key[0] == "line" else "%s|%s" % key,
				"group_name": (ln.custom_mix_name or ln.custom_line or "") if key[0] != "line" else "",
				"team": teams.get(ln.name) or "",
				"crop": crop_of.get(groups.get(ln.item_code)) or "",
				# the farm whose stock it is confirmed against: confirming it again keeps it there
				"stock_farm": pl.get("farm") or "",
			}
		)
	out = []
	for o in orders.values():
		o["lines"].sort(key=lambda x: (x["mixed"], x["group"], x["idx"]))
		o["open"] = sum(1 for x in o["lines"] if x["planned"] <= 0.5 and x["left"] > 0.5)
		o["boxes"] = sum(x["boxes"] for x in o["lines"] if not x["mixed"]) + sum(
			{x["group"]: x["boxes"] for x in o["lines"] if x["mixed"]}.values()
		)
		out.append(o)
	out.sort(key=lambda o: (o["delivery_date"], -o["open"], o["customer"] or ""))
	return out


def _mixes(lines, days, farm):
	"""Every mixed box and bunch of the day's (or week's) orders, grouped the way Sales Allocation packs them
	(box_groups: a spec's mono and mixed bunches are one box): [{key, sales_order, order_name, customer,
	delivery_date, mix_name, boxes, team, confirmed, lines: [{soi, variety, length, ordered, allocated,
	planned, left, per_box}]}]. A mix is confirmed together, all of its varieties at once."""
	from upande_packhouse.api.v2.allocation_checks import per_box
	from upande_packhouse.box_groups import box_keys

	planned = _planned_week(days, farm) if len(days) > 1 else _planned_by_line(days[0], farm)
	keys = box_keys(lines)
	names = dict(
		frappe.db.sql(
			"SELECT name, IFNULL(custom_order_name, '') FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": tuple({ln.parent for ln in lines}) or ("",)},
		)
	)
	groups = {}
	for ln in lines:
		key = keys.get(ln.name)
		if not key or key[0] == "line":
			continue
		g = groups.setdefault(
			(ln.parent, key),
			{
				"key": "%s|%s|%s" % (ln.parent, key[0], key[1]),
				"sales_order": ln.parent,
				"order_name": names.get(ln.parent) or "",
				"customer": ln.customer_name or ln.customer,
				"delivery_date": str(ln.delivery_date),
				"mix_name": ln.custom_mix_name or ln.custom_line or "",
				"boxes": cint(ln.custom_number_of_boxes),
				"team": "",
				"lines": [],
			},
		)
		pl = planned.get(ln.name, {"real": 0.0, "expected": 0.0})
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		have = pl["real"] + pl["expected"]
		g["boxes"] = max(g["boxes"], cint(ln.custom_number_of_boxes))
		g["team"] = g["team"] or plans.team_for_line(ln.name) or ""
		g["lines"].append(
			{
				"soi": ln.name,
				"variety": ln.item_code,
				"length": ln.custom_length or "",
				"ordered": ordered,
				"allocated": min(allocated, ordered),
				"planned": have,
				"left": max(0.0, ordered - allocated - have),
				"per_box": per_box(ln),
			}
		)
	out = []
	for g in groups.values():
		g["confirmed"] = all(x["planned"] > 0.5 or x["left"] <= 0.5 for x in g["lines"])
		out.append(g)
	out.sort(key=lambda g: (g["confirmed"], g["delivery_date"], g["customer"] or "", g["mix_name"]))
	return out


def _discard_ages():
	"""age(variety) -> its discard age in days, as Production Settings sets it (the variety's own, else its row
	in Discard Age by Item Group, else its item group's, else the default), once per request."""

	def build():
		try:
			from upande_quality.upande_quality.doctype.production_settings.production_settings import (
				discard_age_resolver,
			)

			return discard_age_resolver()
		except ImportError:
			default = flt(stock._config()["discard_age"])
			return lambda _item=None: default

	return _memo(("discard_ages",), build)


def _expected_confirmed(farm, last_day):
	"""{(variety, length): stems} confirmed on the harvest (not holding buckets) for deliveries cut from today on, up to
	`last_day`, still to be fulfilled: the forecast _forecast_supply reads is already promised to them."""
	out = defaultdict(float)
	for v, ln, q in frappe.db.sql(
		"""SELECT c.variety, IFNULL(c.length, ''), SUM(GREATEST(0, c.stems - IFNULL(c.fulfilled_stems, 0)))
		   FROM `tabAllocation Confirmation` c
		   JOIN `tabAllocation Plan` p ON p.name = c.plan
		   WHERE p.farm = %s AND c.source != 'Real' AND c.status IN %s
		     AND c.delivery_date BETWEEN %s AND %s
		   GROUP BY c.variety, IFNULL(c.length, '')""",
		(farm, tuple(plans.LIVE), add_days(today(), plans.HARVEST_LEAD_DAYS), getdate(last_day)),
	):
		out[(v, ln)] += flt(q)
	return out


def _forecast_supply(days, farm, only=None):
	"""[{variety, length, on_shelf, rolling, production, total, total_production}] for every variety, ordered or
	not: what is free on the sales farm's shelves (and received, not shelved yet) now, and what every harvest day
	from today to the last delivery day less the harvest lead is forecast to bring. Today counts only what is
	still to be cut, plus what was cut and not received yet (the rest is already on the shelf). `only`: the
	(variety, length) pairs wanted, read for those varieties only."""
	varieties = {k[0] for k in only} if only is not None else None
	farms = tuple(_growing_farms(farm) or ())
	first = getdate(today())
	last = max(getdate(d) for d in days) - timedelta(days=plans.HARVEST_LEAD_DAYS)
	harvest_days = [first + timedelta(days=i) for i in range(max(0, (last - first).days + 1))]
	weeks = defaultdict(list)
	for h in harvest_days:
		weeks[h.isocalendar()[:2]].append(h)
	production = defaultdict(float)
	prod_today = defaultdict(float)
	for (year, week), hs in weeks.items():
		week_rows = (
			_shared(
				("fc_rows", year, week, farms, _order_kind()),
				lambda: _forecast_rows_for(None, year, week, farms),
			)
			if varieties is None
			else _forecast_rows_for(varieties, year, week, farms)
		)
		for variety, rows in week_rows.items():
			for length in {r.grade for r in rows if r.grade and r.grade != "all"}:
				total_by_farm = defaultdict(float)
				share_by_farm = defaultdict(lambda: [0.0, 0.0])
				_split_forecast(rows, length, total_by_farm, share_by_farm)
				per_day = sum(
					total / 7.0 * share_by_farm[f][0] / share_by_farm[f][1]
					for f, total in total_by_farm.items()
					if total > 0 and share_by_farm[f][1] > 0 and share_by_farm[f][0] > 0
				)
				production[(variety, length)] += per_day * len(hs)
				if first in hs:
					prod_today[(variety, length)] = per_day
	rolling = defaultdict(float)
	roll_today = {}
	for h in harvest_days:
		got = _rolling_by_pool(h + timedelta(days=plans.HARVEST_LEAD_DAYS), farm, varieties)
		for key, stems in got.items():
			rolling[key] += stems
		if h == first:
			roll_today = got
	# the shelf as it will be on the processing day (the last delivery day less the harvest lead): every bucket
	# that will still be under the discard age then, whatever day it was harvested -- for a delivery on Saturday
	# with a 6-day discard age, the stems of Monday to Friday -- free of what is allocated and what the plan holds.
	# One still cooling today will have cooled by then.
	ahead = max(0, (last - first).days)
	age_of = _discard_ages()
	shelf_farms = _stock_farms(farm)
	shelf = defaultdict(float)
	every = _memo(
		("bucket_rows", tuple(shelf_farms) if shelf_farms is not None else None),
		lambda: _shared(
			("bucket_rows", tuple(shelf_farms) if shelf_farms is not None else None),
			lambda: stock.bucket_rows(farms=shelf_farms),
		),
	)
	for r in every:
		if (varieties is not None and r.variety not in varieties) or r.in_discard_request:
			continue
		if (r.age_days or 0) + ahead >= age_of(r.variety):
			continue
		shelf[(r.variety, r.stem_length or "")] += max(0.0, flt(r.free))
	recv = received.received_unshelved(farms=shelf_farms, varieties=sorted(varieties) if varieties else None)
	for r in recv:
		if (r.age_days or 0) + ahead >= age_of(r.variety):
			continue
		shelf[(r.variety, r.stem_length or "")] += max(0.0, flt(r.stems))
	# what the plan confirmed from stock and has not allocated yet
	for (_f, v, ln), stems in plans.real_outstanding(shelf_farms).items():
		if (v, ln) in shelf:
			shelf[(v, ln)] = max(0.0, shelf[(v, ln)] - stems)
	# allocated, waiting release: still on the shelf, but those orders have it
	waiting = defaultdict(float)
	if frappe.db.table_exists("Pending Allocation Row"):
		for v, ln, q in frappe.db.sql(
			"""SELECT r.item_code, IFNULL(r.stem_length, ''), SUM(r.qty)
			   FROM `tabPending Allocation Row` r
			   JOIN `tabPending Allocation` p ON p.name = r.parent AND r.parenttype = 'Pending Allocation'
			   WHERE p.status = 'Pending' GROUP BY r.item_code, IFNULL(r.stem_length, '')"""
		):
			if varieties is None or v in varieties:
				waiting[(v, ln)] += flt(q)
	if first in harvest_days:
		cut = received.harvested_day(
			first, _growing_farms(farm), {k[0] for k in set(production) | set(rolling) | set(shelf)}
		)
		# today: what is still to be cut, and what was cut and is on its way; the rest is on the shelf already
		for key, h in cut.items():
			for fc, today_fc in ((production, prod_today), (rolling, roll_today)):
				fc[key] += h["not_received"] - min(h["harvested"], today_fc.get(key, 0.0))
	out = []
	for key in set(production) | set(rolling) | set(shelf):
		if only is not None and key not in only:
			continue
		# free (net of what is allocated and what the plan holds) and received, less what waits release
		on_shelf = max(0.0, shelf.get(key, 0.0) - waiting.get(key, 0.0))
		roll, prod = max(0.0, rolling.get(key, 0.0)), max(0.0, production.get(key, 0.0))
		if on_shelf < 0.5 and roll < 0.5 and prod < 0.5:
			continue
		out.append(
			{
				"variety": key[0],
				"length": key[1],
				"on_shelf": round(on_shelf),
				"rolling": round(roll),
				"production": round(prod),
				"total": round(on_shelf + roll),
				"total_production": round(on_shelf + prod),
			}
		)
	return out, [str(h) for h in harvest_days]


def _attach_available(farm, variety, length, day, rows, longer):
	"""Each line's `available`: the shelf now and the forecast up to its delivery day less the harvest lead."""
	every = list(rows) + list(longer)
	got = available_by_day(farm, {(str(r.get("delivery_date") or day), variety, length or "") for r in every})
	for r in every:
		r["available"] = got.get((str(r.get("delivery_date") or day), variety, length or ""))


def available_by_day(farm, wanted):
	"""{(delivery_date, variety, length): {on_shelf, rolling, production, total, total_production, to}} for
	`wanted` [(delivery_date, variety, length)]: the shelf now plus every harvest day up to that delivery day
	less the harvest lead (see _forecast_supply)."""
	by_day = defaultdict(set)
	for d, v, ln in wanted:
		by_day[str(d)].add((v, ln or ""))
	out = {}
	for d, keys in by_day.items():
		rows, harvest_days = _forecast_supply([d], farm, only=keys)
		got = {(r["variety"], r["length"]): r for r in rows}
		for k in keys:
			r = got.get(k) or {
				"on_shelf": 0,
				"rolling": 0,
				"production": 0,
				"total": 0,
				"total_production": 0,
			}
			out[(d, k[0], k[1])] = {**r, "to": harvest_days[-1] if harvest_days else ""}
	return out


def _group_of(ln):
	"""What kind of box a line is in, and which lines belong together: a straight box line stands alone; a mixed box is
	every line with the same mix group; a mixed bunch is every line with the same bunch group."""
	if ln.custom_mixed_box:
		g = ln.custom_mix_group or ln.custom_box_id or ""
		return {
			"kind": "mixed_box",
			"group_key": "box|%s" % g,
			"group_label": _("Mixed box {0}").format(g) if g else _("Mixed box"),
		}
	if ln.custom_mixed_bunch:
		g = ln.custom_bunch_group or ""
		num = g.rsplit("::", 1)[-1] if "::" in g else g
		return {
			"kind": "mixed_bunch",
			"group_key": "bunch|%s" % g,
			"group_label": _("Mixed bunch {0}").format(num) if num else _("Mixed bunch"),
		}
	return {"kind": "straight", "group_key": "line|%s" % ln.name, "group_label": _("Straight box")}


def _held_sources(confs):
	"""{confirmation: {"shelf": stems, "received": stems}} for what a Real confirmation still has to allocate: from the
	shelf of its farm, variety and length first, the rest from what is received and waiting for a shelf."""
	out = {}
	pools = {}
	for c in confs:
		key = (c.farm, c.variety, c.length or "")
		if key not in pools:
			rows = stock.bucket_rows(
				farms=[c.farm], varieties=[c.variety], lengths=[c.length] if c.length else None
			)
			pools[key] = [sum(flt(r.allocatable) for r in rows if (r.stem_length or "") == key[2])]
		left = max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
		shelf = min(left, pools[key][0])
		pools[key][0] -= shelf
		out[c.name] = {"shelf": shelf, "received": left - shelf}
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
	from upande_packhouse.api.v2.allocation_teams import teams_at

	rows = {t: _team_row(t) for t in teams_at(farm)}
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
				"top": [
					{"variety": v, "stems": s}
					for v, s in sorted(r["varieties"].items(), key=lambda kv: -kv[1])[:3]
				],
				"share": (r["stems"] / total) if total else 0.0,
				"vs_even": ((r["stems"] - even) / even) if even else 0.0,
			}
		)
	stems_list = [r["stems"] for r in working]
	spread = (max(stems_list) - min(stems_list)) / even if even and stems_list else 0.0
	planned_lines = {soi for (_t, soi) in per_line}
	unplanned = sum(
		max(0.0, flt(l.ordered_stems) - flt(l.allocated_stems))
		for l in lines.values()
		if l.name not in planned_lines
	)
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
	return {
		"name": name,
		"stems": 0.0,
		"boxes": 0.0,
		"lines": 0,
		"orders": set(),
		"customers": set(),
		"varieties": {},
		"boxes_of": set(),
	}


@frappe.whitelist()
def get_confirmed_orders(delivery_date: str | None = None, farm: str | None = None, week: str | None = None):
	"""The Confirmed page: every order with something confirmed, with ALL its lines (confirmed or not), so the whole order stays in
	view. For each line: what was ordered and confirmed, where the stems come from, and, for the variety and length it is
	confirmed against, how much was available and how much is already taken. With `week`, every delivery day of it."""
	if week:
		week, days = _week(week)
		out = {
			"success": True,
			"delivery_date": days[0],
			"week": week,
			"days": days,
			"orders": [],
			"totals": {},
		}
		for d in days:
			r = get_confirmed_orders(d, farm)
			out["farm"] = r["farm"]
			for o in r["orders"]:
				o.setdefault("delivery_date", d)
			out["orders"] += r["orders"]
			for k, v in r["totals"].items():
				out["totals"][k] = out["totals"].get(k, 0) + v
		return out
	day = _day(delivery_date)
	farm = farm or _default_farm()
	name = _plan_name(day, farm)
	confs = plans.live_confirmations(plan=name) if name else []
	if not confs:
		return {
			"success": True,
			"delivery_date": day,
			"farm": farm,
			"orders": [],
			"totals": {"orders": 0, "lines": 0, "lines_confirmed": 0},
		}
	by_soi = {}
	for c in confs:
		by_soi.setdefault(c.sales_order_item, []).append(c)
	orders_in = {c.sales_order for c in confs}
	lines = [ln for ln in _lines(day) if ln.parent in orders_in]
	supply = _stock_by_pool(farm, {ln.item_code for ln in lines} | {c.variety for c in confs}, day)
	ch = _confirmed_here(day, farm)
	held = _held_sources([c for c in confs if c.source == "Real"])
	names = dict(
		frappe.db.sql(
			"SELECT name, custom_order_name FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": tuple(orders_in)},
		)
	)
	seq = (
		dict(
			frappe.db.sql(
				"SELECT name, custom_allocation_sequence FROM `tabSales Order` WHERE name IN %(s)s",
				{"s": tuple(orders_in)},
			)
		)
		if frappe.db.has_column("Sales Order", "custom_allocation_sequence")
		else {}
	)
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
					sources.append(
						{
							"kind": "forecast",
							"stems": left - hv,
							"farm": c.farm,
							"basis": "Rolling" if "rolling" in (c.forecast_basis or "") else "Production",
						}
					)
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
				"customer_id": ln.customer,
				"order_name": names.get(ln.parent) or "",
				"sequence": cint(seq.get(ln.parent)),
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
	orders = sorted(
		out.values(),
		key=lambda o: (not o["sequence"], o["sequence"], (o["customer"] or "").lower(), o["sales_order"]),
	)
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
@_with_greenhouses
def get_pool(
	delivery_date: str | None = None,
	farm: str | None = None,
	variety: str | None = None,
	length: str | None = None,
	week: str | None = None,
):
	"""One variety and length: its customer lines with what is planned for each. With `week`, the lines of every
	delivery day of that week, each counting on the week's running balance (see _build_pools_week)."""
	farm = farm or _default_farm()
	length = length or ""
	if week:
		return _week_pool(week, farm, variety, length)
	day = _day(delivery_date)
	pool = next(
		(
			p
			for p in _build_pools(day, farm, only_variety=variety)
			if p["variety"] == variety and p["length"] == length
		),
		None,
	)
	if pool is None:
		# the list moved on (another day, a confirmation): nothing to open, not an error
		return {
			"success": False,
			"empty": True,
			"message": _("There are no orders for {0} {1} on {2}.").format(variety, length, day),
		}
	planned = _planned_by_line(day, farm)
	rows, longer = _pool_rows(_lines_for(day, farm), planned, variety, length)
	confirmed = _confirmed_lines(day, farm, variety, length)
	detail = expected_detail(variety, length, day, farm)
	sup = {**SUPPLY, **_stock_by_pool(farm, {variety}, day).get((variety, length), {})}
	remaining = max(0.0, detail["likely"] - sup["harvested"])
	scale = (remaining / detail["likely"]) if detail["likely"] > 0 else 0.0
	rolling = rolling_forecast(variety, length, day, farm)
	_line_view(day, farm, pool, rows, longer, sup, rolling, remaining)
	need = max(0.0, pool["ordered"] - pool["allocated"])
	held = pool["held"]
	safe = [
		{"basis": "rolling", "stems": min(need, max(held, cut_day_total(sup, rolling)))},
		{"basis": "production", "stems": min(need, max(held, sup["harvested"] + remaining))},
	]
	for x in safe:
		x["need"], x["short"] = need, max(0.0, need - x["stems"])
	_attach_available(farm, variety, length, day, rows, longer)
	return {
		"cut_day": str(_cut_of(day)),
		"safe": safe,
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
			"rooms": [
				{"room": r, "stems": v} for r, v in sorted(sup["rooms"].items(), key=lambda kv: -kv[1])
			],
			"on_the_way": sup["on_the_way"],
			"to_cut": remaining,
			"rolling": max(0.0, rolling - sup["harvested"]),
			"to_cut_sure": remaining * (detail["sure"] / detail["likely"]) if detail["likely"] > 0 else 0.0,
			"to_cut_by_farm": [
				{"farm": f, "stems": v * scale}
				for f, v in sorted(detail["by_farm"].items(), key=lambda kv: -kv[1])
				if v * scale > 0
			],
			"error_pct": None if detail["error"] is None else round(100.0 * detail["error"]),
			"error_weeks": detail["weeks"],
		},
	}


def _week_pool(week, farm, variety, length):
	"""get_pool for a whole week."""
	from upande_packhouse.api.v2.allocation_checks import mix_key

	week, days = _week(week)
	lines = _week_lines(days, farm)
	# the variety and every variety sharing a mix with it, in one go
	mixes = {mix_key(ln) for ln in lines if ln.item_code == variety} - {None, ""}
	varieties = frozenset({variety} | {ln.item_code for ln in lines if mixes and mix_key(ln) in mixes})
	pools = {(p["variety"], p["length"]): p for p in _build_pools_week(days, farm, only_variety=varieties)}
	pool = pools.get((variety, length))
	if pool is None:
		return {
			"success": False,
			"empty": True,
			"message": _("There are no orders for {0} {1} in week {2}.").format(variety, length, week),
		}
	planned = _planned_week(days, farm)
	rows, longer = _pool_rows(lines, planned, variety, length)
	confirmed = []
	for d in days:
		confirmed += _confirmed_lines(d, farm, variety, length)
	sup = {**SUPPLY, **_stock_by_pool(farm, {variety}).get((variety, length), {})}
	likely = sure = to_cut = roll = 0.0
	by_farm = defaultdict(float)
	detail = None
	for d in days:
		detail = expected_detail(variety, length, d, farm)
		h = received.harvested_day(_cut_of(d), _growing_farms(farm), {variety}).get((variety, length)) or {}
		harvested = flt(h.get("harvested"))
		left = max(0.0, detail["likely"] - harvested)
		scale = (left / detail["likely"]) if detail["likely"] > 0 else 0.0
		likely += detail["likely"]
		sure += detail["sure"]
		to_cut += left
		roll += max(0.0, rolling_forecast(variety, length, d, farm) - harvested)
		for f, v in detail["by_farm"].items():
			by_farm[f] += v * scale
	avail = {r["date"]: (r["avail"], r["avail_production"]) for r in pool["days"]}
	_line_view(
		days[0],
		farm,
		pool,
		rows,
		longer,
		sup,
		0.0,
		0.0,
		week={"lines": {ln.name: ln for ln in lines}, "planned": planned, "avail": avail, "pools": pools},
	)
	rows.sort(key=lambda r: (r["delivery_date"], -r["left"], r["customer"] or ""))
	longer.sort(key=lambda r: (r["delivery_date"], -r["left"], r["customer"] or ""))
	need = max(0.0, pool["ordered"] - pool["allocated"])
	safe = [
		{"basis": "rolling", "stems": max(0.0, need - pool["short"])},
		{"basis": "production", "stems": max(0.0, need - pool["short_production"])},
	]
	for x in safe:
		x["need"], x["short"] = need, max(0.0, need - x["stems"])
	_attach_available(farm, variety, length, days[0], rows, longer)
	return {
		"cut_day": str(_cut_of(days[0])),
		"cut_to": str(_cut_of(days[-1])),
		"safe": safe,
		"success": True,
		"delivery_date": days[0],
		"week": week,
		"days": days,
		"farm": farm,
		"pool": pool,
		"lines": rows,
		"longer_lines": longer,
		"confirmed_lines": confirmed,
		"stock_farms": [
			{"farm": f, "on_shelf": sup["by_farm"].get(f, 0.0), "received": sup["rec_by_farm"].get(f, 0.0)}
			for f in _stock_farms(farm)
		],
		"best_farm": _best_farm(farm, sup),
		"supply": {
			"on_shelf": sup["free"],
			"received": sup["received"],
			"rooms": [
				{"room": r, "stems": v} for r, v in sorted(sup["rooms"].items(), key=lambda kv: -kv[1])
			],
			"on_the_way": pool["on_the_way"],
			"to_cut": to_cut,
			"rolling": roll,
			"to_cut_sure": to_cut * (sure / likely) if likely > 0 else 0.0,
			"to_cut_by_farm": [
				{"farm": f, "stems": v} for f, v in sorted(by_farm.items(), key=lambda kv: -kv[1]) if v > 0
			],
			"error_pct": None if not detail or detail["error"] is None else round(100.0 * detail["error"]),
			"error_weeks": detail["weeks"] if detail else 0,
		},
	}


def _pool_rows(lines, planned, variety, length):
	"""The order lines of a variety and length (`rows`) and those that ordered it shorter (`longer`)."""
	rows, longer = [], []
	pool_cm = availability.length_cm(length)
	for ln in lines:
		if ln.item_code != variety:
			continue
		pl = planned.get(ln.name, {"real": 0.0, "expected": 0.0, "length": ""})
		ordered, allocated = flt(ln.ordered_stems), flt(ln.allocated_stems)
		row = {
			"soi": ln.name,
			"sales_order": ln.parent,
			"customer": ln.customer_name or ln.customer,
			"customer_id": ln.customer,
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
			row["left"] = (
				max(0.0, ordered - allocated - pl["real"] - pl["expected"]) if not at_here else row["left"]
			)
			if row["left"] > 0 or at_here:
				longer.append(row)
	rows.sort(key=lambda r: (-r["left"], r["customer"] or ""))
	longer.sort(key=lambda r: (-r["left"], r["customer"] or ""))
	return rows, longer


def _forecast_view(detail, harvested):
	"""Both forecasts for the cutting day (stems still to be cut: the forecast less what was harvested that day), the one being
	confirmed against, and how far apart they are."""

	def one(d):
		if d is None:
			return None
		left = max(0.0, d["likely"] - harvested)
		scale = (left / d["likely"]) if d["likely"] > 0 else 0.0
		return {
			"by_farm": [
				{"farm": f, "stems": v * scale}
				for f, v in sorted(d["by_farm"].items(), key=lambda kv: -kv[1])
				if v * scale > 0
			],
			"day": d["likely"],
			"to_cut": left,
			"to_cut_sure": left * (d["sure"] / d["likely"]) if d["likely"] > 0 else 0.0,
			"error_pct": None if d["error"] is None else round(100.0 * d["error"]),
		}

	rev, roll = one(detail["revised"]), one(detail["rolling"])
	return {
		"basis": detail["basis"],
		"revised": rev,
		"rolling": roll,
		"variance": None if roll is None else roll["to_cut"] - rev["to_cut"],
		"variance_pct": None
		if roll is None or rev["to_cut"] <= 0
		else (roll["to_cut"] - rev["to_cut"]) / rev["to_cut"],
	}


@frappe.whitelist()
def get_forecast_by_day(
	farm: str | None = None,
	variety: str | None = None,
	length: str | None = None,
	week: str | None = None,
	delivery_date: str | None = None,
):
	"""One variety and length, each delivery day of the week (or the one day): its harvest day (delivery less the
	harvest lead), the rolling and the production forecast for it, what that harvest day has cut so far, and the
	shelf now (free, net of what is allocated, held by the plan or waiting release)."""
	farm = farm or _default_farm()
	length = length or ""
	days = _week(week)[1] if week else [_day(delivery_date)]
	cut = received.harvested_day
	growing = _growing_farms(farm)
	out = []
	for d in days:
		h = getdate(d) - timedelta(days=plans.HARVEST_LEAD_DAYS)
		got = (cut(h, growing, {variety}) or {}).get((variety, length)) or {}
		out.append(
			{
				"delivery_date": str(d),
				"harvest_day": str(h),
				"rolling": round(rolling_forecast(variety, length, d, farm)),
				"production": round(expected_detail(variety, length, d, farm)["likely"]),
				"harvested": round(flt(got.get("harvested"))),
			}
		)
	shelf, _days = _forecast_supply([days[0]], farm, only={(variety, length)})
	return {
		"success": True,
		"variety": variety,
		"length": length,
		"on_shelf": shelf[0]["on_shelf"] if shelf else 0,
		"days": out,
	}


def rolling_forecast(variety, length, day, farm):
	"""Stems the cutting day for `day` is likely to bring, from what was really harvested: the average of the
	last 4 same weekdays' Harvesting entries for this variety and length on the sales farm's farms."""
	return _rolling_by_pool(day, farm, {variety}).get((variety, length or ""), 0.0)


def _rolling_by_pool(day, farm, varieties):
	"""{(variety, length): rolling forecast} for the varieties, in one query (see rolling_forecast). `None`: every
	variety harvested on those days."""
	if varieties is not None and not varieties:
		return {}
	cut = getdate(day) - timedelta(days=plans.HARVEST_LEAD_DAYS)
	days = [cut - timedelta(weeks=w) for w in range(1, 5)]
	farms = tuple(_stock_farms(farm)) or ("",)

	def load():
		# every variety of those past cutting days at once, kept a while: they are past days, so switching
		# from one variety to another reads them from the cache instead of the stock ledger again
		key = "sa2_rolling:{0}:{1}".format(cut, ",".join(farms))
		kept = frappe.cache.get_value(key)
		if kept is None:
			rows = frappe.db.sql(
				"""SELECT sed.item_code, COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length, '') AS len,
				          SUM(sed.transfer_qty)
				   FROM `tabStock Entry` se
				   INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
				   WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus = 1
				     AND se.posting_date IN %(d)s
				     AND IFNULL(se.custom_farm, se.farm) IN %(f)s
				   GROUP BY sed.item_code, len""",
				{"d": tuple(days), "f": farms},
			)
			kept = [[v, ln, flt(t)] for v, ln, t in rows]
			frappe.cache.set_value(key, kept, expires_in_sec=ROLLING_KEPT)
		return {(v, ln): t / len(days) for v, ln, t in kept if varieties is None or v in varieties}

	return _memo(
		("rolling", str(cut), farms, tuple(sorted(varieties)) if varieties is not None else None), load
	)


#: Seconds the past cutting days' harvests are kept for the rolling forecast.
ROLLING_KEPT = 900


def _line_view(day, farm, pool, rows, longer, sup, rolling, to_cut, week=None):
	"""Per line: the safe amounts on the rolling and on the production forecast, whole boxes only
	unless the line is covered in full, and the checks Sales Allocation would otherwise trip on. With `week`
	({lines, planned, avail, pools}), each line counts on the week's balance on its own delivery day."""
	from upande_packhouse.api.v2 import allocation_guards as guards
	from upande_packhouse.api.v2.allocation_checks import mix_key, per_box
	from upande_packhouse.api.v2.allocation_teams import crop_of_groups

	lines = week["lines"] if week else {ln.name: ln for ln in _lines(day)}
	crop_of = crop_of_groups()
	planned = week["planned"] if week else _planned_by_line(day, farm)
	discard = cint(stock._config()["discard_age"])
	counted = pool["held"] + pool["expected_plan"]
	roll_left = max(0.0, cut_day_total(sup, rolling) - counted)
	prod_left = max(0.0, sup["harvested"] + to_cut - counted)

	def member_cover(m, key):
		"""What the mix member's variety and length has for it: on its delivery day's balance for a week."""
		if not week:
			ms = {**SUPPLY, **_stock_by_pool(farm, {m.item_code}, day).get(key, {})}
			return cut_day_total(ms, rolling_forecast(m.item_code, key[1], day, farm))
		mp = week["pools"].get(key) or {}
		row = next((r for r in mp.get("days", []) if r["date"] == str(m.delivery_date)), None)
		return row["avail"] if row else 0.0

	specs = guards._spec_varieties(list(lines.values()))
	spec_rank = _spec_rank({ln.custom_line for ln in lines.values() if ln.custom_line})
	member_supply = {}

	def boxes_of(ln, stems):
		size = per_box(ln)
		return (int(stems / size + 1e-9), cint(ln.custom_number_of_boxes) or 1, size) if size else (0, 0, 0)

	def whole(ln, stems, left):
		if stems >= left - 0.5:
			return left
		b, _n, size = boxes_of(ln, stems)
		return b * size if size and not mix_key(ln) else stems

	for row, is_longer in [(r, False) for r in rows] + [(r, True) for r in longer]:
		ln = lines.get(row["soi"])
		if not ln:
			continue
		left = row["left"]
		line_day = str(ln.delivery_date)
		days_out = (getdate(line_day) - getdate(today())).days
		if week:
			roll_left, prod_left = week["avail"].get(line_day, (0.0, 0.0))
		roll = whole(ln, min(left, roll_left), left)
		prod = whole(ln, min(left, prod_left), left)
		row["per_box"] = 0 if mix_key(ln) else (per_box(ln) or 0)
		row["scenarios"] = [
			{
				"basis": "rolling",
				"stems": roll,
				"boxes": boxes_of(ln, roll + row["held"] + row["expected"] + row["allocated"])[:2],
			},
			{
				"basis": "production",
				"stems": prod,
				"boxes": boxes_of(ln, prod + row["held"] + row["expected"] + row["allocated"])[:2],
			},
		]
		checks = []
		if left > 0 and roll < left - 0.5:
			checks.append(
				{"code": "shortage", "severity": "red", "label": _("Shortage · {0}").format(int(left - roll))}
			)
		covered = row["allocated"] + row["held"] + row["expected"]
		b, nb, size = boxes_of(ln, covered)
		if size and not mix_key(ln) and 0 < covered < row["ordered"] - 0.5 and covered - b * size > 0.5:
			checks.append(
				{"code": "part_box", "severity": "amber", "label": _("Part box · {0} of {1}").format(b, nb)}
			)
		if is_longer:
			old = sup["old"] >= max(left, 1)
			checks.append(
				{
					"code": "longer",
					"severity": "amber",
					"label": (
						_("Longer stem · {0} for {1}")
						if old
						else _("Longer stem · {0} for {1} · needs a reason")
					).format(pool["length"], row["ordered_length"]),
				}
			)
		mk = mix_key(ln)
		members = []
		if mk:
			for m in lines.values():
				if m.name == ln.name or mix_key(m) != mk:
					continue
				key = (m.item_code, m.custom_length or "")
				at = (key, str(m.delivery_date))
				if at not in member_supply:
					member_supply[at] = member_cover(m, key)
				pl = planned.get(m.name, {"real": 0.0, "expected": 0.0})
				m_cov = min(flt(m.ordered_stems), flt(m.allocated_stems) + pl["real"] + pl["expected"])
				m_left = max(0.0, flt(m.ordered_stems) - m_cov)
				short = max(0.0, m_left - member_supply[at])
				members.append(
					{
						"soi": m.name,
						"variety": m.item_code,
						"length": key[1],
						"ordered": flt(m.ordered_stems),
						"allocated": min(flt(m.allocated_stems), flt(m.ordered_stems)),
						"covered": m_cov,
						"left": m_left,
						"short": short,
						"per": cint(m.conversion_factor) or 1,
					}
				)
			worst = max(members, key=lambda x: x["short"], default=None)
			if worst and worst["short"] > 0.5:
				checks.append(
					{
						"code": "mix",
						"severity": "red",
						"label": _("Incomplete mix · {0} short {1}").format(
							worst["variety"], int(worst["short"])
						),
					}
				)
		row["mix_members"] = members
		subs = []
		if left > 0 and roll < left - 0.5:
			others = sorted((specs.get(ln.custom_line) or set()) - {ln.item_code})
			in_mix = {m["variety"] for m in members}
			if others:
				have = _stock_by_pool(farm, set(others), day)
				for v in others:
					spare = sum(
						x["free"] + x["received"]
						for (sv, sl), x in have.items()
						if sv == v and availability.length_cm(sl) >= availability.length_cm(ln.custom_length)
					)
					if spare > 0:
						subs.append({"variety": v, "spare": spare, "in_mix": v in in_mix})
				# the specification's order: its primary variety, then the others as listed; a variety already in
				# the line's mix goes last, it needs the planner's say-so
				rank = spec_rank.get(ln.custom_line, {})
				subs.sort(key=lambda x: (x["in_mix"], rank.get(x["variety"], (1, 999))))
		if subs:
			checks.append(
				{
					"code": "substitute",
					"severity": "amber",
					"label": _("Substitute {0} with {1}").format(ln.item_code, subs[0]["variety"])
					if len(subs) == 1
					else _("Substitutes · {0} options").format(len(subs)),
				}
			)
		row["substitutes"] = subs
		if left > 0 and sup["free"] > 0 and (sup["oldest"] or 0) + days_out >= discard:
			checks.append(
				{
					"code": "delivery",
					"severity": "blue",
					"label": _("Delivery date · {0} days old by {1}").format(
						cint(sup["oldest"]) + days_out, frappe.utils.formatdate(line_day, "d MMM")
					),
				}
			)
		row["checks"] = checks
		row["crop"] = crop_of.get(ln.item_group) or ""
		row["per_box"] = per_box(ln)
		row["boxes"] = cint(ln.custom_number_of_boxes) or 1
		row["delivery_date"] = str(ln.delivery_date)
		row["confirmed"] = covered >= row["ordered"] - 0.5 and (row["held"] + row["expected"]) > 0


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
				"delivery_date": day,
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


def _whole_boxes(soi, stems, room):
	"""`stems` rounded up to complete boxes of the line (stems per box = its packrate, a mix's share of the mixed
	box), at most `room`; 0 stays 0."""
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import _line_capacity

	if stems <= 0:
		return 0.0
	it = frappe.db.get_value(
		"Sales Order Item",
		soi,
		[
			"name",
			"custom_number_of_boxes",
			"custom_packrate",
			"custom_packrate_mixed_box",
			"custom_mixed_box",
			"custom_mixed_bunch",
			"stock_qty",
			"custom_ordered_quantity",
			"qty",
			"conversion_factor",
		],
		as_dict=True,
	)
	boxes = cint(it.custom_number_of_boxes) or 1
	per_box = _line_capacity(it) / boxes
	if per_box <= 0:
		return stems
	if stems > room + 0.0001:
		return stems  # more than is left: refused as it is
	return min(math.ceil(stems / per_box - 1e-9) * per_box, room)


def lines_by_day(lines):
	"""{delivery day: [items]} for confirmation items ({"sales_order_item": ...}), each under its line's own day."""
	if isinstance(lines, str):
		lines = json.loads(lines or "[]")
	out = defaultdict(list)
	for item in lines or []:
		out[str(_line(item["sales_order_item"]).delivery_date)].append(item)
	return dict(sorted(out.items()))


@frappe.whitelist()
def confirm_lines(
	delivery_date: str | None = None,
	farm: str | None = None,
	lines: str | list | None = None,
	team: str | None = None,
	week: str | None = None,
):
	"""Confirm stems for order lines: `lines` = [{"sales_order_item": ..., "stems": ..., "length": ...}], the TOTAL
	confirmed for each line (0 takes the line out of the plan; `length` is optional and only ever a longer one than
	ordered). Everything is confirmed on the cutting day's forecast, never on stock already on the shelf: `basis` is
	"rolling" (default) or "production". The harvest is held for the line once it reaches the shelf
	(allocation_plan.convert_virtual).

	Every confirmation has a team, and the team belongs to the order line: each item can carry its own `team`
	(`team` is only the fallback for items that do not), or the line (or its mixed box) must already have one. A mixed box is
	packed by one team, so the team given for one of its lines is saved on every line of the box; allocation can then
	autofill it.

	The page shows the heads-ups (allocation_guards.preview_confirmation) before it calls this; going ahead is the planner's call."""
	frappe.only_for(PLANNERS)
	_changed()
	from upande_packhouse.api.v2 import allocation_guards as guards
	from upande_packhouse.api.v2.sea_freight_plan import require_greenhouses

	# a sea freight line is confirmed with the greenhouse that cuts it (the confirmation hook plans it there)
	items = frappe.parse_json(lines) if isinstance(lines, str) else lines
	chosen = require_greenhouses(items)
	if chosen:
		frappe.flags.sea_freight_greenhouses = {
			**(frappe.flags.get("sea_freight_greenhouses") or {}),
			**chosen,
		}

	if week:
		# a week's lines go to the plan of their own delivery day
		out = {"success": True, "plans": [], "lines": [], "quiet": []}
		for d, items in lines_by_day(lines).items():
			r = confirm_lines(d, farm, items, team)
			out["plans"].append(r["plan"])
			out["lines"] += r["lines"]
			out["quiet"] += r["quiet"]
		out["plan"] = out["plans"][0] if out["plans"] else None
		return out
	day = _day(delivery_date)
	if not farm:
		frappe.throw(_("Choose a farm."))
	if isinstance(lines, str):
		lines = json.loads(lines)
	# a delivery day that has passed is not planned any more (a page left open overnight, a direct call);
	# taking stems off its lines (0) still goes through
	if getdate(day) < getdate(today()) and any(flt(i.get("stems")) > 0 for i in lines or []):
		frappe.throw(_("{0} has passed: its lines can no longer be confirmed.").format(day))
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
	cut_day = getdate(day) - timedelta(days=plans.HARVEST_LEAD_DAYS)
	from upande_packhouse.api.v2.allocation_teams import team_sales_farms

	home = team_sales_farms()
	plan_of = {farm: plan}
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
		# only complete boxes are confirmed: what is asked goes up to the line's next full box (its packrate),
		# never past what the line has left -- except stems given from one line to another (swap or split): they
		# are moved as they are, so the two lines together keep exactly what was confirmed
		if not frappe.flags.get("pg_exact_confirm"):
			want = _whole_boxes(soi, want, room)
		if want > room + 0.0001:
			frappe.throw(
				_("Only {0} stems of this line are left to confirm, and you asked for {1}.").format(
					int(room), int(want)
				)
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
		if want > 0 and not line_team:
			frappe.throw(_("Choose a team for this confirmation."))
		# the line is processed at its team's sales farm: confirmed there, on that sales farm's plan -- one order
		# can have its lines (or its mixes) packed by teams of different sales farms
		sales_farm = home.get(line_team) or farm
		own = _stock_farms(sales_farm)
		stock_farm = next(
			(f for f in (item.get("stock_farm"), plans.farm_for_line(soi)) if f and f in own), sales_farm
		)
		if sales_farm not in plan_of:
			plan_of[sales_farm] = plans.get_plan(day, sales_farm)
		line_plan = plan_of[sales_farm]
		held = 0.0
		# a revision keeps what was confirmed before it (released as "Revised"), so the summary can show both
		before = plans.live_confirmations(soi=soi)
		for c in before:
			plans.release_confirmation(c.name, "Revised" if item.get("revised") else "Manual")
		rest = max(0.0, want - held)
		hv = (
			min(rest, _harvest_room(day, stock_farm, line_plan.name, ln.item_code, length, soi))
			if rest > 0
			else 0.0
		)
		plans.plan_virtual(
			plan=line_plan,
			line=info,
			harvest_stems=hv,
			farm=stock_farm,
			variety=ln.item_code,
			length=length,
			stems=rest,
			forecast_basis="%s forecast, harvest day %s"
			% ("production" if item.get("basis") == "production" else "rolling", cut_day),
			team=line_team,
		)
		if want > 0:
			plans.apply_team_to_group(info["mix_key"], line_team)
		_log_confirmed(ln, item, before, want, line_team, stock_farm, length)
		done.append({"soi": soi, "held": held, "expected": rest, "team": line_team, "farm": stock_farm})
	for so in {_line(x["soi"]).parent for x in done}:
		set_processing_farm(so)
	for p_ in plan_of.values():
		plans.sequence_orders(p_.name)
	return {"success": True, "plan": plan.name, "lines": done, "quiet": result["quiet"]}


def _log_confirmed(ln, item, before, want, team, farm, length):
	"""The Allocation Log line of one confirmation: planned, re-confirmed, revised or taken out of the plan."""
	from upande_packhouse.allocation_log import log

	was = sum(flt(c.stems) for c in before)
	team_was = next((c.team for c in before if c.team), "")
	if item.get("revised"):
		stage = "Revised"
	else:
		stage = "Planned"
	if want <= 0:
		what = "taken out of the plan (was {0})".format(int(was))
	elif before:
		what = "{0} -> {1} stems".format(int(was), int(want))
	else:
		what = "{0} stems planned".format(int(want))
	if team_was and team and team_was != team:
		what += ", team {0} -> {1}".format(team_was, team)
	elif team:
		what += " for {0}".format(team)
	log(
		stage,
		"{0} {1}: {2} (expected harvest at {3})".format(ln.item_code, length or "", what, farm),
		sales_order=ln.parent,
		sales_order_item=ln.name,
		item_code=ln.item_code,
		stem_length=length,
		team=team,
		farm=farm,
		stems_before=was,
		stems_after=want,
		delivery_date=ln.delivery_date,
	)


def _confirmed_at(soi, variety, length):
	"""A line's live confirmations: (stems still to be allocated, team, True when they are all at this variety and length)."""
	confs = plans.live_confirmations(soi=soi)
	total = sum(max(0.0, flt(c.stems) - flt(c.fulfilled_stems)) for c in confs)
	here = all(c.variety == variety and (c.length or "") == (length or "") for c in confs)
	return total, plans.team_for_line(soi), here


@frappe.whitelist()
def move_stems(
	delivery_date: str, farm: str, from_soi: str, to_soi: str, stems: float, team: str | None = None
):
	"""Give stems that are confirmed for one customer to another customer who still needs them.

	Giving all of them is a swap (the first customer goes back to "still to confirm"); giving part is a split. The stems
	keep their variety and length, so a customer who ordered a shorter length receives the longer stem (the heads-up for that
	is on the page). Stock held on the shelf moves with them, then what was confirmed from the expected harvest.
	Both lines are confirmed again with their new totals, so the holds, the Confirmed Stems rows, the teams and the
	buckets stay consistent. A mixed box can receive stems (it is confirmed like any other line, and its team applies to the
	whole box), but it cannot give them away: its lines are packed together."""
	frappe.only_for(PLANNERS)
	_changed()
	day, stems = _day(delivery_date), flt(stems)
	if from_soi == to_soi:
		frappe.throw(_("Choose another customer to give the stems to."))
	if stems <= 0:
		frappe.throw(_("Choose how many stems to give."))
	src, dst = _line(from_soi), _line(to_soi)
	for ln in (src, dst):
		if str(ln.delivery_date) != day:
			frappe.throw(
				_("{0} is for {1}, not {2}.").format(ln.customer_name or ln.customer, ln.delivery_date, day)
			)
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
		frappe.throw(
			_("{0} is confirmed at more than one length. Unconfirm it first.").format(
				src.customer_name or src.customer
			)
		)
	if stems > have + 0.0001:
		frappe.throw(
			_("{0} has only {1} stems confirmed.").format(src.customer_name or src.customer, int(have))
		)
	dst_have, dst_team, dst_here = _confirmed_at(to_soi, dst.item_code, length)
	if plans.live_confirmations(soi=to_soi) and not dst_here:
		frappe.throw(
			_("{0} is already confirmed at another length. Unconfirm it first.").format(
				dst.customer_name or dst.customer
			)
		)
	if any(c.farm != src_farm for c in plans.live_confirmations(soi=to_soi)):
		frappe.throw(
			_("{0} is already confirmed from another farm. Unconfirm it first.").format(
				dst.customer_name or dst.customer
			)
		)
	room = max(0.0, flt(dst.ordered_stems) - flt(dst.allocated_stems) - dst_have)
	if stems > room + 0.0001:
		frappe.throw(_("{0} only needs {1} more stems.").format(dst.customer_name or dst.customer, int(room)))
	dst_team = team or dst_team
	if not dst_team:
		frappe.throw(_("Choose a team for {0}.").format(dst.customer_name or dst.customer))
	left = have - stems
	# the stems move as they are (never rounded up to whole boxes on either side): see confirm_lines
	was_exact, frappe.flags.pg_exact_confirm = frappe.flags.get("pg_exact_confirm"), True
	try:
		return _move_confirmed(
			day, farm, from_soi, to_soi, stems, left, src_farm, src_team, dst_have, dst_team, dst, length
		)
	finally:
		frappe.flags.pg_exact_confirm = was_exact


def _move_confirmed(
	day, farm, from_soi, to_soi, stems, left, src_farm, src_team, dst_have, dst_team, dst, length
):
	"""move_stems' two confirmations: the giver first, so what it lets go is free for the receiver."""
	confirm_lines(
		day,
		farm,
		[
			{
				"sales_order_item": from_soi,
				"stems": left,
				"stock_farm": src_farm,
				**({"team": src_team} if left > 0 and src_team else {}),
			}
		],
	)
	item = {
		"sales_order_item": to_soi,
		"stems": dst_have + stems,
		"team": dst_team,
		"stock_farm": src_farm,
	}
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
	_changed()
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
	_changed()
	return _unconfirm([sales_order_item], whole_mix=False)


@frappe.whitelist(methods=["POST"])
def unconfirm_lines(sales_order_items: str | list):
	"""Take these order lines out of the plan (unconfirm_line for each); a line of a mixed box takes the whole
	box with it -- a mix is confirmed and packed as one."""
	frappe.only_for(PLANNERS)
	_changed()
	sois = frappe.parse_json(sales_order_items) if isinstance(sales_order_items, str) else sales_order_items
	return _unconfirm(sois or [], whole_mix=True)


@frappe.whitelist(methods=["POST"])
def unconfirm_order(sales_order: str):
	"""Take every confirmed line of an order out of the plan."""
	frappe.only_for(PLANNERS)
	_changed()
	sois = frappe.get_all("Sales Order Item", filters={"parent": sales_order}, pluck="name")
	return _unconfirm(sois, whole_mix=False)


def _unconfirm(sois, whole_mix):
	sois = [s for s in dict.fromkeys(sois) if s]
	if whole_mix and sois:
		keys = {
			k
			for k in frappe.get_all(
				"Allocation Confirmation",
				filters={
					"sales_order_item": ("in", sois),
					"status": ("in", plans.LIVE),
					"mix_key": ("!=", ""),
				},
				pluck="mix_key",
			)
			if k
		}
		if keys:
			sois += [
				s
				for s in frappe.get_all(
					"Allocation Confirmation",
					filters={"mix_key": ("in", list(keys)), "status": ("in", plans.LIVE)},
					pluck="sales_order_item",
					distinct=True,
				)
				if s not in sois
			]
	live = [c for soi in sois for c in plans.live_confirmations(soi=soi)]
	for c in live:
		plans.release_confirmation(c.name, "Manual")
	for plan in {c.plan for c in live}:
		plans.sequence_orders(plan)
	orders = set(frappe.get_all("Sales Order Item", filters={"name": ("in", sois or [""])}, pluck="parent"))
	for so in orders:
		set_processing_farm(so)
	return {"success": True, "released": len(live), "lines": len({c.sales_order_item for c in live})}


PROCESSING_FIELD = "custom_processing_sales_farm"


def set_processing_farm(sales_order):
	"""Where each line of the order is processed: its team's sales farm (Packing Teams > Sales Farm), a mix
	all together. Lines of one order can go to teams of different sales farms. Each line's confirmations sit
	on its sales farm's plan and the line's Processing Location says where; the order's Processing Sales Farm
	is that farm when all its planned lines agree, else empty. A line with nothing confirmed goes back to its
	order's own farm."""
	from upande_packhouse.api.v2.allocation_teams import team_sales_farms

	home = team_sales_farms()
	sois = frappe.get_all("Sales Order Item", filters={"parent": sales_order}, pluck="name")
	farms, moved = set(), set()
	for soi in sois:
		confs = plans.live_confirmations(soi=soi)
		farm = next((home.get(c.team) for c in confs if c.team and home.get(c.team)), None)
		if frappe.db.has_column("Sales Order Item", "custom_processing_location") and (
			frappe.db.get_value("Sales Order Item", soi, "custom_processing_location") != farm
		):
			frappe.db.set_value(
				"Sales Order Item", soi, "custom_processing_location", farm, update_modified=False
			)
		if not farm:
			continue
		farms.add(farm)
		own = set(_stock_farms(farm))
		for c in confs:
			# a stock farm of the team's sales farm is kept: only a farm of another sales farm moves
			stock_farm = c.farm if c.farm in own else farm
			if frappe.db.get_value("Allocation Plan", c.plan, "farm") == farm and c.farm == stock_farm:
				continue
			to = plans.get_plan(c.delivery_date, farm)
			old_farm = c.farm
			frappe.db.set_value(
				"Allocation Confirmation",
				c.name,
				{"plan": to.name, "farm": stock_farm},
				update_modified=False,
			)
			# what Sales Allocation shows each sales farm follows the line
			if old_farm and old_farm != stock_farm:
				plans.refresh_mirror(sales_order, soi, old_farm)
			plans.refresh_mirror(sales_order, soi, stock_farm)
			moved.update({c.plan, to.name})
	one = next(iter(farms)) if len(farms) == 1 else None
	if frappe.db.has_column("Sales Order", PROCESSING_FIELD) and (
		frappe.db.get_value("Sales Order", sales_order, PROCESSING_FIELD) != one
	):
		frappe.db.set_value("Sales Order", sales_order, PROCESSING_FIELD, one, update_modified=False)
	for name in moved:
		plans.sequence_orders(name)
	return one


@frappe.whitelist()
def unconfirm(confirmation: str):
	"""Take one confirmation out; whatever it held goes back to the shelf for everyone."""
	frappe.only_for(PLANNERS)
	_changed()
	if not frappe.db.exists("Allocation Confirmation", confirmation):
		frappe.throw(_("That confirmation no longer exists."))
	# only a live one: a fulfilled confirmation is the line's allocation, a released one is gone already
	status = frappe.db.get_value("Allocation Confirmation", confirmation, "status")
	if status not in plans.LIVE:
		frappe.throw(_("That confirmation is {0}: there is nothing to take out.").format(_(status).lower()))
	plans.release_confirmation(confirmation, "Manual")
	plans.sequence_orders(frappe.db.get_value("Allocation Confirmation", confirmation, "plan"))
	so = frappe.db.get_value("Allocation Confirmation", confirmation, "sales_order")
	if so:
		set_processing_farm(so)
	return {"success": True}


@frappe.whitelist()
def set_order_sequence(delivery_date: str, farm: str, orders: str | list):
	"""Put the day's planned orders in this order (sales orders, first to last): the order Sales Allocation lists them in."""
	frappe.only_for(PLANNERS)
	_changed()
	if isinstance(orders, str):
		orders = json.loads(orders)
	plan = _plan_name(_day(delivery_date), farm)
	if not plan:
		frappe.throw(_("Nothing is planned for {0} yet.").format(_day(delivery_date)))
	return {"success": True, "orders": plans.sequence_orders(plan, first=orders or [])}
