# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Planning v2 (www/sales-allocation-planning-v2.html).

Demand (order lines) against farm stock, plus the per-farm stem confirmations
the page records. Every number comes from api/v2/core:

  ordered      pipeline.fetch_lines -> ordered_stems (Sales Order Item.stock_qty,
               never the stale custom_ordered_quantity -- audit OR-A2)
  boxes        boxes.order_boxes per order over the listed lines (OR-A6)
  allocated    pipeline.attach_pipeline -> allocated_stems (Bucket Allocations)
  confirmed    Confirmed Stems rows with a farm and stems > 0 (the chips the
               page shows; same rows confimSalesOrderItem keeps)
  stock        stock.bucket_rows: shelf farm (Shelf.farm), split into held /
               allocated / free; "available" = `allocatable` (free, not too old,
               cooled) -- what the allocator will actually accept (OR-A5)

Date meaning: Sales Order delivery_date (README rule 4), any from/to range.
Orders: submitted (docstatus 1).
Farm / region (README rule 6, 6a):
  * stock is scoped by the SHELF's farm;
  * order lines are scoped by the ORDER's farm (rule 6, amended):
    COALESCE(Sales Order.farm, Sales Order.custom_farm, Order Pick List.farm)
    via region.order_farm_sql. Orders with no farm at all drop out while a
    farm or region filter is on.
