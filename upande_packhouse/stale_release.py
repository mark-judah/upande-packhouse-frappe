# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Release a stale allocation without giving its flowers back.

A stale allocation holds stems for an order that is cancelled, past its delivery date or gone, or for the
previous harvest of a bucket that has been filled again. Its flowers never go back to the shelf: the allocation
is cancelled and its pick-list rows removed (the Sales Allocation unallocate), but the stock ledger is left as it
is (the stems stay where allocation moved them), the stems come off the bucket's shelf row, and the bucket is
recorded as a Bucket Reuse Anomaly (Stale Allocation) for a person to deal with.

A stale transfer (a bucket waiting at a farm, on a trolley or a truck for an order that will not take it) clears
the bucket off the farm shelf it was to move from, whole: a remote bucket goes whole. Nothing is reversed in the
stock ledger on any path.

A reused bucket's old allocation is the same, except its shelf row holds the NEW harvest, which stays."""

import frappe
from frappe.utils import flt, now_datetime

STEP = "Stale Allocation"
FALLBACK_STEP = "Issuing Skipped"


def release_to_anomaly(sales_order_item, bucket_id, stem_length, stems, why, reused=False, transfer=False):
	"""Cancel one line's outstanding allocation of a bucket (at one length) and record the bucket as an anomaly.
	`stems`: the outstanding stems being released (None: read from the allocation); `why` the stale reason.
	A bucket harvested again since it was allocated is treated as `reused` whatever the caller says: its shelf
	row holds the new harvest. `transfer`: clear the bucket off the shelf its pick rows were to move it from.
	Returns {"success", "message"}."""
	stem_length = stem_length or ""
	line = frappe.db.get_value("Sales Order Item", sales_order_item, ["parent", "item_code"], as_dict=True)
	variety = _held_variety(bucket_id, sales_order_item, stem_length) or (line.item_code if line else None)
	held, since = _outstanding(bucket_id, sales_order_item, stem_length)
	if stems is None:
		stems = held
	reused = reused or _harvested_after(bucket_id, since)
	opls = _pick_lists(sales_order_item, bucket_id)
	shelves = _shelves(bucket_id, variety, stem_length)
	sources = _source_shelves(sales_order_item, bucket_id) if transfer else []
	sales_order = line.parent if line else _order_of(bucket_id, sales_order_item)

	if line:
		from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
			unallocate_bucket_from_opl,
		)

		frappe.flags.unallocate_keep_ledger = True
		try:
			res = unallocate_bucket_from_opl(sales_order_item, bucket_id, stem_length)
		finally:
			frappe.flags.unallocate_keep_ledger = False
		if not (res or {}).get("success"):
			return res or {"success": False, "message": "Not released"}
	else:
		# the order line is gone (replaced by Update Items, a split): nothing to unallocate through it
		_cancel_line_gone(sales_order_item, bucket_id, variety, stem_length)

	if reused:
		pass
	elif transfer:
		_clear_source_shelves(bucket_id, variety, stem_length, sources)
	else:
		_take_off_shelf(bucket_id, variety, stem_length, stems)
	_record(bucket_id, variety, stem_length, stems, shelves, opls, why, sales_order)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- each release stands on its own
	return {"success": True}


def _source_shelves(soi, bucket_id):
	"""The farm shelves the line's pick rows were to move the bucket from."""
	return [
		s
		for s in frappe.db.sql_list(
			"""SELECT DISTINCT IFNULL(shelf, '') FROM `tabPick List Item`
			   WHERE parenttype = 'Order Pick List' AND sales_order_item = %s AND bucket = %s
			     AND IFNULL(issued, 0) = 0 AND IFNULL(shelved, 0) = 0""",
			(soi, bucket_id),
		)
		if s
	]


