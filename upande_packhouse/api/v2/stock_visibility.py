# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Stock Visibility v2 (www/stock-visibility-v2.html) -- stock vs orders.

Replaces the page's use of the v1 endpoint
api.stock_visibility.getStockVisibilityData (unchanged, still serves v1).
Every number is computed here over the full filtered set (README rules 3, 7);
the browser only formats.

Definitions
-----------
Shelf stock     core/stock.bucket_rows -- one row per (bucket, variety, length),
                each stem in exactly one state:
                    shelved = held + allocated + free
                held       whole bucket on an open discard request
                allocated  outstanding Bucket Allocation Status for that row
                free       the rest;  allocatable = free that is neither too old
                           nor still cooling (what the allocator will accept)
                Location = the SHELF's farm (rule 6). Age = days since harvest,
                else since shelving (the core's single anchor).
Ordered         core/pipeline.fetch_lines -> ordered_stems, for Sales Orders
                delivering in [from_date, to_date] (the page's date meaning,
                rule 4). allocated_to_orders = pipeline allocated_stems (capped
                at ordered per line); to_allocate = ordered - that.
Variance        allocatable - to_allocate (stems). Stock already allocated to a
                line is neither "free" nor "still to allocate", so it is not
                double counted.
Coldroom        received (Receiving / Late Receipt) buckets whose LATEST receipt
                is in the window, with no later harvest, not on a shelf now, not
                discarded and not issued since that receipt. Stems in stock UOM.

Date range ("basis")
--------------------
Shelf items are deleted when stems leave a shelf, so a past shelf position can't
be rebuilt. The range therefore always scopes demand (delivery date) and the
stock side is one of:
  basis=all     (default) everything on the shelf now; coldroom = received in
                the last 3 days up to today (live position)
  basis=range   only shelf stems harvested (else shelved) within the range, and
                coldroom buckets received within the range.

Filters -> scope
----------------
region / farm (multi)  shelf farm; coldroom receiving farm; order lines whose
                       ORDER's farm (COALESCE of Sales Order farm / custom_farm /
                       pick-list farm, region.order_farm_sql) is selected;
                       orders with no farm anywhere drop out (rule 6)
length (multi)         shelf stem length; order custom_length; coldroom length
rose                   Item Group subtree (core/rose) on all three
q                      variety code / name substring on all three
customer, delivery_pt  order lines only (supply has no customer) -- captioned
age (oldest >= N d),   variety-level: a variety stays in or drops out as a
cover, demand          whole, so every KPI (summed over visible varieties)
                       follows them
