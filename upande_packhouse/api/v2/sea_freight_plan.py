# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Sea freight on Allocation Planning: each line of a sea freight order planned to the greenhouses that grow
its variety, for the production team to harvest.

A sea freight order is harvested for the order (the production app tags each harvest with the line), never
allocated from the shelf. Here every line gets its greenhouses -- by default split in proportion to each
greenhouse's production forecast from this week to the week before delivery (by planted area when none is
forecast) -- and the planner may change the stems, drop a greenhouse or add another growing the variety. The
plan is a Sea Freight Greenhouse Plan per line; the production app shows it and asks before harvesting a line
in a greenhouse that is not on it.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, today

from upande_packhouse.api.v2.allocation_playground import _week

SEA_FREIGHT = "Sea Freight"
CLOSED = ("Completed", "Closed", "Cancelled")
PLAN = "Sea Freight Greenhouse Plan"


def _growing(varieties):
	"""variety -> [{name, label, farm, area}]: the greenhouses with a running crop cycle of it -- the same list
	the production app harvests a sea freight line from."""
	try:
		from upande_agriculture.mobile.api import greenhouses_growing
	except ImportError:
		return {}
	return greenhouses_growing(list(varieties))


def _weeks(until):
	"""The (iso year, week) pairs from this week to the week of `until`."""
	d, end, out = getdate(today()), getdate(until), []
	if end < d:
		end = d
	while d <= end + timedelta(days=6 - end.weekday()):
		y, w, _wd = d.isocalendar()
		if (y, w) not in out:
			out.append((y, w))
		d = d + timedelta(days=7)
	return out


def _forecast(varieties, greenhouses, until):
	"""{(variety, greenhouse): stems} of the production forecast from this week to `until`."""
	weeks = _weeks(until)
	if not (varieties and greenhouses and weeks):
		return {}
	conds = " OR ".join("(w.iso_year = %d AND w.week_number = %d)" % (int(y), int(n)) for y, n in weeks)
	out = defaultdict(float)
	for r in frappe.db.sql(
		"""SELECT f.variety, f.greenhouse, SUM(IFNULL(w.revised_forecast_stems, 0)) AS stems
		FROM `tabProduction Forecast Week` w
		JOIN `tabProduction Forecast` f ON f.name = w.parent
		WHERE f.variety IN %(v)s AND f.greenhouse IN %(g)s AND ("""
		+ conds
		+ """)
		GROUP BY f.variety, f.greenhouse""",
		{"v": tuple(varieties), "g": tuple(greenhouses)},
		as_dict=True,
	):
		out[(r.variety, r.greenhouse)] += flt(r.stems)
	return out


def _harvested(so_items):
	"""{(so item, greenhouse): stems} harvested for these sea freight lines."""
	if not so_items or not frappe.db.has_column("Stock Entry", "custom_sea_freight_item"):
		return {}
	gh = "se.custom_greenhouse" if frappe.db.has_column("Stock Entry", "custom_greenhouse") else "''"
	return {
		(r.so_item, r.greenhouse or ""): flt(r.stems)
		for r in frappe.db.sql(
			"""SELECT se.custom_sea_freight_item AS so_item, """
			+ gh
			+ """ AS greenhouse, SUM(sed.qty) AS stems
			FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
			WHERE se.docstatus = 1 AND se.stock_entry_type = 'Harvesting'
			  AND se.custom_sea_freight_item IN %(i)s
			GROUP BY se.custom_sea_freight_item, greenhouse""",
			{"i": tuple(so_items)},
			as_dict=True,
		)
	}


def _split(stems, weights):
	"""`stems` shared out in proportion to `weights` ({key: weight}) in whole stems, the rest to the largest."""
	total = sum(weights.values())
	if stems <= 0 or not weights:
		return {k: 0 for k in weights}
	if total <= 0:
		weights = {k: 1.0 for k in weights}
		total = float(len(weights))
	out = {k: int(stems * w / total) for k, w in weights.items()}
	rest = int(stems) - sum(out.values())
	for k in sorted(weights, key=lambda k: -weights[k])[: max(0, rest)]:
		out[k] += 1
	return out


def suggest(variety, ordered, houses, forecast):
	"""{greenhouse: stems} for a line: by forecast, else by planted area, else evenly."""
	by_forecast = {h["name"]: forecast.get((variety, h["name"]), 0.0) for h in houses}
	if sum(by_forecast.values()) > 0:
		weights = {k: v for k, v in by_forecast.items() if v > 0}
	else:
		weights = {h["name"]: flt(h.get("area")) for h in houses}
	return _split(int(ordered), weights)


