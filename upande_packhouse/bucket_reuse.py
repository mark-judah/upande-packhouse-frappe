# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Give back what an order still holds in a bucket that has been filled again.

An allocation holds stems in a bucket (Bucket Allocation Status, one Bucket Allocations row per order line). It is
cleared when the stems are issued. A bucket that is NOT issued but taken back to the greenhouse and harvested
again starts a new life: its old shelf row is deleted, yet the allocation stays, because it is keyed on bucket + variety + length. When the
bucket is shelved again the old allocation is subtracted from the new flowers, so allocation shows fewer stems
than are physically in the bucket, and the old order holds stems that were never its own.

An allocation is from a previous life when a Harvesting stock entry for the same bucket was made after it. This
releases such allocations the way the Sales Allocation page unallocates (stock ledger, pick list, order line) and
breaks the plan's holds on the bucket. It runs when a bucket is harvested again and when it is shelved.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from upande_packhouse.api.v2.stock_truth import _blocked_pairs


def stale_allocations(bucket_id=None):
	"""Outstanding allocations made before the bucket was last harvested, one row each."""
	cond = "AND bas.bucket_id = %(b)s" if bucket_id else ""
	return frappe.db.sql(
		f"""
		SELECT ba.name AS ba, bas.bucket_id, bas.item_code AS variety, IFNULL(bas.stem_length, '') AS length,
		       ba.sales_order, ba.sales_order_item, ba.quantity_allocated AS qty, ba.creation,
		       so.per_delivered,
		       EXISTS (SELECT 1 FROM `tabDelivery Note Item` dni INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		               WHERE dni.so_detail = ba.sales_order_item AND dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0) AS delivered
		FROM `tabBucket Allocations` ba
		INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		LEFT JOIN `tabSales Order` so ON so.name = ba.sales_order
		WHERE ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0 AND ba.quantity_allocated > 0 {cond}
		  AND EXISTS (
			SELECT 1 FROM `tabStock Entry` se
			WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus != 2
			  AND se.custom_bucket_id = bas.bucket_id AND se.creation > ba.creation
		  )
		""",  # nosemgrep: the f-string hole is fixed SQL, values are bound
		{"b": bucket_id},
		as_dict=True,
	)


def break_old_holds(bucket_id=None):
	"""The allocation plan's active holds on a bucket made before it was harvested again are broken."""
	cond = "AND r.bucket_id = %(b)s" if bucket_id else ""
	try:
		rows = frappe.db.sql(
			f"""
			SELECT r.name, r.confirmation FROM `tabBucket Reservation` r
			WHERE r.status = 'Active' {cond}
			  AND EXISTS (
				SELECT 1 FROM `tabStock Entry` se
				WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus != 2
				  AND se.custom_bucket_id = r.bucket_id AND se.creation > r.creation
			  )
			""",  # nosemgrep: the f-string hole is fixed SQL, values are bound
			{"b": bucket_id},
			as_dict=True,
		)
	except frappe.db.TableMissingError:
		return 0
	for r in rows:
		frappe.db.set_value("Bucket Reservation", r.name, {"status": "Broken", "released_on": now_datetime()})
		if r.confirmation:
			frappe.db.set_value("Allocation Confirmation", r.confirmation, "status", "Broken")
	return len(rows)


def release_reused_bucket(bucket_id=None):
	"""Release every stale allocation (of one bucket, or of all). Returns {released, skipped, holds_broken}.

	Left alone, and counted in `skipped`: one on a trolley or truck (it is on its way) and one whose order line was
	already delivered (those stems went out the door; a person reviews it in Sales Allocation). Issued ones are not
	listed at all."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import unallocate_bucket_from_opl

	rows = stale_allocations(bucket_id)
	blocked = _blocked_pairs({r.sales_order_item for r in rows})
	groups, skipped = {}, []
	for r in rows:
		if r.delivered or (r.per_delivered or 0) >= 100 or blocked.get((r.sales_order_item, str(r.bucket_id).upper())):
			skipped.append(r)
			continue
		g = groups.setdefault(
			(r.sales_order_item, r.bucket_id, r.length),
			{"soi": r.sales_order_item, "bucket": r.bucket_id, "length": r.length, "order": r.sales_order, "qty": 0.0},
		)
		g["qty"] += r.qty or 0
	released = []
	for g in groups.values():
		try:
			res = unallocate_bucket_from_opl(g["soi"], g["bucket"], g["length"])
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Release reused bucket {g['bucket']}")
			continue
		if res.get("success"):
			released.append(g)
			try:
				frappe.get_doc("Sales Order", g["order"]).add_comment(
					"Info",
					_("Bucket {0} was harvested again, so its {1} stems held for this order were released.").format(
						g["bucket"], int(g["qty"])
					),
				)
			except Exception:
				pass
	broken = break_old_holds(bucket_id)
	if released or broken:
		frappe.db.commit()
	return {"released": released, "skipped": len(skipped), "holds_broken": broken}

