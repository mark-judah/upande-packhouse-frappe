# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Quality B orders for Allocation Planning's Orders filter: the webstore orders of a week or a delivery day.

They are never planned on the board (their pick lists come from Grade B buckets when they are submitted,
upande_packhouse.quality_b), so this lists them as they stand: sales farm, teams, what was ordered, what is on
their pick lists and issued, and the Grade B buckets still at a remote farm.
"""

from collections import defaultdict

import frappe
from frappe.utils import flt

from upande_packhouse import quality_b
from upande_packhouse.api.v2.allocation_playground import _week

FARM_SQL = (
	"COALESCE(NULLIF(pli.farm, ''), NULLIF(SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse, ''),"
	" pli.warehouse), ' ', 1), ''))"
)


@frappe.whitelist()
def get_quality_b_orders(week: str | None = None, delivery_date: str | None = None, farm: str | None = None):
	if delivery_date:
		days = [str(delivery_date)]
	else:
		week, days = _week(week)
	cond, params = "", {"f": days[0], "t": days[-1], "ot": quality_b.ORDER_TYPE}
	has_farm = frappe.db.has_column("Sales Order", quality_b.PROCESSING_FIELD)
	if farm and has_farm:
		cond = " AND so.custom_processing_sales_farm = %(farm)s"
		params["farm"] = farm
	orders = frappe.db.sql(
		"""SELECT so.name, so.customer, so.customer_name, so.custom_order_name AS order_name, so.delivery_date,
		          so.status, """
		+ ("so.custom_processing_sales_farm" if has_farm else "''")
		+ """ AS sales_farm,
		          (SELECT SUM(soi.stock_qty) FROM `tabSales Order Item` soi WHERE soi.parent = so.name) AS ordered
		FROM `tabSales Order` so
		WHERE so.docstatus = 1 AND so.order_type = %(ot)s AND so.delivery_date BETWEEN %(f)s AND %(t)s"""
		+ cond
		+ " ORDER BY so.delivery_date, so.name",
		params,
		as_dict=True,
	)
	by_so = {o.name: o for o in orders}
	for o in orders:
		o.update(teams=set(), opls=[], picked=0.0, issued=0.0, at_farm=defaultdict(set), on_road=set())
	if by_so:
		for r in frappe.db.sql(
			"""SELECT opl.name AS opl, opl.sales_order, opl.team, pli.stock_qty, pli.issued, pli.bucket,
			          pli.awaiting_transfer, pli.loaded_in_trolley, pli.in_transit, pli.shelved, """
			+ FARM_SQL
			+ """ AS farm
			FROM `tabOrder Pick List` opl
			JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
			WHERE opl.sales_order IN %(s)s AND opl.docstatus < 2""",
			{"s": tuple(by_so)},
			as_dict=True,
		):
			o = by_so[r.sales_order]
			if r.team:
				o.teams.add(r.team)
			if r.opl not in o.opls:
				o.opls.append(r.opl)
			o.picked += flt(r.stock_qty)
			if r.issued:
				o.issued += flt(r.stock_qty)
			waiting = r.awaiting_transfer and not (
				r.loaded_in_trolley or r.in_transit or r.shelved or r.issued
			)
			bucket = (r.bucket or "").upper()
			if waiting and r.farm and r.farm != o.sales_farm:
				o.at_farm[r.farm].add(bucket)
			elif r.in_transit and not r.shelved:
				o.on_road.add(bucket)
	out = []
	for o in orders:
		out.append(
			{
				"sales_order": o.name,
				"customer": o.customer_name or o.customer,
				"order_name": o.order_name or "",
				"delivery_date": str(o.delivery_date),
				"status": o.status,
				"sales_farm": o.sales_farm or "",
				"teams": sorted(o.teams),
				"opls": o.opls,
				"ordered": flt(o.ordered),
				"picked": o.picked,
				"issued": o.issued,
				"at_farm": [{"farm": f, "buckets": len(b)} for f, b in sorted(o.at_farm.items())],
				"on_road": len(o.on_road),
			}
		)
	return {"success": True, "week": week, "days": days, "orders": out}
