# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Orders & Workflow v2 (www/packhouse-dashboard-v2.html).

The old "Packhouse Workflow" and "Order Summary" pages merged into one. The unit
is an ITEM: one Sales Order line on one Order Pick List (a line with no pick
list is one item on its own), named "<order name> · <variety> · <length> · <OPL>".
Items are grouped by packing team (default) or by customer; both groupings
partition the same items, so totals are identical between views.

Numbers (stems unless the key says *_boxes):
  picked / issued / planned   per (pick list, line): Pick List Item.stock_qty,
                              issued = issued OR issued_offline, Packing Guide stems
  ordered / confirmed / allocated / packed / staged / loaded / dispatched
                              per line from pipeline.attach_pipeline; a line on
                              several pick lists carries them on its first in-scope
                              pick list only (0 on the others), so sums stay exact
  planned / complete / staged / loaded boxes   per pick list, counted once per
                              distinct pick list in a group
  boxes (ordered)            boxes.order_boxes over each order's primary items (KPI only)
Roll-ups are sums of items on the server; KPI = sum of sections = sum of items.

Date meaning: Sales Order delivery_date (README rule 4); submitted orders.
Farm / region: the ORDER's farm, region.order_farm_sql (Sales Order.farm,
custom_farm, else Order Pick List.farm) -- README rule 6 as amended; orders
with no farm anywhere drop out while a farm/region filter is on. The pick
list farm (Pick List Item origin) is shown per row as "origin_farm" only. Team: the pick list's team; unallocated lines drop out while a
team is chosen. The pipeline is attached to every line of the date range
first and the filters applied afterwards, so a Delivery Note row without
so_detail is attributed the same way whatever the filters are.
"""

from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import getdate, today

from upande_packhouse.api.v2.core import boxes as bx
from upande_packhouse.api.v2.core import pipeline
from upande_packhouse.api.v2.core import region as region_mod
from upande_packhouse.api.v2.core import rose as rose_mod

NO_TEAM = "No team"
NOT_ALLOCATED = "Not allocated"

STEM_MEASURES = (
	"ordered_stems",
	"confirmed_stems",
	"allocated_stems",
	"picked_stems",
	"issued_stems",
	"planned_stems",
	"packed_stems",
	"precooled_stems",
	"staged_stems",
	"loaded_stems",
	"dispatched_stems",
)

STAGE_LABEL = {
	"notalloc": "Not allocated",
	"allocating": "Allocating",
	"to_issue": "To issue",
	"issuing": "Issuing",
	"to_pack": "To pack",
	"packing": "Packing",
	"packed": "Packed",
	"precooling": "Precooling",
	"staged": "Staged",
	"loaded": "Loaded",
	"dispatched": "Dispatched",
	"invoiced": "Invoiced",
}
# Stage tabs on the page (Order Summary's buckets) -> the row stages they hold.
BUCKETS = {
	"notalloc": ("notalloc",),
	"issue": ("allocating", "to_issue", "issuing"),
	"pack": ("to_pack", "packing"),
	"packed": ("packed",),
	"precooling": ("precooling",),
	"staged": ("staged",),
	"loaded": ("loaded",),
	"dispatched": ("dispatched",),
	"invoiced": ("invoiced",),
}


def _num(v):
	try:
		return float(v or 0)
	except (TypeError, ValueError):
		return 0.0


def _pct(num, den):
	return round(num * 100.0 / den, 1) if den else None


def _s(v):
	return (v or "").strip() if isinstance(v, str) else (v or "")


# ---------------------------------------------------------------- filters


def _doc_orders(sales_order=None, delivery_note=None, sales_invoice=None):
	"""Sales Orders a document filter points at, or None when no document filter."""
	sets = []
	if sales_order:
		sets.append({sales_order})
	if delivery_note:
		sets.append(
			set(
				frappe.db.sql_list(
					"""SELECT DISTINCT against_sales_order FROM `tabDelivery Note Item`
				   WHERE parent = %s AND IFNULL(against_sales_order, '') != ''""",
					delivery_note,
				)
			)
		)
	if sales_invoice:
		sos = set(
			frappe.db.sql_list(
				"""SELECT sales_order FROM `tabSales Invoice Item`
			   WHERE parent = %(si)s AND IFNULL(sales_order, '') != ''
			   UNION
			   SELECT dni.against_sales_order FROM `tabSales Invoice Item` sii
			   INNER JOIN `tabDelivery Note Item` dni ON dni.parent = sii.delivery_note
			   WHERE sii.parent = %(si)s AND IFNULL(dni.against_sales_order, '') != ''
			   UNION
			   SELECT custom_so FROM `tabSales Invoice`
			   WHERE name = %(si)s AND IFNULL(custom_so, '') != ''""",
				{"si": sales_invoice},
			)
		)
		sets.append(sos)
	if not sets:
		return None
	out = sets[0]
	for s in sets[1:]:
		out = out & s
	return out


def _opl_farms(opls):
	"""{opl: set(farms)} -- Order Pick List.farm, else its Pick List Item farms."""
	out = {}
	missing = []
	for name, o in opls.items():
		if o.get("farm"):
			out[name] = {o.farm}
		else:
			missing.append(name)
	if missing:
		for r in frappe.db.sql(
			"""SELECT DISTINCT parent, farm FROM `tabPick List Item`
			   WHERE parent IN %(o)s AND IFNULL(farm, '') != ''""",
			{"o": tuple(missing)},
			as_dict=True,
		):
			out.setdefault(r.parent, set()).add(r.farm)
	return out


def _pairs(opl_names):
	"""{(opl, soi): {picked, issued, planned}} -- pipeline steps 4/5 kept per pair."""
	out = defaultdict(lambda: {"picked_stems": 0.0, "issued_stems": 0.0, "planned_stems": 0.0, "buckets": 0})
	if not opl_names:
		return out
	for r in frappe.db.sql(
		"""SELECT parent AS opl, sales_order_item AS soi, SUM(stock_qty) AS picked,
		          SUM(CASE WHEN issued = 1 OR issued_offline = 1 THEN stock_qty ELSE 0 END) AS issued,
		          COUNT(DISTINCT UPPER(bucket)) AS buckets
		   FROM `tabPick List Item` WHERE parent IN %(o)s
		   GROUP BY parent, sales_order_item""",
		{"o": tuple(opl_names)},
		as_dict=True,
	):
		c = out[(r.opl, r.soi)]
		c["picked_stems"] += _num(r.picked)
		c["issued_stems"] += _num(r.issued)
		c["buckets"] += int(r.buckets or 0)
	for r in frappe.db.sql(
		"""SELECT parent AS opl, sales_order_item AS soi, SUM(stems) AS stems
		   FROM `tabPacking Guide` WHERE parenttype = 'Order Pick List' AND parent IN %(o)s
		   GROUP BY parent, sales_order_item""",
		{"o": tuple(opl_names)},
		as_dict=True,
	):
		out[(r.opl, r.soi)]["planned_stems"] += _num(r.stems)
	return out


def _flow_times(opl):
	"""Per-box flow of one pick list, following mobile/api.py:
	Issue -> Pack -> Precooling -> Stage -> Load -> Dispatch (each its own stage).

	Takt time = issuing -> precooling. Issuing has no stamp of its own, so it is the pick list's
	last issued Pick List Item `modified` (every stem is issued before the order can be finished);
	precooling is the box's `precooling_in_at` (createPrecoolingEntry "in"). Per box: takt =
	precooling_in_at - issued_at; the order's takt is the average over boxes that have both.
	Packed = Box Label creation; staged = `precooled_at` (the staging scan takes the box out of
	precooling and sets it); loaded = the box's Loading Sheet Item creation; dispatched = delivery
	note posting. Boxes without a stamp fall back to the label's `modified` for staged/loaded, and
	show None for precooling. Times are 'YYYY-MM-DD HH:MM:SS' strings or None."""
	issued = frappe.db.sql(
		"SELECT MAX(modified) FROM `tabPick List Item` WHERE parent = %s AND issued = 1", opl
	)[0][0]
	rows = frappe.db.sql(
		"""SELECT bl.name, bl.box_number, bl.creation, bl.precooling_in_at, bl.precooled_at,
		          bl.staged, bl.loaded, bl.delivered, bl.modified, bl.delivery_note,
		          (SELECT MIN(lsi.creation) FROM `tabLoading Sheet Item` lsi WHERE lsi.box_label = bl.name) AS load_at,
		          (SELECT TIMESTAMP(dn.posting_date, dn.posting_time) FROM `tabDelivery Note` dn
		            WHERE dn.name = bl.delivery_note AND dn.docstatus = 1) AS dispatch_at
		   FROM `tabBox Label` bl
		   WHERE bl.order_pick_list = %s AND IFNULL(bl.farm_pack_lis, '') != ''
		   ORDER BY bl.box_number, bl.name""",
		opl,
		as_dict=True,
	)

	def ts(x):
		return str(x)[:19] if x else None

	def mins(a, b):
		return int((b - a).total_seconds() // 60) if a and b and b >= a else None

	boxes, takts = [], []
	for r in rows:
		in_stage = bool(r.staged or r.loaded or r.delivered)
		staged_at = r.precooled_at or (r.modified if in_stage else None)
		loaded_at = r.load_at or (r.modified if (r.loaded or r.delivered) else None)
		takt = mins(issued, r.precooling_in_at)
		if takt is not None:
			takts.append(takt)
		boxes.append(
			{
				"box": r.name,
				"no": r.box_number,
				"packed_at": ts(r.creation),
				"precooling_at": ts(r.precooling_in_at),
				"staged_at": ts(staged_at),
				"loaded_at": ts(loaded_at),
				"dispatched_at": ts(r.dispatch_at),
				"takt_mins": takt,
				"precooling_mins": mins(r.precooling_in_at, staged_at),
			}
		)
	return {
		"issued_at": ts(issued),
		"boxes": boxes,
		"avg_takt_mins": int(round(sum(takts) / len(takts))) if takts else None,
		"takt_boxes": len(takts),
	}


def _issue_counts(opl_names):
	"""{opl: packing issues} -- Packing Bypass Log rows + under-packed pack-list rows."""
	out = defaultdict(int)
	if not opl_names:
		return out
	for o, n in frappe.db.sql(
		"""SELECT order_pick_list, COUNT(*) FROM `tabPacking Bypass Log`
		   WHERE order_pick_list IN %(o)s GROUP BY order_pick_list""",
		{"o": tuple(opl_names)},
	):
		out[o] += int(n)
	for o, n in frappe.db.sql(
		"""SELECT fpl.order_pick_list, COUNT(*) FROM `tabFarm Packlist Item` fpi
		   INNER JOIN `tabFarm Pack List` fpl ON fpl.name = fpi.parent
		   WHERE fpl.docstatus < 2 AND fpl.order_pick_list IN %(o)s
		     AND IFNULL(fpi.under_pack_reason, '') != ''
		   GROUP BY fpl.order_pick_list""",
		{"o": tuple(opl_names)},
	):
		out[o] += int(n)
	return out


def _documents(sos):
	"""({so: [delivery notes]}, {so: [sales invoices]}) -- submitted documents."""
	dns, sis = defaultdict(list), defaultdict(list)
	if not sos:
		return dns, sis
	for so, dn in frappe.db.sql(
		"""SELECT DISTINCT dni.against_sales_order, dn.name
		   FROM `tabDelivery Note Item` dni INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		   WHERE dn.docstatus = 1 AND dni.against_sales_order IN %(s)s
		   ORDER BY dn.creation""",
		{"s": tuple(sos)},
	):
		if dn not in dns[so]:
			dns[so].append(dn)
	for so, si in frappe.db.sql(
		"""SELECT so_name, name FROM (
		     SELECT si.custom_so AS so_name, si.name, si.creation FROM `tabSales Invoice` si
		     WHERE si.docstatus = 1 AND si.custom_so IN %(s)s
		     UNION
		     SELECT sii.sales_order, si.name, si.creation FROM `tabSales Invoice Item` sii
		     INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
		     WHERE si.docstatus = 1 AND sii.sales_order IN %(s)s
		   ) x ORDER BY creation""",
		{"s": tuple(sos)},
	):
		if si not in sis[so]:
			sis[so].append(si)
	return dns, sis


# ---------------------------------------------------------------- items
#
# The unit of this page is an ITEM = one Sales Order line on one pick list
# (or, for a line with no pick list, the line alone). Team sections and
# customer sections are two groupings of the same items, so their totals are
# identical for identical filters.
#
# A line on several pick lists (2 lines on this site) is listed once per pick
# list: picked / issued / planned are that pick list's own rows. Figures the
# pipeline only knows per LINE (ordered, confirmed, allocated, packed, staged,
# loaded, dispatched) are attributed to the line's FIRST in-scope pick list
# (`primary`), and are 0 on its other items, so every sum over items equals the
# line figure once. Pick-list box counts (planned / complete / staged / loaded)
# are counted once per distinct pick list.


def _stage(it):
	if not it["opl"]:
		return "notalloc"
	if it["sales_invoices"] and it["dispatched_stems"] > 0:
		return "invoiced"
	if it["dispatched_stems"] > 0:
		return "dispatched"
	if it["docstatus"] == 0:
		return "allocating"
	if it["loaded_stems"] > 0:
		return "loaded"
	if it["staged_stems"] > 0:
		return "staged"
	if it["precooled_stems"] > 0:
		return "precooling"
	if it["planned_stems"] and it["packed_stems"] >= it["planned_stems"] - 0.5:
		return "packed"
	if it["packed_stems"] > 0:
		return "packing"
	if it["picked_stems"] > 0 and it["issued_stems"] >= it["picked_stems"] - 0.5:
		return "to_pack"
	if it["issued_stems"] > 0:
		return "issuing"
	return "to_issue"


def _flags(it, today_d):
	"""Problems on an item, most severe first: [{code, label, sev}] (sev bad|warn)."""
	st = it["stage"]
	f = []
	due = getdate(it["delivery_date"]) if it["delivery_date"] else None
	days = (due - today_d).days if due else None
	packed_done = st in ("packed", "precooling", "staged", "loaded", "dispatched", "invoiced")
	if days is not None and days < 0 and st not in ("dispatched", "invoiced"):
		if st == "notalloc":
			f.append({"code": "late", "label": "Past due · not allocated", "sev": "bad"})
		elif not packed_done:
			f.append({"code": "late", "label": "Past due · not packed", "sev": "bad"})
		else:
			f.append({"code": "late_dispatch", "label": "Past due · not dispatched", "sev": "warn"})
	elif days == 0 and not packed_done:
		f.append(
			{
				"code": "due_today",
				"label": "Due today · not packed",
				"sev": "bad" if st == "notalloc" else "warn",
			}
		)
	elif days == 1 and st == "notalloc":
		f.append({"code": "due_soon", "label": "Due tomorrow · not allocated", "sev": "warn"})
	if it["opl"]:
		if it["packed_stems"] > it["issued_stems"] + 0.5:
			f.append({"code": "packed_unissued", "label": "Packed more than issued", "sev": "warn"})
		elif (
			it["docstatus"] == 1
			and it["picked_stems"] > 0
			and it["issued_stems"] == 0
			and it["packed_stems"] == 0
		):
			f.append({"code": "not_issued", "label": "Allocated · not issued", "sev": "warn"})
		elif st == "to_pack":
			f.append({"code": "not_packing", "label": "Issued · not packing", "sev": "warn"})
		if it["primary"] and it["ordered_stems"] and it["picked_stems"] < it["ordered_stems"] - 0.5:
			f.append({"code": "short", "label": "Short on pick list", "sev": "warn"})
	f.sort(key=lambda x: 0 if x["sev"] == "bad" else 1)
	return f


def _sum_items(items, opls):
	"""Stem sums over items; box counts once per distinct pick list; percentages."""
	t = {m: float(sum(i[m] for i in items)) for m in STEM_MEASURES}
	names = {i["opl"] for i in items if i["opl"]}
	t["planned_boxes"] = sum(opls[o].planned_box_count for o in names)
	t["complete_boxes"] = sum(opls[o].complete_box_count for o in names)
	t["precooled_boxes"] = sum(opls[o].precooled_boxes for o in names)
	t["staged_boxes"] = sum(opls[o].staged_boxes for o in names)
	t["loaded_boxes"] = sum(opls[o].loaded_boxes for o in names)
	t["issuing_pct"] = _pct(t["issued_stems"], t["picked_stems"])
	t["packing_pct"] = _pct(t["packed_stems"], t["planned_stems"])
	t["allocated_pct"] = _pct(t["picked_stems"], t["ordered_stems"])
	t["dispatched_pct"] = _pct(t["dispatched_stems"], t["ordered_stems"])
	t["lines"] = len(items)
	t["orders"] = len({i["sales_order"] for i in items})
	t["opls"] = len(names)
	t["bad"] = sum(1 for i in items if i["severity"] == "bad")
	t["warn"] = sum(1 for i in items if i["severity"] == "warn")
	return t


@frappe.whitelist()
def get_orders_workflow(
	from_date: Any = None,
	to_date: Any = None,
	region: Any = None,
	farm: Any = None,
	team: Any = None,
	rose_type: Any = None,
	item_group: Any = None,
	customer: Any = None,
	sales_order: Any = None,
	delivery_note: Any = None,
	sales_invoice: Any = None,
	q: Any = None,
	stage: Any = None,
	view: Any = None,
):
	"""Lines grouped by team (view=team, default) or customer (view=customer), plus KPIs."""
	from_date = from_date or today()
	to_date = to_date or from_date
	if str(from_date) > str(to_date):
		from_date, to_date = to_date, from_date
	today_d = getdate(today())
	farm, team, customer = _s(farm), _s(team), _s(customer)
	item_group, q, stage = _s(item_group), _s(q).lower(), _s(stage) or "all"
	view = "customer" if _s(view) == "customer" else "team"

	params = {}
	extra = rose_mod.rose_sql("soi.item_group", rose_type, params)
	if item_group:
		extra += " AND soi.item_group = %(ow_ig)s"
		params["ow_ig"] = item_group
	if customer:
		extra += " AND so.customer = %(ow_cust)s"
		params["ow_cust"] = customer

	doc_sos = _doc_orders(_s(sales_order), _s(delivery_note), _s(sales_invoice))
	farms = region_mod.farms_for(region=region, farm=farm)

	# Pipeline over every line of the range (attribution independent of filters).
	all_lines = pipeline.fetch_lines(delivery_from=from_date, delivery_to=to_date)
	opls = pipeline.attach_pipeline(all_lines)
	keep = (
		set(
			frappe.db.sql_list(
				f"""SELECT soi.name FROM `tabSales Order Item` soi
				   INNER JOIN `tabSales Order` so ON so.name = soi.parent
				   WHERE soi.name IN %(ow_all)s {extra}""",  # nosemgrep: extra is fixed SQL with bound values
				dict(params, ow_all=tuple(ln.name for ln in all_lines) or ("",)),
			)
		)
		if (extra and all_lines)
		else {ln.name for ln in all_lines}
	)
	lines = [ln for ln in all_lines if ln.name in keep and (doc_sos is None or ln.parent in doc_sos)]

	opl_farm = _opl_farms(opls)  # pick list farm: where the stems came from (origin), display only
	so_farm = {
		r.name: r.farm
		for r in frappe.db.sql(
			f"""SELECT so.name, {region_mod.order_farm_sql("so", None)} AS farm
			   FROM `tabSales Order` so WHERE so.name IN %(s)s""",  # nosemgrep: fixed SQL, bound values
			{"s": tuple({ln.parent for ln in lines}) or ("",)},
			as_dict=True,
		)
	}

	def order_farm(so, opl=None):
		"""The ORDER's farm (README rule 6): Sales Order.farm, custom_farm, else the pick list's."""
		f = so_farm.get(so)
		if not f and opl and opl_farm.get(opl):
			f = sorted(opl_farm[opl])[0]
		return f or ""

	def farm_ok(so, opl=None):
		return farms is None or order_farm(so, opl) in farms

	cells = _pairs(list(opls))
	order_names = {
		r.name: r.custom_order_name
		for r in frappe.get_all(
			"Sales Order",
			filters={"name": ["in", list({ln.parent for ln in lines}) or [""]]},
			fields=["name", "custom_order_name"],
		)
	}
	opl_order_name = {
		r.name: r.order_name
		for r in frappe.get_all(
			"Order Pick List", filters={"name": ["in", list(opls) or [""]]}, fields=["name", "order_name"]
		)
	}

	dns, sis = _documents({ln.parent for ln in lines})

	# One item per (line, pick list); a line with none is one item of its own.
	items = []
	for ln in lines:
		in_scope = sorted(
			o for o in ln.opls if (not team or (opls[o].get("team") or "") == team) and farm_ok(ln.parent, o)
		)
		if not ln.opls:
			if team or not farm_ok(ln.parent):
				continue
			in_scope = [None]
		for i, o in enumerate(in_scope):
			primary = i == 0
			cell = cells.get((o, ln.name)) if o else None
			opl = opls.get(o) if o else None
			oname = (opl_order_name.get(o) if o else None) or order_names.get(ln.parent) or ln.parent
			it = {
				"key": "{0}::{1}".format(o or "so", ln.name),
				"soi": ln.name,
				"opl": o,
				"docstatus": opl.get("docstatus") if opl else None,
				"team": (opl.get("team") if opl else None) or "",
				"sales_order": ln.parent,
				"order_name": oname,
				"customer": ln.customer,
				"customer_name": ln.customer_name or ln.customer,
				"delivery_date": str(ln.delivery_date) if ln.delivery_date else None,
				"variety": ln.item_code,
				"item_name": ln.item_name,
				"item_group": ln.item_group,
				"rose_type": ln.rose_type,
				"length": ln.custom_length,
				"box_type": ln.custom_box_type,
				"mix_name": ln.custom_mix_name,
				"farm": order_farm(ln.parent, o),
				"origin_farm": ", ".join(sorted(opl_farm.get(o, set()))) if o else "",
				"primary": primary,
				"line_boxes": ln.boxes if primary else 0,
				"stems_per_box": ln.stems_per_box,
				"picked_stems": cell["picked_stems"] if cell else 0.0,
				"issued_stems": cell["issued_stems"] if cell else 0.0,
				"planned_stems": cell["planned_stems"] if cell else 0.0,
				"delivery_notes": dns.get(ln.parent, []),
				"sales_invoices": sis.get(ln.parent, []),
			}
			for m in (
				"ordered_stems",
				"confirmed_stems",
				"allocated_stems",
				"packed_stems",
				"precooled_stems",
				"staged_stems",
				"loaded_stems",
				"dispatched_stems",
			):
				it[m] = float(ln[m] or 0) if primary else 0.0
			it["name"] = " · ".join([oname, ln.item_code or "", ln.custom_length or "", o or "No pick list"])
			it["stage"] = _stage(it)
			it["stage_label"] = STAGE_LABEL[it["stage"]]
			it["flags"] = _flags(it, today_d)
			it["severity"] = it["flags"][0]["sev"] if it["flags"] else ""
			items.append(it)

	if q:
		items = [
			i
			for i in items
			if q
			in " ".join(
				str(x or "")
				for x in [i["name"], i["sales_order"], i["customer"], i["customer_name"], i["team"], i["opl"]]
				+ i["delivery_notes"]
				+ i["sales_invoices"]
			).lower()
		]

	# Stage counts over every filter except the stage itself.
	counts = {"all": len(items)}
	for b, stages in BUCKETS.items():
		counts[b] = sum(1 for i in items if i["stage"] in stages)
	if stage != "all" and stage in BUCKETS:
		items = [i for i in items if i["stage"] in BUCKETS[stage]]

	# Pick-list level facts for the drill-down and the issue count.
	names = sorted({i["opl"] for i in items if i["opl"]})
	issues = _issue_counts(names)
	for i in items:
		o = opls.get(i["opl"]) if i["opl"] else None
		i["opl_boxes"] = (
			{
				"planned": o.planned_box_count,
				"complete": o.complete_box_count,
				"precooled": o.precooled_boxes,
				"staged": o.staged_boxes,
				"loaded": o.loaded_boxes,
			}
			if o
			else None
		)
		i["packing_issues"] = issues.get(i["opl"], 0) if i["opl"] else 0

	sev_rank = {"bad": 0, "warn": 1, "": 2}
	groups = defaultdict(list)
	for i in items:
		if view == "team":
			key = (2, NOT_ALLOCATED) if not i["opl"] else ((0, i["team"]) if i["team"] else (1, NO_TEAM))
		else:
			key = (0, i["customer"] or "—")
		groups[key].append(i)
	sections = []
	for key, rs in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
		rs.sort(key=lambda r: (sev_rank[r["severity"]], r["delivery_date"] or "", r["name"]))
		tot = _sum_items(rs, opls)
		if view == "team":
			kind, name, skey = ("team", "noteam", "unallocated")[key[0]], key[1], key[1]
		else:
			kind, skey = "customer", key[1]
			name = rs[0]["customer_name"] or key[1]
		sections.append(
			{
				"key": skey,
				"name": name,
				"kind": kind,
				"totals": tot,
				"stage_counts": {k: sum(1 for r in rs if r["stage"] in v) for k, v in BUCKETS.items()},
				"rows": rs,
			}
		)
	if view == "customer":
		sections.sort(key=lambda x: x["name"].lower())

	k = _sum_items(items, opls)
	k.update(
		rows=len(items),
		ready_to_issue=sum(1 for i in items if i["stage"] in ("to_issue", "issuing")),
		unallocated_lines=sum(1 for i in items if not i["opl"]),
		unallocated_orders=len({i["sales_order"] for i in items if not i["opl"]}),
		packing_issues=sum(issues.values()),
		customers=len({i["customer"] for i in items}),
	)
	# Ordered boxes: physical boxes once per order over each line's primary item.
	by_so = defaultdict(list)
	prim = {ln.name: ln for ln in lines}
	used = {i["soi"] for i in items if i["primary"]}
	for s_ in used:
		by_so[prim[s_].parent].append(prim[s_])
	k["boxes"] = sum(bx.order_boxes(v) for v in by_so.values())
	k["to_deliver_stems"] = max(0.0, k["ordered_stems"] - k["dispatched_stems"])
	return {
		"success": True,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"today": str(today_d),
		"view": view,
		"farms": farms,
		"kpis": k,
		"stage_counts": counts,
		"sections": sections,
	}


@frappe.whitelist()
def get_opl_detail(opl: Any):
	"""Drill-down extras for one pick list: shelves and packing issues."""
	if not opl or not frappe.db.exists("Order Pick List", opl):
		return {"success": False, "error": "Pick list not found."}
	shelves = frappe.db.sql_list(
		"""SELECT DISTINCT TRIM(shelf) FROM `tabPick List Item`
		   WHERE parent = %s AND IFNULL(TRIM(shelf), '') != '' ORDER BY 1""",
		opl,
	)
	issues = []
	for r in frappe.get_all(
		"Packing Bypass Log",
		filters={"order_pick_list": opl},
		fields=["box_id", "reason", "bunches", "packed_by", "creation"],
		order_by="creation desc",
	):
		issues.append(
			{
				"type": "Bypass",
				"box_id": r.box_id,
				"reason": r.reason,
				"quantity": r.bunches,
				"unit": "bunches",
				"packed_by": r.packed_by,
				"creation": str(r.creation),
			}
		)
	for r in frappe.db.sql(
		"""SELECT fpi.box_id, fpi.under_pack_reason AS reason, fpi.stock_qty, fpi.item_code,
		          fpl.owner AS packed_by, fpl.creation
		   FROM `tabFarm Packlist Item` fpi
		   INNER JOIN `tabFarm Pack List` fpl ON fpl.name = fpi.parent
		   WHERE fpl.order_pick_list = %s AND fpl.docstatus < 2
		     AND IFNULL(fpi.under_pack_reason, '') != ''
		   ORDER BY fpl.creation DESC""",
		opl,
		as_dict=True,
	):
		issues.append(
			{
				"type": "Under-pack",
				"box_id": r.box_id,
				"reason": r.reason,
				"quantity": _num(r.stock_qty),
				"unit": "stems (" + (r.item_code or "") + ")",
				"packed_by": r.packed_by,
				"creation": str(r.creation),
			}
		)
	return {
		"success": True,
		"opl": opl,
		"shelves": shelves,
		"packing_issues": issues,
		"flow": _flow_times(opl),
	}


@frappe.whitelist()
def get_filter_options():
	"""Teams (Packing Teams + any team on a pick list), farms and item groups."""
	teams = set(frappe.get_all("Packing Teams", pluck="name"))
	teams |= set(
		frappe.db.sql_list("SELECT DISTINCT team FROM `tabOrder Pick List` WHERE IFNULL(team, '') != ''")
	)
	farms = frappe.get_all("Farm", pluck="name", order_by="name asc")
	groups = frappe.db.sql_list(
		"""SELECT DISTINCT item_group FROM `tabSales Order Item`
		   WHERE IFNULL(TRIM(item_group), '') != '' ORDER BY item_group"""
	)
	return {"success": True, "teams": sorted(teams), "farms": farms, "item_groups": groups}


@frappe.whitelist()
def get_document_dates(doctype: Any, name: Any):
	"""Delivery-date span of the Sales Orders a document belongs to, so picking a
	document from the search moves the page's date range onto it."""
	kw = {"Sales Order": "sales_order", "Delivery Note": "delivery_note", "Sales Invoice": "sales_invoice"}
	if doctype not in kw or not name:
		return {"success": False, "error": "Choose a sales order, delivery note or sales invoice."}
	sos = _doc_orders(**{kw[doctype]: name}) or set()
	if not sos:
		return {"success": True, "from_date": None, "to_date": None, "sales_orders": []}
	lo, hi = frappe.db.sql(
		"SELECT MIN(delivery_date), MAX(delivery_date) FROM `tabSales Order` WHERE name IN %(s)s",
		{"s": tuple(sos)},
	)[0]
	return {
		"success": True,
		"from_date": str(lo) if lo else None,
		"to_date": str(hi) if hi else None,
		"sales_orders": sorted(sos),
	}