Available stock is summed once per DISTINCT (variety, length, farm) needed by
the listed lines, never once per line (OR-A4). Every KPI is computed here over
the full filtered set; the line list may be truncated (rule 3), the KPIs never.
"""

from collections import defaultdict

import frappe

from upande_packhouse.api.v2.core import boxes as bx
from upande_packhouse.api.v2.core import pipeline, stock
from upande_packhouse.api.v2.core import region as region_mod

LINE_LIMIT = 1500
STATUSES = ("open", "partial", "full")


def _f(v):
	try:
		return float(v or 0)
	except (TypeError, ValueError):
		return 0.0


def _line_farms(lines, opls):
	"""{soi: set(farms)} -- the ORDER's farm (README rule 6):
	COALESCE(Sales Order.farm, Sales Order.custom_farm, Order Pick List.farm),
	the pick-list part falling back to the Pick List Item farm."""
	out = defaultdict(set)
	if not lines:
		return out
	so_farm = {}
	for r in frappe.db.sql(
		f"""SELECT so.name, {region_mod.order_farm_sql("so", None)} AS farm
		    FROM `tabSales Order` so WHERE so.name IN %(s)s""",
		{"s": tuple({ln.parent for ln in lines})},
		as_dict=True,
	):  # nosemgrep: fixed SQL fragment from region.order_farm_sql
		if r.farm:
			so_farm[r.name] = r.farm
	need_pli = set()
	for ln in lines:
		if ln.parent in so_farm:
			out[ln.name].add(so_farm[ln.parent])
			continue
		for o in ln.opls:
			f = (opls.get(o) or {}).get("farm")
			if f:
				out[ln.name].add(f)
			else:
				need_pli.add(o)
	if need_pli:
		for r in frappe.db.sql(
			"""SELECT DISTINCT sales_order_item AS soi, farm FROM `tabPick List Item`
			   WHERE parent IN %(o)s AND IFNULL(farm, '') != ''""",
			{"o": tuple(need_pli)},
			as_dict=True,
		):
			if r.soi not in out:
				out[r.soi].add(r.farm)
	return out


def _entries(soi_names):
	"""{soi: [{farm, stems}]} -- live confirmations (farm set, stems > 0)."""
	out = defaultdict(list)
	if not soi_names:
		return out
	for r in frappe.db.sql(
		"""SELECT sales_order_item AS soi, farm, stems FROM `tabConfirmed Stems`
		   WHERE parenttype = 'Sales Order' AND parentfield = 'custom_confirmed_stems_table'
		     AND sales_order_item IN %(s)s AND IFNULL(farm, '') != '' AND stems > 0
		   ORDER BY idx""",
		{"s": tuple(soi_names)},
		as_dict=True,
	):
		out[r.soi].append({"farm": r.farm, "stems": _f(r.stems)})
	return out


def _state(ordered, confirmed):
	if confirmed <= 0:
		return "open"
	if ordered > 0 and confirmed >= ordered:
		return "full"
	return "partial"


@frappe.whitelist()
def get_allocation_planning(
	from_date=None,
	to_date=None,
	region=None,
	farm=None,
	customer=None,
	sales_order=None,
	variety=None,
	length=None,
	status=None,
	by=None,
	q=None,
	my_farm=None,
):
	"""Order lines, per-farm stock cells and KPIs for the planning grid."""
	today = frappe.utils.today()
	from_date = from_date or today
	to_date = to_date or from_date
	if from_date > to_date:
		from_date, to_date = to_date, from_date
	q = (q or "").strip().lower()
	status = status if status in STATUSES else "all"

	lines = pipeline.fetch_lines(delivery_from=from_date, delivery_to=to_date)
	opls = pipeline.attach_pipeline(lines)
	farms_of = _line_farms(lines, opls)
	entries = _entries([ln.name for ln in lines])

	sos = tuple({ln.parent for ln in lines})
	so_info = {}
	if sos:
		for r in frappe.db.sql(
			"SELECT name, custom_order_name FROM `tabSales Order` WHERE name IN %(s)s",
			{"s": sos},
			as_dict=True,
		):
			so_info[r.name] = r.custom_order_name or ""

	# Live shelf farms (any stock) -- the farm picker and the column universe.
	stock_farms = [
		r[0]
		for r in frappe.db.sql(
			"""SELECT DISTINCT s.farm FROM `tabShelf Item` si INNER JOIN `tabShelf` s ON s.name = si.parent
			   WHERE si.stem_qty > 0 AND IFNULL(si.bucket_id, '') != '' AND IFNULL(s.farm, '') != ''"""
		)
	]
	conf_farms = {e["farm"] for es in entries.values() for e in es}

	# Filter options come from the whole date range so any value can be picked.
	options = {
		"customers": sorted({ln.customer for ln in lines if ln.customer}, key=str.lower),
		"orders": [
			{"value": so, "label": so + (" · " + so_info[so] if so_info.get(so) else "")}
			for so in sorted(sos)
		],
		"varieties": sorted({ln.item_code for ln in lines if ln.item_code}, key=str.lower),
		"lengths": sorted({ln.custom_length for ln in lines if ln.custom_length}),
		"farms": sorted(
			set(stock_farms)
			| conf_farms
			| {f for s in farms_of.values() for f in s}
			| set(region_mod.REGIONS["Karen"] + region_mod.REGIONS["Ravine"])
		),
		"by": sorted(conf_farms),
		"my_farms": [r[0] for r in frappe.db.sql("SELECT name FROM `tabFarm` ORDER BY name")],
	}

	farms = region_mod.farms_for(region=region, farm=farm)
	farm_set = set(farms) if farms is not None else None

	def confirmed_of(ln):
		return sum(e["stems"] for e in entries.get(ln.name, ()))

	def keep(ln):
		if farm_set is not None and not (farms_of.get(ln.name, set()) & farm_set):
			return False
		if customer and ln.customer != customer:
			return False
		if sales_order and ln.parent != sales_order:
			return False
		if variety and ln.item_code != variety:
			return False
		if length and (ln.custom_length or "") != length:
			return False
		if by and not any(e["farm"] == by for e in entries.get(ln.name, ())):
			return False
		if status != "all" and _state(_f(ln.ordered_stems), confirmed_of(ln)) != status:
			return False
		if q:
			hay = " ".join(
				str(x or "")
				for x in (
					ln.customer,
					ln.customer_name,
					ln.parent,
					so_info.get(ln.parent),
					ln.item_code,
					ln.custom_length,
				)
			).lower()
			if q not in hay:
				return False
		return True

	kept = [ln for ln in lines if keep(ln)]

	# ---- supply: one stock read for the (variety, length) pairs the lines need
	pairs = {(ln.item_code, ln.custom_length or "") for ln in kept}
	cells = {}  # (variety, length, farm) -> summary
	if pairs and farm_set != set():
		rows = stock.bucket_rows(
			farms=sorted(farm_set) if farm_set is not None else None,
			varieties=sorted({p[0] for p in pairs}),
			# a line with no length matches NULL-length stock, which IN can't
			lengths=None if any(not p[1] for p in pairs) else sorted({p[1] for p in pairs}),
		)
		rows = [r for r in rows if (r.variety, r.stem_length or "") in pairs]
		for g in stock.summarize(rows, by=("variety", "stem_length", "shelf_farm")):
			cells[(g.variety, g.stem_length or "", g.shelf_farm)] = g
	else:
		rows = []

	# Columns: farms in scope that hold stock for these lines or confirmed one.
	col_set = {k[2] for k in cells} | {e["farm"] for ln in kept for e in entries.get(ln.name, ())}
	if farm_set is not None:
		col_set &= farm_set
		if farm:
			col_set |= {farm} & farm_set
	columns = sorted(col_set)

	# ---- KPIs over the full filtered set
	ordered = sum(_f(ln.ordered_stems) for ln in kept)
	confirmed = sum(confirmed_of(ln) for ln in kept)
	mine = (
		sum(e["stems"] for ln in kept for e in entries.get(ln.name, ()) if e["farm"] == my_farm)
		if my_farm
		else 0.0
	)
	by_order = defaultdict(list)
	for ln in kept:
		by_order[ln.parent].append(ln)
	boxes = sum(bx.order_boxes(v) for v in by_order.values())
	demand = defaultdict(float)
	for ln in kept:
		demand[(ln.item_code, ln.custom_length or "")] += _f(ln.ordered_stems)
	supply = defaultdict(float)
	for (v, length_, _farm), g in cells.items():
		supply[(v, length_)] += g.allocatable
	uncovered = sum(max(0.0, d - supply.get(k, 0.0)) for k, d in demand.items())
	stock_tot = stock.totals(rows) if rows else None
	kpis = {
		"lines": len(kept),
		"orders": len(by_order),
		"customers": len({ln.customer for ln in kept}),
		"ordered_stems": ordered,
		"ordered_boxes": boxes,
		"allocated_stems": sum(_f(ln.allocated_stems) for ln in kept),
		"available_stems": sum(g.allocatable for g in cells.values()),
		"on_shelf_stems": stock_tot.stems if stock_tot else 0.0,
		"held_stems": stock_tot.held if stock_tot else 0.0,
		"stock_allocated_stems": stock_tot.allocated if stock_tot else 0.0,
		"stock_buckets": stock_tot.buckets if stock_tot else 0,
		"uncovered_stems": uncovered,
		"confirmed_stems": confirmed,
		"confirmed_pct": round(confirmed * 100.0 / ordered, 1) if ordered else None,
		"my_confirmed_stems": mine,
	}

	# ---- line rows (possibly truncated; KPIs above are not)
	shown = kept[:LINE_LIMIT]
	caps = {}
	if shown:
		caps = {
			r[0]: _f(r[1])
			for r in frappe.db.sql(
				"SELECT name, custom_ordered_quantity FROM `tabSales Order Item` WHERE name IN %(s)s",
				{"s": tuple(ln.name for ln in shown)},
			)
		}
	out = []
	for ln in shown:
		ordered_l = _f(ln.ordered_stems)
		lf = {}
		for f in columns:
			g = cells.get((ln.item_code, ln.custom_length or "", f))
			if not g:
				continue
			avail = g.allocatable
			lf[f] = {
				"available": avail,
				"on_shelf": g.stems,
				"held": g.held,
				"allocated": g.allocated,
				"buckets": g.buckets,
				"balance": avail - ordered_l,
				"cover_pct": round(avail * 100.0 / ordered_l, 1) if ordered_l else None,
				"status": "Sufficient"
				if avail >= ordered_l and avail > 0
				else ("Partial" if avail > 0 else "No Stock"),
			}
		conf = confirmed_of(ln)
		out.append(
			{
				"key": ln.parent + "||" + str(ln.idx),
				"soi": ln.name,
				"sales_order": ln.parent,
				"line_no": ln.idx,
				"order_name": so_info.get(ln.parent, ""),
				"customer": ln.customer,
				"customer_name": ln.customer_name or ln.customer,
				"transaction_date": str(ln.transaction_date or ""),
				"delivery_date": str(ln.delivery_date or ""),
				"item_code": ln.item_code,
				"item_name": ln.item_name,
				"length": ln.custom_length or "",
				"kind": ln.kind,
				"boxes": ln.boxes,
				"stems_ordered": ordered_l,
				"allocated_stems": _f(ln.allocated_stems),
				"pick_list_farm": ", ".join(sorted(farms_of.get(ln.name, ()))),
				# confimSalesOrderItem caps a line at custom_ordered_quantity (stale on
				# older lines, OR-A3); the page caps its input the same way.
				"confirm_cap": caps.get(ln.name, 0.0),
				"confirmed_stems": conf,
				"state": _state(ordered_l, conf),
				"entries": entries.get(ln.name, []),
				"farms": lf,
			}
		)
	return {
		"success": True,
		"from_date": from_date,
		"to_date": to_date,
		"columns": columns,
		"lines": out,
		"total_lines": len(kept),
		"truncated": len(kept) > LINE_LIMIT,
		"kpis": kpis,
		"options": options,
	}
