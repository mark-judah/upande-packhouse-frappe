"""Bucket holds whose stems were already issued.

Issuing a bucket marks its allocation rows issued. When that does not happen (a downgraded pick settled on
the picked length's status, or a save that lost a race), the row keeps holding stems: the bucket list
offers the bucket again once it is shelved again (LIVE_HOLD_SQL), but allocating from it is refused as
over-allocation. `settle` marks those rows issued and recomputes their statuses' quantities.
"""

import frappe
from frappe.utils import flt

ISSUED_HOLDS_SQL = """
	SELECT ba.name, ba.parent
	FROM `tabBucket Allocations` ba
	JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
	WHERE ba.parenttype = 'Bucket Allocation Status' AND ba.cancelled = 0 AND IFNULL(ba.issued, 0) = 0
	  AND EXISTS (
	      SELECT 1 FROM `tabPick List Item` p
	      JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus < 2
	      WHERE p.sales_order_item = ba.sales_order_item AND p.bucket = bas.bucket_id
	        AND (IFNULL(p.issued, 0) = 1 OR IFNULL(p.issued_offline, 0) = 1)
	        AND COALESCE(p.issued_at, p.modified) < (
	            SELECT MAX(s.date_added) FROM `tabShelf Item` s
	            WHERE s.bucket_id = bas.bucket_id AND s.variety = bas.item_code))"""


def settle():
	"""Scheduled every 5 minutes. Writes with set_value only, so it never races a status being saved."""
	rows = frappe.db.sql(ISSUED_HOLDS_SQL, as_dict=True)
	for r in rows:
		frappe.db.set_value("Bucket Allocations", r.name, "issued", 1, update_modified=False)
	for parent in {r.parent for r in rows}:
		recompute(parent)
	return len(rows)


def recompute(parent):
	"""Re-derive a status' quantities from the shelf and its outstanding rows."""
	bas = frappe.db.get_value(
		"Bucket Allocation Status", parent, ["bucket_id", "item_code", "stem_length"], as_dict=True
	)
	if not bas:
		return
	filters = {"bucket_id": bas.bucket_id, "variety": bas.item_code}
	if bas.stem_length:
		filters["stem_length"] = bas.stem_length
	shelf = sum(flt(q) for q in frappe.get_all("Shelf Item", filters=filters, pluck="stem_qty"))
	held = sum(
		flt(q)
		for q in frappe.get_all(
			"Bucket Allocations",
			filters={"parent": parent, "parenttype": "Bucket Allocation Status", "cancelled": 0, "issued": 0},
			pluck="quantity_allocated",
		)
	)
	frappe.db.set_value(
		"Bucket Allocation Status",
		parent,
		{"total_quantity": shelf, "allocated_quantity": held, "available_quantity": max(0, shelf - held)},
		update_modified=False,
	)
