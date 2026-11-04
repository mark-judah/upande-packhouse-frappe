# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Planning v2: revise the day's confirmations to what is really there.

Each variety and length's confirmed lines are given, in the day's sequence (Sales Order allocation sequence,
then line), what net stock -- free on the shelf, received, and what the plan already holds for these lines --
plus the rolling forecast of the cutting day can cover, in whole boxes (a line's last part box only when it
completes the line). Teams stay as they are; a line the stock cannot reach at all leaves the plan.
"""

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, today

from upande_packhouse import allocation_plan as plans
from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.api.v2.allocation_checks import per_box
from upande_packhouse.api.v2.cold_room import ONWARD_TYPES, RECEIPT_TYPES
from upande_packhouse.api.v2.core import received, stock

#: Entries of a bucket that is still on its way to the receiving cold store.
CUT = ("Harvesting", "Grading")
CYCLE = (
	("Harvesting", "Grading", "Receiving Quarantined", "Quarantine Accept", "Quarantine Rejects")
	+ RECEIPT_TYPES
	+ ONWARD_TYPES
)


def stock_stages(farm, varieties, places=None):
	"""{(variety, length): {shelf, received, quarantine, harvested}} in stems that no pick list uses: free on
	the shelf (plan holds included), received and not shelved, received into quarantine and not released, and
	harvested with no receiving since (graded or not), within the discard age. `places`, a dict, also gets
	where the stock not on the shelf is: {(variety, length): {(stage, farm, warehouse): [stems, {buckets}]}}."""

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
	for r in stock.bucket_rows(farms=farms, varieties=vs):
		out[(r.variety, r.stem_length or "")]["shelf"] += flt(r.allocatable) + flt(r.reserved)
	for r in received.received_unshelved(farms=farms, varieties=vs):
		out[(r.variety, r.stem_length or "")]["received"] += flt(r.stems)
		place((r.variety, r.stem_length or ""), "received", r.farm, r.cold_room, r.stems, r.bucket_id)
	since = add_days(today(), -min(cint(stock._config()["discard_age"]) or 7, 14))
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
		{"types": CYCLE, "since": since, "farms": tuple(farms)},
		as_dict=True,
	)
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


def _changes(day, farm, keep=None):
	"""Lines whose confirmation the revision would change; `keep(line)` also lists lines that stay the same."""
	planned = pg._planned_by_line(day, farm)
	lines = [ln for ln in pg._lines_for(day, farm) if ln.name in planned]
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
	stock = pg._stock_by_pool(farm, {v for v, _l in pools}, day)
	rolling = pg._rolling_by_pool(day, farm, {v for v, _l in pools})
	stages = stock_stages(farm, {v for v, _l in pools})
	out = []
	for (variety, length), pool_lines in pools.items():
		sup = {**pg.SUPPLY, **stock.get((variety, length), {})}
		held_here = sum(flt(planned[ln.name]["real"]) for ln in pool_lines)
		budget = (
			flt(sup["free"])
			+ flt(sup["received"])
			+ held_here
			+ pg.cut_day_total(sup, rolling.get((variety, length), 0.0))
		)
		pool_lines.sort(
			key=lambda ln: (not cint(seq.get(ln.parent)), cint(seq.get(ln.parent)), ln.parent, ln.idx)
		)
		for ln in pool_lines:
			p = planned[ln.name]
			was = flt(p["real"]) + flt(p["expected"])
			want = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
			give = min(want, max(0.0, budget))
			size = per_box(ln)
			if give < want - 0.5 and size:
				give = int(give / size + 1e-9) * size
			budget -= give
			if abs(give - was) > 0.5 or (keep and keep(ln)):
				out.append(
					{
						"sales_order_item": ln.name,
						"sales_order": ln.parent,
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
						"stock": stages[(variety, length)],
					}
				)
	out.sort(key=lambda r: (not r["sequence"], r["sequence"], r["sales_order"], r["variety"]))
	return out


@frappe.whitelist()
def preview_revision(
	delivery_date: str | None = None, farm: str | None = None, sales_order: str | None = None
):
	"""What revising would change: [{sales_order, variety, length, was, now (suggested), max, ...}]. For one
	order, every one of its confirmed lines, changed or not."""
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	changes = _changes(day, farm, (lambda ln: ln.parent == sales_order) if sales_order else None)
	if sales_order:
		changes = [c for c in changes if c["sales_order"] == sales_order]
	return {"success": True, "delivery_date": day, "farm": farm, "changes": changes}


@frappe.whitelist(methods=["POST"])
def revise_confirmations(
	delivery_date: str,
	farm: str,
	sales_order_items: str | list | None = None,
	lines: str | list | None = None,
):
	"""Apply the revision (only these lines when given), then re-number the teams' sequence. `lines`
	([{sales_order_item, stems}]) confirms the planner's own amounts instead of the suggestion."""
	frappe.only_for(pg.PLANNERS)
	day = pg._day(delivery_date)
	if isinstance(sales_order_items, str):
		sales_order_items = json.loads(sales_order_items or "[]")
	if isinstance(lines, str):
		lines = json.loads(lines or "[]")
	if lines:
		planned = pg._planned_by_line(day, farm)
		changes = []
		for row in lines:
			soi = row["sales_order_item"]
			ln = pg._line(soi)
			want = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
			now = min(max(0.0, flt(row.get("stems"))), want)
			p = planned.get(soi, {"real": 0.0, "expected": 0.0})
			was = flt(p["real"]) + flt(p["expected"])
			if abs(now - was) > 0.5:
				changes.append(
					{"sales_order_item": soi, "was": was, "now": now, "team": plans.team_for_line(soi) or ""}
				)
	else:
		changes = [
			c
			for c in _changes(day, farm)
			if not sales_order_items or c["sales_order_item"] in sales_order_items
		]
	if not changes:
		return {"success": True, "lines": 0}
	pg.confirm_lines(
		day,
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
	)
	plan = pg._plan_name(day, farm)
	if plan:
		from upande_packhouse.api.v2.allocation_teams import _queues, _write_rounds

		_write_rounds(plan, _queues(plan))
	return {
		"success": True,
		"lines": len(changes),
		"up": sum(max(0.0, c["now"] - c["was"]) for c in changes),
		"down": sum(max(0.0, c["was"] - c["now"]) for c in changes),
	}
