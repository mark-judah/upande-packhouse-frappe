import frappe


def execute():
	"""Bucket Reservation is gone: the allocation plan confirms stems, it no longer holds buckets. A confirmation
	marked Broken (a held bucket left the shelf) is live again as its stems, then the doctype and its table go."""
	if frappe.db.table_exists("Allocation Confirmation"):
		frappe.db.sql(
			"""UPDATE `tabAllocation Confirmation`
			   SET status = IF(IFNULL(fulfilled_stems, 0) > 0, 'Partly Fulfilled', 'Reserved')
			   WHERE status = 'Broken'"""
		)
	if frappe.db.exists("DocType", "Bucket Reservation"):
		frappe.delete_doc("DocType", "Bucket Reservation", force=True, ignore_permissions=True)
	frappe.db.sql_ddl("DROP TABLE IF EXISTS `tabBucket Reservation`")
