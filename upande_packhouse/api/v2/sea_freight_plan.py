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


def line_greenhouses(sois):
	"""{so item: {"greenhouses": [{name, label, farm, forecast}], "greenhouse": the default}} for sea freight
	lines: the greenhouses growing the line's variety, the default being the one its saved plan cuts most from,
	else the one with the most forecast to delivery (then the most planted area)."""
	sois = [s for s in dict.fromkeys(sois or ()) if s]
	if not sois:
		return {}
	lines = frappe.db.sql(
		"""SELECT soi.name, soi.item_code, so.delivery_date FROM `tabSales Order Item` soi
		JOIN `tabSales Order` so ON so.name = soi.parent WHERE soi.name IN %(s)s""",
		{"s": tuple(sois)},
		as_dict=True,
	)
	if not lines:
		return {}
	growing = _growing({ln.item_code for ln in lines})
	houses = {h["name"] for hs in growing.values() for h in hs}
	forecast = _forecast(
		{ln.item_code for ln in lines}, houses, add_days(max(ln.delivery_date for ln in lines), -1)
	)
	planned = {}
	if frappe.db.table_exists(PLAN):
		for r in frappe.db.sql(
			"""SELECT p.sales_order_item AS soi, r.greenhouse FROM `tabSea Freight Greenhouse Plan` p
			JOIN `tabSea Freight Greenhouse Plan Row` r ON r.parent = p.name
			WHERE p.sales_order_item IN %(s)s ORDER BY r.stems DESC""",
			{"s": tuple(sois)},
			as_dict=True,
		):
			planned.setdefault(r.soi, r.greenhouse)
	out = {}
	for ln in lines:
		hs = growing.get(ln.item_code, [])
		opts = [
			{
				"name": h["name"],
				"label": h["label"],
				"farm": h["farm"],
				"forecast": forecast.get((ln.item_code, h["name"]), 0.0),
			}
			for h in hs
		]
		best = max(
			hs, key=lambda h: (forecast.get((ln.item_code, h["name"]), 0.0), flt(h.get("area"))), default=None
		)
		default = planned.get(ln.name) if planned.get(ln.name) in {h["name"] for h in hs} else None
		out[ln.name] = {"greenhouses": opts, "greenhouse": default or (best["name"] if best else "")}
	return out


def with_greenhouses(fn):
	"""Allocation Planning endpoints under the Orders filter's Sea freight: every line ("soi") they return
	carries its greenhouses and the default one, for the greenhouse picker beside its team."""
	import functools

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		out = fn(*args, **kwargs)
		if (frappe.form_dict or {}).get("order_kind") != "seafreight":
			return out
		found = []

		def walk(x):
			if isinstance(x, dict):
				if x.get("soi"):
					found.append(x)
				for v in x.values():
					walk(v)
			elif isinstance(x, list | tuple):
				for v in x:
					walk(v)

		walk(out)
		gh = line_greenhouses([x["soi"] for x in found])
		for x in found:
			x.update(gh.get(x["soi"]) or {"greenhouses": [], "greenhouse": ""})
		return out

	return wrapper


def plan_greenhouses(sois):
	"""{so item: [{greenhouse, label, stems}]}: the stems each line's plan cuts in each greenhouse."""
	sois = [s for s in dict.fromkeys(sois or ()) if s]
	if not sois or not frappe.db.table_exists(PLAN):
		return {}
	rows = frappe.db.sql(
		"""SELECT p.sales_order_item AS soi, r.greenhouse, r.stems, w.warehouse_name AS label
		FROM `tabSea Freight Greenhouse Plan` p
		JOIN `tabSea Freight Greenhouse Plan Row` r ON r.parent = p.name
		LEFT JOIN `tabWarehouse` w ON w.name = r.greenhouse
		WHERE p.sales_order_item IN %(s)s ORDER BY r.stems DESC""",
		{"s": tuple(sois)},
		as_dict=True,
	)
	out = {}
	for r in rows:
		out.setdefault(r.soi, []).append(
			{"greenhouse": r.greenhouse, "label": r.label or r.greenhouse, "stems": flt(r.stems)}
		)
	return out


def with_plan_greenhouses(fn):
	"""The Confirmation summary under the Orders filter's Sea freight: every line ("soi") carries the stems its
	plan cuts in each greenhouse (`gh`), and every order (a dict with "items") the same summed over its lines."""
	import functools

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		out = fn(*args, **kwargs)
		if (frappe.form_dict or {}).get("order_kind") != "seafreight":
			return out
		lines, orders = [], []

		def walk(x):
			if isinstance(x, dict):
				if x.get("soi"):
					lines.append(x)
				if isinstance(x.get("items"), list):
					orders.append(x)
				for v in x.values():
					walk(v)
			elif isinstance(x, list | tuple):
				for v in x:
					walk(v)

		walk(out)
		gh = plan_greenhouses([x["soi"] for x in lines])
		for x in lines:
			x["gh"] = gh.get(x["soi"], [])
		for o in orders:
			tot = {}
			for x in o["items"]:
				for g in x.get("gh") or []:
					t = tot.setdefault(g["greenhouse"], {**g, "stems": 0.0})
					t["stems"] += g["stems"]
			o["gh"] = sorted(tot.values(), key=lambda g: -g["stems"])
		return out

	return wrapper


