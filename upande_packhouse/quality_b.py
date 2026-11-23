# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Quality B orders: local (webstore) orders packed from Grade B flowers.

Any Sales Order of order type "Shopping Cart" is a Quality B order. It is never planned or allocated on Sales
Allocation: when it is submitted, its pick lists are made here, from Grade B buckets only -- buckets whose stems
an approved Discard Request moved to their farm's "<Farm> Grade B" warehouse, still on a shelf, not discarded and
not held by another order -- oldest Grade B first, by variety. The release is the export one
(_allocate_stock_with_buckets_impl), with each bucket's stock taken from its Grade B warehouse; its pick lists are
marked Quality B so packing knows they are not export.

The pick lists go to the teams in Production Settings > Quality B Packing Teams of the order's sales farm: a new
order goes to the one with the fewest Quality B stems already waiting for that delivery day.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

ORDER_TYPE = "Shopping Cart"


#: The delivery point every Quality B order goes to: local sales, not an export agent.
DELIVERY_POINT = "Quality B Local Sales"


def is_quality_b(doc):
	return (doc.get("order_type") or "") == ORDER_TYPE


def before_validate(doc, method=None):
	"""Sales Order before_validate: a Quality B order goes to the Quality B delivery point, and is named for its
	customer when it has no order name, so its pick lists say whose local order they are."""
	if not is_quality_b(doc):
		return
	# Grade B flowers are Roses stock: the order routes as Roses unless its customer says otherwise
	if not (doc.get("business_unit") or doc.get("custom_business_unit")):
		unit = frappe.db.get_value("Customer", doc.customer, "custom_business_unit") or "Roses"
		for field in ("business_unit", "custom_business_unit"):
			if doc.meta.has_field(field):
				doc.set(field, unit)
	if doc.meta.has_field("custom_delivery_point"):
		if not frappe.db.exists("Delivery Point", DELIVERY_POINT):
			frappe.get_doc(
				{"doctype": "Delivery Point", "description": "Quality B (webstore) local sales"}
			).insert(ignore_permissions=True, set_name=DELIVERY_POINT)
		doc.custom_delivery_point = DELIVERY_POINT
	if doc.meta.has_field("custom_order_name") and not doc.get("custom_order_name"):
		doc.custom_order_name = "QB {0}".format(doc.customer_name or doc.customer or "")
	# sold by the stem (the webstore's unit): every line in its stock unit, keeping its stems -- a line the
	# webstore made has no unit yet, so its quantity is already stems
	for line in doc.items:
		if not line.item_code:
			continue
		stock_uom = line.get("stock_uom") or frappe.db.get_value("Item", line.item_code, "stock_uom")
		if line.get("uom") and line.uom != stock_uom:
			stems = flt(line.get("stock_qty")) or flt(line.qty) * flt(line.conversion_factor or 1)
		else:
			stems = flt(line.qty)
		line.uom = line.stock_uom = stock_uom
		line.conversion_factor = 1
		line.qty = stems
		line.stock_qty = stems
	# not boxed for export: a line with no packrate is one pack holding all its stems
	for line in doc.items:
		if line.get("custom_mixed_box"):
			continue
		stems = cint(flt(line.qty))
		if stems <= 0:
			continue
		if not frappe.db.exists("Packrate", str(stems)):
			frappe.get_doc({"doctype": "Packrate", "stems_per_box": stems}).insert(
				ignore_permissions=True, set_name=str(stems)
			)
		line.custom_packrate = str(stems)
		line.custom_number_of_boxes = 1


def on_submit(doc, method=None):
	"""Sales Order on_submit: a Quality B order gets its pick lists from Grade B buckets."""
	if not is_quality_b(doc):
		return
	make_pick_lists(doc)


def _sales_farm(doc):
	from upande_packhouse.api.remote_transfer.transfer_scheduling import order_hub, transfer_hub

	return order_hub(doc.name) or transfer_hub(required=False)


