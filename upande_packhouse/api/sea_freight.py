# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Sea freight orders for the packhouse app: every open Sales Order whose Mode Of Transport is Sea Freight.
#
#   - The production app tags each harvest with the order line it is for and, once every line is 100% harvested,
#     marks the order harvested (Sales Order custom_sea_freight_harvested).
#   - `orders()` lists them here with what was ordered, harvested, received and packed, and the buckets to expect.
#   - `receive()` takes the scanned buckets in: it builds the order's Order Pick List from them (one per line,
#     boxes split by packrate like an allocation), marks every row issued, and marks the order received, so it
#     packs on the Packing screen like any other order.
#   - `pick_lists()` names the pick lists of sea freight orders, for the Packing screen's Sea Freight banner.

import frappe
from frappe import _
from frappe.utils import now_datetime

SEA_FREIGHT = "Sea Freight"
CLOSED = ("Completed", "Closed", "Cancelled")


def _allow_unshelved_buckets():
	"""Sea freight buckets go from the scan straight to packing, never onto a shelf, so the pick list builder's
	shelf lookup is told to accept them: for the buckets in `frappe.flags.sea_freight_buckets` (set only while
	receive() builds the pick lists, per request) it returns no shelf instead of failing."""
	from upande_packhouse.server_scripts import create_mixed_box_picklist as boxes

	if getattr(boxes._lookup_shelf, "_sea_freight", False):
		return
	original = boxes._lookup_shelf

	def lookup(actual_farm, item_code, bucket_id, fallback_farm):
		if bucket_id in (frappe.flags.get("sea_freight_buckets") or ()):
			return ""
		return original(actual_farm, item_code, bucket_id, fallback_farm)

	lookup._sea_freight = True
	boxes._lookup_shelf = lookup


def _has(field):
	return frappe.db.has_column("Sales Order", field)


def _tagged_buckets(sales_orders):
	"""Harvesting entries tagged to these orders, one row per bucket and line: {sales_order, so_item, bucket,
	item_code, stems, warehouse, greenhouse, received_by_entry}."""
	if not sales_orders or not frappe.db.has_column("Stock Entry", "custom_sea_freight_order"):
		return []
	received_col = (
		"se.custom_receiving_entry"
		if frappe.db.has_column("Stock Entry", "custom_receiving_entry")
		else "NULL"
	)
	return frappe.db.sql(
		f"""
		SELECT se.custom_sea_freight_order AS sales_order, se.custom_sea_freight_item AS so_item,
		       se.custom_bucket_id AS bucket, sed.item_code, SUM(sed.qty) AS stems,
		       MAX(COALESCE(NULLIF(sed.t_warehouse, ''), se.to_warehouse)) AS warehouse,
		       MAX(se.custom_greenhouse) AS greenhouse,
		       MAX(IF(IFNULL({received_col}, '') != '', 1, 0)) AS received_by_entry
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1 AND se.stock_entry_type = 'Harvesting'
		  AND se.custom_sea_freight_order IN %(orders)s
		GROUP BY se.custom_sea_freight_order, se.custom_sea_freight_item, se.custom_bucket_id, sed.item_code
		ORDER BY se.custom_sea_freight_order, se.custom_bucket_id
		""",
		{"orders": tuple(sales_orders)},
		as_dict=True,
	)


def _pick_lists(orders):
	"""sales_order -> {names, packed_stems} of its submitted Order Pick Lists."""
	if not orders:
		return {}
	opls = frappe.get_all(
		"Order Pick List",
		filters={"sales_order": ["in", orders], "docstatus": 1},
		fields=["name", "sales_order"],
		order_by="creation asc",
	)
	from upande_packhouse.mobile.api import _packed_stems

	packed = _packed_stems([o.name for o in opls])
	out = {}
	for o in opls:
		row = out.setdefault(o.sales_order, {"names": [], "packed_stems": 0})
		row["names"].append(o.name)
		row["packed_stems"] += int(packed.get(o.name, 0))
	return out


@frappe.whitelist(methods=["GET"])
def orders():
	"""Open sea freight orders, soonest delivery first: per line ordered / harvested / received stems, the buckets
	harvested for the order, whether harvesting is done and the order received, and its pick lists."""
	extra = [
		f
		for f in ("custom_sea_freight_harvested", "custom_sea_freight_received", "custom_order_name")
		if _has(f)
	]
	sos = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"custom_mode_of_transport": SEA_FREIGHT,
			"status": ["not in", list(CLOSED)],
			"per_delivered": ["<", 100],
		},
		fields=["name", "customer", "customer_name", "delivery_date", "status", *extra],
		order_by="delivery_date asc, name asc",
		limit=200,
	)
	if not sos:
		return {"orders": []}
	names = [s.name for s in sos]
	lines = frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", names]},
		fields=[
			"name",
			"parent",
			"idx",
			"item_code",
			"item_name",
			"custom_length",
			"stock_qty",
			"custom_total_stems",
		],
		order_by="parent asc, idx asc",
		limit=0,
	)
	buckets = _tagged_buckets(names)
	opls = _pick_lists(names)
	received_orders = {s.name for s in sos if s.get("custom_sea_freight_received")}

	harvested, received, by_bucket = {}, {}, {}
	for b in buckets:
		stems = int(b.stems or 0)
		harvested[b.so_item] = harvested.get(b.so_item, 0) + stems
		if b.sales_order in received_orders or b.received_by_entry:
			received[b.so_item] = received.get(b.so_item, 0) + stems
		by_bucket.setdefault(b.sales_order, []).append(
			{
				"bucket": b.bucket,
				"item_code": b.item_code,
				"sales_order_item": b.so_item,
				"stems": stems,
				"greenhouse": b.greenhouse or "",
			}
		)

	by_order = {}
	for ln in lines:
		by_order.setdefault(ln.parent, []).append(
			{
				"name": ln.name,
				"item_code": ln.item_code,
				"item_name": ln.item_name or ln.item_code,
				"stem_length": ln.custom_length or "",
				"ordered_stems": int(ln.custom_total_stems or 0) or int(ln.stock_qty or 0),
				"harvested_stems": harvested.get(ln.name, 0),
				"received_stems": received.get(ln.name, 0),
			}
		)
	out = []
	for so in sos:
		items = by_order.get(so.name, [])
		opl = opls.get(so.name) or {"names": [], "packed_stems": 0}
		out.append(
			{
				"name": so.name,
				"order_name": so.get("custom_order_name") or "",
				"customer": so.customer_name or so.customer,
				"delivery_date": str(so.delivery_date or ""),
				"status": so.status,
				"harvest_done": bool(so.get("custom_sea_freight_harvested")),
				"received": so.name in received_orders,
				"ordered_stems": sum(i["ordered_stems"] for i in items),
				"harvested_stems": sum(i["harvested_stems"] for i in items),
				"received_stems": sum(i["received_stems"] for i in items),
				"packed_stems": opl["packed_stems"],
				"pick_list": opl["names"][0] if opl["names"] else None,
				"pick_lists": opl["names"],
				"items": items,
				"buckets": by_bucket.get(so.name, []),
			}
		)
	return {"orders": out}


