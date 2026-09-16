# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Single entry point for work that brackets this app's resource sync.

Two phases, and the difference matters:

* `before_install` / `before_migrate` run *before* frappe syncs doctypes and
  customizations. Only prerequisites belong here -- things the sync itself
  would fall over without -- and they are allowed to raise, because failing
  here with a clear message beats failing three steps later with a
  WrongOptionsDoctypeLinkError.
* `after_install` / `after_migrate` run once everything is in place, which is
  the first moment the app can rely on its own fields existing. Anything there
  must be idempotent -- both hooks run again on every deploy -- and must never
  raise: a failure would abort an otherwise good install/migrate.
"""

import frappe

from upande_packhouse.dashboard_links import ensure_dashboard_links

# Mirrors the definition upande_agriculture ships (autoname field:cutstage, one
# unique Data field) so that app's JSON syncs cleanly over this stand-in and
# takes ownership -- same table, same column, existing rows preserved.
#
# Module is "Custom", not "Upande Packhouse": `before_install` runs before
# sync_for(), so this app's own Module Def does not exist yet and a Link to it
# would fail. "Custom" ships with frappe and is always present.
CUT_STAGE_DOCTYPE = {
	"doctype": "DocType",
	"name": "Cut Stage",
	"module": "Custom",
	"custom": 1,
	"naming_rule": "By fieldname",
	"autoname": "field:cutstage",
	"sort_field": "creation",
	"sort_order": "DESC",
	"fields": [
		{
			"fieldname": "cutstage",
			"fieldtype": "Data",
			"label": "Cut Stage",
			"in_list_view": 1,
			"reqd": 1,
			"unique": 1,
		}
	],
	"permissions": [
		{
			"role": "System Manager",
			"read": 1,
			"write": 1,
			"create": 1,
			"delete": 1,
			"report": 1,
			"export": 1,
			"print": 1,
			"email": 1,
			"share": 1,
		}
	],
}


def ensure_cut_stage_doctype() -> bool:
	"""Create the `Cut Stage` master if no app has installed it yet.

	Two of this app's Link fields resolve against it -- `Sales Order Item.
	custom_cut_stage` and `Specifications.cut_stage` -- and frappe validates Link
	options while syncing doctypes and customizations, so a site without the
	doctype cannot install this app at all.

	The master really belongs to upande_agriculture, but that app Links at THIS
	app's `Bucket QR Code` (`Stock Entry.custom_bucket_id`), so it has to install
	*after* us -- neither app can go first. Creating it here breaks the cycle.
	`custom: 1` is load-bearing: DocType.check_developer_mode only permits
	creating a doctype on a site without developer_mode when it is custom, and CI
	and production both run with developer_mode off.

	When upande_agriculture installs later its standard JSON takes ownership
	(import_file sets ignore_validate, so the custom -> standard switch goes
	through); nothing here needs to be undone.

	Returns True when it created the doctype, False when one already existed.
	"""
	if frappe.db.exists("DocType", "Cut Stage"):
		return False

	frappe.get_doc(CUT_STAGE_DOCTYPE).insert(ignore_permissions=True)
	return True


def before_install():
	_ensure_prerequisites()


def before_migrate():
	_ensure_prerequisites()


def _ensure_prerequisites():
	"""Link targets the doctype/customization sync would otherwise choke on."""
	ensure_cut_stage_doctype()


def after_install():
	_run()


def after_migrate():
	_run()


def _run():
	for step in (ensure_dashboard_links,):
		try:
			step()
		except Exception:
			frappe.log_error(
				title=f"upande_packhouse post-deploy step failed: {step.__name__}",
				message=frappe.get_traceback(),
			)