@frappe.whitelist()
def get_sea_freight_plan(week: str | None = None, delivery_date: str | None = None):
	"""The sea freight orders delivering in the week (or on the day), each line with its greenhouses: forecast,
	planned stems (saved, else suggested) and harvested so far."""
	if delivery_date:
		days = [str(getdate(delivery_date))]
	else:
		week, days = _week(week)
	if not frappe.db.has_column("Sales Order", "custom_mode_of_transport"):
		return {"success": True, "week": week, "days": days, "orders": []}
	orders = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"custom_mode_of_transport": SEA_FREIGHT,
			"status": ["not in", list(CLOSED)],
			"delivery_date": ["between", [days[0], days[-1]]],
		},
		fields=["name", "customer", "customer_name", "delivery_date", "custom_order_name"],
		order_by="delivery_date asc, name asc",
	)
	if not orders:
		return {"success": True, "week": week, "days": days, "orders": []}
	lines = frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", [o.name for o in orders]]},
		fields=[
			"name",
			"parent",
			"item_code",
			"item_name",
			"custom_length",
			"stock_qty",
			"custom_total_stems",
		],
		order_by="parent asc, idx asc",
	)
	growing = _growing({ln.item_code for ln in lines})
	houses = {h["name"] for hs in growing.values() for h in hs}
	until = add_days(max(o.delivery_date for o in orders), -1)
	forecast = _forecast({ln.item_code for ln in lines}, houses, until)
	harvested = _harvested([ln.name for ln in lines])
	saved = {}
	for p in frappe.get_all(
		PLAN, filters={"sales_order_item": ["in", [ln.name for ln in lines]]}, pluck="name"
	):
		doc = frappe.get_doc(PLAN, p)
		saved[doc.sales_order_item] = {r.greenhouse: flt(r.stems) for r in doc.greenhouses}
	by_order = defaultdict(list)
	for ln in lines:
		ordered = flt(ln.custom_total_stems) or flt(ln.stock_qty)
		hs = growing.get(ln.item_code, [])
		plan = saved.get(ln.name)
		stems = plan if plan is not None else suggest(ln.item_code, ordered, hs, forecast)
		known = {h["name"] for h in hs}
		# a greenhouse kept on a saved plan whose crop cycle has since ended stays listed
		extra = [{"name": g, "label": g, "farm": "", "area": 0.0} for g in (plan or {}) if g not in known]
		rows = [
			{
				"greenhouse": h["name"],
				"label": h["label"],
				"farm": h["farm"],
				"forecast": forecast.get((ln.item_code, h["name"]), 0.0),
				"stems": flt(stems.get(h["name"], 0)),
				"harvested": harvested.get((ln.name, h["name"]), 0.0),
			}
			for h in hs + extra
		]
		done = sum(v for (soi, _g), v in harvested.items() if soi == ln.name)
		by_order[ln.parent].append(
			{
				"sales_order_item": ln.name,
				"variety": ln.item_code,
				"item_name": ln.item_name or ln.item_code,
				"stem_length": ln.custom_length or "",
				"ordered": ordered,
				"planned": sum(r["stems"] for r in rows),
				"harvested": done,
				"saved": plan is not None,
				"rows": rows,
			}
		)
	out = [
		{
			"sales_order": o.name,
			"order_name": o.custom_order_name or "",
			"customer": o.customer_name or o.customer,
			"delivery_date": str(o.delivery_date),
			"lines": by_order.get(o.name, []),
		}
		for o in orders
	]
	return {"success": True, "week": week, "days": days, "orders": out}


@frappe.whitelist(methods=["POST"])
def save_sea_freight_plan(sales_order_item: str, rows: str | list):
	"""Save a sea freight line's greenhouses: rows [{greenhouse, stems}]; greenhouses with no stems are dropped."""
	rows = frappe.parse_json(rows) if isinstance(rows, str) else rows
	line = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		["parent", "item_code", "custom_length", "stock_qty", "custom_total_stems"],
		as_dict=True,
	)
	if not line:
		frappe.throw(_("{0} is not an order line.").format(sales_order_item))
	so = frappe.db.get_value(
		"Sales Order", line.parent, ["docstatus", "custom_mode_of_transport", "delivery_date"], as_dict=True
	)
	if not so or so.docstatus != 1 or so.custom_mode_of_transport != SEA_FREIGHT:
		frappe.throw(_("{0} is not a submitted sea freight order.").format(line.parent))
	houses = {h["name"]: h for h in _growing([line.item_code]).get(line.item_code, [])}
	keep = []
	for r in rows or []:
		gh, stems = (r.get("greenhouse") or "").strip(), flt(r.get("stems"))
		if not gh or stems <= 0:
			continue
		if gh not in houses:
			frappe.throw(_("{0} has no running crop cycle of {1}.").format(gh, line.item_code))
		keep.append({"greenhouse": gh, "farm": houses[gh]["farm"] or None, "stems": stems})
	name = frappe.db.get_value(PLAN, {"sales_order_item": sales_order_item})
	doc = frappe.get_doc(PLAN, name) if name else frappe.new_doc(PLAN)
	doc.update(
		{
			"sales_order": line.parent,
			"sales_order_item": sales_order_item,
			"delivery_date": so.delivery_date,
			"variety": line.item_code,
			"stem_length": line.custom_length or "",
			"ordered_stems": flt(line.custom_total_stems) or flt(line.stock_qty),
			"planned_stems": sum(r["stems"] for r in keep),
		}
	)
	doc.set("greenhouses", keep)
	if name:
		doc.save()
	else:
		doc.insert()
	return {"success": True, "planned": doc.planned_stems, "greenhouses": len(keep)}
