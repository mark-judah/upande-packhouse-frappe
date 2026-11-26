# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Give back what an order still holds in a bucket that has been filled again.

An allocation holds stems in a bucket (Bucket Allocation Status, one Bucket Allocations row per order line). It is
cleared when the stems are issued. A bucket that is NOT issued but taken back to the greenhouse and harvested
again starts a new life: its old shelf row is deleted, yet the allocation stays, because it is keyed on bucket + variety + length. When the
bucket is shelved again the old allocation is subtracted from the new flowers, so allocation shows fewer stems
than are physically in the bucket (often none, and the bucket is not shown at all), and the old order holds stems
that were never its own. An allocation waiting release (Pending Allocation Row) does the same.

An allocation is from a previous life when a Harvesting stock entry for the same bucket was made after it. This
cancels such allocations and takes them off their pick lists, but never gives their flowers back to the shelf or
the stock ledger: the bucket is recorded as a Bucket Reuse Anomaly instead (stale_release). It runs when a bucket
is shelved.
"""

import frappe
from frappe import _

from upande_packhouse.api.v2.stock_truth import _blocked_pairs

_HARVESTED_SINCE = """EXISTS (
	SELECT 1 FROM `tabStock Entry` se
	WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus != 2
	  AND se.custom_bucket_id = {bucket} AND se.creation > {since}
)"""


def stale_allocations(bucket_id=None, bucket_ids=None, item_code=None):
	"""Outstanding allocations made before the bucket was last harvested, one row each: of one bucket, of
	`bucket_ids` (optionally of one variety), or of all."""
	if bucket_ids is not None and not bucket_ids:
		return []
	cond = "AND bas.bucket_id = %(b)s" if bucket_id else ""
	if bucket_ids:
		cond += " AND bas.bucket_id IN %(ids)s"
	if item_code:
		cond += " AND bas.item_code = %(item)s"
	harvested = _HARVESTED_SINCE.format(bucket="bas.bucket_id", since="ba.creation")
	# nosemgrep: frappe-sql-format-injection -- the f-string holes are fixed SQL, values are bound
	return frappe.db.sql(
		f"""
		SELECT ba.name AS ba, ba.parent AS bas, bas.bucket_id, bas.item_code AS variety,
		       IFNULL(bas.stem_length, '') AS length, ba.sales_order, ba.sales_order_item,
		       ba.quantity_allocated AS qty, ba.creation, so.per_delivered,
		       soi.name IS NULL AS line_gone,
		       EXISTS (SELECT 1 FROM `tabDelivery Note Item` dni INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		               WHERE dni.so_detail = ba.sales_order_item AND dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0) AS delivered
		FROM `tabBucket Allocations` ba
		INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
		LEFT JOIN `tabSales Order` so ON so.name = ba.sales_order
		LEFT JOIN `tabSales Order Item` soi ON soi.name = ba.sales_order_item
		WHERE ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0 AND ba.quantity_allocated > 0 {cond}
		  AND {harvested}
		""",
		{"b": bucket_id, "ids": tuple(bucket_ids or ()) or ("",), "item": item_code},
		as_dict=True,
	)


def stale_pending_rows(bucket_id=None):
	"""Rows of allocations waiting release made before the bucket was last harvested."""
	if not frappe.db.table_exists("Pending Allocation Row"):
		return []
	cond = "AND r.bucket_id = %(b)s" if bucket_id else ""
	harvested = _HARVESTED_SINCE.format(bucket="r.bucket_id", since="r.creation")
	# nosemgrep: frappe-sql-format-injection -- the f-string holes are fixed SQL, values are bound
	return frappe.db.sql(
		f"""
		SELECT r.name, r.parent, r.bucket_id, r.qty, p.sales_order
		FROM `tabPending Allocation Row` r
		JOIN `tabPending Allocation` p ON p.name = r.parent AND r.parenttype = 'Pending Allocation'
		WHERE p.status = 'Pending' {cond} AND {harvested}
		""",
		{"b": bucket_id},
		as_dict=True,
	)


def _drop_pending(rows):
	"""Take stale rows off their allocations waiting release; one left with no rows goes."""
	for r in rows:
		frappe.db.delete("Pending Allocation Row", {"name": r.name})
	for parent in {r.parent for r in rows}:
		if not frappe.db.exists(
			"Pending Allocation Row", {"parent": parent, "parenttype": "Pending Allocation"}
		):
			frappe.delete_doc("Pending Allocation", parent, ignore_permissions=True, force=True)


def release_reused_bucket(bucket_id=None):
	"""Release every stale allocation (of one bucket, or of all). Returns {released, cancelled, pending, skipped}.

	Its flowers were gone with the last harvest, so nothing goes back to the shelf or the stock ledger: each one is
	cancelled and the bucket recorded as an anomaly (stale_release; the new harvest on the shelf is not touched).
	Left alone, and counted in `skipped`: one on a trolley or truck (it is on its way) and one whose order line was
	already delivered (those stems went out the door; a person reviews it). Issued ones are not listed at all."""
	from upande_packhouse.stale_release import release_to_anomaly

	rows = stale_allocations(bucket_id)
	blocked = _blocked_pairs({r.sales_order_item for r in rows if r.sales_order_item})
	groups, skipped = {}, []
	for r in rows:
		if not r.line_gone and (
			r.delivered
			or (r.per_delivered or 0) >= 100
			or blocked.get((r.sales_order_item, str(r.bucket_id).upper()))
		):
			skipped.append(r)
			continue
		g = groups.setdefault(
			(r.sales_order_item, r.bucket_id, r.length),
			{
				"soi": r.sales_order_item,
				"bucket": r.bucket_id,
				"length": r.length,
				"order": r.sales_order,
				"gone": bool(r.line_gone),
				"qty": 0.0,
			},
		)
		g["qty"] += r.qty or 0
	released = []
	for g in groups.values():
		frappe.db.savepoint("release_reused_bucket")
		try:
			res = release_to_anomaly(
				g["soi"],
				g["bucket"],
				g["length"],
				g["qty"],
				_("Bucket {0} was harvested again").format(g["bucket"]),
				reused=True,
			)
		except Exception:
			try:
				frappe.db.rollback(save_point="release_reused_bucket")
			except Exception:
				pass  # the unallocation had committed: nothing of it is left to undo
			frappe.clear_messages()
			frappe.log_error(title=f"Release reused bucket {g['bucket']}", message=frappe.get_traceback())
			continue
		if not (res or {}).get("success"):
			frappe.log_error(
				title=f"Release reused bucket {g['bucket']}",
				message=f"{g['soi']}: {(res or {}).get('message') or res}",
			)
			continue
		released.append(g)
	pending = stale_pending_rows(bucket_id)
	if pending:
		_drop_pending(pending)
	if released or pending:
		frappe.db.commit()
	return {
		"released": [g for g in released if not g["gone"]],
		"cancelled": len([g for g in released if g["gone"]]),
		"pending": len(pending),
		"skipped": len(skipped),
	}