def require_greenhouses(items):
	"""Allocation Planning confirm: every sea freight line confirmed must name its greenhouse (one growing its
	variety). Returns {so item: greenhouse} for the confirmation hook to plan the line to."""
	items = [x for x in items or [] if x.get("sales_order_item") and flt(x.get("stems")) > 0]
	if not items or not frappe.db.has_column("Sales Order", "custom_mode_of_transport"):
		return {}
	rows = frappe.db.sql(
		"""SELECT soi.name, soi.item_code, soi.item_name FROM `tabSales Order Item` soi
		JOIN `tabSales Order` so ON so.name = soi.parent
		WHERE soi.name IN %(s)s AND so.custom_mode_of_transport = %(m)s""",
		{"s": tuple(x["sales_order_item"] for x in items), "m": SEA_FREIGHT},
		as_dict=True,
	)
	if not rows:
		return {}
	by_soi = {x["sales_order_item"]: x for x in items}
	growing = _growing({r.item_code for r in rows})
	out = {}
	for r in rows:
		x = by_soi[r.name]
		known = {h["name"] for h in growing.get(r.item_code, [])}
		# the stems split over several greenhouses, or one greenhouse for the whole line
		split = {
			(g.get("greenhouse") or "").strip(): flt(g.get("stems"))
			for g in x.get("greenhouses") or []
			if (g.get("greenhouse") or "").strip() and flt(g.get("stems")) > 0
		}
		if not split and (x.get("greenhouse") or "").strip():
			split = {x["greenhouse"].strip(): flt(x.get("stems"))}
		if not split:
			frappe.throw(
				_("Choose the greenhouse that cuts {0} for this sea freight order.").format(r.item_name)
			)
		for gh in split:
			if gh not in known:
				frappe.throw(_("{0} has no running crop cycle of {1}.").format(gh, r.item_name))
		out[r.name] = split
	return out


def confirmation_after_insert(doc, method=None):
	"""Allocation Confirmation after_insert: a sea freight line confirmed on Allocation Planning gets its
	greenhouse plan -- its confirmed stems split over the greenhouses growing the variety by their forecast to
	delivery (suggest) -- for the production app. Confirming the line again plans it again."""
	if not frappe.db.table_exists(PLAN) or not frappe.db.has_column(
		"Sales Order", "custom_mode_of_transport"
	):
		return
	so = frappe.db.get_value(
		"Sales Order", doc.sales_order, ["custom_mode_of_transport", "delivery_date"], as_dict=True
	)
	if not so or so.custom_mode_of_transport != SEA_FREIGHT:
		return
	from upande_packhouse.allocation_plan import LIVE

	variety = doc.variety or doc.ordered_variety
	stems = flt(
		frappe.db.sql(
			"""SELECT SUM(stems) FROM `tabAllocation Confirmation`
			WHERE sales_order_item = %s AND status IN %s""",
			(doc.sales_order_item, tuple(LIVE)),
		)[0][0]
	) or flt(doc.stems)
	houses = _growing([variety]).get(variety, [])
	forecast = _forecast({variety}, {h["name"] for h in houses}, add_days(so.delivery_date, -1))
	chosen = (frappe.flags.get("sea_freight_greenhouses") or {}).get(doc.sales_order_item)
	if chosen:
		# the greenhouses (and their stems) chosen on Allocation Planning; one alone cuts the whole line
		split = dict(chosen) if len(chosen) > 1 else {next(iter(chosen)): stems}
		named = {h["name"]: h for h in houses}
		houses = [named.get(g) or {"name": g, "farm": "", "area": 0.0} for g in split]
	else:
		split = suggest(variety, stems, houses, forecast)
	rows = [
		{
			"greenhouse": h["name"],
			"farm": h["farm"] or None,
			"stems": flt(split.get(h["name"])),
			"forecast": forecast.get((variety, h["name"]), 0.0),
		}
		for h in houses
		if flt(split.get(h["name"])) > 0
	]
	name = frappe.db.get_value(PLAN, {"sales_order_item": doc.sales_order_item})
	plan = frappe.get_doc(PLAN, name) if name else frappe.new_doc(PLAN)
	plan.update(
		{
			"sales_order": doc.sales_order,
			"sales_order_item": doc.sales_order_item,
			"delivery_date": so.delivery_date,
			"variety": variety,
			"stem_length": doc.length or doc.ordered_length or "",
			"ordered_stems": flt(frappe.db.get_value("Sales Order Item", doc.sales_order_item, "stock_qty")),
			"planned_stems": sum(r["stems"] for r in rows),
		}
	)
	plan.set("greenhouses", rows)
	plan.flags.ignore_permissions = True
	if name:
		plan.save()
	else:
		plan.insert()


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
	# before the site's migrate has made the plan table, every line shows its suggested split
	names = (
		frappe.get_all(PLAN, filters={"sales_order_item": ["in", [ln.name for ln in lines]]}, pluck="name")
		if frappe.db.table_exists(PLAN)
		else []
	)
	for p in names:
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
	if not frappe.db.table_exists(PLAN):
		frappe.throw(_("Sea freight plans can be saved once this site has been migrated."))
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
