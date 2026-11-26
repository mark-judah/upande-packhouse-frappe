# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Planning v2: revise the confirmations to what is really there.

The confirmed lines share the stock of their variety and length in sequence (on the shelf, received, in
quarantine, harvested and not received yet): each is suggested what it still needs, as far as what the lines
before it left goes. No forecast.
Whole boxes (a line's last part box only when it completes the line). The suggestion keeps the teams; the
planner can move a line to another team when applying it. A line the stock cannot reach at all leaves the plan.
"""

import json
from collections import defaultdict
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from upande_packhouse import allocation_plan as plans
from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.api.v2.allocation_checks import mix_key, per_box
from upande_packhouse.api.v2.cold_room import ONWARD_TYPES, RECEIPT_TYPES
from upande_packhouse.api.v2.core import received, stock

#: Entries of a bucket that is still on its way to the receiving cold store.
CUT = ("Harvesting", "Grading")
CYCLE = (
	("Harvesting", "Grading", "Receiving Quarantined", "Quarantine Accept", "Quarantine Rejects")
	+ RECEIPT_TYPES
	+ ONWARD_TYPES
)


def stock_stages(farm, varieties, places=None, process_day=None):
	"""{(variety, length): {shelf, received, quarantine, harvested}} in stems that no pick list uses: free on
	the shelf (plan holds included), received and not shelved, received into quarantine and not released, and
	harvested with no receiving since (graded or not), within the discard age. `places`, a dict, also gets
	where the stock not on the shelf is: {(variety, length): {(stage, farm, warehouse): [stems, {buckets}]}}.
	`process_day`: count the shelf and the received stock that will still be under its variety's discard age on that
	day (a bucket still cooling now will have cooled), instead of what is allocatable today."""

	def place(key, stage, farm_, warehouse, stems, bucket):
		if places is None:
			return
		p = places.setdefault(key, {}).setdefault((stage, farm_ or "", warehouse or ""), [0.0, set()])
		p[0] += flt(stems)
		p[1].add(bucket)

	out = defaultdict(lambda: {"shelf": 0.0, "received": 0.0, "quarantine": 0.0, "harvested": 0.0})
	if not varieties:
		return out
	farms = pg._stock_farms(farm)
	vs = sorted(varieties)
	ahead = max(0, (getdate(process_day) - getdate(today())).days) if process_day else None
	age_of = pg._discard_ages() if process_day else None
	for r in stock.bucket_rows(farms=farms, varieties=vs):
		if ahead is None:
			out[(r.variety, r.stem_length or "")]["shelf"] += flt(r.allocatable)
		elif not r.in_discard_request and (r.age_days or 0) + ahead < age_of(r.variety):
			out[(r.variety, r.stem_length or "")]["shelf"] += max(0.0, flt(r.free))
	for r in received.received_unshelved(farms=farms, varieties=vs):
		if ahead is not None and (r.age_days or 0) + ahead >= age_of(r.variety):
			continue
		out[(r.variety, r.stem_length or "")]["received"] += flt(r.stems)
		place((r.variety, r.stem_length or ""), "received", r.farm, r.cold_room, r.stems, r.bucket_id)
	since = add_days(today(), -min(cint(stock._config()["discard_age"]), 14))
	# the planning page's revise reads it for one order after another: a minute old is near enough there
	rows = (
		_cycle_rows(tuple(farms), since) if places is None else _cycle_rows(tuple(farms), since, fresh=True)
	)
	rows = [r for r in rows if r.variety in set(vs)]
	by_bucket = defaultdict(list)
	for r in rows:
		by_bucket[r.bucket_id].append(r)
	tails = {}
	for bucket, entries in by_bucket.items():
		entries.sort(key=lambda r: (r.posting_date, r.creation))
		last = entries[-1]
		if last.stock_entry_type == "Receiving Quarantined":
			tails[bucket] = ("quarantine", [r for r in entries if r.creation == last.creation])
			continue
		# harvested, not received: every harvest scan since the bucket last left the cut stage (grading moves
		# the same stems, so it counts only when the harvest itself is older than the window)
		tail = []
		for r in reversed(entries):
			if r.stock_entry_type not in CUT:
				break
			tail.append(r)
		cut = [r for r in tail if r.stock_entry_type == "Harvesting"] or tail
		if cut:
			tails[bucket] = ("harvested", cut)
	shelved = {}
	for bucket, added in frappe.db.sql(
		"""SELECT bucket_id, MAX(DATE(date_added)) FROM `tabShelf Item`
		WHERE bucket_id IN %(b)s AND stem_qty > 0 GROUP BY bucket_id""",
		{"b": tuple(tails) or ("",)},
	):
		shelved[bucket] = added
	wanted = set(vs)
	for bucket, (stage, entries) in tails.items():
		since_cut = min(r.posting_date for r in entries)
		if shelved.get(bucket) and shelved[bucket] >= since_cut:
			continue
		for r in entries:
			if r.variety in wanted:
				out[(r.variety, r.stem_length)][stage] += flt(r.stems)
				place((r.variety, r.stem_length), stage, r.farm, r.warehouse, r.stems, bucket)
	return out


def _cycle_rows(farms, since, fresh=False):
	"""Every bucket's harvest-to-shelf entries at these farms since `since`, kept for a minute."""
	key = "upande_revise_cycle:{0}:{1}".format(",".join(farms), since)
	if not fresh:
		got = frappe.cache().get_value(key)
		if got is not None:
			return [frappe._dict(r) for r in got]
	rows = frappe.db.sql(
		"""
		SELECT se.custom_bucket_id AS bucket_id, se.stock_entry_type, se.posting_date, se.creation,
			se.farm, MAX(sed.t_warehouse) AS warehouse, sed.item_code AS variety, SUM(sed.qty) AS stems,
			COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length, '') AS stem_length
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1 AND se.stock_entry_type IN %(types)s AND se.posting_date >= %(since)s
			AND IFNULL(se.custom_bucket_id, '') != '' AND se.farm IN %(farms)s
		GROUP BY se.name, sed.item_code, stem_length
		""",
		{"types": CYCLE, "since": since, "farms": farms},
		as_dict=True,
	)
	frappe.cache().set_value(key, [dict(r) for r in rows], expires_in_sec=60)
	return rows


def _changes(day, farm, keep=None, days=None, only=None):
	"""Lines whose confirmation the revision would change; `keep(line)` also lists lines that stay the same.
	`days`: every delivery day of a week, sharing one stock. `only(line)`: list those lines alone -- the lines
	before them in the sequence, at the same variety and length, still take their share of the stock first."""
	days = days or [day]
	planned = pg._planned_week(days, farm)
	lines = [ln for ln in pg._lines_for(days[0], farm, days[-1]) if ln.name in planned]
	if only is not None:
		key = lambda ln: (ln.item_code, planned[ln.name].get("length") or ln.custom_length or "")  # noqa: E731
		wanted = {key(ln) for ln in lines if only(ln)}
		lines = [ln for ln in lines if key(ln) in wanted]
	if not lines:
		return []
	seq, names = {}, {}
	for name, sequence, order_name in frappe.db.sql(
		"""SELECT name, IFNULL(custom_allocation_sequence, 0), IFNULL(custom_order_name, '')
		FROM `tabSales Order` WHERE name IN %(s)s""",
		{"s": tuple({ln.parent for ln in lines})},
	):
		seq[name], names[name] = sequence, order_name
	pools = defaultdict(list)
	for ln in lines:
		length = planned[ln.name].get("length") or ln.custom_length or ""
		pools[(ln.item_code, length)].append(ln)
	# the stock as it will be on the processing day: the last delivery day less the harvest lead
	process_day = getdate(days[-1]) - timedelta(days=plans.HARVEST_LEAD_DAYS)
	stages = stock_stages(farm, {v for v, _l in pools}, process_day=process_day)
	out = []
	for (variety, length), pool_lines in pools.items():
		st = stages[(variety, length)]
		# the lines share the stock in sequence: each takes what it still needs from what the lines before it
		# left, so the same stems are never suggested to two lines
		total = flt(st["shelf"]) + flt(st["received"]) + flt(st["quarantine"]) + flt(st["harvested"])
		pool_lines.sort(
			key=lambda ln: (
				str(ln.delivery_date),
				not cint(seq.get(ln.parent)),
				cint(seq.get(ln.parent)),
				ln.parent,
				ln.idx,
			)
		)
		for ln in pool_lines:
			p = planned[ln.name]
			was = flt(p["real"]) + flt(p["expected"])
			want = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
			give = min(want, max(0.0, total))
			size = per_box(ln)
			if give < want - 0.5 and size:
				give = int(give / size + 1e-9) * size
			total -= give
			if only is not None and not only(ln):
				continue
			if abs(give - was) > 0.5 or (keep and keep(ln)):
				out.append(
					{
						"sales_order_item": ln.name,
						"sales_order": ln.parent,
						# grouped as the Sales Order shows them: the straight lines, then each mix by its name
						"kind": "mix" if mix_key(ln) else "straight",
						"mix_name": (ln.custom_mix_name or ln.custom_line or _("Mix")) if mix_key(ln) else "",
						"boxes": cint(ln.custom_number_of_boxes),
						"idx": cint(ln.idx),
						"order_name": names.get(ln.parent) or "",
						"customer": ln.customer_name or ln.customer,
						"sequence": cint(seq.get(ln.parent)),
						"variety": variety,
						"length": length,
						"ordered": flt(ln.ordered_stems),
						"was": was,
						"now": give,
						"max": want,
						"team": plans.team_for_line(ln.name) or "",
						"delivery_date": str(ln.delivery_date),
						"stock": st,
						# what the lines before it in the sequence left of that stock
						"left_for_it": max(0.0, total + give),
					}
				)
	out.sort(
		key=lambda r: (r["delivery_date"], not r["sequence"], r["sequence"], r["sales_order"], r["variety"])
	)
	return out


@frappe.whitelist()
def preview_revision(
	delivery_date: str | None = None,
	farm: str | None = None,
	sales_order: str | None = None,
	week: str | None = None,
	sales_order_item: str | None = None,
):
	"""What revising would change: [{sales_order, variety, length, was, now (suggested), max, ...}]. For one
	order (or one of its lines), every one of its confirmed lines, changed or not. With `week`, every delivery
	day of it."""
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	days = pg._week(week)[1] if week else None
	if days:
		day = days[0]
	mine = None
	if sales_order_item:
		mine = lambda ln: ln.name == sales_order_item  # noqa: E731
	elif sales_order:
		mine = lambda ln: ln.parent == sales_order  # noqa: E731
	changes = _changes(day, farm, mine, days, only=mine)
	if sales_order or sales_order_item:
		changes += _covered_lines(sales_order, sales_order_item, {c["sales_order_item"] for c in changes})
	return {"success": True, "delivery_date": day, "farm": farm, "changes": changes}


def _covered_lines(sales_order, sales_order_item, listed):
	"""The order's confirmed lines the revision does not list because nothing is left to confirm (fully
	covered or already allocated): shown so their team can still be changed. Their stems stay as they are."""
	filters = {"status": ("in", TEAM_STATUSES)}
	if sales_order_item:
		filters["sales_order_item"] = sales_order_item
	else:
		filters["sales_order"] = sales_order
	confs = frappe.get_all(
		"Allocation Confirmation",
		filters=filters,
		fields=["sales_order_item", "stems", "team", "variety", "length", "mix_key", "modified"],
		order_by="modified asc",
	)
	by = {}
	for c in confs:
		if c.sales_order_item in listed:
			continue
		row = by.setdefault(
			c.sales_order_item, {"stems": 0.0, "team": "", "variety": c.variety, "length": c.length}
		)
		row["stems"] += flt(c.stems)
		row["team"] = c.team or row["team"]
	if not by:
		return []
	lines = {
		r.name: r
		for r in frappe.get_all(
			"Sales Order Item",
			filters={"name": ("in", list(by))},
			fields=[
				"name",
				"parent",
				"idx",
				"item_code",
				"custom_length",
				"stock_qty",
				"custom_number_of_boxes",
				"custom_mixed_box",
				"custom_mixed_bunch",
				"custom_mix_group",
				"custom_bunch_group",
				"custom_mix_name",
				"custom_line",
				"delivery_date",
			],
		)
	}
	orders = {
		r.name: r
		for r in frappe.get_all(
			"Sales Order",
			filters={"name": ("in", list({ln.parent for ln in lines.values()}))},
			fields=["name", "customer", "customer_name", "custom_order_name", "custom_allocation_sequence"],
		)
	}
	out = []
	for soi, c in by.items():
		ln = lines.get(soi)
		if not ln:
			continue
		so = orders.get(ln.parent) or frappe._dict()
		mixed = cint(ln.custom_mixed_box) or cint(ln.custom_mixed_bunch)
		out.append(
			{
				"sales_order_item": soi,
				"sales_order": ln.parent,
				"kind": "mix" if mixed else "straight",
				"mix_name": (ln.custom_mix_name or ln.custom_line or _("Mix")) if mixed else "",
				"boxes": cint(ln.custom_number_of_boxes),
				"idx": cint(ln.idx),
				"order_name": so.get("custom_order_name") or "",
				"customer": so.get("customer_name") or so.get("customer") or "",
				"sequence": cint(so.get("custom_allocation_sequence")),
				"variety": c["variety"] or ln.item_code,
				"length": c["length"] or ln.custom_length or "",
				"ordered": flt(ln.stock_qty),
				"was": c["stems"],
				"now": c["stems"],
				"max": c["stems"],
				"team": c["team"],
				"delivery_date": str(ln.delivery_date),
				"stock": {},
				"left_for_it": 0.0,
				"covered": 1,
			}
		)
	return out


@frappe.whitelist(methods=["POST"])
def revise_confirmations(
	delivery_date: str,
	farm: str,
	sales_order_items: str | list | None = None,
	lines: str | list | None = None,
	week: str | None = None,
):
	"""Apply the revision (only these lines when given), then re-number the teams' sequence. `lines`
	([{sales_order_item, stems, team}]) confirms the planner's own amounts, and team, instead of the suggestion. With `week`, every
	delivery day of it; each line stays in its own day's plan."""
	frappe.only_for(pg.PLANNERS)
	pg._changed()
	days = pg._week(week)[1] if week else [pg._day(delivery_date)]
	day = days[0]
	if isinstance(sales_order_items, str):
		sales_order_items = json.loads(sales_order_items or "[]")
	if isinstance(lines, str):
		lines = json.loads(lines or "[]")
	team_only = []
	if lines:
		planned = pg._planned_week(days, farm)
		changes = []
		for row in lines:
			soi = row["sales_order_item"]
			ln = pg._line(soi)
			want = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
			now = min(max(0.0, flt(row.get("stems"))), want)
			p = planned.get(soi, {"real": 0.0, "expected": 0.0})
			was = flt(p["real"]) + flt(p["expected"])
			# the planner can move the line to another team as well (a mixed box moves whole: confirm_lines)
			team_was = plans.team_for_line(soi) or _any_team(soi)
			team = (row.get("team") or "").strip() or team_was
			# a new team on a line whose stems stay (fully confirmed, or nothing left to confirm because it
			# is covered): the line moves to the team as it stands, its stems are not confirmed again
			if (
				team
				and team != team_was
				and (cint(row.get("team_only")) or abs(now - was) <= 0.5 or want <= 0.5)
			):
				team_only.append({"sales_order_item": soi, "team": team, "team_was": team_was})
				continue
			if abs(now - was) > 0.5 or (team != team_was and now > 0.5):
				changes.append({"sales_order_item": soi, "was": was, "now": now, "team": team})
	else:
		changes = [
			c
			for c in _changes(day, farm, days=days)
			if not sales_order_items or c["sales_order_item"] in sales_order_items
		]
	for t in team_only:
		_move_line_team(t["sales_order_item"], t["team"], t["team_was"])
	if not changes and not team_only:
		return {"success": True, "lines": 0}
	if changes:
		pg.confirm_lines(
			None if week else day,
			farm,
			json.dumps(
				[
					{
						"sales_order_item": c["sales_order_item"],
						"stems": c["now"],
						"team": c["team"],
						"basis": "rolling",
						"revised": 1,
					}
					for c in changes
				]
			),
			week=week,
		)
	from upande_packhouse.api.v2.allocation_teams import _queues, _write_rounds

	for d in days:
		plan = pg._plan_name(d, farm)
		if plan:
			_write_rounds(plan, _queues(plan))
	return {
		"success": True,
		"lines": len(changes) + len(team_only),
		"teams": len(team_only),
		"up": sum(max(0.0, c["now"] - c["was"]) for c in changes),
		"down": sum(max(0.0, c["was"] - c["now"]) for c in changes),
	}


#: a line's confirmations a team move carries: the live ones and those already allocated
TEAM_STATUSES = (*plans.LIVE, "Fulfilled")


def _any_team(soi):
	"""The team on any of a line's confirmations, allocated ones included ('' when none)."""
	return (
		frappe.db.get_value(
			"Allocation Confirmation",
			{"sales_order_item": soi, "status": ("in", TEAM_STATUSES), "team": ("!=", "")},
			"team",
			order_by="modified desc",
		)
		or ""
	)


def _move_line_team(soi, team, team_was):
	"""Put a line on another team without confirming its stems again: its confirmations (allocated ones too),
	its Confirmed Stems rows and its stems waiting release. A mixed box is packed by one team, so every line of
	the box moves. The team must pack at the same sales farm: another sales farm's team takes the line to that
	farm's plan, which re-confirming does."""
	from upande_packhouse.allocation_log import log
	from upande_packhouse.api.v2.allocation_teams import team_sales_farms

	if not frappe.db.exists("Packing Teams", team):
		frappe.throw(_("{0} is not a team we know.").format(team))
	home = team_sales_farms()
	if team_was and home.get(team) != home.get(team_was):
		frappe.throw(
			_(
				"{0} packs at {1}, not {2}: change its stems too so the line is confirmed at that farm."
			).format(team, home.get(team) or _("another farm"), home.get(team_was) or _("this farm"))
		)
	key = frappe.db.get_value(
		"Allocation Confirmation",
		{"sales_order_item": soi, "status": ("in", TEAM_STATUSES), "mix_key": ("!=", "")},
		"mix_key",
	)
	sois = (
		frappe.get_all(
			"Allocation Confirmation",
			filters={"mix_key": key, "status": ("in", TEAM_STATUSES)},
			pluck="sales_order_item",
			distinct=True,
		)
		if key
		else []
	) or [soi]
	sois = sorted(set(sois) | {soi})
	frappe.db.sql(
		"""UPDATE `tabAllocation Confirmation` SET team = %(t)s
		   WHERE sales_order_item IN %(s)s AND status IN %(st)s""",
		{"t": team, "s": tuple(sois), "st": TEAM_STATUSES},
	)
	frappe.db.sql(
		"""UPDATE `tabPending Allocation Row` r
		   JOIN `tabPending Allocation` p ON p.name = r.parent AND r.parenttype = 'Pending Allocation'
		   SET r.team = %(t)s
		   WHERE p.status = 'Pending' AND r.sales_order_item IN %(s)s""",
		{"t": team, "s": tuple(sois)},
	)
	meta = frappe.get_meta("Confirmed Stems")
	sets = {"team": team} if meta.has_field("team") else {}
	if meta.has_field("processing_sales_farm") and home.get(team):
		sets["processing_sales_farm"] = home.get(team)
	if sets:
		for name in frappe.get_all(
			"Confirmed Stems",
			filters={"sales_order_item": ("in", sois), "parenttype": "Sales Order"},
			pluck="name",
		):
			frappe.db.set_value("Confirmed Stems", name, sets, update_modified=False)
	from upande_packhouse import quality_b

	for s in sois:
		quality_b.sync_opl_team(s, team)
	for s in sois:
		log(
			"Revised",
			"team {0} -> {1}".format(team_was or "-", team),
			sales_order_item=s,
			team=team,
		)


@frappe.whitelist()
def get_stock_stages(
	delivery_date: str | None = None,
	farm: str | None = None,
	week: str | None = None,
	pairs: str | list | None = None,
):
	"""The stock each variety and length has, by stage, for the Plan and Checks tabs: {"variety|length":
	{harvested, received, quarantine, shelf, total}}. The same stock Revise counts -- as it will be on the
	processing day (the last delivery day less the harvest lead), every farm of the sales farm's location."""
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	days = pg._week(week)[1] if week else [day]
	if isinstance(pairs, str):
		pairs = json.loads(pairs or "[]")
	pairs = [p for p in pairs or [] if p.get("variety")]
	if not pairs:
		return {}
	process_day = getdate(days[-1]) - timedelta(days=plans.HARVEST_LEAD_DAYS)
	stages = stock_stages(farm, {p["variety"] for p in pairs}, process_day=process_day)
	out = {}
	for p in pairs:
		key = (p["variety"], p.get("length") or "")
		s = stages[key] if key in stages else {}
		row = {k: flt(s.get(k)) for k in ("harvested", "received", "quarantine", "shelf")}
		row["total"] = sum(row.values())
		out["{0}|{1}".format(*key)] = row
	return out
