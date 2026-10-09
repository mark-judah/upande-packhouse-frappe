# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""The order-line pipeline: one row per Sales Order line, every stage in stems.

    ordered -> confirmed -> allocated -> picked/issued -> planned -> packed
            -> precooling -> staged -> loaded -> delivered / dispatched

Every v2 page that shows an order, a pick list, a box or a delivery figure
reads it from here, so Workflow, Order Summary, Order Fulfilment, Allocation
Planning, the Scheduler and the Sales Order list can't disagree.

Sources (one grouped query each, keyed by line or pick list):

  ordered      Sales Order Item.stock_qty                         (units.ordered_stems)
  boxes        box_key dedup                                     (boxes.py)
  confirmed    Confirmed Stems.stems by sales_order_item
  allocated    Bucket Allocations.quantity_allocated, not cancelled  -- the
               allocator's own coverage figure (_cumulative_allocated_stems)
  picked       Pick List Item.stock_qty on live pick lists; issued = issued=1
  planned      Packing Guide rows (box_number, variety, stems)
  packed       Farm Packlist Item.stock_qty on non-cancelled Farm Pack Lists,
               attributed to a line through the packing guide's
               (box, variety) -> sales_order_item
  staged/      Box Labels that came from a Farm Pack List (test and manual
  loaded/      labels excluded, OR-S7), stems = that box's packed stems
  delivered    (never boxes x packrate: OR-S3/S4); flags are cumulative, a
               loaded box counts as staged too (OR-S5)
  dispatched   submitted, non-return Delivery Note Items by so_detail (OR-S1)

