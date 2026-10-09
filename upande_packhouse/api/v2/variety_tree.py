# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Variety Tree v2 (www/variety-tree-v2.html).

The page is the Cut Flowers catalogue (Item Group tree -> Items) with, per
variety, how many stems were demanded and harvested over a chosen date range.

    demand      Sales Order lines (submitted, business unit Roses) whose order
                `delivery_date` is in the range; stems = units.ordered_stems
                via pipeline.fetch_lines (README rule 4: order date meaning).
                With a farm/region filter only orders whose ORDER farm is one of
                those farms count (README rule 6, amended): Sales Order.farm,
                else custom_farm, else Order Pick List.farm
                (region.order_farm_sql). Orders with no farm drop out while a
                farm/region filter is on.
    production  harvested stems from core.harvest (Harvesting Stock Entries,
                transfer_qty, by posting_date), farm = Stock Entry farm.

Every filter (status, line, data gap, search, region, farm, date range) is
applied here and the KPIs are computed over the same filtered set the page
lists, so KPI = sum of the rows (README rules 3 and 7). Catalogue filters
(status/line/gap/search) narrow which varieties count; region/farm/date only
change demand and production, which is stated in the KPI captions.

The colour / photo writes stay on the v1 endpoints (api/variety_tree.py),
which this module does not change.
"""

import frappe
from frappe.utils import add_days, date_diff, getdate, nowdate

from upande_packhouse.api.v2.core import harvest as core_harvest
from upande_packhouse.api.v2.core import pipeline
from upande_packhouse.api.v2.core import region as region_mod

ROOT = "Cut Flowers"
LEGACY = "Cut Flowers - Legacy"
CACHE_TTL = 600  # seconds; demand/production aggregates per (range, farms)


# ------------------------------------------------------------ catalogue


def _catalogue():
	"""[{name, cats:[{name, group, items:[...]}]}] — same tree as v1 getVarietyTree."""
	if not frappe.db.exists("Item Group", ROOT):
		return []
	colours = {c.name: c.color for c in frappe.get_all("Color", fields=["name", "color"])}
	line_names = [
		g.name
		for g in frappe.get_all(
			"Item Group", filters={"parent_item_group": ROOT}, fields=["name"], order_by="name"
		)
		if g.name != LEGACY
	]
	cats_by_line = {}
	if line_names:
		for c in frappe.get_all(
			"Item Group",
			filters={"parent_item_group": ["in", line_names]},
			fields=["name", "parent_item_group"],
			order_by="name",
		):
			cats_by_line.setdefault(c.parent_item_group, []).append(c.name)
	groups = [cg for ln in line_names for cg in (cats_by_line.get(ln) or [ln])]
	items_by_group = {}
	if groups:
		for it in frappe.get_all(
			"Item",
			filters={"item_group": ["in", groups]},
			fields=["name", "item_name", "image", "custom_color", "stock_uom", "disabled", "item_group"],
			order_by="item_name",
		):
			items_by_group.setdefault(it.item_group, []).append(it)
	out = []
	for ln in line_names:
		cats = []
		for cg in cats_by_line.get(ln) or [ln]:
			items = items_by_group.get(cg)
			if not items:
				continue
			cats.append(
				{
					"name": cg[len(ln) + 3 :] if cg.startswith(ln + " - ") else cg,
					"group": cg,
					"items": [
						{
							"n": it.item_name or it.name,
							"code": it.name,
							"img": it.image or "",
							"s": "Inactive" if it.disabled else "Active",
							"colour": it.custom_color or "",
							"hex": (colours.get(it.custom_color) or "") if it.custom_color else "",
							"uom": it.stock_uom or "",
						}
						for it in items
					],
				}
			)
		if cats:
			out.append({"name": ln, "cats": cats})
	return out


# ------------------------------------------------------- demand / production


def _orders_at_farms(sales_orders, farms):
	"""Sales Orders whose ORDER farm is one of `farms` (README rule 6, amended).

	Order farm = region.order_farm_sql: Sales Order.farm, else custom_farm, else
	the Order Pick List farm (one farm per order from its live pick lists)."""
	if not sales_orders:
		return set()
	farm_expr = region_mod.order_farm_sql(so_alias="so", opl_alias="opl")
	rows = frappe.db.sql(
		f"""
		SELECT so.name
		FROM `tabSales Order` so
		LEFT JOIN (
			SELECT sales_order, MIN(NULLIF(farm, '')) AS farm
			FROM `tabOrder Pick List` WHERE docstatus < 2 GROUP BY sales_order
		) opl ON opl.sales_order = so.name
		WHERE so.name IN %(sos)s AND {farm_expr} IN %(farms)s
		""",  # nosemgrep: farm_expr is fixed SQL from region.order_farm_sql, values bound
		{"sos": tuple(sales_orders), "farms": region_mod.sql_tuple(farms)},
	)
	return {r[0] for r in rows}


def _demand(date_from, date_to, farms):
	"""{item_code: ordered stems} for orders delivering in the range."""
	lines = pipeline.fetch_lines(delivery_from=date_from, delivery_to=date_to)
	if farms is not None:
		keep = _orders_at_farms({ln.parent for ln in lines}, farms)
		lines = [ln for ln in lines if ln.parent in keep]
	out = {}
	for ln in lines:
		out[ln.item_code] = out.get(ln.item_code, 0.0) + float(ln.ordered_stems or 0)
	return out


def _production(date_from, date_to, farms):
	"""{item_code: harvested stems} from core.harvest."""
	if farms is not None and not farms:
		return {}
	rows = core_harvest.harvest(date_from=date_from, date_to=date_to, group_by=("item_code",), farms=farms)
	return {r["item_code"]: float(r["stems"] or 0) for r in rows if r.get("item_code")}


def _cached(kind, fn, date_from, date_to, farms):
	key = "ph2_vt2:{0}:{1}:{2}:{3}".format(
		kind, date_from, date_to, "*" if farms is None else ",".join(sorted(farms)) or "-"
	)
	cache = frappe.cache()
	hit = cache.get_value(key)
	if hit is not None:
		return hit
	val = fn(date_from, date_to, farms)
	cache.set_value(key, val, expires_in_sec=CACHE_TTL)
	return val


# ------------------------------------------------------------- endpoint


def _passes(it, status, gap):
	if status == "active" and it["s"] != "Active":
		return False
	if status == "inactive" and it["s"] == "Active":
		return False
	if gap == "nocolour" and it["colour"]:
		return False
	if gap == "nophoto" and it["img"]:
		return False
	return True


@frappe.whitelist()
def get_variety_tree(
	from_date: str | None = None,
	to_date: str | None = None,
	region: str | None = None,
	farm: str | None = None,
	status: str | None = "all",
	line: str | None = None,
	gap: str | None = None,
	q: str | None = None,
):
	"""Filtered Cut Flowers tree + per-variety demand / production stems + KPIs."""
	try:
		date_to = str(getdate(to_date or nowdate()))
		date_from = str(getdate(from_date or add_days(date_to, -29)))
		if date_from > date_to:
			date_from, date_to = date_to, date_from
		days = date_diff(date_to, date_from) + 1
		weeks = days / 7.0
		farms = region_mod.farms_for(region=region, farm=farm)

		catalogue = _catalogue()
		demand = _cached("d", _demand, date_from, date_to, farms)
		prod = _cached("p", _production, date_from, date_to, farms)

		status = (status or "all").lower()
		gap = (gap or "").lower()
		ql = (q or "").strip().lower()
		has = lambda v: bool(v) and ql in str(v).lower()  # noqa: E731

		out = []
		k = {
			"varieties": 0,
			"active": 0,
			"inactive": 0,
			"missing_colour": 0,
			"missing_photo": 0,
			"demand_stems": 0,
			"production_stems": 0,
			"demanded_varieties": 0,
			"produced_varieties": 0,
			"lines": 0,
			"categories": 0,
		}
		for ln in catalogue:
			if line and ln["name"] != line:
				continue
			line_hit = ql and has(ln["name"])
			cats = []
			for c in ln["cats"]:
				cat_hit = line_hit or (ql and has(c["name"]))
				items = []
				for it in c["items"]:
					if not _passes(it, status, gap):
						continue
					if ql and not (cat_hit or has(it["n"]) or has(it["code"])):
						continue
					d = round(demand.get(it["code"], 0))
					p = round(prod.get(it["code"], 0))
					row = dict(it)
					row.update(
						demand_stems=d,
						production_stems=p,
						demand_per_week=round(d / weeks),
						production_per_week=round(p / weeks),
					)
					items.append(row)
					k["varieties"] += 1
					k["active" if it["s"] == "Active" else "inactive"] += 1
					k["missing_colour"] += 0 if it["colour"] else 1
					k["missing_photo"] += 0 if it["img"] else 1
					k["demand_stems"] += d
					k["production_stems"] += p
					k["demanded_varieties"] += 1 if d else 0
					k["produced_varieties"] += 1 if p else 0
				if items:
					cats.append({"name": c["name"], "group": c["group"], "items": items})
			if cats:
				out.append({"name": ln["name"], "cats": cats, "count": sum(len(c["items"]) for c in cats)})
				k["lines"] += 1
				k["categories"] += len(cats)
		k["demand_per_week"] = round(k["demand_stems"] / weeks)
		k["production_per_week"] = round(k["production_stems"] / weeks)

		return {
			"success": True,
			"root": ROOT,
			"from_date": date_from,
			"to_date": date_to,
			"days": days,
			"weeks": round(weeks, 2),
			"farms": farms,
			"lines": out,
			"all_lines": [ln["name"] for ln in catalogue],
			"catalogue_total": sum(len(c["items"]) for ln in catalogue for c in ln["cats"]),
			"kpis": k,
		}
	except Exception as e:
		frappe.log_error("v2 get_variety_tree error: " + str(e))
		return {"success": False, "error": str(e), "lines": []}
