# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Sales Order v2 list + KPIs (www/sales-order-v2.html, list view only).

The editor keeps saving through api/sales_order.py; this module only reads.

Filters (all applied on the server, before any limit -- README rule 3):
  from_date / to_date  Sales Order.delivery_date (rule 4 / 6b)
  status               all (draft + submitted, never cancelled) | draft |
                       submitted | cancelled
  customer             exact Customer
  q                    order number, customer name or PO number (LIKE)
  rose                 all | spray | standard  -- line level (rose.rose_sql)
  team                 Order Pick List.team     -- line level
  region / farm        the ORDER's farm (rule 6 as amended / 6a): COALESCE(so.farm,
                       so.custom_farm, pick list farm) via region.order_farm_sql

Rose and team are line-level: an order is listed when at least one of its
lines matches, and its boxes, stems and value are those of the matching lines
only. Team comes from the line's pick list (pipeline.attach_pipeline: Packing
Guide + Pick List Item, never custom_opl), so lines with no pick list drop out
while a team filter is on. Region / farm are order-level (the whole order is in
or out) and orders without a pick list still count.

Boxes per order = boxes.order_boxes over the counted lines; stems =
units.ordered_stems per line (pipeline.fetch_lines). The stored
custom_total_boxes / custom_total_stems are never read (rule 8, audit SO-2).
KPIs are sums over the full filtered order set, so KPI = sum of the rows
(when not truncated, and always over the full set when truncated).
"""

from collections import defaultdict

import frappe
from frappe.utils import add_days, get_last_day, getdate, nowdate

from upande_packhouse.api.v2.core import boxes as bx
from upande_packhouse.api.v2.core import pipeline, region, rose

ROW_LIMIT = 1000
STATUS_DOCSTATUS = {
	"all": (0, 1),
	"draft": (0,),
	"submitted": (1,),
	"cancelled": (2,),
}


def _arg(args, key):
	v = args.get(key)
	return str(v).strip() if v not in (None, "") else ""


def _orders(args, docstatus, farms=None):
	"""Header rows matching the order-level filters (no limit)."""
	where = ["so.business_unit = 'Roses'", "so.docstatus IN %(ds)s"]
	params = {"ds": docstatus}
	if _arg(args, "from_date"):
		where.append("so.delivery_date >= %(from_date)s")
		params["from_date"] = _arg(args, "from_date")
	if _arg(args, "to_date"):
		where.append("so.delivery_date <= %(to_date)s")
		params["to_date"] = _arg(args, "to_date")
	if _arg(args, "customer"):
		where.append("so.customer = %(customer)s")
		params["customer"] = _arg(args, "customer")
	if _arg(args, "q"):
		where.append("(so.name LIKE %(q)s OR so.customer_name LIKE %(q)s OR IFNULL(so.po_no, '') LIKE %(q)s)")
		params["q"] = "%" + _arg(args, "q") + "%"
	if farms is not None:
		where.append("{0} IN %(farms)s".format(region.order_farm_sql("so", "opl")))
		params["farms"] = region.sql_tuple(farms)
	return frappe.db.sql(
		"""SELECT so.name, so.customer, so.customer_name, so.transaction_date, so.delivery_date,
		          so.currency, so.status, so.docstatus, so.po_no, so.modified,
		          {1} AS order_farm
		   FROM `tabSales Order` so
		   LEFT JOIN (SELECT sales_order, MIN(NULLIF(farm, '')) AS farm FROM `tabOrder Pick List`
		              WHERE docstatus < 2 GROUP BY sales_order) opl ON opl.sales_order = so.name
		   WHERE {0}
		   ORDER BY so.modified DESC""".format(
			" AND ".join(where), region.order_farm_sql("so", "opl")
		),  # nosemgrep: fixed SQL, values bound
		params,
		as_dict=True,
	)


@frappe.whitelist()
def get_sales_orders(**kwargs):
	"""Sales Order list rows + KPIs for the v2 list view. Read only."""
	try:
		args = frappe._dict(kwargs)
		status = (_arg(args, "status") or "all").lower()
		docstatus = STATUS_DOCSTATUS.get(status, STATUS_DOCSTATUS["all"])
		team = _arg(args, "team")
		rose_kind = rose.normalize(_arg(args, "rose"))
		farms = region.farms_for(region=_arg(args, "region"), farm=_arg(args, "farm"))
		line_filtered = bool(team) or rose_kind != "all"

		headers = _orders(args, docstatus, farms)
		names = [h.name for h in headers]

		params = {}
		rose_where = rose.rose_sql("soi.item_group", rose_kind, params)
		lines = (
			pipeline.fetch_lines(
				sales_orders=names, docstatus=docstatus, extra_where=rose_where, params=params
			)
			if names
			else []
		)
		opls = pipeline.attach_pipeline(lines) if lines else {}

		kept = []
		for ln in lines:
			if team and team not in {opls[o].get("team") for o in ln.opls if o in opls}:
				continue
			kept.append(ln)

		by_so = defaultdict(list)
		for ln in kept:
			by_so[ln.parent].append(ln)

		rows = []
		for h in headers:
			ls = by_so.get(h.name, [])
			if line_filtered and not ls:
				continue
			pick_teams = sorted({opls[o].get("team") for ln in ls for o in ln.opls if o in opls} - {None, ""})
			rows.append(
				{
					"name": h.name,
					"customer": h.customer,
					"customer_name": h.customer_name,
					"transaction_date": h.transaction_date,
					"delivery_date": h.delivery_date,
					"currency": h.currency,
					"status": h.status,
					"docstatus": h.docstatus,
					"po_no": h.po_no,
					"farm": h.order_farm or "",
					"team": ", ".join(pick_teams),
					"lines": len(ls),
					"boxes": bx.order_boxes(ls),
					"stems": sum(ln.ordered_stems for ln in ls),
					"value": round(sum(float(ln.amount or 0) for ln in ls), 2),
					"modified": h.modified,
				}
			)

		def tally(rs):
			return {
				"orders": len(rs),
				"boxes": sum(r["boxes"] for r in rs),
				"stems": sum(r["stems"] for r in rs),
			}

		kpis = tally(rows)
		drafts = [r for r in rows if r["docstatus"] == 0]
		kpis["drafts"] = len(drafts)
		kpis["draft_boxes"] = sum(r["boxes"] for r in drafts)
		kpis["draft_stems"] = sum(r["stems"] for r in drafts)
		kpis["submitted"] = sum(1 for r in rows if r["docstatus"] == 1)

		teams = frappe.db.sql_list(
			"""SELECT DISTINCT team FROM `tabOrder Pick List`
			   WHERE docstatus < 2 AND IFNULL(team, '') != '' ORDER BY team"""
		)
		return {
			"success": True,
			"orders": rows[:ROW_LIMIT],
			"total": len(rows),
			"truncated": len(rows) > ROW_LIMIT,
			"kpis": kpis,
			"teams": teams,
			"line_filtered": line_filtered,
		}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="v2 get_sales_orders error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Standing orders (Frappe Auto Repeat on a Sales Order) -- calendar view
# ---------------------------------------------------------------------------
# A standing order is an Auto Repeat whose reference_document is a Sales Order.
# Frappe copies that order on every scheduled date and stamps the copy with
# `auto_repeat`, setting its (required) Date fields -- delivery_date included --
# to the schedule date. So the page's one date meaning (delivery_date, rule 4)
# is also the date an occurrence belongs to.
#
#   past / today  -> real Sales Orders with auto_repeat = <Auto Repeat> (kind
#                    "generated"), plus the source order itself (kind "source").
#   future        -> occurrences computed with Frappe's OWN schedule rule,
#                    AutoRepeat.get_next_schedule_date(for_full_schedule=True)
#                    (kind "scheduled"): start/end dates, weekday list, day of
#                    month and last-day rules all come from Frappe. Disabled
#                    repeats produce none; nothing past end_date.
# Boxes / stems come from the shared core on the order's Roses lines; a
# scheduled occurrence carries the source order's figures (what Frappe will
# copy). Reads only.

SCHEDULE_CAP = 5000  # iterations per Auto Repeat (daily repeats since an old start)


def _ar_status(ar, today):
	if ar.disabled:
		return "Disabled"
	if ar.end_date and getdate(ar.end_date) < today:
		return "Completed"
	return "Active"


def occurrence_dates(ar_doc, window_from, window_to):
	"""Schedule dates of an Auto Repeat inside [window_from, window_to], by
	Frappe's own get_next_schedule_date (full schedule, so no skipping to
	today). The start date itself is the source order's own date, not an
	occurrence. Pure with respect to the database."""
	start = getdate(ar_doc.start_date) if ar_doc.start_date else None
	if not start or not ar_doc.frequency:
		return []
	end = getdate(ar_doc.end_date) if ar_doc.end_date else None
	limit = min(getdate(window_to), end) if end else getdate(window_to)
	out, cur = [], start
	for _ in range(SCHEDULE_CAP):
		nxt = ar_doc.get_next_schedule_date(schedule_date=cur, for_full_schedule=True)
		nxt = getdate(nxt)
		if nxt <= cur or nxt > limit:
			break
		if nxt >= getdate(window_from):
			out.append(nxt)
		cur = nxt
	return out


def _repeats(args, farms):
	where = ["ar.reference_doctype = 'Sales Order'", "so.business_unit = 'Roses'", "so.docstatus < 2"]
	params = {}
	if _arg(args, "customer"):
		where.append("so.customer = %(customer)s")
		params["customer"] = _arg(args, "customer")
	if farms is not None:
		where.append("{0} IN %(farms)s".format(region.order_farm_sql("so", "opl")))
		params["farms"] = region.sql_tuple(farms)
	return frappe.db.sql(
		"""SELECT ar.name, ar.reference_document, ar.frequency, ar.start_date, ar.end_date,
		          ar.next_schedule_date, ar.disabled, ar.status AS stored_status,
		          so.customer, so.customer_name, so.docstatus AS source_docstatus,
		          so.delivery_date AS source_delivery_date, {1} AS farm
		   FROM `tabAuto Repeat` ar
		   INNER JOIN `tabSales Order` so ON so.name = ar.reference_document
		   LEFT JOIN (SELECT sales_order, MIN(NULLIF(farm, '')) AS farm FROM `tabOrder Pick List`
		              WHERE docstatus < 2 GROUP BY sales_order) opl ON opl.sales_order = so.name
		   WHERE {0}
		   ORDER BY so.customer_name, ar.name""".format(
			" AND ".join(where), region.order_farm_sql("so", "opl")
		),  # nosemgrep: fixed SQL, values bound
		params,
		as_dict=True,
	)


def _order_figures(names):
	"""{order: (boxes, stems)} over each order's Roses lines (core definitions)."""
	if not names:
		return {}
	lines = pipeline.fetch_lines(sales_orders=list(names), docstatus=(0, 1))
	by_so = defaultdict(list)
	for ln in lines:
		by_so[ln.parent].append(ln)
	return {n: (bx.order_boxes(by_so.get(n, [])), sum(l.ordered_stems for l in by_so.get(n, []))) for n in names}