def grade_b_buckets(item_code, farms):
	"""[{bucket_id, farm, shelf, stem_length, warehouse, free, moved_on}] for `item_code` at `farms`: buckets in
	Grade B (on an approved Discard Request, moved, not discarded) and still on a shelf there, with the stems no
	other order holds -- oldest Grade B first."""
	rows = frappe.db.sql(
		"""SELECT drb.bucket_id, drb.grade_b_warehouse AS warehouse, drb.grade_b_qty, drb.grade_b_moved_on AS moved_on,
		          si.parent AS shelf, sh.farm, si.stem_length, si.stem_qty
		FROM `tabDiscard Request Bucket` drb
		JOIN `tabDiscard Request` dr ON dr.name = drb.parent AND dr.docstatus = 1
		JOIN `tabShelf Item` si ON si.bucket_id = drb.bucket_id AND si.variety = %(item)s
		JOIN `tabShelf` sh ON sh.name = si.parent
		WHERE drb.parenttype = 'Discard Request' AND IFNULL(drb.grade_b_entry, '') != ''
		  AND IFNULL(drb.discarded, 0) = 0 AND drb.variety = %(item)s AND sh.farm IN %(farms)s
		ORDER BY drb.grade_b_moved_on ASC, drb.bucket_id""",
		{"item": item_code, "farms": tuple(farms) or ("",)},
		as_dict=True,
	)
	out, seen = [], set()
	for r in rows:
		key = (str(r.bucket_id).upper(), r.stem_length or "")
		if key in seen:
			continue
		seen.add(key)
		held = flt(
			frappe.db.sql(
				"""SELECT SUM(ba.quantity_allocated) FROM `tabBucket Allocations` ba
				JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
				WHERE bas.bucket_id = %s AND bas.item_code = %s AND IFNULL(bas.stem_length, '') = %s
				  AND IFNULL(ba.cancelled, 0) = 0 AND IFNULL(ba.issued, 0) = 0""",
				(r.bucket_id, item_code, r.stem_length or ""),
			)[0][0]
		)
		free = min(flt(r.grade_b_qty) or flt(r.stem_qty), flt(r.stem_qty)) - held
		if free > 0:
			out.append({**r, "free": free})
	return out


def pick_team(sales_farm, delivery_date):
	"""The Quality B team of `sales_farm` with the fewest Quality B stems waiting for `delivery_date`, else None."""
	from upande_packhouse.api.v2.allocation_teams import team_sales_farms

	ps = frappe.get_cached_doc("Production Settings")
	home = team_sales_farms()
	teams = [
		r.team for r in ps.get("quality_b_packing_teams") or [] if r.team and home.get(r.team) == sales_farm
	]
	if not teams:
		return None
	load = defaultdict(float)
	for team, stems in frappe.db.sql(
		"""SELECT team, SUM(IFNULL(custom_total_stems, 0)) FROM `tabOrder Pick List`
		WHERE quality_b = 1 AND docstatus < 2 AND delivery_date = %s AND team IN %s GROUP BY team""",
		(delivery_date, tuple(teams)),
	):
		load[team] = flt(stems)
	return min(teams, key=lambda t: (load[t], teams.index(t)))