"""

from collections import defaultdict

import frappe
from frappe.utils import add_days, getdate, today

from upande_packhouse.api.v2.core import pipeline, region, stock
from upande_packhouse.api.v2.core.rose import rose_sql, rose_type

AGE_EDGES = (3, 7)  # fresh [0,3) . aging [3,7) . old [7, inf) days
COLDROOM_LIVE_DAYS = 3
STATE_FIELDS = ("stems", "held", "allocated", "free", "allocatable", "allocated_in_held", "over_allocated")


def _list(v):
	if not v:
		return []
	if isinstance(v, (list, tuple)):
		return [str(x).strip() for x in v if str(x).strip()]
	return [x.strip() for x in str(v).split(",") if x.strip()]


def _farms(region_arg, farm_list):
	"""Farm restriction from region + a multi farm pick (None = unrestricted).

	Each picked farm is intersected with the region by core/region.farms_for."""
	if not farm_list:
		return region.farms_for(region=region_arg)
	out = []
	for f in farm_list:
		for x in region.farms_for(region=region_arg, farm=f) or []:
			if x not in out:
				out.append(x)
	return out


def _len_key(v):
	s = (v or "").strip()
	digits = "".join(ch for ch in s if ch.isdigit())
	return (0, int(digits), s) if digits and s[0].isdigit() else (1, 0, s)


def _qmatch(q, *vals):
	return not q or any(q in (v or "").lower() for v in vals)


def _f(v):
	return float(v or 0)


# ------------------------------------------------------------------ sources


def _shelf(farms, lengths, rose, q, basis, d_from, d_to):
	if farms is not None and not farms:
		return []  # region + farm that don't intersect: nothing (bucket_rows treats [] as "no filter")
	rows = stock.bucket_rows(farms=farms, lengths=lengths or None, rose=rose)
	out = []
	for r in rows:
		if not _qmatch(q, r.variety, r.item_name):
			continue
		if basis == "range":
			anchor = getdate(r.age_anchor) if r.age_anchor else None
			if not anchor or anchor < d_from or anchor > d_to:
				continue
		r.stem_length = (r.stem_length or "").strip() or "--"
		r.shelf_farm = r.shelf_farm or "Unknown"
		out.append(r)
	return out


def _orders(farms, lengths, rose, q, d_from, d_to):
	"""Order lines delivering in the range, farm-scoped by the order's farm (rule 6)."""
	params = {}
	extra = rose_sql("soi.item_group", rose, params)
	lines = pipeline.fetch_lines(delivery_from=d_from, delivery_to=d_to, extra_where=extra, params=params)
	lines = [ln for ln in lines if _qmatch(q, ln.item_code, ln.item_name)]
	for ln in lines:
		ln.length = (ln.custom_length or "").strip() or "--"
	if lengths:
		lines = [ln for ln in lines if ln.length in lengths]
	if not lines:
		return []
	pipeline.attach_pipeline(lines)
	for ln in lines:
		ln.alloc_to_line = min(_f(ln.allocated_stems), _f(ln.ordered_stems))
		ln.to_allocate = max(0.0, _f(ln.ordered_stems) - ln.alloc_to_line)
	if farms is not None:
		# Order-side farm scope (README rule 6): the ORDER's farm.
		of = {
			r.name: r.farm
			for r in frappe.db.sql(
				f"""SELECT so.name, {region.order_farm_sql("so", "opl")} AS farm
				    FROM `tabSales Order` so
				    LEFT JOIN (SELECT sales_order, MIN(farm) AS farm FROM `tabOrder Pick List`
				               WHERE docstatus < 2 GROUP BY sales_order) opl ON opl.sales_order = so.name
				    WHERE so.name IN %(n)s""",
				{"n": tuple({ln.parent for ln in lines})},
				as_dict=True,
			)
		}  # nosemgrep: the f-string hole is fixed SQL
		fset = set(region.sql_tuple(farms))
		lines = [ln for ln in lines if of.get(ln.parent) in fset]
	return lines