def _clear_source_shelves(bucket_id, variety, length, sources):
	"""Take the bucket's rows off the shelves it was to move from (every shelf it is on when the pick rows named
	none), whole, unless another live pick list is still sending it from there."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
		recompute_bas_quantities,
	)
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
		_clear_shelf_item,
	)

	if frappe.db.sql(
		"""SELECT 1 FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name = pli.parent AND o.docstatus < 2
		   WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s AND IFNULL(pli.issued, 0) = 0
		     AND IFNULL(pli.shelved, 0) = 0
		     AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1) LIMIT 1""",
		bucket_id,
	):
		return  # another order still asks the farm for it
	for r in _shelves(bucket_id, variety, length):
		if not sources or r.shelf in sources:
			_clear_shelf_item(r.name, r.shelf, STEP)
	for name in frappe.get_all(
		"Bucket Allocation Status",
		filters={"bucket_id": bucket_id, "item_code": variety, "stem_length": length},
		pluck="name",
	):
		bas = frappe.get_doc("Bucket Allocation Status", name, for_update=True)
		left = sum(flt(r.stem_qty) for r in _shelves(bucket_id, variety, length))
		if not recompute_bas_quantities(bas, shelf_qty=left):
			bas.in_transit = 0
		if not left:
			bas.shelf_location = ""
		bas.flags.ignore_links = True
		bas.flags.ignore_validate = True
		bas.flags.ignore_mandatory = True
		bas.save(ignore_permissions=True)


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


def _harvested_after(bucket_id, since):
	return bool(
		since
		and frappe.db.sql(
			"""SELECT 1 FROM `tabStock Entry` WHERE stock_entry_type = 'Harvesting' AND docstatus != 2
			   AND custom_bucket_id = %s AND creation > %s LIMIT 1""",
			(bucket_id, since),
		)
	)


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


def _cancel_line_gone(soi, bucket_id, variety, length):
	"""Cancel a gone line's allocation rows on the bucket and take its rows off the pick lists."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
		_force_delete_opl,
		_reindex_opl_rows,
		recompute_bas_quantities,
	)

	for bas_name in frappe.get_all(
		"Bucket Allocation Status",
		filters={"bucket_id": bucket_id, "item_code": variety, "stem_length": length},
		pluck="name",
	):
		bas = frappe.get_doc("Bucket Allocation Status", bas_name, for_update=True)
		changed = False
		for row in bas.bucket_allocations:
			if row.sales_order_item == soi and not row.cancelled and not row.issued:
				row.cancelled = 1
				changed = True
		if changed:
			if not recompute_bas_quantities(bas):
				bas.in_transit = 0
			bas.flags.ignore_links = True
			bas.flags.ignore_validate = True
			bas.flags.ignore_mandatory = True
			bas.save(ignore_permissions=True)
	for opl in _pick_lists(soi, bucket_id):
		frappe.db.sql(
			"""DELETE FROM `tabPick List Item` WHERE parent = %s AND sales_order_item = %s AND bucket = %s
			   AND IFNULL(issued, 0) = 0 AND IFNULL(issued_offline, 0) = 0""",
			(opl, soi, bucket_id),
		)
		if not frappe.db.count("Pick List Item", {"parent": opl}):
			_force_delete_opl(opl)
		else:
			_reindex_opl_rows(opl)


def _take_off_shelf(bucket_id, variety, length, stems):
	"""The released stems leave the bucket's shelf row: lowered, or the row taken off once it is empty."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
		recompute_bas_quantities,
	)
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
		_clear_shelf_item,
	)

	left = flt(stems)
	for r in _shelves(bucket_id, variety, length):
		if left <= 1e-9:
			break
		take = min(left, flt(r.stem_qty))
		left -= take
		if flt(r.stem_qty) - take <= 1e-9:
			_clear_shelf_item(r.name, r.shelf, STEP)
		else:
			frappe.db.set_value(
				"Shelf Item", r.name, "stem_qty", flt(r.stem_qty) - take, update_modified=False
			)
	on_shelf = sum(flt(r.stem_qty) for r in _shelves(bucket_id, variety, length))
	bas_name = frappe.db.get_value(
		"Bucket Allocation Status",
		{"bucket_id": bucket_id, "item_code": variety, "stem_length": length},
		"name",
	)
	if bas_name:
		bas = frappe.get_doc("Bucket Allocation Status", bas_name, for_update=True)
		recompute_bas_quantities(bas, shelf_qty=on_shelf)
		bas.flags.ignore_links = True
		bas.flags.ignore_validate = True
		bas.flags.ignore_mandatory = True
		bas.save(ignore_permissions=True)


def _record(bucket_id, variety, length, stems, shelves, opls, why, sales_order):
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
			"skipped_step": STEP if STEP in options else FALLBACK_STEP,
			"detected_on": now_datetime(),
			"farm": shelves[0].farm if shelves else None,
			"variety": variety,
			"stems": int(flt(stems)),
			"previous_shelf": ", ".join(sorted({r.shelf for r in shelves}))[:140],
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
