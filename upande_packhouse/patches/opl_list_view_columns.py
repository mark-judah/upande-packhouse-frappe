import json

import frappe


def execute():
	"""A saved list layout overrides the doctype's list columns: add Delivery Date and Total Boxes to it."""
	if not frappe.db.exists("List View Settings", "Order Pick List"):
		return
	settings = frappe.get_doc("List View Settings", "Order Pick List")
	fields = json.loads(settings.fields or "[]")
	names = [f.get("fieldname") for f in fields]
	for fieldname, label, after in (
		("delivery_date", "Delivery Date", "customer"),
		("custom_total_stems", "Total Stems", None),
		("total_boxes", "Total Boxes", "custom_total_stems"),
	):
		if fieldname in names:
			continue
		at = names.index(after) + 1 if after in names else len(fields)
		fields.insert(at, {"fieldname": fieldname, "label": label})
		names.insert(at, fieldname)
	settings.fields = json.dumps(fields)
	settings.save(ignore_permissions=True)