def _coldroom(farms, lengths, rose, q, w_from, w_to):
	"""Received buckets not yet shelved / issued / discarded (see module doc)."""
	params = {"f": w_from, "t": w_to}
	cond = ""
	if farms is not None:
		cond += " AND se.farm IN %(farms)s"
		params["farms"] = region.sql_tuple(farms)
	cond += rose_sql("i.item_group", rose, params)
	rec = frappe.db.sql(
		f"""
		SELECT se.name, se.custom_bucket_id AS bucket_id, se.posting_date, se.farm,
		       IFNULL(TRIM(se.custom_stem_length), '') AS length,
		       sed.item_code AS variety, i.item_name, i.item_group,
		       SUM(sed.transfer_qty) AS stems
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		LEFT JOIN `tabItem` i ON i.name = sed.item_code
		WHERE se.stock_entry_type IN ('Receiving', 'Late Receipt') AND se.docstatus = 1
		  AND se.posting_date BETWEEN %(f)s AND %(t)s
		  AND IFNULL(se.custom_bucket_id, '') != '' {cond}
		GROUP BY se.name, sed.item_code
		""",
		params,
		as_dict=True,
	)  # nosemgrep: the f-string holes are fixed SQL, values are bound
	rec = [r for r in rec if _qmatch(q, r.variety, r.item_name)]
	for r in rec:
		r.length = r.length or "--"
	if lengths:
		rec = [r for r in rec if r.length in lengths]
	if not rec:
		return []
	# Latest receipt per bucket inside the window wins.
	latest = {}
	for r in rec:
		cur = latest.get(r.bucket_id)
		if cur is None or (r.posting_date, r.name) > cur:
			latest[r.bucket_id] = (r.posting_date, r.name)
	b = tuple(latest)
	since = min(v[0] for v in latest.values())
	gone = set()
	# On a shelf now
	gone |= set(frappe.db.sql_list(
		"SELECT DISTINCT bucket_id FROM `tabShelf Item` WHERE stem_qty > 0 AND bucket_id IN %(b)s", {"b": b}
	))
	# A later receipt / harvest (new cycle, also beyond the window) or a discard
	for r in frappe.db.sql(
		"""SELECT custom_bucket_id AS bucket_id, stock_entry_type AS t, posting_date, name
		   FROM `tabStock Entry` FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		   WHERE stock_entry_type IN ('Receiving', 'Late Receipt', 'Harvesting', 'Discard')
		     AND docstatus = 1 AND posting_date >= %(s)s AND custom_bucket_id IN %(b)s""",
		{"b": b, "s": since},
		as_dict=True,
	):
		d, n = latest[r.bucket_id]
		if r.t == "Discard" and r.posting_date >= d:
			gone.add(r.bucket_id)
		elif r.t != "Discard" and (r.posting_date, r.name) > (d, n) and r.name != n:
			gone.add(r.bucket_id)
	# Issued since the receipt
	for r in frappe.db.sql(
		"""SELECT pli.bucket AS bucket_id, MAX(DATE(pli.modified)) AS d
		   FROM `tabPick List Item` pli
		   WHERE pli.issued = 1 AND pli.bucket IN %(b)s GROUP BY pli.bucket""",
		{"b": b},
		as_dict=True,
	):
		if r.d and r.d >= latest[r.bucket_id][0]:
			gone.add(r.bucket_id)
	return [r for r in rec if r.bucket_id not in gone and latest[r.bucket_id][1] == r.name]


# ------------------------------------------------------------------ roll-up


def _band(age):
	lo = 0
	for i, e in enumerate(AGE_EDGES):
		if age < e:
			return i
		lo = e
	return len(AGE_EDGES)


def _cover(r):
	if r["to_allocate"] <= 0:
		return "surplus"
	c = r["allocatable"] / r["to_allocate"]
	return "deficit" if c < 1 else "tight" if c < 1.1 else "surplus"


def _new_var(v):
	d = {"variety": v, "item_name": "", "item_group": "", "rose_type": None,
	     "ordered": 0.0, "alloc_to_orders": 0.0, "to_allocate": 0.0, "coldroom": 0.0,
	     "oldest_days": None, "bands": [0.0] * (len(AGE_EDGES) + 1),
	     "_buckets": set(), "_cr_buckets": set(), "_age_x": 0.0,
	     "by_len": {}, "farms": {}}
	for f in STATE_FIELDS:
		d[f] = 0.0
	return d


def _cell(store, key, extra=()):
	c = store.get(key)
	if c is None:
		c = store[key] = {f: 0.0 for f in (*STATE_FIELDS, "coldroom", *extra)}
	return c


