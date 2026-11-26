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
the stock ledger: the bucket is recorded as a Bucket Reuse Anomaly instead (stale_release).

`release_on_harvest` runs when the bucket is harvested again (Upande Agriculture's journey start, the first point
of contact back at the greenhouse): every hold of the last life is released there, the allocation row (and the
bucket's allocation status once nothing is left on it) and the line's pick row deleted, the pick list itself left
as it is. A hold that was part of a transfer (waiting at the farm, on a trolley or truck, on the road) is recorded
as Transfer Incomplete, the rest as Issuing Skipped. `release_reused_bucket` is the same at shelving, for a bucket
that got there without it.
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


def _transfer_flags(alias="p"):
	"""SQL: the pick row is part of a transfer that has not completed (waiting at the farm, on a trolley or truck,
	or on the road), is not shelved, issued or packed."""
	truck = (
		f" OR IFNULL({alias}.loaded_on_truck, 0) = 1"
		if frappe.db.has_column("Pick List Item", "loaded_on_truck")
		else ""
	)
	return (
		f"(IFNULL({alias}.awaiting_transfer, 0) = 1 OR IFNULL({alias}.loaded_in_trolley, 0) = 1 "
		f"OR IFNULL({alias}.in_transit, 0) = 1{truck}) AND IFNULL({alias}.shelved, 0) = 0 "
		f"AND IFNULL({alias}.issued, 0) = 0 AND IFNULL({alias}.issued_offline, 0) = 0 "
		f"AND IFNULL({alias}.custom_ready_for_packing, 0) = 0"
	)


def stale_transfers(bucket_id):
	"""Pick rows of an unfinished transfer of this bucket made before it was last harvested: the bucket is back
	in the greenhouse, so the transfer will never complete. One row each."""
	harvested = _HARVESTED_SINCE.format(bucket="p.bucket", since="p.creation")
	# nosemgrep: frappe-sql-format-injection -- the f-string holes are fixed SQL, values are bound
	return frappe.db.sql(
		f"""
		SELECT p.name, p.parent AS opl, p.sales_order_item AS soi, p.item_code AS variety,
		       IFNULL(p.stem_length, '') AS length, p.stock_qty AS qty, opl.sales_order
		FROM `tabPick List Item` p
		INNER JOIN `tabOrder Pick List` opl ON opl.name = p.parent AND opl.docstatus < 2
		WHERE p.parenttype = 'Order Pick List' AND p.bucket = %(b)s AND {_transfer_flags("p")} AND {harvested}
		""",
		{"b": bucket_id},
		as_dict=True,
	)


def release_on_harvest(bucket_id, harvest_stock_entry=None):
	"""A bucket harvested again has left what it was held for: release every hold of its last life now, not when
	it is shelved. Returns {allocations, transfers, pending, skipped}.

	- A stale allocation: the allocation row is deleted (and the bucket's allocation status when nothing is left
	  on it), the line's pick row is deleted, the order pick list is left as it is, and the bucket is recorded as a
	  Bucket Reuse Anomaly: Issuing Skipped, or Transfer Incomplete when the line's pick row was part of a transfer.
	- A pick row of an unfinished transfer with no allocation behind it: deleted the same way, Transfer Incomplete.
	- Left alone, and counted in `skipped`: an allocation whose order line was already delivered (those stems went
	  out the door; a person reviews it). Issued rows are not listed at all.
	Its flowers were gone with the last harvest: nothing goes back to the shelf or the stock ledger."""
	from upande_packhouse.stale_release import TRANSFER_INCOMPLETE, release_to_anomaly

	allocations = stale_allocations(bucket_id)
	transfers = stale_transfers(bucket_id)
	moving = {(t.soi, str(bucket_id).upper()) for t in transfers}
	groups, skipped = {}, 0
	for r in allocations:
		if not r.line_gone and (r.delivered or (r.per_delivered or 0) >= 100):
			skipped += 1
			continue
		g = groups.setdefault(
			(r.sales_order_item, r.length),
			{
				"soi": r.sales_order_item,
				"length": r.length,
				"qty": 0.0,
				"step": TRANSFER_INCOMPLETE
				if (r.sales_order_item, str(bucket_id).upper()) in moving
				else None,
			},
		)
		g["qty"] += r.qty or 0
	covered = {g["soi"] for g in groups.values()}
	for t in transfers:  # a transfer whose allocation is already gone
		if t.soi in covered:
			continue
		covered.add(t.soi)
		groups[(t.soi, t.length)] = {
			"soi": t.soi,
			"length": t.length,
			"qty": sum(x.qty or 0 for x in transfers if x.soi == t.soi),
			"step": TRANSFER_INCOMPLETE,
			"transfer": True,
		}
	done_alloc = done_transfer = 0
	for g in groups.values():
		step = g["step"]
		frappe.db.savepoint("release_on_harvest")
		try:
			res = release_to_anomaly(
				g["soi"],
				bucket_id,
				g["length"],
				g["qty"],
				_("Bucket {0} was harvested again").format(bucket_id),
				step=step,
			)
		except Exception:
			try:
				frappe.db.rollback(save_point="release_on_harvest")
			except Exception:
				pass  # the release had committed: nothing of it is left to undo
			frappe.clear_messages()
			frappe.log_error(title=f"Release on harvest {bucket_id}", message=frappe.get_traceback())
			continue
		if not (res or {}).get("success"):
			frappe.log_error(
				title=f"Release on harvest {bucket_id}",
				message=f"{g['soi']}: {(res or {}).get('message') or res}",
			)
			continue
		if step == TRANSFER_INCOMPLETE:
			done_transfer += 1
			_log_incomplete_transfer(bucket_id, g, harvest_stock_entry)
		else:
			done_alloc += 1
	pending = stale_pending_rows(bucket_id)
	if pending:
		_drop_pending(pending)
	if done_alloc or done_transfer or pending:
		frappe.db.commit()
	return {
		"allocations": done_alloc,
		"transfers": done_transfer,
		"pending": len(pending),
		"skipped": skipped,
	}


def _log_incomplete_transfer(bucket_id, g, harvest_stock_entry):
	"""The bucket journey's transfer log gets a step of its own for it: never fails the release."""
	try:
		from upande_packhouse.api.remote_transfer.transfer_scheduling import log_transfer_event

		line = frappe.db.get_value("Sales Order Item", g["soi"], "parent")
		log_transfer_event(
			bucket_id,
			"Transfer incomplete",
			outcome="Refused",
			stock_entry=harvest_stock_entry,
			details="Harvested again before the transfer finished{0}; the hold was released.".format(
				f" (order {line})" if line else ""
			),
		)
	except Exception:
		frappe.log_error(title=f"Transfer Incomplete event for {bucket_id}", message=frappe.get_traceback())


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
