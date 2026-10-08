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
