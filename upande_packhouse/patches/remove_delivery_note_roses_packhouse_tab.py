import frappe


def execute():
	"""One-time cleanup, ships with the fixture change to Delivery Note (see
	custom/delivery_note.json) -- fixture sync only upserts what's listed
	there, it never deletes a Custom Field that's no longer in the file, so
	removing these needs an explicit patch to actually take effect on any
	site that migrates this in.

	These fields (and the "Roses Packhouse" tab they lived under) described
	ONE Sales Order per Delivery Note (custom_so, custom_consignee,
	custom_delivery_point, custom_freight, custom_transport_mode,
	custom_brn_ref, custom_truck_details) -- meaningless now that one
	Delivery Note consolidates every order for a customer's whole delivery
	day (see mobile/api.py's createOrUpdateDispatch). The same data now
	lives per Delivery Note ITEM row instead (custom/delivery_note_item.json
	+ the native against_sales_order/so_detail fields), so the header
	versions and their tab are just dead weight. custom_total_boxes
	(header) was a per-Sales-Order box count, same problem; custom_dispatch_form
	was already dead (roses_invoice.py always overwrote it with the real DN
	name before it was ever read back from anywhere).

	custom_flo_id / custom_flo_id_2 are NOT Sales-Order-derived (manually
	filled, shipment-level) and are kept -- just without the tab wrapper.
	"""
	fieldnames = (
		"custom_roses_packhouse", "custom_so", "custom_consignee", "custom_delivery_point",
		"custom_dispatch_form", "custom_freight", "custom_transport_mode", "custom_truck_details",
		"custom_total_boxes", "custom_brn_ref",
	)
	for fieldname in fieldnames:
		cf_name = frappe.db.get_value("Custom Field", {"dt": "Delivery Note", "fieldname": fieldname})
		if cf_name:
			frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True, force=True)

		column_exists = frappe.db.sql(
			"SHOW COLUMNS FROM `tabDelivery Note` LIKE %s", fieldname
		)
		if column_exists:
			# ALTER TABLE implicitly commits in MySQL; Frappe's own DDL guard
			# refuses to run one while anything from the delete_doc() above
			# is still uncommitted, rather than let that implicit commit
			# hide the transaction boundary. Commit explicitly first.
			frappe.db.commit()
			frappe.db.sql(f"ALTER TABLE `tabDelivery Note` DROP COLUMN `{fieldname}`")

	frappe.clear_cache(doctype="Delivery Note")