@frappe.whitelist(methods=["POST"])
def receive(sales_order=None, buckets=None):
	"""Take a harvested sea freight order into the packhouse from its scanned buckets.

	Every bucket harvested for the order must be scanned. Builds the order's pick lists from them (one per line,
	boxes split by packrate, as an allocation would), marks every row shelved and issued, and marks the order
	received."""
	if isinstance(buckets, str):
		buckets = frappe.parse_json(buckets)
	scanned = {str(b).strip() for b in (buckets or []) if str(b).strip()}
	so = frappe.get_doc("Sales Order", sales_order)
	if so.docstatus != 1 or so.get("custom_mode_of_transport") != SEA_FREIGHT:
		frappe.throw(_("{0} is not a submitted sea freight order.").format(sales_order))
	if not so.get("custom_sea_freight_harvested"):
		frappe.throw(_("{0} is not marked as harvested yet.").format(sales_order))
	if so.get("custom_sea_freight_received"):
		frappe.throw(_("{0} has already been received.").format(sales_order))

	expected = _tagged_buckets([sales_order])
	expected_ids = {b.bucket for b in expected}
	unknown = sorted(scanned - expected_ids)
	if unknown:
		frappe.throw(_("Not harvested for {0}: {1}").format(sales_order, ", ".join(unknown)))
	missing = sorted(expected_ids - scanned)
	if missing:
		frappe.throw(_("Still to scan: {0}").format(", ".join(missing)))

	lengths = {i.name: i.custom_length for i in so.items}
	allocations = [
		{
			"sales_order_item": b.so_item,
			"bucket_id": b.bucket,
			"qty": float(b.stems or 0),
			"warehouse": b.warehouse,
			"stem_length": lengths.get(b.so_item),
		}
		for b in expected
		if b.so_item
	]
	from upande_packhouse.server_scripts.create_straight_box_pick_list import (
		create_straight_box_pick_list_for_allocated_items,
	)

	before = set(frappe.get_all("Order Pick List", filters={"sales_order": sales_order}, pluck="name"))
	_allow_unshelved_buckets()
	frappe.flags.sea_freight_buckets = expected_ids
	try:
		create_straight_box_pick_list_for_allocated_items(so, allocations, submit=True)
	finally:
		frappe.flags.sea_freight_buckets = None
	created = [
		n
		for n in frappe.get_all("Order Pick List", filters={"sales_order": sales_order}, pluck="name")
		if n not in before
	]
	now, user = now_datetime(), frappe.session.user
	for name in created:
		opl = frappe.get_doc("Order Pick List", name)
		if opl.docstatus == 0:
			opl.flags.ignore_permissions = True
			# The allocation coverage check doesn't apply: these stems were never allocated from shelves, and
			# the order was checked fully harvested and every bucket scanned above.
			opl.flags.ignore_opl_submit_checks = True
			opl.submit()
		frappe.db.sql(
			"""UPDATE `tabPick List Item`
			SET shelved = 1, shelved_at = %(now)s, shelved_by = %(user)s,
			    issued = 1, issued_at = %(now)s, issued_by = %(user)s,
			    awaiting_transfer = 0, custom_ready_for_packing = 1
			WHERE parent = %(opl)s""",
			{"now": now, "user": user, "opl": name},
		)
	frappe.db.set_value(
		"Sales Order",
		sales_order,
		{
			"custom_sea_freight_received": 1,
			"custom_sea_freight_received_on": now,
			"custom_sea_freight_received_by": user,
		},
	)
	frappe.db.commit()
	return {"sales_order": sales_order, "pick_lists": created}


@frappe.whitelist(methods=["GET"])
def pick_lists():
	"""Names of the submitted pick lists of sea freight orders that are still open."""
	return frappe.db.sql_list(
		"""SELECT opl.name FROM `tabOrder Pick List` opl
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE opl.docstatus = 1 AND so.custom_mode_of_transport = %s
		  AND so.status NOT IN ('Completed', 'Closed', 'Cancelled')""",
		(SEA_FREIGHT,),
	)
