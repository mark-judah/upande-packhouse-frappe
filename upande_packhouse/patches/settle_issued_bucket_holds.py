import frappe
from frappe.utils import flt


def execute():
	"""Bucket Allocations rows still outstanding although their order's pick list issued that bucket, on a bucket
	label shelved again since (a new harvest): a downgraded pick (a 72cm bucket issued to a 52cm line) was settled
	on the picked length's status only, so the shelf length's row kept holding the new stems. Marked issued, and
	their statuses' quantities recomputed. Holds on stems shelved before the issue stay: those are the issued
	stems."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
		recompute_bas_quantities,
	)

	rows = frappe.db.sql(
		"""SELECT ba.name, ba.parent
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
		               WHERE s.bucket_id = bas.bucket_id AND s.variety = bas.item_code))""",
		as_dict=True,
	)
	for r in rows:
		frappe.db.set_value("Bucket Allocations", r.name, "issued", 1, update_modified=False)
	for parent in {r.parent for r in rows}:
		bas = frappe.get_doc("Bucket Allocation Status", parent)
		filters = {"bucket_id": bas.bucket_id, "variety": bas.item_code}
		if bas.stem_length:
			filters["stem_length"] = bas.stem_length
		shelf = sum(flt(q) for q in frappe.get_all("Shelf Item", filters=filters, pluck="stem_qty"))
		recompute_bas_quantities(bas, shelf_qty=shelf)
		bas.flags.ignore_validate = True
		bas.flags.ignore_mandatory = True
		bas.save(ignore_permissions=True)
	if rows:
		print(f"Settled {len(rows)} issued bucket holds on {len({r.parent for r in rows})} statuses")
