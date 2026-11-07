"""What deleting an Order Pick List does to the buckets it held.

Each sales order line gives up its buckets, so it can be allocated again from available
stock. A bucket still waiting at a farm to be sent is not put back as available on that
farm's shelf: it comes off the shelf and is logged as an anomaly (ORDER_DELETED). Shelved
at the sales farm later, it is ordinary stock there and can be allocated again. A bucket
already shelved at the sales farm is simply free again there.
"""

import frappe
from frappe.utils import flt

#: The transfer event of a bucket whose order was deleted while it waited at a farm.
ORDER_DELETED = "Anomaly: order deleted while awaiting transfer"


def release_on_delete(opl):
	"""Release `opl`'s buckets (an Order Pick List doc being deleted). Issued rows are
	left as they are: their stems already left the shelf."""
	from upande_packhouse import stock_movement
	from upande_packhouse.api.remote_transfer.transfer_scheduling import log_transfer_event

	rows = [r for r in opl.get("table_ytkc") or [] if r.bucket and r.get("sales_order_item") and not r.issued]
	if not rows:
		_clear_line_links(opl.name)
		return

	so = (
		frappe.db.get_value("Sales Order", opl.sales_order, ["name", "delivery_date"], as_dict=True)
		or frappe._dict()
	)

	# ── each line gives up the bucket, unless another live pick list of the same line uses it ──
	pairs = {(r.sales_order_item, r.bucket, r.item_code) for r in rows}
	lines = set()
	for soi, bucket, item_code in pairs:
		if _other_rows(opl.name, bucket, soi=soi):
			continue
		stock_movement.reverse_allocation_movement(soi, bucket_id=bucket, item_code=item_code)
		_cancel_allocations(soi, bucket, item_code)
		lines.add(soi)

	# ── buckets still waiting at a farm: off their shelf there, kept as anomalies ──
	done = set()
	for r in rows:
		if not r.awaiting_transfer or r.shelved or r.in_transit or r.loaded_in_trolley:
			continue
		if r.get("loaded_on_truck") or r.bucket in done:
			continue
		done.add(r.bucket)
		if _other_rows(opl.name, r.bucket, waiting=True):
			continue  # another order still asks the farm for it
		shelves = _take_off_shelf(r)
		log_transfer_event(
			r.bucket,
			ORDER_DELETED,
			farm=r.farm,
			shelf=", ".join(shelves),
			details="; ".join(
				p
				for p in (
					"{0} deleted by {1}".format(opl.name, frappe.session.user),
					"sales order {0}".format(so.name) if so.name else "",
					"delivery {0}".format(so.delivery_date) if so.delivery_date else "",
					"taken off shelf {0}".format(", ".join(shelves)) if shelves else "not on a shelf",
					"allocatable again once shelved at the sales farm",
				)
				if p
			),
		)

	_clear_line_links(opl.name, lines)


def _other_rows(opl_name, bucket, soi=None, waiting=False):
	"""Rows of `bucket` on another live pick list, not issued: of line `soi`, or (waiting)
	still to be sent from the farm or on its way."""
	cond = ""
	if soi:
		cond += " AND pli.sales_order_item = %(soi)s"
	if waiting:
		cond += """ AND IFNULL(pli.shelved, 0) = 0 AND (pli.awaiting_transfer = 1
		  OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)"""
	return frappe.db.sql(
		"""SELECT 1 FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` o ON o.name = pli.parent AND o.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND pli.parent != %(opl)s
		  AND pli.bucket = %(bucket)s AND IFNULL(pli.issued, 0) = 0"""
		+ cond
		+ " LIMIT 1",
		{"opl": opl_name, "bucket": bucket, "soi": soi},
	)


def _cancel_allocations(soi, bucket, item_code):
	"""Cancel line `soi`'s outstanding allocations of `bucket` (every length of the variety)."""
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
		recompute_bas_quantities,
	)

	for name in frappe.get_all(
		"Bucket Allocation Status", filters={"bucket_id": bucket, "item_code": item_code}, pluck="name"
	):
		bas = frappe.get_doc("Bucket Allocation Status", name, for_update=True)
		changed = False
		for a in bas.bucket_allocations:
			if a.sales_order_item == soi and not a.cancelled and not a.issued:
				a.cancelled = 1
				a.db_update()
				changed = True
		if not changed:
			continue
		if not recompute_bas_quantities(bas):
			bas.in_transit = 0
		bas.flags.ignore_validate = True
		bas.flags.ignore_mandatory = True
		bas.save(ignore_permissions=True)


def _take_off_shelf(row):
	"""Remove the bucket's rows of this variety from the farm shelf it waits on, so it
	isn't offered there again. Returns the shelves it came off."""
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
		_clear_shelf_item,
	)

	filters = {"bucket_id": row.bucket, "variety": row.item_code}
	if row.shelf:
		filters["parent"] = row.shelf
	out = []
	for si in frappe.get_all("Shelf Item", filters=filters, fields=["name", "parent", "stem_length"]):
		if row.stem_length and si.stem_length and si.stem_length != row.stem_length:
			continue
		_clear_shelf_item(si.name, si.parent, "Order deleted (anomaly)")
		out.append(si.parent)
	if out:
		for name in frappe.get_all(
			"Bucket Allocation Status",
			filters={"bucket_id": row.bucket, "item_code": row.item_code, "shelf_location": ["in", out]},
			pluck="name",
		):
			frappe.db.set_value("Bucket Allocation Status", name, "shelf_location", "")
	return sorted(set(out))


def _clear_line_links(opl_name, lines=()):
	"""Sales order lines stop pointing at the deleted pick list; released lines show as
	needing allocation again."""
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
		_so_item_is_fully_allocated,
	)

	for soi in set(lines) | set(
		frappe.get_all("Sales Order Item", filters={"custom_opl": opl_name}, pluck="name")
	):
		outstanding = flt(
			frappe.db.sql(
				"""SELECT COALESCE(SUM(ba.quantity_allocated), 0) FROM `tabBucket Allocations` ba
				WHERE ba.sales_order_item = %s AND ba.cancelled = 0""",
				soi,
			)[0][0]
		)
		values = {"custom_fully_allocated": 1 if outstanding and _so_item_is_fully_allocated(soi) else 0}
		if not outstanding:
			values["custom_stock_available"] = 0
		if frappe.db.get_value("Sales Order Item", soi, "custom_opl") == opl_name:
			values["custom_opl"] = None
		frappe.db.set_value("Sales Order Item", soi, values, update_modified=False)