def build(args):
	a = frappe._dict(args or {})
	d_to = getdate(a.to_date or a.from_date or today())
	d_from = getdate(a.from_date or d_to)
	if d_from > d_to:
		d_from, d_to = d_to, d_from
	basis = "range" if (a.basis or "") == "range" else "all"
	farm_pick = _list(a.farm)
	farms = _farms(a.get("region"), farm_pick)
	lengths = _list(a.length)
	rose = a.rose or "all"
	q = (a.q or "").strip().lower()
	cust = (a.customer or "").strip()
	dp = (a.delivery_point or "").strip()
	age_min = int(a.age or 0) if str(a.age or "").isdigit() else 0
	cover = a.cover if a.cover in ("deficit", "tight", "surplus") else ""
	demand = str(a.demand or "").lower() in ("1", "true", "yes")

	shelf = _shelf(farms, lengths, rose, q, basis, d_from, d_to)
	lines_all = _orders(farms, lengths, rose, q, d_from, d_to)
	if basis == "range":
		w_from, w_to = d_from, d_to
	else:
		w_to = getdate(today())
		w_from = getdate(add_days(w_to, -COLDROOM_LIVE_DAYS))
	cold = _coldroom(farms, lengths, rose, q, w_from, w_to)

	options = {
		"customers": sorted({ln.customer for ln in lines_all if ln.customer}),
		"delivery_points": sorted({ln.custom_delivery_point for ln in lines_all if ln.custom_delivery_point}),
	}
	lines = [ln for ln in lines_all
	         if (not cust or ln.customer == cust) and (not dp or ln.custom_delivery_point == dp)]

	V = {}

	def var(code, name=None, group=None):
		v = V.get(code)
		if v is None:
			v = V[code] = _new_var(code)
		if name and not v["item_name"]:
			v["item_name"] = name
		if group and not v["item_group"]:
			v["item_group"] = group
		return v

	for r in shelf:
		v = var(r.variety, r.item_name, r.item_group)
		cl = _cell(v["by_len"], r.stem_length, ("ordered", "to_allocate"))
		fm = _cell(v["farms"], r.shelf_farm)
		for f in STATE_FIELDS:
			v[f] += r[f]
			cl[f] += r[f]
			fm[f] += r[f]
		age = r.age_days or 0
		v["oldest_days"] = age if v["oldest_days"] is None else max(v["oldest_days"], age)
		v["bands"][_band(age)] += r.stems
		v["_age_x"] += age * r.stems
		v["_buckets"].add(r.bucket_id)
	for ln in lines:
		v = var(ln.item_code, ln.item_name, ln.item_group)
		cl = _cell(v["by_len"], ln.length, ("ordered", "to_allocate"))
		v["ordered"] += ln.ordered_stems
		v["alloc_to_orders"] += ln.alloc_to_line
		v["to_allocate"] += ln.to_allocate
		cl["ordered"] += ln.ordered_stems
		cl["to_allocate"] += ln.to_allocate
	for r in cold:
		v = var(r.variety, r.item_name, r.item_group)
		st_ = _f(r.stems)
		v["coldroom"] += st_
		v["_cr_buckets"].add(r.bucket_id)
		_cell(v["by_len"], r.length, ("ordered", "to_allocate"))["coldroom"] += st_
		_cell(v["farms"], r.farm or "Unknown")["coldroom"] += st_

	# Variety-level filters, then KPIs over exactly the visible varieties.
	visible = []
	for v in V.values():
		v["rose_type"] = rose_type(v["item_group"])
		v["variance"] = v["allocatable"] - v["to_allocate"]
		v["cover"] = _cover(v)
		if demand and v["ordered"] <= 0:
			continue
		if age_min and (v["oldest_days"] or 0) < age_min:
			continue
		if cover and v["cover"] != cover:
			continue
		visible.append(v)
	keep = {v["variety"] for v in visible}

	all_lengths = sorted({k for v in visible for k in v["by_len"]}, key=_len_key)
	T = {f: 0.0 for f in (*STATE_FIELDS, "ordered", "alloc_to_orders", "to_allocate", "coldroom")}
	bands = [0.0] * (len(AGE_EDGES) + 1)
	age_x = 0.0
	buckets, cr_buckets, shelves = set(), set(), set()
	for v in visible:
		for f in T:
			T[f] += v[f]
		for i, s in enumerate(v["bands"]):
			bands[i] += s
		age_x += v["_age_x"]
		buckets |= v["_buckets"]
		cr_buckets |= v["_cr_buckets"]
	for r in shelf:
		if r.variety in keep:
			shelves.add(r.shelf)
	short = sum(1 for v in visible if v["variance"] < 0)
	oldest = max((v["oldest_days"] for v in visible if v["oldest_days"] is not None), default=None)
	T.update(
		variance=T["allocatable"] - T["to_allocate"],
		buckets=len(buckets),
		shelves=len(shelves),
		coldroom_buckets=len(cr_buckets),
		varieties=len(visible),
		varieties_short=short,
		oldest_days=oldest,
		avg_age_days=round(age_x / T["stems"], 1) if T["stems"] else None,
		too_old_or_cooling=T["free"] - T["allocatable"],
		lines=sum(1 for ln in lines if ln.item_code in keep),
	)

	rows = []
	for v in visible:
		out = {k: val for k, val in v.items() if not k.startswith("_")}
		out["buckets"] = len(v["_buckets"])
		out["coldroom_buckets"] = len(v["_cr_buckets"])
		out["avg_age_days"] = round(v["_age_x"] / v["stems"], 1) if v["stems"] else None
		out["farms"] = [dict(farm=k, **c) for k, c in sorted(v["farms"].items())]
		out["by_len"] = v["by_len"]
		rows.append(out)
	rows.sort(key=lambda r: r["variety"])

	cold_rows = defaultdict(lambda: {"stems": 0.0, "_b": set()})
	for r in cold:
		if r.variety not in keep:
			continue
		c = cold_rows[(r.variety, r.item_name or r.variety, r.length, r.farm or "Unknown")]
		c["stems"] += _f(r.stems)
		c["_b"].add(r.bucket_id)
	coldroom = [
		{"variety": k[0], "item_name": k[1], "length": k[2], "farm": k[3], "stems": c["stems"], "buckets": len(c["_b"])}
		for k, c in cold_rows.items()
	]

	lo = 0
	band_out = []
	for i, e in enumerate((*AGE_EDGES, None)):
		band_out.append({"from_days": lo, "to_days": e, "stems": bands[i]})
		lo = e
	return {
		"success": True,
		"from_date": str(d_from),
		"to_date": str(d_to),
		"basis": basis,
		"coldroom_window": [str(w_from), str(w_to)],
		"farms": farms,
		"lengths": all_lengths,
		"kpis": T,
		"age_bands": band_out,
		"rows": rows,
		"coldroom": coldroom,
		"options": options,
	}


