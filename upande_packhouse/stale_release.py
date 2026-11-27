# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Release what an order or a transfer still holds of a bucket that has been harvested again, without giving
its flowers back.

The bucket's last life is over: the allocation rows are deleted (and the bucket's allocation status with them,
once nothing is left on it) and the line's pick rows are deleted. The Order Pick List itself is left as it is:
never cancelled, never deleted, even when it ends up with no rows. The stock ledger is left as it is too (the
stems stay where allocation moved them), the shelf row is not touched (it holds the new harvest, or is taken off
by the harvest's own cleanup), and the bucket is recorded as a Bucket Reuse Anomaly for a person to deal with:
Issuing Skipped, or Transfer Incomplete for a transfer that never finished. See bucket_reuse.release_on_harvest.
"""

import frappe
from frappe.utils import flt, now_datetime

STEP = "Stale Allocation"
FALLBACK_STEP = "Issuing Skipped"
#: a transfer that never completed: the bucket was harvested again before it arrived (a Bucket Reuse Anomaly step)
TRANSFER_INCOMPLETE = "Transfer Incomplete"


def release_to_anomaly(sales_order_item, bucket_id, stem_length, stems, why, step=None):
	"""Release one line's hold on a bucket (at one length) and record the bucket as an anomaly: the allocation row
	and the line's pick row are deleted (drop_allocation_and_pick_rows), nothing goes back to the shelf or the stock
	ledger, and the shelf row (which holds the new harvest, or is taken off by the harvest's own cleanup) is not
	touched. `stems`: the stems released (None: read from the allocation); `why`: the reason, for the order's
	comment; `step`: the anomaly's skipped step (TRANSFER_INCOMPLETE for a transfer that never completed).
	Returns {"success", "message"}."""
	stem_length = stem_length or ""
	line = frappe.db.get_value("Sales Order Item", sales_order_item, ["parent", "item_code"], as_dict=True)
	variety = _held_variety(bucket_id, sales_order_item, stem_length) or (line.item_code if line else None)
	if stems is None:
		stems = _outstanding(bucket_id, sales_order_item, stem_length)[0]
	opls = _pick_lists(sales_order_item, bucket_id)
	shelves = _shelves(bucket_id, variety, stem_length)
	sales_order = line.parent if line else _order_of(bucket_id, sales_order_item)

	# the line may be gone (replaced by Update Items, a split): the rows are found by its name alone
	res = drop_allocation_and_pick_rows(sales_order_item, bucket_id)
	if not (res or {}).get("success"):
		return res or {"success": False, "message": "Not released"}
	_record(bucket_id, variety, stem_length, stems, shelves, opls, why, sales_order, step)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- each release stands on its own
	return {"success": True}


def _outstanding(bucket_id, soi, length):
	"""(stems, earliest creation) of the line's outstanding allocation of the bucket at this length."""
	r = frappe.db.sql(
		"""SELECT SUM(ba.quantity_allocated), MIN(ba.creation) FROM `tabBucket Allocations` ba
		   JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		   WHERE bas.bucket_id = %s AND ba.sales_order_item = %s AND IFNULL(bas.stem_length, '') = %s
		     AND ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0""",
		(bucket_id, soi, length),
	)
	return flt(r[0][0]) if r else 0.0, r[0][1] if r else None


def _held_variety(bucket_id, soi, length):
	"""The variety the allocation holds (a substitute holds another one than the line's own)."""
	r = frappe.db.sql(
		"""SELECT bas.item_code FROM `tabBucket Allocations` ba
		   JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		   WHERE bas.bucket_id = %s AND ba.sales_order_item = %s AND IFNULL(bas.stem_length, '') = %s
		     AND ba.cancelled = 0 LIMIT 1""",
		(bucket_id, soi, length),
	)
	return r[0][0] if r else None


def _order_of(bucket_id, soi):
	r = frappe.db.sql(
		"""SELECT ba.sales_order FROM `tabBucket Allocations` ba
		   JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		   WHERE bas.bucket_id = %s AND ba.sales_order_item = %s LIMIT 1""",
		(bucket_id, soi),
	)
	return r[0][0] if r else None


def _pick_lists(soi, bucket_id):
	return frappe.db.sql_list(
		"""SELECT DISTINCT parent FROM `tabPick List Item`
		   WHERE parenttype = 'Order Pick List' AND sales_order_item = %s AND bucket = %s""",
		(soi, bucket_id),
	)


def _shelves(bucket_id, variety, length):
	return frappe.db.sql(
		"""SELECT si.name, si.parent AS shelf, s.farm, COALESCE(si.stem_qty, 0) AS stem_qty
		   FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent
		   WHERE si.parenttype = 'Shelf' AND si.bucket_id = %s AND si.variety = %s
		     AND IFNULL(si.stem_length, '') = %s
		   ORDER BY si.date_added, si.name""",
		(bucket_id, variety, length),
		as_dict=True,
	)


def drop_allocation_and_pick_rows(sales_order_item, bucket_id):
	"""Take one line's hold on a bucket away, whatever its length or variety: delete the line's outstanding
	allocation rows of the bucket (and the bucket's Bucket Allocation Status when no row is left on it), then
	delete the line's unissued pick rows of it. The Order Pick List is never cancelled or deleted, even left
	without rows: its rows are renumbered and its stem total re-added. A bucket already issued for the line is
	refused: its stems have left the shelf. Returns {"success", "allocations", "pick_rows"} or
	{"success": False, "message"}."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
		_reindex_opl_rows,
		_so_item_is_fully_allocated,
		recompute_bas_quantities,
	)

	if frappe.db.sql(
		"""SELECT 1 FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2
		   WHERE p.bucket = %(b)s AND %(soi)s IN (p.sales_order_item, p.custom_sale_order_item)
		     AND (IFNULL(p.issued, 0) = 1 OR IFNULL(p.issued_offline, 0) = 1) LIMIT 1""",
		{"b": bucket_id, "soi": sales_order_item},
	):
		return {
			"success": False,
			"message": f"Bucket {bucket_id} has already been issued for this line: its stems have left the shelf.",
		}

	allocations = 0
	for bas_name in frappe.db.sql_list(
		"""SELECT DISTINCT bas.name FROM `tabBucket Allocation Status` bas
		   JOIN `tabBucket Allocations` ba ON ba.parent = bas.name AND ba.parenttype = 'Bucket Allocation Status'
		   WHERE bas.bucket_id = %s AND ba.sales_order_item = %s AND ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0""",
		(bucket_id, sales_order_item),
	):
		frappe.db.sql(
			"""DELETE FROM `tabBucket Allocations` WHERE parent = %s AND parenttype = 'Bucket Allocation Status'
			   AND sales_order_item = %s AND cancelled = 0 AND IFNULL(issued, 0) = 0""",
			(bas_name, sales_order_item),
		)
		allocations += frappe.db.sql("SELECT ROW_COUNT()")[0][0] or 0
		if not frappe.db.count("Bucket Allocations", {"parent": bas_name}):
			frappe.delete_doc(
				"Bucket Allocation Status",
				bas_name,
				force=True,
				ignore_permissions=True,
				ignore_on_trash=True,
			)
			continue
		bas = frappe.get_doc("Bucket Allocation Status", bas_name, for_update=True)
		if not recompute_bas_quantities(bas):
			bas.in_transit = 0
		bas.flags.ignore_links = True
		bas.flags.ignore_validate = True
		bas.flags.ignore_mandatory = True
		bas.save(ignore_permissions=True)

	rows = frappe.db.sql(
		"""SELECT p.name, p.parent FROM `tabPick List Item` p
		   JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2
		   WHERE p.parenttype = 'Order Pick List' AND p.bucket = %(b)s
		     AND %(soi)s IN (p.sales_order_item, p.custom_sale_order_item)
		     AND IFNULL(p.issued, 0) = 0 AND IFNULL(p.issued_offline, 0) = 0""",
		{"b": bucket_id, "soi": sales_order_item},
	)
	for name, _parent in rows:
		frappe.db.sql("DELETE FROM `tabPick List Item` WHERE name = %s", name)
	for opl in {parent for _name, parent in rows}:
		_reindex_opl_rows(opl)
		frappe.db.sql(
			"UPDATE `tabOrder Pick List` SET custom_total_stems = "
			"(SELECT COALESCE(SUM(stock_qty), 0) FROM `tabPick List Item` WHERE parent = %(o)s), modified = NOW() "
			"WHERE name = %(o)s",
			{"o": opl},
		)

	if frappe.db.exists("Sales Order Item", sales_order_item):
		left = frappe.db.sql(
			"""SELECT COALESCE(SUM(ba.quantity_allocated), 0) FROM `tabBucket Allocations` ba
			   JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
			   WHERE ba.sales_order_item = %s AND ba.cancelled = 0""",
			sales_order_item,
		)[0][0]
		frappe.db.set_value(
			"Sales Order Item",
			sales_order_item,
			{"custom_fully_allocated": 0, "custom_stock_available": 0, "custom_opl": None}
			if not left
			else {"custom_fully_allocated": 1 if _so_item_is_fully_allocated(sales_order_item) else 0},
			update_modified=False,
		)
	return {"success": True, "allocations": allocations, "pick_rows": len(rows)}


def _record(bucket_id, variety, length, stems, shelves, opls, why, sales_order, step=None):
	if not frappe.db.exists("DocType", "Bucket Reuse Anomaly"):
		return
	meta = frappe.get_meta("Bucket Reuse Anomaly")
	options = (meta.get_field("skipped_step").options or "").split("\n")
	harvest = frappe.db.get_value(
		"Stock Entry",
		{"stock_entry_type": "Harvesting", "custom_bucket_id": bucket_id, "docstatus": 1},
		"name",
		order_by="creation desc",
	)
	doc = frappe.get_doc(
		{
			"doctype": "Bucket Reuse Anomaly",
			"bucket_id": bucket_id,
			# a step this site's doctype does not list yet (the doctype ships with Upande Agriculture) falls back
			"skipped_step": step if step in options else (STEP if STEP in options else FALLBACK_STEP),
			"detected_on": now_datetime(),
			"farm": shelves[0].farm if shelves else None,
			"variety": variety,
			"stems": int(flt(stems)),
			"previous_shelf": ", ".join(sorted({r.shelf for r in shelves}))[:140],
			"sales_order": sales_order,
			"order_pick_list": ", ".join(opls)[:140],
			"harvest_stock_entry": harvest,
		}
	)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	if sales_order:
		try:
			frappe.get_doc("Sales Order", sales_order).add_comment(
				"Info",
				"{0} stems of bucket {1} ({2} {3}) were held for this order: {4}. Released as a bucket anomaly ({5}); they do not go back to the shelf.".format(
					int(flt(stems)), bucket_id, variety or "", length or "", why, doc.name
				),
			)
		except Exception:
			pass
