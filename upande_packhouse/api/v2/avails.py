# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Avails v2 (www/avails-v2.html): live shelf stock against order demand.

One cell per (variety, stem length, farm), every number in stems:

    on shelf   = held + allocated + free                 (core/stock.py, live now)
    unmet      = demand not yet covered by an allocation  (core/pipeline.py)
    available  = free - unmet                             (signed: < 0 is a shortfall)

so for any filter set   on shelf - held - allocated - unmet = available,
per cell, per variety, per length and for the KPIs (no flooring anywhere, so
the parts always add up -- audit ST-1, ST-2, ST-3, ST-11).

Stock (core.stock.bucket_rows): live Shelf Items, farm = the SHELF's farm,
split per bucket into held (whole bucket on an open discard request), allocated
(Bucket Allocation Status for that bucket + variety + length, capped at the stems
present) and free. Stock has no history: it is what is on the shelves now, for
every date range. The age filter is applied per bucket before aggregation (ST-12).

Demand (core.pipeline): submitted Roses Sales Order lines whose order's
delivery_date is in [from_date, to_date] (the page's one date meaning, README
rule 4), Closed orders excluded. Per line:
    ordered  = pipeline ordered_stems
    covered  = the furthest of allocated / issued / packed / dispatched stems,
               capped at ordered (a line packed or shipped without an allocation
               record is still covered)
    unmet    = ordered - covered