def _options():
	"""Farm and length pick lists (live shelf farms/lengths + every known farm)."""
	farms = set(frappe.db.sql_list(
		"""SELECT DISTINCT s.farm FROM `tabShelf` s INNER JOIN `tabShelf Item` si ON si.parent = s.name
		   WHERE si.stem_qty > 0 AND IFNULL(s.farm, '') != ''"""
	))
	for fs in region.REGIONS.values():
		farms |= set(fs)
	lengths = set(frappe.db.sql_list(
		"SELECT DISTINCT TRIM(stem_length) FROM `tabShelf Item` WHERE stem_qty > 0 AND IFNULL(stem_length, '') != ''"
	))
	return sorted(farms), sorted(lengths, key=_len_key)


@frappe.whitelist(methods=["GET"])
def get_stock_visibility(
	from_date=None,
	to_date=None,
	basis=None,
	region=None,
	farm=None,
	length=None,
	rose=None,
	q=None,
	customer=None,
	delivery_point=None,
	age=None,
	cover=None,
	demand=None,
):
	try:
		out = build(
			dict(from_date=from_date, to_date=to_date, basis=basis, region=region, farm=farm, length=length,
			     rose=rose, q=q, customer=customer, delivery_point=delivery_point, age=age, cover=cover, demand=demand)
		)
		farms, lens = _options()
		out["options"].update(farms=farms, lengths=sorted(set(lens) | set(out["lengths"]) - {"--"}, key=_len_key))
		return out
	except Exception:
		frappe.log_error("v2 get_stock_visibility")
		return {"success": False, "error": "Could not load stock visibility; the error was logged."}
