# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Packing Insights v2 (www/packing-insights-v2.html): the live packing picture for management.

One screen answers "will each team finish on time, and what is in the way?".

Two reads, both read-only:

  get_packing_insights   the live work for a delivery-date range, by team: stems waiting
                         at each step, boxes packed against expected, each team's speed
                         today and the time it will finish at that speed.
  get_speed_history      the last days of packing and issuing speed, and the boxes ordered
                         for the next delivery dates, for the what-if calculator.

Numbers (stems unless the key says otherwise):

  can pack now     issued stems not yet on a pack list, on submitted pick lists
  to issue         open pick rows on a submitted pick list, not issued, not blocked
  transfer         open pick rows whose bucket is on a trolley, in transit or awaiting transfer
                   (a draft pick list with any such bucket counts here, it stays a draft until
                   the last bucket is shelved)
  not found        open pick rows a picker marked not found
  draft            open pick rows on a pick list that is not submitted
  expected boxes   core/boxes.order_boxes over each pick list's Sales Order lines
  packed boxes     complete boxes, as Orders & Workflow counts them: every Packing Guide
                   (box, variety) met on the pick list, or closed short on purpose
  not allocated    stems on order lines that have no pick list yet

Speed and finish time are live: activity today (pack lists created today, stems issued today),
per team, over the time since the team's first event, never less than 30 minutes. Finish time
= now + the slower of (stems left to pack / packing speed) and (stems left to issue / issuing
speed); issuing and packing run side by side. Stems blocked by transfers, missing buckets or
drafts are reported as blocked and are not in the estimate.