Farm = the ORDER's farm (README rule 6): Sales Order.farm, else custom_farm, else the
pick list's farm. Orders with no farm show as "No farm" and drop out while a
farm/region filter is on.
Allocated stems are never subtracted twice: an allocated line's stems sit in
"allocated" on the shelf and are not in its unmet demand (ST-3).
"""

import frappe

from upande_packhouse.api.v2.core import pipeline, stock
from upande_packhouse.api.v2.core import region as rg

NO_FARM = "No farm"
UNKNOWN_FARM = "Unknown"
NO_LENGTH = "--"
CELL = ("stems", "held", "allocated", "free", "allocatable", "allocated_in_held", "ordered", "covered", "unmet")


def _list(v):
	"""'a|b' or a JSON list -> ['a', 'b']; empty -> []."""
	if not v:
		return []
	if isinstance(v, list | tuple):
		return [str(x).strip() for x in v if str(x).strip()]
	v = str(v).strip()
	if v.startswith("["):
		return [str(x).strip() for x in frappe.parse_json(v) if str(x).strip()]
	return [x.strip() for x in v.split("|") if x.strip()]


def _farms(region_arg, farm_list):
	"""Farm restriction from region + a multi-farm pick: None = unrestricted,
	[] = match nothing (e.g. Karen + Torongo)."""
	if not farm_list:
		return rg.farms_for(region=region_arg)
	allowed = []
	for f in farm_list:
		hit = rg.farms_for(region=region_arg, farm=f)
		allowed.extend(hit or [])
	return sorted(set(allowed))


def _len(v):
	return (str(v).strip() if v else "") or NO_LENGTH


@frappe.whitelist()
def get_avails(
	from_date=None,
	to_date=None,
	region=None,
	farm=None,
	length=None,
	variety=None,
	min_age=None,
	q=None,
):
	"""Avails pivot + KPIs for the filters. All roll-ups are computed here."""
	from_date = from_date or frappe.utils.today()
	to_date = to_date or from_date
	if from_date > to_date:
		from_date, to_date = to_date, from_date
	farm_list = _list(farm)
	lengths = _list(length)
	varieties = _list(variety)
	farms = _farms(region, farm_list)
	min_age = int(min_age or 0)
	q = (q or "").strip().lower()

	cells = {}

	def cell(v, ln, fm):
		key = (v, ln, fm)
		c = cells.get(key)
		if c is None:
			c = cells[key] = dict.fromkeys(CELL, 0.0)
			c.update(variety=v, length=ln, farm=fm, buckets=set(), lines=0)
		return c

	# ---- stock (live) -------------------------------------------------
	stock_rows = []
	if farms != []:
		stock_rows = stock.bucket_rows(farms=farms, varieties=varieties or None)
	for r in stock_rows:
		ln = _len(r.stem_length)
		if lengths and ln not in lengths:
			continue
		if q and q not in (r.variety or "").lower():
			continue
		if min_age and (r.age_days or 0) < min_age:
			continue
		c = cell(r.variety, ln, r.shelf_farm or UNKNOWN_FARM)
		for f in ("stems", "held", "allocated", "free", "allocatable", "allocated_in_held"):
			c[f] += r[f]
		c["buckets"].add(r.bucket_id)

	# ---- demand (delivery date range) ---------------------------------
	params = {}
	extra = " AND so.status != 'Closed'"
	if varieties:
		extra += " AND soi.item_code IN %(av_var)s"
		params["av_var"] = tuple(varieties)
	lines = pipeline.fetch_lines(delivery_from=from_date, delivery_to=to_date, extra_where=extra, params=params)
	if q:
		lines = [ln for ln in lines if q in (ln.item_code or "").lower()]
	if lengths:
		lines = [ln for ln in lines if _len(ln.custom_length) in lengths]
	pipeline.attach_pipeline(lines)
	# Order farm (README rule 6): Sales Order.farm, else custom_farm, else pick list farm
	# -- the same COALESCE as region.order_farm_sql(), resolved here because the lines
	# are already fetched through the shared pipeline.
	so_farm = {}
	if lines:
		so_farm = {
			r.name: r.f
			for r in frappe.db.sql(
				f"SELECT so.name, NULLIF(so.farm, '') AS sf, NULLIF(so.custom_farm, '') AS cf, "
				f"{rg.order_farm_sql('so', None)} AS f FROM `tabSales Order` so WHERE so.name IN %(n)s",
				{"n": tuple({ln.parent for ln in lines})},
				as_dict=True,
			)
		}
	orders = set()
	for ln in lines:
		fm = so_farm.get(ln.parent) or (ln.opl_farm or "").split(",")[0].strip() or None
		if farms is not None and fm not in farms:
			continue
		fm = fm or NO_FARM
		ordered = float(ln.ordered_stems or 0)
		covered = min(
			ordered,
			max(ln.allocated_stems, ln.issued_stems, ln.packed_stems, ln.dispatched_stems),
		)
		c = cell(ln.item_code, _len(ln.custom_length), fm)
		c["ordered"] += ordered
		c["covered"] += covered
		c["unmet"] += ordered - covered
		c["lines"] += 1
		orders.add(ln.parent)

	# ---- roll-ups -----------------------------------------------------
	out_cells = []
	for c in cells.values():
		c["buckets"] = len(c["buckets"])
		c["avail"] = c["free"] - c["unmet"]
		out_cells.append(c)

	def _sum(rows, f):
		return sum(r[f] for r in rows)

	totals = {f: _sum(out_cells, f) for f in CELL}
	totals["avail"] = totals["free"] - totals["unmet"]
	totals["buckets"] = len({r.bucket_id for r in stock_rows if _keep(r, lengths, q, min_age)})
	totals["lines"] = sum(c["lines"] for c in out_cells)
	totals["orders"] = len(orders)

	by_var = {}
	for c in out_cells:
		v = by_var.setdefault(c["variety"], {"variety": c["variety"], "cells": []})
		v["cells"].append(c)
	rows = []
	for v in by_var.values():
		for f in (*CELL, "avail"):
			v[f] = _sum(v["cells"], f)
		v["short_cells"] = sum(1 for c in v["cells"] if c["avail"] < 0)
		rows.append(v)
	rows.sort(key=lambda r: r["variety"])
	totals["varieties"] = len(rows)
	totals["varieties_available"] = sum(1 for r in rows if r["avail"] > 0)
	totals["short"] = sum(-r["avail"] for r in rows if r["avail"] < 0)
	totals["surplus"] = sum(r["avail"] for r in rows if r["avail"] > 0)

	length_cols = sorted({c["length"] for c in out_cells}, key=_len_key)
	names = [r["variety"] for r in rows]
	return {
		"success": True,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"farms": farms,
		"lengths": length_cols,
		"rows": rows,
		"totals": totals,
		"options": _options(from_date, to_date),
		"images": _images(names),
		"colours": _colours(names),
	}


def _keep(r, lengths, q, min_age):
	if lengths and _len(r.stem_length) not in lengths:
		return False
	if q and q not in (r.variety or "").lower():
		return False
	return not (min_age and (r.age_days or 0) < min_age)


def _len_key(v):
	digits = "".join(ch for ch in str(v) if ch.isdigit())
	return (0, int(digits), "") if digits else (1, 0, str(v))


def _options(from_date, to_date):
	"""Filter choices, independent of the filters themselves: live shelf stock
	plus the order lines in the date range."""
	stock_opts = frappe.db.sql(
		"""SELECT DISTINCT s.farm, si.variety, si.stem_length
		   FROM `tabShelf Item` si INNER JOIN `tabShelf` s ON s.name = si.parent
		   WHERE si.stem_qty > 0 AND IFNULL(si.bucket_id, '') != '' AND IFNULL(si.variety, '') != ''""",
		as_dict=True,
	)
	order_opts = frappe.db.sql(
		"""SELECT DISTINCT soi.item_code AS variety, soi.custom_length AS stem_length
		   FROM `tabSales Order Item` soi INNER JOIN `tabSales Order` so ON so.name = soi.parent
		   WHERE so.docstatus = 1 AND so.business_unit = 'Roses' AND so.status != 'Closed'
		     AND so.delivery_date BETWEEN %(f)s AND %(t)s AND IFNULL(soi.item_code, '') != ''""",
		{"f": from_date, "t": to_date},
		as_dict=True,
	)
	opl_farms = frappe.db.sql_list(
		"SELECT DISTINCT farm FROM `tabOrder Pick List` WHERE docstatus < 2 AND IFNULL(farm, '') != ''"
	)
	farms = {r.farm for r in stock_opts if r.farm} | set(opl_farms)
	return {
		"farms": sorted(farms),
		"lengths": sorted({_len(r.stem_length) for r in stock_opts + order_opts}, key=_len_key),
		"varieties": sorted({r.variety for r in stock_opts + order_opts if r.variety}),
	}


def _images(names):
	"""Item.image, else the first image File attached to the Item (as v1)."""
	images = {}
	if not names:
		return images
	for it in frappe.get_all("Item", filters={"name": ["in", names]}, fields=["name", "image"]):
		if it.image:
			images[it.name] = it.image
	missing = [v for v in names if v not in images]
	if missing:
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Item", "attached_to_name": ["in", missing]},
			fields=["attached_to_name", "file_url"],
			order_by="creation asc",
		):
			url = (f.file_url or "").lower()
			if f.file_url and url.endswith((".png", ".jpg", ".jpeg", ".webp")):
				images.setdefault(f.attached_to_name, f.file_url)
	return images


def _colours(names):
	"""Item.custom_color -> Color.color, for the PDF's banner when there is no photo."""
	if not names:
		return {}
	return {
		r.v: {"name": r.cname, "hex": r.hex or "#cccccc"}
		for r in frappe.db.sql(
			"""SELECT i.name AS v, i.custom_color AS cname, c.color AS hex
			   FROM `tabItem` i LEFT JOIN `tabColor` c ON c.name = i.custom_color
			   WHERE i.name IN %(v)s AND IFNULL(i.custom_color, '') != ''""",
			{"v": tuple(names)},
			as_dict=True,
		)
	}
