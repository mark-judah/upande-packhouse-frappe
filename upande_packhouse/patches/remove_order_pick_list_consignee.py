import frappe


def execute():
	"""One-time cleanup, ships with the schema change to Order Pick List (see
	order_pick_list.json) -- removing a field from a DocType's own JSON does
	NOT drop its DB column on migrate (Frappe never drops columns implicitly,
	to avoid silent data loss), so an explicit patch is needed to actually
	take effect on any site that migrates this in.

	`consignee` was never populated or read anywhere in Order Pick List's own
	code (Box Label carries its own, separate consignee field used for
	dispatch/loading -- unrelated), so it's just dead weight on the OPL form.
	"""
	column_exists = frappe.db.sql(
		"SHOW COLUMNS FROM `tabOrder Pick List` LIKE %s", "consignee"
	)
	if column_exists:
		# ALTER TABLE implicitly commits in MySQL; Frappe's own DDL guard
		# refuses to run one while anything upstream is still uncommitted.
		frappe.db.commit()
		frappe.db.sql("ALTER TABLE `tabOrder Pick List` DROP COLUMN `consignee`")

	frappe.clear_cache(doctype="Order Pick List")
