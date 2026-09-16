# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Dashboard links this app owns but cannot ship in its customization files.

`custom/sales_order.json` carries the "Packhouse" link group. Three of its rows
point at fields that ship in the same file or on this app's own doctypes, so
they sync safely. The Sales Invoice row does not: `custom_so` is defined in
`custom/sales_invoice.json`, a sibling file.

That matters because `frappe.modules.utils.sync_customizations` walks the
`custom/` folder with `os.listdir` -- filesystem order, not sorted -- and
`sync_customizations_for_doctype` calls `validate_fields_for_doctype` at the end
of every file it syncs. Validating Sales Order runs
`validate_links_table_fieldnames`, which issues a real query against
`Sales Invoice.custom_so`. Whenever the OS happens to list `sales_order.json`
first, that column does not exist yet and a fresh install dies with:

    Document Links Row #3: Invalid doctype or fieldname.
    (1054, "Unknown column 'custom_so' in 'WHERE'")

Nothing can reorder that walk, and `after_install` is too late -- the throw
happens inside `installer.install_app` itself. So the row is kept out of the
shipped JSON and created here instead, once every customization is in place.
Idempotent, and it skips rather than throws when a target is still missing, so a
half-synced site degrades into a missing connection rather than an unusable form.
"""

import frappe

# Rows this app adds to another doctype's connections panel, keyed by the
# doctype that owns the panel. Only rows whose target field lives outside that
# doctype's own customization file belong here.
DEFERRED_LINKS = {
	"Sales Order": [
		{"link_doctype": "Sales Invoice", "link_fieldname": "custom_so", "group": "Packhouse"},
	],
}


def ensure_dashboard_links():
	"""Create any missing deferred link row whose target field now exists."""
	added = []
	for parent, rows in DEFERRED_LINKS.items():
		if not frappe.db.exists("DocType", parent):
			continue
		for row in rows:
			if _link_exists(parent, row):
				continue
			if not _target_ready(row):
				frappe.log_error(
					title="Packhouse dashboard link skipped",
					message=(
						f"{parent}: {row['link_doctype']}.{row['link_fieldname']} "
						"not created — the target field does not exist yet."
					),
				)
				continue
			_insert_link(parent, row)
			added.append(f"{parent}: {row['link_doctype']}.{row['link_fieldname']}")

	if added:
		for parent in DEFERRED_LINKS:
			frappe.clear_cache(doctype=parent)
	return added


def _link_exists(parent, row):
	return bool(
		frappe.db.exists(
			"DocType Link",
			{
				"parent": parent,
				"parentfield": "links",
				"link_doctype": row["link_doctype"],
				"link_fieldname": row["link_fieldname"],
			},
		)
	)


def _target_ready(row):
	if not frappe.db.exists("DocType", row["link_doctype"]):
		return False
	# has_field alone is not enough: the Custom Field row can exist before
	# `frappe.db.updatedb` has added the column, and the connections panel
	# queries the column.
	if not frappe.get_meta(row["link_doctype"]).has_field(row["link_fieldname"]):
		return False
	return frappe.db.has_column(row["link_doctype"], row["link_fieldname"])


def _insert_link(parent, row):
	"""Insert the child row directly.

	Saving the parent DocType is not an option: it re-runs the very validation
	this module exists to work around, and a standard DocType is not editable
	outside developer mode.
	"""
	idx = (
		frappe.db.sql(
			"SELECT COALESCE(MAX(idx), 0) FROM `tabDocType Link` WHERE parent = %s AND parentfield = 'links'",
			parent,
		)[0][0]
		or 0
	) + 1

	doc = frappe.new_doc("DocType Link")
	doc.update(
		{
			"parent": parent,
			"parenttype": "DocType",
			"parentfield": "links",
			"link_doctype": row["link_doctype"],
			"link_fieldname": row["link_fieldname"],
			"group": row.get("group"),
			"custom": 1,
			"idx": idx,
		}
	)
	doc.db_insert()