@frappe.whitelist()
def get_standing_orders(**kwargs):
	"""Standing orders (Auto Repeat on Sales Orders) for the calendar. Read only.

	Args: from_date / to_date (the visible range, default = this month),
	customer, region / farm (the source order's farm), state (all | active |
	disabled), kpi_from / kpi_to (range of the "generated" KPI, default = the
	visible range; the calendar grid can show days of the neighbouring months).  Returns `events` (one per order-or-occurrence on a date),
	`standing` (one per Auto Repeat) and KPIs that follow every filter.
	"""
	try:
		frappe.has_permission("Auto Repeat", "read", throw=True)
		args = frappe._dict(kwargs)
		today = getdate(nowdate())
		first = today.replace(day=1)
		w_from = getdate(_arg(args, "from_date")) if _arg(args, "from_date") else first
		w_to = getdate(_arg(args, "to_date")) if _arg(args, "to_date") else get_last_day(first)
		state = (_arg(args, "state") or "all").lower()
		farms = region.farms_for(region=_arg(args, "region"), farm=_arg(args, "farm"))

		repeats = _repeats(args, farms)
		for r in repeats:
			r["status"] = _ar_status(r, today)
		if state == "active":
			repeats = [r for r in repeats if r["status"] == "Active"]
		elif state == "disabled":
			repeats = [r for r in repeats if r["status"] == "Disabled"]
		by_ar = {r.name: r for r in repeats}
		week_to = add_days(today, 6)

		generated = (
			frappe.db.sql(
				"""SELECT name, auto_repeat, customer, customer_name, delivery_date, docstatus
				   FROM `tabSales Order`
				   WHERE auto_repeat IN %(ars)s AND docstatus IN (0, 1) AND business_unit = 'Roses'
				     AND delivery_date BETWEEN %(f)s AND %(t)s
				   ORDER BY delivery_date, name""",
				{"ars": tuple(by_ar), "f": w_from, "t": w_to},
				as_dict=True,
			)
			if by_ar
			else []
		)
		# every generated order of these repeats (any date): which dates are already taken
		taken = defaultdict(set)
		if by_ar:
			for o in frappe.db.sql(
				"""SELECT auto_repeat, delivery_date FROM `tabSales Order`
				   WHERE auto_repeat IN %(ars)s AND docstatus IN (0, 1)""",
				{"ars": tuple(by_ar)},
				as_dict=True,
			):
				taken[o.auto_repeat].add(getdate(o.delivery_date))

		sources_in_window = [
			r for r in repeats if r.source_delivery_date and w_from <= getdate(r.source_delivery_date) <= w_to
		]
		fig = _order_figures(
			{g.name for g in generated} | {r.reference_document for r in repeats}
		)

		events, due_week = [], 0
		for g in generated:
			ar = by_ar[g.auto_repeat]
			if g.name == ar.reference_document:
				continue  # shown once, as the source
			b, s = fig.get(g.name, (0, 0))
			events.append(
				{
					"date": g.delivery_date, "kind": "generated", "order": g.name,
					"auto_repeat": ar.name, "source": ar.reference_document,
					"customer": g.customer, "customer_name": g.customer_name,
					"frequency": ar.frequency, "status": ar.status, "next_date": ar.next_schedule_date,
					"farm": ar.farm or "", "docstatus": g.docstatus, "boxes": b, "stems": s,
				}
			)
		for ar in sources_in_window:
			b, s = fig.get(ar.reference_document, (0, 0))
			events.append(
				{
					"date": ar.source_delivery_date, "kind": "source", "order": ar.reference_document,
					"auto_repeat": ar.name, "source": ar.reference_document,
					"customer": ar.customer, "customer_name": ar.customer_name,
					"frequency": ar.frequency, "status": ar.status, "next_date": ar.next_schedule_date,
					"farm": ar.farm or "", "docstatus": ar.source_docstatus, "boxes": b, "stems": s,
				}
			)

		standing = []
		for ar in repeats:
			b, s = fig.get(ar.reference_document, (0, 0))
			if ar.status == "Active":
				doc = frappe.get_doc("Auto Repeat", ar.name)
				for d in occurrence_dates(doc, today, max(w_to, week_to)):
					if d < today or d in taken[ar.name] or d == getdate(ar.source_delivery_date or "1900-01-01"):
						continue
					if d <= week_to:
						due_week += 1
					if w_from <= d <= w_to:
						events.append(
							{
								"date": d, "kind": "scheduled", "order": None,
								"auto_repeat": ar.name, "source": ar.reference_document,
								"customer": ar.customer, "customer_name": ar.customer_name,
								"frequency": ar.frequency, "status": ar.status,
								"next_date": ar.next_schedule_date, "farm": ar.farm or "",
								"docstatus": None, "boxes": b, "stems": s,
							}
						)
			standing.append(
				{
					"name": ar.name, "source": ar.reference_document, "customer": ar.customer,
					"customer_name": ar.customer_name, "frequency": ar.frequency,
					"start_date": ar.start_date, "end_date": ar.end_date,
					"next_date": ar.next_schedule_date, "status": ar.status, "farm": ar.farm or "",
					"boxes": b, "stems": s,
				}
			)

		events.sort(key=lambda e: (str(e["date"]), e["customer_name"] or "", e["order"] or ""))
		k_from = getdate(_arg(args, "kpi_from")) if _arg(args, "kpi_from") else w_from
		k_to = getdate(_arg(args, "kpi_to")) if _arg(args, "kpi_to") else w_to
		gen = [e for e in events if e["kind"] == "generated" and k_from <= getdate(e["date"]) <= k_to]
		kpis = {
			"active": sum(1 for r in repeats if r["status"] == "Active"),
			"due_week": due_week,
			"generated": len(gen),
			"generated_boxes": sum(e["boxes"] for e in gen),
			"generated_stems": sum(e["stems"] for e in gen),
			"disabled": sum(1 for r in repeats if r["status"] == "Disabled"),
			"total": len(repeats),
		}
		return {
			"success": True,
			"from_date": w_from, "to_date": w_to, "today": today,
			"events": events, "standing": standing, "kpis": kpis,
		}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="v2 get_standing_orders error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}