Filters follow README rules 4, 6 and 6a: delivery date range, region, farm (the ORDER's farm),
rose type, and team. Every figure on the page follows every filter.
"""

from datetime import timedelta
from typing import Any

import frappe
from frappe.utils import add_days, get_datetime, getdate, nowdate

from upande_packhouse.api.v2.core import boxes as boxes_mod
from upande_packhouse.api.v2.core import region as region_mod
from upande_packhouse.api.v2.core import rose
from upande_packhouse.api.v2.core.pipeline import business_unit_sql

#: A team that started a few minutes ago has no meaningful speed yet.
MIN_WINDOW_MIN = 30.0
#: Rows kept per team and per cause in the drill-downs.
DRILL_ROWS = 5
HISTORY_DAYS = 14

_TRANSFER = "(p.in_transit = 1 OR p.loaded_in_trolley = 1 OR p.awaiting_transfer = 1)"
_NOT_ISSUED = "IFNULL(p.issued, 0) = 0 AND IFNULL(p.issued_offline, 0) = 0"
_TEAM = "IFNULL(NULLIF(o.team, ''), 'No team')"
_CAUSE = (
	"CASE WHEN p.not_found = 1 THEN 'not_found' "
	"WHEN " + _TRANSFER + " THEN 'transfer' "
	"WHEN o.docstatus = 0 AND EXISTS (SELECT 1 FROM `tabPick List Item` t WHERE t.parent = o.name "
	"AND (t.in_transit = 1 OR t.loaded_in_trolley = 1 OR t.awaiting_transfer = 1)) THEN 'transfer' "
	"WHEN o.docstatus = 0 THEN 'draft' ELSE 'issue' END"
)


def _box_no(v):
	try:
		return int(float(v))
	except (TypeError, ValueError):
		return None


def _num(v):
	return float(v or 0)


def _hm(t):
	return t.strftime("%H:%M") if t else ""


def _minutes(a, b):
	return (b - a).total_seconds() / 60.0 if a and b else 0.0


def _median(values):
	v = sorted(values)
	n = len(v)
	if not n:
		return None
	return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def _scope(args, dates=True):
	"""(where_sql, params): submitted Roses orders for the page's filters.

	Needs the Order Pick List aliased `o` and its Sales Order aliased `so`. With dates=False the
	delivery-date range is left out (speed is today's activity whatever the orders' due dates).
	"""
	params = {}
	where = ["so.docstatus = 1", "o.docstatus < 2"]
	where.append(_business_unit_on_pick_list(args.get("business_unit") or "Roses", params))
	if dates:
		where.append("so.delivery_date BETWEEN %(ph_from)s AND %(ph_to)s")
		params["ph_from"], params["ph_to"] = args["from_date"], args["to_date"]
	farms = region_mod.farms_for(region=args.get("region"), farm=args.get("farm"))
	if farms is not None:
		where.append(region_mod.order_farm_sql("so", "o") + " IN %(ph_farms)s")
		params["ph_farms"] = region_mod.sql_tuple(farms)
	team = (args.get("team") or "").strip()
	if team == "No team":
		where.append("IFNULL(o.team, '') = ''")
	elif team:
		where.append("o.team = %(ph_team)s")
		params["ph_team"] = team
	cond = " AND ".join(where) + _rose_on_pick_list(args.get("rose_type"), params)
	return cond, params


def _business_unit_on_pick_list(business_unit, params):
	"""Orders of one business unit. An order with no unit counts as Roses when its lines are rose item
	groups (as core business_unit_sql does), read from the order lines because the pick list's own
	item group is empty on live data."""
	params["ph_bu"] = business_unit
	cond = "so.business_unit = %(ph_bu)s"
	if business_unit == "Roses":
		params["ph_bu_groups"] = tuple(rose.rose_groups("spray") + rose.rose_groups("standard")) or ("",)
		cond += (
			" OR (IFNULL(so.business_unit, '') = '' AND o.name IN (SELECT bx.parent FROM `tabPick List Item` bx"
			" JOIN `tabSales Order Item` bi ON bi.name = bx.sales_order_item WHERE bi.item_group IN %(ph_bu_groups)s))"
		)
	return "(" + cond + ")"


def _rose_on_pick_list(rose_type, params):
	"""Spray / Standard for a pick list. Order Pick List.item_group is empty on live data, so the
	rose type is read from the Sales Order lines the pick list's rows come from."""
	kind = rose.normalize(rose_type)
	if kind == "all":
		return ""
	groups = rose.rose_groups(kind)
	if not groups:
		return " AND 1=0"
	params["ph_rose"] = tuple(groups)
	return (
		" AND o.name IN (SELECT px.parent FROM `tabPick List Item` px"
		" JOIN `tabSales Order Item` sx ON sx.name = px.sales_order_item WHERE sx.item_group IN %(ph_rose)s)"
	)


def _args(from_date, to_date, region, farm, team, rose_type, business_unit):
	tomorrow = str(add_days(nowdate(), 1))
	f = str(getdate(from_date)) if from_date else tomorrow
	t = str(getdate(to_date)) if to_date else tomorrow
	if t < f:
		f, t = t, f
	return {
		"from_date": f,
		"to_date": t,
		"region": region,
		"farm": farm,
		"team": team,
		"rose_type": rose_type,
		"business_unit": business_unit or "Roses",
	}


def _team_row(teams, name):
	if name not in teams:
		teams[name] = {
			"team": name,
			"cells": {},
			"queue": {"stems": 0.0, "orders": 0},
			"exp_boxes": 0,
			"packed_boxes": 0,
			"pack": {},
			"issue": {},
		}
	return teams[name]


@frappe.whitelist()
def get_packing_insights(
	from_date: Any = None,
	to_date: Any = None,
	region: Any = None,
	farm: Any = None,
	team: Any = None,
	rose_type: Any = "all",
	business_unit: Any = "Roses",
):
	"""The live work and each team's finish time. Defaults to the orders due tomorrow."""
	a = _args(from_date, to_date, region, farm, team, rose_type, business_unit)
	now = get_datetime()
	today = nowdate()
	where, params = _scope(a)
	act_where, act_params = _scope(a, dates=False)
	teams = {}
	# The day the speeds are read from: today once anything was packed or issued today, else the
	# last day with packing (so after midnight yesterday's speed still shows, labelled as such).
	day = _work_day(act_where, act_params, today)
	live = day == today
	past = a["to_date"] < today  # delivery dates that have passed: estimated against actual finish

	# 1. open pick rows by team and cause
	for r in frappe.db.sql(
		f"""SELECT {_TEAM} AS team, {_CAUSE} AS cause, COUNT(DISTINCT o.name) AS orders,
		           ROUND(SUM(p.stock_qty)) AS stems, MIN(so.delivery_date) AS due
		    FROM `tabPick List Item` p
		    JOIN `tabOrder Pick List` o ON o.name = p.parent
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {where} AND {_NOT_ISSUED}
		    GROUP BY team, cause""",
		params,
		as_dict=True,
	):
		_team_row(teams, r.team)["cells"][r.cause] = {
			"stems": _num(r.stems),
			"orders": int(r.orders),
			"due": str(r.due),
		}

	# 2. issued, not yet packed (submitted pick lists): one pass for the totals and the drill-down
	queue_rows = []
	for r in frappe.db.sql(
		f"""SELECT {_TEAM} AS team, o.name AS opl, so.delivery_date AS due,
		           ROUND(GREATEST(IFNULL(i.s, 0) - IFNULL(k.s, 0), 0)) AS stems
		    FROM `tabOrder Pick List` o
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    LEFT JOIN (SELECT p.parent, SUM(p.stock_qty) AS s FROM `tabPick List Item` p
		               WHERE p.issued = 1 OR p.issued_offline = 1 GROUP BY p.parent) i ON i.parent = o.name
		    LEFT JOIN (SELECT f.order_pick_list AS opl, SUM(fi.stock_qty) AS s
		               FROM `tabFarm Pack List` f JOIN `tabFarm Packlist Item` fi ON fi.parent = f.name
		               WHERE f.docstatus < 2 GROUP BY f.order_pick_list) k ON k.opl = o.name
		    WHERE {where} AND o.docstatus = 1 AND IFNULL(i.s, 0) > IFNULL(k.s, 0) + 100""",
		params,
		as_dict=True,
	):
		t = _team_row(teams, r.team)
		t["queue"] = {"stems": t["queue"]["stems"] + _num(r.stems), "orders": t["queue"]["orders"] + 1}
		queue_rows.append({"team": r.team, "opl": r.opl, "due": str(r.due), "stems": _num(r.stems)})

	# 3. boxes expected (one rule, core/boxes) and packed, per team
	_boxes(teams, where, params)

	# 4. speed today, per team
	for r in frappe.db.sql(
		f"""SELECT {_TEAM} AS team, COUNT(DISTINCT f.owner) AS people, MIN(f.creation) AS first_t,
		           MAX(f.modified) AS last_t,
		           COUNT(DISTINCT CONCAT(f.name, '|', TRIM(fi.box_id))) AS boxes, SUM(fi.stock_qty) AS stems
		    FROM `tabFarm Pack List` f
		    JOIN `tabFarm Packlist Item` fi ON fi.parent = f.name
		    JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND f.docstatus < 2 AND f.creation >= %(ph_today)s AND f.creation < %(ph_next)s
		      AND IFNULL(TRIM(fi.box_id), '') != ''
		    GROUP BY team""",
		{**act_params, "ph_today": day, "ph_next": str(add_days(day, 1))},
		as_dict=True,
	):
		_team_row(teams, r.team)["pack"] = {
			"people": int(r.people),
			"first": _hm(r.first_t),
			"last": _hm(r.last_t),
			"boxes": _num(r.boxes),
			"stems": _num(r.stems),
			"mins": _minutes(r.first_t, now if live else r.last_t),
			"idle": _minutes(r.last_t, now) if live else None,
		}
	for r in frappe.db.sql(
		f"""SELECT {_TEAM} AS team, COUNT(DISTINCT p.issued_by) AS people, MIN(p.issued_at) AS first_t,
		           MAX(p.issued_at) AS last_t, SUM(p.stock_qty) AS stems
		    FROM `tabPick List Item` p
		    JOIN `tabOrder Pick List` o ON o.name = p.parent
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND p.issued_at >= %(ph_today)s AND p.issued_at < %(ph_next)s
		    GROUP BY team""",
		{**act_params, "ph_today": day, "ph_next": str(add_days(day, 1))},
		as_dict=True,
	):
		_team_row(teams, r.team)["issue"] = {
			"people": int(r.people),
			"first": _hm(r.first_t),
			"last": _hm(r.last_t),
			"stems": _num(r.stems),
			"mins": _minutes(r.first_t, now if live else r.last_t),
			"idle": _minutes(r.last_t, now) if live else None,
		}

	if past:
		_past_packing(teams, where, params)
	team_list = [_finish(t, live) for t in teams.values()]
	team_list.sort(key=lambda x: -(x["to_pack"] + x["blocked"]))
	line = _line(act_where, act_params, day)
	if past:
		line["pack"] = _past_line(teams)

	return {
		"success": True,
		"as_of": str(now).split(".")[0],
		"today": today,
		"work_day": day,
		"live": live,
		"past": past,
		"from_date": a["from_date"],
		"to_date": a["to_date"],
		"teams": team_list,
		"kpis": _kpis(team_list, line, act_where, act_params, day),
		"drill": _drill(where, params),
		"qdrill": _top_queue(queue_rows),
		"remote": _remote(where, params),
		"unalloc": _unallocated(a),
		"issuers": _issuers(act_where, act_params, day),
		"line": line,
		"waits": _waits(act_where, act_params, day),
	}


def _boxes(teams, where, params):
	"""Expected boxes per team from the pick lists' order lines, packed boxes from pack lists."""
	opl_team = {}
	lines = {}
	for r in frappe.db.sql(
		f"""SELECT DISTINCT o.name AS opl, {_TEAM} AS team, i.name AS name, i.item_code, i.custom_line,
		           i.custom_length, i.custom_bunch_group, i.custom_mix_group, i.custom_mixed_bunch,
		           i.custom_mixed_box, i.custom_number_of_boxes
		    FROM `tabOrder Pick List` o
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    JOIN `tabPick List Item` p ON p.parent = o.name
		    JOIN `tabSales Order Item` i ON i.name = p.sales_order_item
		    WHERE {where}""",
		params,
		as_dict=True,
	):
		opl_team[r.opl] = r.team
		lines.setdefault(r.opl, []).append(r)
	for opl, rows in lines.items():
		_team_row(teams, opl_team[opl])["exp_boxes"] += boxes_mod.order_boxes(rows)
	# a box is packed when it is complete, as Orders & Workflow counts it (core/pipeline step 6):
	# every Packing Guide (box, variety) met on the pick list, or closed short on purpose
	need, have, started, under, opl_of = {}, {}, {}, {}, {}
	for r in frappe.db.sql(
		f"""SELECT o.name AS opl, {_TEAM} AS team, g.box_number, g.variety, SUM(g.stems) AS stems
		    FROM `tabPacking Guide` g
		    JOIN `tabOrder Pick List` o ON o.name = g.parent
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {where} AND g.parenttype = 'Order Pick List'
		    GROUP BY o.name, team, g.box_number, g.variety""",
		params,
		as_dict=True,
	):
		opl_of[r.opl] = r.team
		k = (r.opl, _box_no(r.box_number), r.variety)
		need[k] = need.get(k, 0.0) + _num(r.stems)
	for r in frappe.db.sql(
		f"""SELECT o.name AS opl, {_TEAM} AS team, fi.box_id, fi.item_code, SUM(fi.stock_qty) AS stems,
		           MAX(IFNULL(fi.under_pack_reason, '') != '') AS short
		    FROM `tabFarm Pack List` f
		    JOIN `tabFarm Packlist Item` fi ON fi.parent = f.name
		    JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {where} AND f.docstatus < 2
		    GROUP BY o.name, team, fi.box_id, fi.item_code""",
		params,
		as_dict=True,
	):
		opl_of[r.opl] = r.team
		b = _box_no(r.box_id) or 1
		started[(r.opl, b)] = 1
		have[(r.opl, b, r.item_code)] = have.get((r.opl, b, r.item_code), 0.0) + _num(r.stems)
		if r.short:
			under[(r.opl, b)] = 1
	for opl, b in started:
		keys = [k for k in need if k[0] == opl and k[1] == b]
		if (opl, b) in under or (keys and all(have.get(k, 0.0) >= need[k] - 0.001 for k in keys)):
			_team_row(teams, opl_of[opl])["packed_boxes"] += 1


def _work_day(act_where, act_params, today):
	"""Today when anything was packed or issued today, else the latest day in the last week that had packing."""
	since = str(add_days(today, -7))
	p = {**act_params, "ph_since": since}
	last = frappe.db.sql(
		f"""SELECT MAX(DATE(f.creation)) FROM `tabFarm Pack List` f
		    JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND f.docstatus < 2 AND f.creation >= %(ph_since)s""",
		p,
	)[0][0]
	issued = frappe.db.sql(
		f"""SELECT MAX(DATE(p.issued_at)) FROM `tabPick List Item` p
		    JOIN `tabOrder Pick List` o ON o.name = p.parent
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND p.issued_at >= %(ph_since)s""",
		p,
	)[0][0]
	days = [str(d) for d in (last, issued) if d]
	return max(days) if days else today


# Packing speed for the estimate is read over this long after a team's first pack list.
ESTIMATE_AFTER_MIN = 120


def _past_packing(teams, where, params):
	"""Delivery dates that have passed: each team's real packing window and its estimated finish.

	Actual finish = the team's last pack list. Estimated finish = the finish the team's speed over its
	first ESTIMATE_AFTER_MIN minutes of packing predicted, for the stems it packed in the end plus
	what is still unpacked: the estimate someone watching the screen two hours in would have had.
	"""
	rows = {}
	for r in frappe.db.sql(
		f"""SELECT {_TEAM} AS team, f.name AS fpl, f.owner AS who, MIN(f.creation) AS c, MAX(f.modified) AS m,
		           SUM(fi.stock_qty) AS stems, COUNT(DISTINCT TRIM(fi.box_id)) AS boxes
		    FROM `tabFarm Pack List` f
		    JOIN `tabFarm Packlist Item` fi ON fi.parent = f.name
		    JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {where} AND f.docstatus < 2 AND IFNULL(TRIM(fi.box_id), '') != ''
		    GROUP BY team, f.name, f.owner""",
		params,
		as_dict=True,
	):
		rows.setdefault(r.team, []).append(r)
	for name, rs in rows.items():
		t = _team_row(teams, name)
		first = min(r.c for r in rs)
		last = max(r.m for r in rs)
		stems = sum(_num(r.stems) for r in rs)
		t["pack"] = {
			"people": len({r.who for r in rs}),
			"first": _hm(first),
			"last": _hm(last),
			"boxes": sum(_num(r.boxes) for r in rs),
			"stems": stems,
			"mins": _minutes(first, last),
			"idle": None,
		}
		early = sum(_num(r.stems) for r in rs if _minutes(first, r.c) < ESTIMATE_AFTER_MIN)
		window = max(min(_minutes(first, last), ESTIMATE_AFTER_MIN), MIN_WINDOW_MIN)
		t["past"] = {
			"first_at": str(first),
			"last_at": str(last),
			"early_rate": early / window,
			"packed_stems": stems,
		}


def _past_line(teams):
	rs = [t["pack"] for t in teams.values() if t.get("pack")]
	firsts = [t["past"]["first_at"] for t in teams.values() if t.get("past")]
	lasts = [t["past"]["last_at"] for t in teams.values() if t.get("past")]
	stems = sum(r["stems"] for r in rs)
	if not firsts:
		return {"stems": 0.0, "first": "", "last": "", "mins": 0.0}
	first, last = get_datetime(min(firsts)), get_datetime(max(lasts))
	return {"stems": stems, "first": _hm(first), "last": _hm(last), "mins": _minutes(first, last)}


def _finish(t, live=True):
	"""One team's row: what is left, its speed today and the hours to finish at that speed."""
	c = t["cells"]
	queue = t["queue"]["stems"]
	to_issue = c.get("issue", {}).get("stems", 0.0)
	blocked = sum(c.get(k, {}).get("stems", 0.0) for k in ("transfer", "not_found", "draft"))
	pk, iss = t["pack"], t["issue"]
	to_pack = queue + to_issue
	pack_rate = pk["stems"] / max(pk["mins"], MIN_WINDOW_MIN) if pk and pk.get("stems") else None
	issue_rate = iss["stems"] / max(iss["mins"], MIN_WINDOW_MIN) if iss and iss.get("stems") else None
	eta, state = None, "ok"
	if to_pack < 1:
		eta, state = 0.0, "done"
	elif pack_rate is None or (to_issue > 0 and issue_rate is None):
		state = "nodata"
	else:
		eta = max(to_pack / pack_rate, (to_issue / issue_rate) if to_issue > 0 else 0.0)
	bph = pk["boxes"] / (max(pk["mins"], MIN_WINDOW_MIN) / 60.0) if pk else 0.0
	past = None
	if t.get("past"):
		p = t["past"]
		first, last = get_datetime(p["first_at"]), get_datetime(p["last_at"])
		left = to_pack
		est = None
		if p["early_rate"] > 0:
			est = first + timedelta(minutes=(p["packed_stems"] + left) / p["early_rate"])
		past = {
			"first_at": p["first_at"],
			"actual_at": p["last_at"] if left < 1 else None,
			"last_pack_at": p["last_at"],
			"est_at": str(est).split(".")[0] if est else None,
			"diff_min": _minutes(est, last) if est and left < 1 else None,
			"left_stems": left,
		}
	return {
		"past": past,
		"stale": not live,
		"team": t["team"],
		"cells": c,
		"queue": t["queue"],
		"to_pack": to_pack,
		"blocked": blocked,
		"exp_boxes": t["exp_boxes"],
		"packed_boxes": t["packed_boxes"],
		"pack_people": pk.get("people", 0),
		"pack_first": pk.get("first", ""),
		"pack_last": pk.get("last", ""),
		"pack_idle": pk.get("idle"),
		"issue_people": iss.get("people", 0),
		"issue_last": iss.get("last", ""),
		"issue_idle": iss.get("idle"),
		"bph": bph,
		"pack_spm": pack_rate or 0.0,
		"issue_spm": issue_rate or 0.0,
		"eta_min": eta,
		"state": state,
	}


def _kpis(team_list, line, act_where, act_params, today):
	exp = sum(t["exp_boxes"] for t in team_list)
	packed = sum(t["packed_boxes"] for t in team_list)
	can = sum(t["queue"]["stems"] for t in team_list)
	held = sum(t["cells"].get("issue", {}).get("stems", 0.0) + t["blocked"] for t in team_list)
	timed = [t for t in team_list if t["eta_min"] is not None and t["state"] != "done"]
	worst = max(timed, key=lambda t: t["eta_min"]) if timed else None
	pack = line["pack"]
	flow = pack["stems"] / pack["mins"] if pack["mins"] > 1 else 0.0
	return {
		"expected_boxes": exp,
		"packed_boxes": packed,
		"can_pack_stems": can,
		"held_stems": held,
		"packing_stems_per_min": flow,
		"finish_eta_min": worst["eta_min"] if worst else None,
		"finish_team": worst["team"] if worst else None,
	}


def _drill(where, params):
	out, seen = [], {}
	for r in frappe.db.sql(
		f"""SELECT {_TEAM} AS team, {_CAUSE} AS cause, o.name AS opl, so.delivery_date AS due,
		           ROUND(SUM(p.stock_qty)) AS stems, COUNT(*) AS buckets, GROUP_CONCAT(DISTINCT p.farm) AS farms
		    FROM `tabPick List Item` p
		    JOIN `tabOrder Pick List` o ON o.name = p.parent
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {where} AND {_NOT_ISSUED}
		    GROUP BY team, cause, o.name, so.delivery_date
		    ORDER BY stems DESC""",
		params,
		as_dict=True,
	):
		k = (r.team, r.cause)
		if seen.get(k, 0) < DRILL_ROWS:
			seen[k] = seen.get(k, 0) + 1
			out.append(
				{
					"team": r.team,
					"cause": r.cause,
					"opl": r.opl,
					"due": str(r.due),
					"stems": _num(r.stems),
					"buckets": int(r.buckets),
					"farms": r.farms or "",
				}
			)
	return out


def _top_queue(queue_rows):
	out, seen = [], {}
	for r in sorted(queue_rows, key=lambda x: -x["stems"]):
		if seen.get(r["team"], 0) < DRILL_ROWS:
			seen[r["team"]] = seen.get(r["team"], 0) + 1
			out.append(r)
	return out


def _remote(where, params):
	return [
		{"farm": r.farm or "-", "buckets": int(r.buckets), "stems": _num(r.stems), "due": str(r.due)}
		for r in frappe.db.sql(
			f"""SELECT p.farm, COUNT(*) AS buckets, ROUND(SUM(p.stock_qty)) AS stems, MIN(so.delivery_date) AS due
			    FROM `tabPick List Item` p
			    JOIN `tabOrder Pick List` o ON o.name = p.parent
			    JOIN `tabSales Order` so ON so.name = o.sales_order
			    WHERE {where} AND {_NOT_ISSUED} AND {_TRANSFER}
			    GROUP BY p.farm ORDER BY stems DESC""",
			params,
			as_dict=True,
		)
	]


def _unallocated(a):
	"""Ordered stems per delivery date against those on a line with a pick list (Orders & Workflow's "not allocated": lines with no pick list; short lines are not counted)."""
	params = {"ph_from": a["from_date"], "ph_to": a["to_date"]}
	where = ["so.docstatus = 1", "so.delivery_date BETWEEN %(ph_from)s AND %(ph_to)s"]
	where.append(business_unit_sql(a["business_unit"], params, so="so", soi="soi"))
	farms = region_mod.farms_for(region=a.get("region"), farm=a.get("farm"))
	if farms is not None:
		where.append(region_mod.order_farm_sql("so", None) + " IN %(ph_farms)s")
		params["ph_farms"] = region_mod.sql_tuple(farms)
	cond = " AND ".join(where) + rose.rose_sql("soi.item_group", a.get("rose_type"), params, key="ph_rose")
	return [
		{"due": str(r.due), "ordered": _num(r.ordered), "allocated": _num(r.allocated)}
		for r in frappe.db.sql(
			f"""SELECT so.delivery_date AS due, ROUND(SUM(soi.stock_qty)) AS ordered,
			           ROUND(SUM(CASE WHEN al.line IS NULL THEN 0 ELSE soi.stock_qty END)) AS allocated
			    FROM `tabSales Order Item` soi
			    JOIN `tabSales Order` so ON so.name = soi.parent
			    LEFT JOIN (SELECT p.sales_order_item AS line, SUM(p.stock_qty) AS s FROM `tabPick List Item` p
			               JOIN `tabOrder Pick List` o ON o.name = p.parent WHERE o.docstatus < 2
			               GROUP BY p.sales_order_item) al ON al.line = soi.name
			    WHERE {cond}
			    GROUP BY so.delivery_date ORDER BY so.delivery_date""",
			params,
			as_dict=True,
		)
	]


def _issuers(act_where, act_params, today):
	return [
		{"who": r.who or "-", "stems": _num(r.stems), "last": _hm(r.last_t)}
		for r in frappe.db.sql(
			f"""SELECT IFNULL(u.full_name, p.issued_by) AS who, ROUND(SUM(p.stock_qty)) AS stems, MAX(p.issued_at) AS last_t
			    FROM `tabPick List Item` p
			    JOIN `tabOrder Pick List` o ON o.name = p.parent
			    JOIN `tabSales Order` so ON so.name = o.sales_order
			    LEFT JOIN `tabUser` u ON u.name = p.issued_by
			    WHERE {act_where} AND p.issued_at >= %(ph_today)s AND p.issued_at < %(ph_next)s
			    GROUP BY p.issued_by ORDER BY stems DESC LIMIT 8""",
			{**act_params, "ph_today": today, "ph_next": str(add_days(today, 1))},
			as_dict=True,
		)
	]


def _line(act_where, act_params, today):
	"""Stems per minute through each step today: allocated, issued, packed, staged."""
	p = {**act_params, "ph_today": today, "ph_next": str(add_days(today, 1))}
	line = {}
	r = frappe.db.sql(
		f"""SELECT MIN(o.creation) AS first_t, MAX(o.creation) AS last_t,
		           ROUND(SUM((SELECT SUM(x.stock_qty) FROM `tabPick List Item` x WHERE x.parent = o.name))) AS stems
		    FROM `tabOrder Pick List` o JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND o.creation >= %(ph_today)s AND o.creation < %(ph_next)s""",
		p,
		as_dict=True,
	)[0]
	line["alloc"] = _step(r.stems, r.first_t, r.last_t)
	r = frappe.db.sql(
		f"""SELECT SUM(p.stock_qty) AS stems, MIN(p.issued_at) AS first_t, MAX(p.issued_at) AS last_t
		    FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name = p.parent
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND p.issued_at >= %(ph_today)s AND p.issued_at < %(ph_next)s""",
		p,
		as_dict=True,
	)[0]
	line["issue"] = _step(r.stems, r.first_t, r.last_t)
	r = frappe.db.sql(
		f"""SELECT SUM(fi.stock_qty) AS stems, MIN(f.creation) AS first_t, MAX(f.modified) AS last_t
		    FROM `tabFarm Pack List` f JOIN `tabFarm Packlist Item` fi ON fi.parent = f.name
		    JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND f.docstatus < 2 AND f.creation >= %(ph_today)s AND f.creation < %(ph_next)s""",
		p,
		as_dict=True,
	)[0]
	line["pack"] = _step(r.stems, r.first_t, r.last_t)
	r = frappe.db.sql(
		f"""SELECT COUNT(*) AS boxes, MIN(b.precooled_at) AS first_t, MAX(b.precooled_at) AS last_t
		    FROM `tabBox Label` b JOIN `tabOrder Pick List` o ON o.name = b.order_pick_list
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND b.precooled_at >= %(ph_today)s AND b.precooled_at < %(ph_next)s""",
		p,
		as_dict=True,
	)[0]
	line["stage"] = {
		"boxes": _num(r.boxes),
		"first": _hm(r.first_t),
		"last": _hm(r.last_t),
		"mins": _minutes(r.first_t, r.last_t),
	}
	return line


def _step(stems, first, last):
	return {"stems": _num(stems), "first": _hm(first), "last": _hm(last), "mins": _minutes(first, last)}


def _waits(act_where, act_params, today):
	"""Middle wait at each step for the pick lists packed today."""
	steps = {"alloc_to_issue": [], "issuing": [], "wait_pack": [], "packing": [], "wait_stage": []}
	for r in frappe.db.sql(
		f"""SELECT o.creation AS oc, MIN(f.creation) AS pack_start, MAX(f.modified) AS pack_end,
		           (SELECT MIN(p.issued_at) FROM `tabPick List Item` p WHERE p.parent = o.name) AS fi,
		           (SELECT MAX(p.issued_at) FROM `tabPick List Item` p WHERE p.parent = o.name) AS li,
		           (SELECT MAX(b.precooled_at) FROM `tabBox Label` b WHERE b.order_pick_list = o.name
		              AND b.precooled_at IS NOT NULL) AS sl
		    FROM `tabFarm Pack List` f
		    JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {act_where} AND f.docstatus < 2 AND f.creation >= %(ph_today)s AND f.creation < %(ph_next)s
		    GROUP BY o.name, o.creation""",
		{**act_params, "ph_today": today, "ph_next": str(add_days(today, 1))},
		as_dict=True,
	):
		if r.fi:
			steps["alloc_to_issue"].append(_minutes(r.oc, r.fi))
		if r.fi and r.li:
			steps["issuing"].append(_minutes(r.fi, r.li))
		if r.li:
			steps["wait_pack"].append(_minutes(r.li, r.pack_start))
		steps["packing"].append(_minutes(r.pack_start, r.pack_end))
		if r.sl:
			steps["wait_stage"].append(_minutes(r.pack_end, r.sl))
	out = {}
	for k, v in steps.items():
		good = [x for x in v if -5 <= x < 1440]
		out[k] = {"n": len(good), "median": _median(good)}
	return out


@frappe.whitelist()
def get_speed_history(
	region: Any = None,
	farm: Any = None,
	rose_type: Any = "all",
	business_unit: Any = "Roses",
):
	"""Packing and issuing speed per day for the last two weeks, and the boxes ordered for the
	next delivery dates, for the what-if calculator. Follows region, farm and rose type."""
	today = nowdate()
	a = _args(today, today, region, farm, None, rose_type, business_unit)
	where, params = _scope(a, dates=False)
	since = str(add_days(today, -HISTORY_DAYS))
	p = {**params, "ph_since": since}
	days = {}

	def day(d):
		return days.setdefault(
			str(d),
			{
				"d": str(d),
				"fpls": 0,
				"packers": 0,
				"pack_first": "",
				"pack_last": "",
				"pack_win": 0.0,
				"boxes": 0.0,
				"pack_stems": 0.0,
				"issuers": 0,
				"issue_stems": 0.0,
				"issue_first": "",
				"issue_last": "",
				"issue_win": 0.0,
			},
		)

	for r in frappe.db.sql(
		f"""SELECT DATE(f.creation) AS d, COUNT(DISTINCT f.name) AS fpls, COUNT(DISTINCT f.owner) AS packers,
		           MIN(f.creation) AS first_t, MAX(f.modified) AS last_t
		    FROM `tabFarm Pack List` f JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {where} AND f.docstatus < 2 AND f.creation >= %(ph_since)s GROUP BY d""",
		p,
		as_dict=True,
	):
		x = day(r.d)
		x.update(
			fpls=int(r.fpls),
			packers=int(r.packers),
			pack_first=_hm(r.first_t),
			pack_last=_hm(r.last_t),
			pack_win=_minutes(r.first_t, r.last_t),
		)
	for r in frappe.db.sql(
		f"""SELECT x.d, SUM(x.b) AS boxes, SUM(x.s) AS stems FROM (
		        SELECT DATE(f.creation) AS d, f.name, COUNT(DISTINCT NULLIF(TRIM(fi.box_id), '')) AS b, SUM(fi.stock_qty) AS s
		        FROM `tabFarm Pack List` f JOIN `tabFarm Packlist Item` fi ON fi.parent = f.name
		        JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list JOIN `tabSales Order` so ON so.name = o.sales_order
		        WHERE {where} AND f.docstatus < 2 AND f.creation >= %(ph_since)s
		        GROUP BY DATE(f.creation), f.name) x GROUP BY x.d""",
		p,
		as_dict=True,
	):
		x = day(r.d)
		x["boxes"], x["pack_stems"] = _num(r.boxes), _num(r.stems)
	for r in frappe.db.sql(
		f"""SELECT DATE(p.issued_at) AS d, COUNT(DISTINCT p.issued_by) AS issuers, SUM(p.stock_qty) AS stems,
		           MIN(p.issued_at) AS first_t, MAX(p.issued_at) AS last_t
		    FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name = p.parent
		    JOIN `tabSales Order` so ON so.name = o.sales_order
		    WHERE {where} AND p.issued_at >= %(ph_since)s GROUP BY d""",
		p,
		as_dict=True,
	):
		x = day(r.d)
		x.update(
			issuers=int(r.issuers),
			issue_stems=_num(r.stems),
			issue_first=_hm(r.first_t),
			issue_last=_hm(r.last_t),
			issue_win=_minutes(r.first_t, r.last_t),
		)
	return {
		"success": True,
		"today": today,
		"days": sorted(days.values(), key=lambda x: x["d"]),
		"due_boxes": _due_boxes(a, today),
	}


def _due_boxes(a, today):
	"""Boxes ordered for today and the next two delivery dates (core/boxes.order_boxes per order)."""
	params = {"ph_from": today, "ph_to": str(add_days(today, 2))}
	where = [
		"so.docstatus = 1",
		"so.delivery_date BETWEEN %(ph_from)s AND %(ph_to)s",
		"IFNULL(i.item_code, '') != ''",
	]
	where.append(business_unit_sql(a["business_unit"], params, so="so", soi="i"))
	farms = region_mod.farms_for(region=a.get("region"), farm=a.get("farm"))
	if farms is not None:
		where.append(region_mod.order_farm_sql("so", None) + " IN %(ph_farms)s")
		params["ph_farms"] = region_mod.sql_tuple(farms)
	cond = " AND ".join(where) + rose.rose_sql("i.item_group", a.get("rose_type"), params, key="ph_rose")
	by_order, due = {}, {}
	for r in frappe.db.sql(
		f"""SELECT i.parent, so.delivery_date AS due, i.name, i.item_code, i.custom_line, i.custom_length,
		           i.custom_bunch_group, i.custom_mix_group, i.custom_mixed_bunch, i.custom_mixed_box,
		           i.custom_number_of_boxes
		    FROM `tabSales Order Item` i JOIN `tabSales Order` so ON so.name = i.parent WHERE {cond}""",
		params,
		as_dict=True,
	):
		by_order.setdefault(r.parent, []).append(r)
		due[r.parent] = str(r.due)
	out = {}
	for so, rows in by_order.items():
		x = out.setdefault(due[so], {"orders": 0, "boxes": 0})
		x["orders"] += 1
		x["boxes"] += boxes_mod.order_boxes(rows)
	return out