def make_pick_lists(doc):
	"""Allocate the order's lines from Grade B buckets and make its Quality B pick lists."""
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa

	sales_farm = _sales_farm(doc)
	config = sa._get_production_config()
	location = sales_farm if sales_farm in (config.get("farms_by_location") or {}) else None
	if not location:
		frappe.throw(_("{0}: no allocation location for sales farm {1}.").format(doc.name, sales_farm))
	# the sales farm's own Grade B first (no trip), then the other farms of its location: their buckets wait
	# for a trip to the sales farm like any remote bucket
	farms = [sales_farm] + [f for f in config["farms_by_location"][location] if f != sales_farm]
	team = pick_team(sales_farm, doc.delivery_date)
	if not team:
		frappe.throw(
			_(
				"No Quality B packing team for {0}: add one in Production Settings > Quality B Packing Teams."
			).format(sales_farm)
		)

	allocations, sources, short = [], {}, []
	for line in doc.items:
		need = flt(line.stock_qty) or flt(line.qty) * flt(line.conversion_factor or 1)
		# stems go out in whole bunches of the line's bunch size, as on every pick list
		bunch = max(1, cint(line.conversion_factor or 1))
		buckets = sorted(
			grade_b_buckets(line.item_code, farms),
			key=lambda b: (b["farm"] != sales_farm, str(b["moved_on"] or "")),
		)
		for b in buckets:
			if need < bunch:
				break
			take = (min(need, b["free"]) // bunch) * bunch
			if take <= 0:
				continue
			allocations.append(
				{
					"item_code": line.item_code,
					"bucket_id": b["bucket_id"],
					"qty": take,
					"sales_order_item": line.name,
					"stem_length": b["stem_length"],
					"warehouse": b["warehouse"],
					"uom": line.uom,
					"stock_uom": line.stock_uom,
					"conversion_factor": line.conversion_factor,
					"_farm": b["farm"],
				}
			)
			sources[str(b["bucket_id"]).upper()] = b["warehouse"]
			need -= take
		if need > 0:
			short.append("{0}: {1} stems short".format(line.item_code, int(need)))

	if not allocations:
		frappe.throw(
			_("{0}: no Grade B stock at {1} for any of its lines.").format(doc.name, ", ".join(farms))
		)

	frappe.flags.quality_b_sources = sources
	try:
		sa._allocate_stock_with_buckets_impl(
			doc.name, allocations, location, {a["sales_order_item"]: team for a in allocations}
		)
	finally:
		frappe.flags.quality_b_sources = None

	opls = {
		r.custom_opl
		for r in frappe.get_all(
			"Sales Order Item", filters={"parent": doc.name}, fields=["custom_opl"], ignore_permissions=True
		)
		if r.custom_opl
	}
	for opl in opls:
		frappe.db.set_value("Order Pick List", opl, "quality_b", 1, update_modified=False)
	submit_pick_lists(opls)
	doc.add_comment(
		"Info",
		_("Quality B pick lists {0} for team {1} from Grade B buckets at {2}.{3}").format(
			", ".join(sorted(opls)),
			team,
			", ".join(sorted({a["_farm"] for a in allocations})),
			(" " + "; ".join(short)) if short else "",
		),
	)
	return sorted(opls)


def submit_pick_lists(opls):
	"""A Quality B pick list is submitted with its order: short of stems, or with buckets still to come from
	another farm, it goes to packing as it is (the export gates -- every line in full, every bucket shelved at
	the sales farm -- are for export orders)."""
	for name in opls:
		opl = frappe.get_doc("Order Pick List", name)
		if opl.docstatus != 0:
			continue
		opl.quality_b = 1
		opl.flags.ignore_opl_submit_checks = True
		opl.flags.ignore_permissions = True
		opl.submit()


def grade_b_source(bucket_id):
	"""The Grade B warehouse a Quality B release takes `bucket_id`'s stems from, else None (an export release)."""
	sources = frappe.flags.get("quality_b_sources")
	return sources.get(str(bucket_id or "").upper()) if sources else None


# ---- Issuing any Grade B bucket of the right variety ------------------------------------------------------
# A Quality B pick list's buckets are suggestions: with Production Settings > Issue Any Grade B Bucket of the
# Same Variety, a packer may issue another Grade B bucket of a variety still to issue on the pick list (of the same
# stem length too, when Same Stem Length Too is ticked). The row takes the scanned bucket: its allocation and its
# stems move to it, and the suggested bucket's go back to its Grade B warehouse.


def _not_on(bucket, opl):
	return _("Bucket {0} isn't allocated to {1}.").format(bucket, opl)


@frappe.whitelist(methods=["POST"])
def take_scanned_bucket(opl_name: str, bucket: str):
	"""The issuing scan of a bucket that is not on `opl_name`: on a Quality B pick list (with the setting on) an
	unissued row of the bucket's variety takes it, so the scan can issue it. Anything else is refused as a bucket
	not on the pick list. Returns {status, row, old_bucket, new_bucket, qty}."""
	from upande_packhouse import stock_movement

	bucket = (bucket or "").strip()
	if not opl_name or not bucket or not frappe.db.exists("Order Pick List", opl_name):
		frappe.throw(_not_on(bucket, opl_name))
	opl = frappe.get_doc("Order Pick List", opl_name)
	ps = frappe.get_cached_doc("Production Settings")
	if not cint(opl.get("quality_b")) or not cint(ps.get("quality_b_issue_any_bucket")):
		frappe.throw(_not_on(bucket, opl_name))
	rows = opl.get("table_ytkc") or []
	if any(str(r.bucket or "").upper() == bucket.upper() for r in rows):
		return {"status": "on_pick_list", "new_bucket": bucket}
	open_rows = [r for r in rows if not cint(r.issued) and r.item_code]
	if not open_rows:
		frappe.throw(_("Everything on {0} is issued already.").format(opl_name))

	match_length = cint(ps.get("quality_b_match_stem_length"))
	farms = [opl.farm] if opl.farm else []
	candidates = []
	for item in sorted({r.item_code for r in open_rows}):
		for b in grade_b_buckets(item, farms):
			if str(b["bucket_id"]).upper() == bucket.upper():
				candidates.append((item, b))
	if not candidates:
		varieties = ", ".join(sorted({r.item_code for r in open_rows}))
		frappe.throw(
			_("Bucket {0} is not a Grade B bucket of {1} free at {2}.").format(bucket, varieties, opl.farm)
		)

	def fits(row, b):
		return not match_length or (row.stem_length or "") == (b["stem_length"] or "")

	# a row still waiting for its suggested bucket from another farm first, then the biggest
	choice = None
	for item, b in candidates:
		for row in sorted(open_rows, key=lambda r: (not cint(r.awaiting_transfer), -flt(r.stock_qty))):
			if row.item_code == item and fits(row, b):
				choice = (row, b)
				break
		if choice:
			break
	if not choice:
		frappe.throw(
			_("Bucket {0} is {1} at another stem length: this pick list needs the same length.").format(
				bucket, candidates[0][1]["stem_length"] or "-"
			)
		)
	row, b = choice
	qty = min(flt(row.stock_qty), flt(b["free"]))
	old = row.bucket
	business_unit = stock_movement.opl_business_unit(opl) or "Roses"
	stock_movement.lock_bucket(b["bucket_id"])

	# the suggested bucket gives back its allocation for this line, and its stems (if sold already) go back
	_release_line(old, row.item_code, row.sales_order_item)
	if old and not cint(row.awaiting_transfer):
		stock_movement.reverse_allocation_movement(
			row.sales_order_item, bucket_id=old, item_code=row.item_code, business_unit=business_unit
		)
	# the scanned bucket takes it: allocated to the line, its stems sold out of its own Grade B warehouse
	_hold_line(opl, b, row.item_code, row.sales_order_item, qty)
	stock_movement.move_allocation_to_sold(
		[
			{
				"bucket_id": b["bucket_id"],
				"item_code": row.item_code,
				"qty": qty,
				"sales_order_item": row.sales_order_item,
				"warehouse": b["warehouse"],
				"stem_length": b["stem_length"],
				"_shelf_farm": b["farm"],
				"_opl": opl_name,
			}
		],
		business_unit,
		sales_order=opl.sales_order,
		opl=opl_name,
	)
	frappe.db.set_value(
		"Pick List Item",
		row.name,
		{
			"bucket": b["bucket_id"],
			"shelf": b["shelf"],
			"farm": b["farm"],
			"source_warehouse": b["warehouse"],
			"origin_warehouse": b["warehouse"],
			"stem_length": b["stem_length"],
			"qty": qty / flt(row.conversion_factor or 1),
			"stock_qty": qty,
			"awaiting_transfer": 0,
			"custom_ready_for_packing": 1,
			"not_found": 0,
		},
	)
	opl.add_comment(
		"Info",
		_("Issuing took Grade B bucket {0} ({1} stems of {2}) instead of {3}.").format(
			b["bucket_id"], int(qty), row.item_code, old or "-"
		),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the scan issues it next, in its own request
	return {"status": "swapped", "row": row.name, "old_bucket": old, "new_bucket": b["bucket_id"], "qty": qty}


def _recompute(bas_name):
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
		recompute_bas_quantities,
	)

	bas = frappe.get_doc("Bucket Allocation Status", bas_name)
	recompute_bas_quantities(bas)
	# its other rows are other orders' history: only the quantities change here
	bas.flags.ignore_links = True
	bas.save(ignore_permissions=True)


def _release_line(bucket, item_code, sales_order_item):
	"""Cancel `bucket`'s allocation to the order line."""
	if not bucket:
		return
	for ba, parent in frappe.db.sql(
		"""SELECT ba.name, ba.parent FROM `tabBucket Allocations` ba
		JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		WHERE bas.bucket_id = %s AND bas.item_code = %s AND ba.sales_order_item = %s
		  AND IFNULL(ba.cancelled, 0) = 0 AND IFNULL(ba.issued, 0) = 0""",
		(bucket, item_code, sales_order_item),
	):
		frappe.db.set_value("Bucket Allocations", ba, "cancelled", 1, update_modified=False)
		_recompute(parent)


def _hold_line(opl, b, item_code, sales_order_item, qty):
	"""Allocate `qty` stems of Grade B bucket `b` to the order line (its Bucket Allocation Status, as a release
	keeps it: the stems wait in the bucket's Grade B warehouse)."""
	name = frappe.db.get_value(
		"Bucket Allocation Status",
		{"bucket_id": b["bucket_id"], "item_code": item_code, "stem_length": b["stem_length"] or ""},
	)
	if name:
		bas = frappe.get_doc("Bucket Allocation Status", name)
	else:
		bas = frappe.new_doc("Bucket Allocation Status")
		bas.bucket_id = b["bucket_id"]
		bas.item_code = item_code
		bas.stem_length = b["stem_length"] or ""
		bas.total_quantity = flt(b.get("stem_qty") or b["free"])
		bas.shelf_location = b["shelf"]
		bas.shelf_farm = b["farm"]
	bas.warehouse = b["warehouse"]
	bas.append(
		"bucket_allocations",
		{
			"sales_order": opl.sales_order,
			"sales_order_item": sales_order_item,
			"quantity_allocated": qty,
			"cancelled": 0,
		},
	)
	bas.flags.ignore_permissions = True
	bas.flags.ignore_links = True
	bas.save()
	_recompute(bas.name)