The line -> pick list link comes from the Packing Guide and Pick List Item
`sales_order_item` columns (both 100% consistent with the pick list's own
sales order), NEVER from Sales Order Item.custom_opl, which is copied on
duplicate and left dangling on delete (audit OR-X1, WF-1: 103 dangling,
56 pointing at another order's pick list).
"""

from collections import defaultdict

import frappe

from upande_packhouse.api.v2.core import boxes as bx
from upande_packhouse.api.v2.core import units
from upande_packhouse.api.v2.core.rose import rose_type

STAGES = ("ordered", "allocated", "issued", "packed", "precooling", "staged", "loaded", "dispatched")

LINE_FIELDS = """
	soi.name, soi.parent, soi.idx, soi.item_code, soi.item_name, soi.item_group,
	soi.uom, soi.qty, soi.stock_qty, soi.conversion_factor, soi.rate, soi.amount,
	soi.custom_length, soi.custom_box_type, soi.custom_number_of_boxes,
	soi.custom_packrate, soi.custom_packrate_mixed_box,
	soi.custom_mixed_box, soi.custom_mixed_bunch, soi.custom_mix_group,
	soi.custom_bunch_group, soi.custom_line, soi.custom_mix_name,
	soi.custom_processing_location,
	so.customer, so.customer_name, so.delivery_date, so.transaction_date,
	so.docstatus AS so_docstatus, so.status AS so_status, so.farm AS so_farm,
	so.currency, so.order_type, so.po_no, so.custom_consignee,
	so.custom_delivery_point, so.custom_shipping_agent
"""


def _num(v):
	try:
		return float(v or 0)
	except (TypeError, ValueError):
		return 0.0


def _box_no(v):
	try:
		return int(float(v))
	except (TypeError, ValueError):
		return None


# ---------------------------------------------------------------- lines


def fetch_lines(
	*,
	delivery_from=None,
	delivery_to=None,
	sales_orders=None,
	docstatus=(1,),
	business_unit="Roses",
	extra_where="",
	params=None,
):
	"""Sales Order lines for a delivery-date range and/or explicit orders.

	`docstatus`: which orders count (default submitted only; drafts are never
	mixed into KPIs unless a page asks). `extra_where` / `params` let a page add
	a bound condition (e.g. a rose filter from rose.rose_sql on soi.item_group).
	"""
	params = dict(params or {})
	where = ["so.docstatus IN %(ph2_ds)s"]
	params["ph2_ds"] = tuple(docstatus)
	if business_unit:
		where.append("so.business_unit = %(ph2_bu)s")
		params["ph2_bu"] = business_unit
	if delivery_from:
		where.append("so.delivery_date >= %(ph2_from)s")
		params["ph2_from"] = delivery_from
	if delivery_to:
		where.append("so.delivery_date <= %(ph2_to)s")
		params["ph2_to"] = delivery_to
	if sales_orders is not None:
		if not sales_orders:
			return []
		where.append("so.name IN %(ph2_sos)s")
		params["ph2_sos"] = tuple(sales_orders)
	sql = f"""
		SELECT {LINE_FIELDS}
		FROM `tabSales Order Item` soi
		INNER JOIN `tabSales Order` so ON so.name = soi.parent
		WHERE {" AND ".join(where)} {extra_where}
		  AND IFNULL(soi.item_code, '') != ''
		ORDER BY so.delivery_date, so.name, soi.idx
	"""
	lines = frappe.db.sql(sql, params, as_dict=True)  # nosemgrep: the f-string holes are fixed SQL, values are bound
	for ln in lines:
		ln.kind = units.line_kind(ln)
		ln.box_key = bx.box_key(ln)
		ln.boxes = int(ln.custom_number_of_boxes or 0)
		ln.stems_per_box = units.stems_per_box(ln)
		ln.stems_per_bunch = units.stems_per_bunch(ln.uom)
		ln.ordered_stems = units.ordered_stems(ln)
		ln.ordered_bunches = units.to_bunches(ln.ordered_stems, ln.uom)
		ln.rose_type = rose_type(ln.item_group)
	return lines


# ------------------------------------------------------------ pipeline


def _new_opl(name):
	return frappe._dict(
		name=name,
		lines=set(),
		planned_boxes=set(),
		planned_stems=0.0,
		planned_bunches=0.0,
		picked_stems=0.0,
		issued_stems=0.0,
		packed_stems=0.0,
		packed_boxes=set(),
		complete_boxes=set(),
		under_packed_boxes=set(),
		labels=0,
		precooled_boxes=0,
		staged_boxes=0,
		loaded_boxes=0,
		delivered_boxes=0,
		precooled_stems=0.0,
		staged_stems=0.0,
		loaded_stems=0.0,
		delivered_stems=0.0,
	)


def attach_pipeline(lines):
	"""Annotate `lines` (from fetch_lines) with every stage, in place.

	Returns {opl_name: opl_facts} for the pick lists those lines belong to.
	Line fields added: opls, team, opl_farm, confirmed_stems, allocated_stems,
	outstanding_stems, picked_stems, issued_stems, picked_buckets,
	planned_stems, packed_stems, staged_stems, loaded_stems,
	delivered_stems, dispatched_stems.
	"""
	for ln in lines:
		ln.update(
			opls=[],
			team="",
			opl_farm="",
			confirmed_stems=0.0,
			allocated_stems=0.0,
			outstanding_stems=0.0,
			picked_stems=0.0,
			issued_stems=0.0,
			picked_buckets=0,
			planned_stems=0.0,
			packed_stems=0.0,
			precooled_stems=0.0,
			staged_stems=0.0,
			loaded_stems=0.0,
			delivered_stems=0.0,
			dispatched_stems=0.0,
		)
	if not lines:
		return {}
	by_name = {ln.name: ln for ln in lines}
	soi = tuple(by_name)
	sos = tuple({ln.parent for ln in lines})

	# 1. line -> pick lists (Packing Guide + Pick List Item, live pick lists only)
	link_rows = frappe.db.sql(
		"""
		SELECT pg.sales_order_item AS soi, pg.parent AS opl
		FROM `tabPacking Guide` pg
		INNER JOIN `tabOrder Pick List` o ON o.name = pg.parent AND o.docstatus < 2
		WHERE pg.parenttype = 'Order Pick List' AND pg.sales_order_item IN %(soi)s
		UNION
		SELECT pli.sales_order_item AS soi, pli.parent AS opl
		FROM `tabPick List Item` pli
		INNER JOIN `tabOrder Pick List` o ON o.name = pli.parent AND o.docstatus < 2
		WHERE pli.sales_order_item IN %(soi)s
		""",
		{"soi": soi},
		as_dict=True,
	)
	opls = {}
	for r in link_rows:
		opls.setdefault(r.opl, _new_opl(r.opl)).lines.add(r.soi)
		if r.opl not in by_name[r.soi].opls:
			by_name[r.soi].opls.append(r.opl)
	opl_names = tuple(opls) or ("",)

	if opls:
		for o in frappe.db.sql(
			"""SELECT name, docstatus, team, farm, date_created, schedule_number, sales_order,
			          mix_group, bunch_group
			   FROM `tabOrder Pick List` WHERE name IN %(o)s""",
			{"o": opl_names},
			as_dict=True,
		):
			opls[o.name].update(o)
		for ln in lines:
			teams = sorted({opls[o].get("team") or "" for o in ln.opls} - {""})
			farms = sorted({opls[o].get("farm") or "" for o in ln.opls} - {""})
			ln.team = ", ".join(teams)
			ln.opl_farm = ", ".join(farms)

	# 2. confirmed
	for r in frappe.db.sql(
		"""SELECT sales_order_item AS soi, SUM(stems) AS stems FROM `tabConfirmed Stems`
		   WHERE sales_order_item IN %(soi)s GROUP BY sales_order_item""",
		{"soi": soi},
		as_dict=True,
	):
		by_name[r.soi].confirmed_stems = _num(r.stems)

	# 3. allocated (allocator's ledger) and outstanding (allocated, not yet issued)
	for r in frappe.db.sql(
		"""SELECT sales_order_item AS soi, SUM(quantity_allocated) AS alloc,
		          SUM(CASE WHEN IFNULL(issued, 0) = 0 THEN quantity_allocated ELSE 0 END) AS outstanding
		   FROM `tabBucket Allocations`
		   WHERE sales_order_item IN %(soi)s AND IFNULL(cancelled, 0) = 0
		   GROUP BY sales_order_item""",
		{"soi": soi},
		as_dict=True,
	):
		by_name[r.soi].allocated_stems = _num(r.alloc)
		by_name[r.soi].outstanding_stems = _num(r.outstanding)

	if not opls:
		_attach_dispatch(lines, by_name, soi, sos)
		return {}

	# 4. picked / issued per line and per pick list
	for r in frappe.db.sql(
		"""SELECT pli.parent AS opl, pli.sales_order_item AS soi,
		          SUM(pli.stock_qty) AS picked,
		          SUM(CASE WHEN pli.issued = 1 OR pli.issued_offline = 1 THEN pli.stock_qty ELSE 0 END) AS issued,
		          COUNT(DISTINCT UPPER(pli.bucket)) AS buckets
		   FROM `tabPick List Item` pli
		   WHERE pli.parent IN %(o)s
		   GROUP BY pli.parent, pli.sales_order_item""",
		{"o": opl_names},
		as_dict=True,
	):
		o = opls[r.opl]
		o.picked_stems += _num(r.picked)
		o.issued_stems += _num(r.issued)
		ln = by_name.get(r.soi)
		if ln:
			ln.picked_stems += _num(r.picked)
			ln.issued_stems += _num(r.issued)
			ln.picked_buckets += int(r.buckets or 0)

	# 5. planned (Packing Guide); (opl, box, variety) -> line
	guide = defaultdict(dict)  # opl -> {(box, variety): soi}
	need = defaultdict(float)  # (opl, box, variety) -> planned stems
	for r in frappe.db.sql(
		"""SELECT parent AS opl, sales_order_item AS soi, box_number, variety, stems, bunches
		   FROM `tabPacking Guide` WHERE parenttype = 'Order Pick List' AND parent IN %(o)s""",
		{"o": opl_names},
		as_dict=True,
	):
		o = opls[r.opl]
		b = _box_no(r.box_number)
		o.planned_boxes.add(b)
		o.planned_stems += _num(r.stems)
		o.planned_bunches += _num(r.bunches)
		guide[r.opl][(b, r.variety)] = r.soi
		need[(r.opl, b, r.variety)] += _num(r.stems)
		ln = by_name.get(r.soi)
		if ln:
			ln.planned_stems += _num(r.stems)

	def owner(opl, box, item):
		"""The line a packed (box, variety) belongs to."""
		hit = guide[opl].get((box, item))
		if hit:
			return hit
		cands = [s for s in opls[opl].lines if s in by_name and by_name[s].item_code == item]
		if len(cands) == 1:
			return cands[0]
		if len(opls[opl].lines) == 1:
			return next(iter(opls[opl].lines))
		return None

	# 6. packed (Farm Pack List rows), keyed per (fpl, box) for the labels
	box_stems = defaultdict(lambda: defaultdict(float))  # (fpl, box) -> {soi|None: stems}
	have = defaultdict(float)  # (opl, box, variety) -> packed stems
	for r in frappe.db.sql(
		"""SELECT fpl.name AS fpl, fpl.order_pick_list AS opl, fpi.box_id, fpi.item_code,
		          fpi.stock_qty, fpi.under_pack_reason
		   FROM `tabFarm Packlist Item` fpi
		   INNER JOIN `tabFarm Pack List` fpl ON fpl.name = fpi.parent
		   WHERE fpl.docstatus < 2 AND fpl.order_pick_list IN %(o)s""",
		{"o": opl_names},
		as_dict=True,
	):
		o = opls[r.opl]
		b = _box_no(r.box_id) or 1
		stems = _num(r.stock_qty)
		o.packed_stems += stems
		o.packed_boxes.add(b)
		if r.under_pack_reason:
			o.under_packed_boxes.add(b)
		have[(r.opl, b, r.item_code)] += stems
		s = owner(r.opl, b, r.item_code)
		box_stems[(r.fpl, b)][s] += stems
		if s and s in by_name:
			by_name[s].packed_stems += stems

	# complete box = every guide (box, variety) met, or closed short on purpose
	for name, o in opls.items():
		for b in o.packed_boxes:
			if b in o.under_packed_boxes:
				o.complete_boxes.add(b)
				continue
			keys = [k for k in need if k[0] == name and k[1] == b]
			if keys and all(have[k] >= need[k] - 0.001 for k in keys):
				o.complete_boxes.add(b)

	# 7. box labels from a Farm Pack List; cumulative stage flags
	for r in frappe.db.sql(
		"""SELECT name, order_pick_list AS opl, farm_pack_lis AS fpl, box_number,
		          IFNULL(staged, 0) AS staged, IFNULL(loaded, 0) AS loaded, IFNULL(delivered, 0) AS delivered,
		          IFNULL(precooling, 0) AS precooling, IFNULL(precooled, 0) AS precooled
		   FROM `tabBox Label`
		   WHERE order_pick_list IN %(o)s AND IFNULL(farm_pack_lis, '') != ''""",
		{"o": opl_names},
		as_dict=True,
	):
		o = opls[r.opl]
		o.labels += 1
		delivered = bool(r.delivered)
		loaded = bool(r.loaded) or delivered
		staged = bool(r.staged) or loaded
		precooled = bool(r.precooling) or bool(r.precooled) or staged  # entered precooling (cumulative)
		parts = box_stems.get((r.fpl, _box_no(r.box_number) or 1), {})
		total = sum(parts.values())
		for flag, box_attr, stem_attr in (
			(precooled, "precooled_boxes", "precooled_stems"),
			(staged, "staged_boxes", "staged_stems"),
			(loaded, "loaded_boxes", "loaded_stems"),
			(delivered, "delivered_boxes", "delivered_stems"),
		):
			if not flag:
				continue
			o[box_attr] += 1
			o[stem_attr] += total
			for s, stems in parts.items():
				if s and s in by_name:
					by_name[s][stem_attr] += stems

	_attach_dispatch(lines, by_name, soi, sos)
	for o in opls.values():
		o.planned_box_count = len(o.planned_boxes)
		o.packed_box_count = len(o.packed_boxes)
		o.complete_box_count = len(o.complete_boxes)
	return opls


def _attach_dispatch(lines, by_name, soi, sos):
	"""Dispatched = submitted, non-return Delivery Note Items, per line (so_detail).
	A DN row without so_detail is attributed by (order, item) when that is unique."""
	for r in frappe.db.sql(
		"""SELECT dni.so_detail, dni.against_sales_order AS so, dni.item_code, SUM(dni.stock_qty) AS stems
		   FROM `tabDelivery Note Item` dni
		   INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		   WHERE dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0
		     AND (dni.so_detail IN %(soi)s
		          OR (IFNULL(dni.so_detail, '') = '' AND dni.against_sales_order IN %(sos)s))
		   GROUP BY dni.so_detail, dni.against_sales_order, dni.item_code""",
		{"soi": soi, "sos": sos},
		as_dict=True,
	):
		target = by_name.get(r.so_detail)
		if not target:
			cands = [ln for ln in lines if ln.parent == r.so and ln.item_code == r.item_code]
			target = cands[0] if len(cands) == 1 else None
		if target:
			target.dispatched_stems += _num(r.stems)


# ------------------------------------------------------------- roll-ups


def line_stage(ln):
	"""Furthest stage this line has fully reached, and whether the next is under way.

	Stages compare stems against what is ordered; a stage counts as reached
	when it covers the ordered stems (ignoring over-pack)."""
	ordered = ln.ordered_stems or 0
	reached, partial = "ordered", None
	ladder = (
		("allocated", ln.allocated_stems),
		("issued", ln.issued_stems),
		("packed", ln.packed_stems),
		("precooling", ln.precooled_stems),
		("staged", ln.staged_stems),
		("loaded", ln.loaded_stems),
		("dispatched", ln.dispatched_stems),
	)
	for stage, stems in ladder:
		if ordered and stems >= ordered - 0.001:
			reached, partial = stage, None
		elif stems > 0:
			partial = stage
			break
		else:
			break
	return reached, partial


def order_rollup(lines, opls):
	"""{sales_order: totals} — stems summed per line, boxes by box_key, box
	progress from that order's pick lists (planned / complete / staged / loaded)."""
	out = {}
	by_so = defaultdict(list)
	for ln in lines:
		by_so[ln.parent].append(ln)
	for so, rows in by_so.items():
		so_opls = {o for ln in rows for o in ln.opls}
		t = frappe._dict(
			sales_order=so,
			customer=rows[0].customer,
			customer_name=rows[0].customer_name,
			delivery_date=rows[0].delivery_date,
			lines=len(rows),
			boxes=bx.order_boxes(rows),
			opls=sorted(so_opls),
		)
		for f in (
			"ordered_stems",
			"confirmed_stems",
			"allocated_stems",
			"outstanding_stems",
			"picked_stems",
			"issued_stems",
			"planned_stems",
			"packed_stems",
			"precooled_stems",
			"staged_stems",
			"loaded_stems",
			"delivered_stems",
			"dispatched_stems",
		):
			t[f] = sum(ln[f] for ln in rows)
		t.planned_boxes = sum(opls[o].planned_box_count for o in so_opls if o in opls)
		t.complete_boxes = sum(opls[o].complete_box_count for o in so_opls if o in opls)
		t.precooled_boxes = sum(opls[o].precooled_boxes for o in so_opls if o in opls)
		t.staged_boxes = sum(opls[o].staged_boxes for o in so_opls if o in opls)
		t.loaded_boxes = sum(opls[o].loaded_boxes for o in so_opls if o in opls)
		t.delivered_boxes = sum(opls[o].delivered_boxes for o in so_opls if o in opls)
		out[so] = t
	return out


def opl_public(o):
	"""JSON-safe pick-list facts."""
	return {
		"name": o.name,
		"sales_order": o.get("sales_order"),
		"docstatus": o.get("docstatus"),
		"team": o.get("team"),
		"farm": o.get("farm"),
		"date_created": o.get("date_created"),
		"schedule_number": o.get("schedule_number"),
		"lines": sorted(o.lines),
		"planned_boxes": o.planned_box_count,
		"planned_stems": o.planned_stems,
		"planned_bunches": o.planned_bunches,
		"picked_stems": o.picked_stems,
		"issued_stems": o.issued_stems,
		"packed_stems": o.packed_stems,
		"packed_boxes": o.packed_box_count,
		"complete_boxes": o.complete_box_count,
		"labels": o.labels,
		"precooled_boxes": o.precooled_boxes,
		"staged_boxes": o.staged_boxes,
		"loaded_boxes": o.loaded_boxes,
		"delivered_boxes": o.delivered_boxes,
		"precooled_stems": o.precooled_stems,
		"staged_stems": o.staged_stems,
		"loaded_stems": o.loaded_stems,
		"delivered_stems": o.delivered_stems,
	}


LINE_PUBLIC = (
	"name",
	"parent",
	"idx",
	"customer",
	"customer_name",
	"delivery_date",
	"item_code",
	"item_name",
	"item_group",
	"rose_type",
	"custom_length",
	"uom",
	"kind",
	"box_key",
	"boxes",
	"stems_per_box",
	"stems_per_bunch",
	"custom_box_type",
	"custom_mix_name",
	"ordered_stems",
	"ordered_bunches",
	"confirmed_stems",
	"allocated_stems",
	"outstanding_stems",
	"picked_stems",
	"issued_stems",
	"picked_buckets",
	"planned_stems",
	"packed_stems",
	"precooled_stems",
	"staged_stems",
	"loaded_stems",
	"delivered_stems",
	"dispatched_stems",
	"opls",
	"team",
	"opl_farm",
	"so_farm",
)


def line_public(ln):
	d = {k: ln.get(k) for k in LINE_PUBLIC}
	d["stage"], d["stage_partial"] = line_stage(ln)
	return d
