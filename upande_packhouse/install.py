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
from upande_packhouse.patches.index_stock_entry_issued_to import execute as ensure_issued_to_index
from upande_packhouse.stock_movement import ensure_line_bucket_index

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
	from upande_packhouse.allocation_plan import ensure_indexes, setup_settings

	for step in (
		ensure_dashboard_links,
		ensure_line_bucket_index,
		ensure_issued_to_index,
		setup_settings,
		ensure_indexes,
	):
		try:
			step()
		except Exception:
			frappe.log_error(
				title=f"upande_packhouse post-deploy step failed: {step.__name__}",
				message=frappe.get_traceback(),
			)


# ─── tests ────────────────────────────────────────────────────────────────

TEST_COMPANY = "Karen Roses"


def before_tests():
	"""Give a bare CI site the masters live has and the suite reads: a Company (the
	setup wizard's, with its Price Lists and UOMs), the Business Unit and Farm
	accounting dimensions (Sales Order's `business_unit` and `farm` columns), the
	fields this app reads that live gets from other apps, the Roses Business Unit
	and the "Cash before collection" payment terms."""
	frappe.clear_cache()
	if not frappe.db.count("Company"):
		_make_test_company()
	if not frappe.db.exists("Business Unit", "Roses"):
		frappe.get_doc({"doctype": "Business Unit", "business_unit_name": "Roses"}).insert(
			ignore_permissions=True
		)
	for document_type in ("Business Unit", "Farm"):
		_ensure_accounting_dimension(document_type)
	_ensure_foreign_fields()
	# Live has it from the patch; a bare site gets the column after install ran.
	ensure_issued_to_index()
	_ensure_payment_terms("Cash before collection")
	frappe.db.commit()  # nosemgrep
	frappe.clear_cache()


def _make_test_company():
	from erpnext.setup.setup_wizard.setup_wizard import setup_complete

	year = frappe.utils.now_datetime().year
	setup_complete(
		frappe._dict(
			{
				"currency": "KES",
				"full_name": "Test User",
				"company_name": TEST_COMPANY,
				"timezone": "Africa/Nairobi",
				"company_abbr": "KR",
				"country": "Kenya",
				"fy_start_date": "{0}-01-01".format(year),
				"fy_end_date": "{0}-12-31".format(year),
				"language": "english",
				"company_tagline": "Testing",
				"email": "test@example.com",
				"password": "test",
				"chart_of_accounts": "Standard",
			}
		)
	)


def _ensure_accounting_dimension(document_type):
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		make_dimension_in_accounting_doctypes,
	)

	name = frappe.db.get_value("Accounting Dimension", {"document_type": document_type})
	doc = (
		frappe.get_doc("Accounting Dimension", name)
		if name
		else frappe.get_doc({"doctype": "Accounting Dimension", "document_type": document_type}).insert(
			ignore_permissions=True
		)
	)
	frappe.clear_cache(doctype="Sales Order")
	if not frappe.get_meta("Sales Order").has_field(doc.fieldname):
		make_dimension_in_accounting_doctypes(doc=doc)


# Fields this app's code reads that live carries from other apps' customizations.
FOREIGN_FIELDS = {
	"Customer": [("custom_business_unit", "Link", "Business Unit")],
	"Delivery Note": [("custom_business_unit", "Link", "Business Unit")],
	"Item": [("custom_pack_rate", "Float", None)],
	"Sales Invoice": [("custom_business_unit", "Link", "Business Unit"), ("custom_farm", "Link", "Farm")],
	"Sales Order": [
		("custom_priority", "Data", None),
		("custom_business_unit", "Link", "Business Unit"),
		("custom_farm", "Link", "Farm"),
		("custom_order_name", "Data", None),
		("custom_floriday_delivery_id", "Data", None),
		("custom_floriday_fulfillment_order_id", "Data", None),
	],
	"Sales Order Item": [
		("custom_source_warehouse", "Link", "Warehouse"),
		("custom_pack_rate", "Float", None),
	],
	"Stock Entry": [
		("custom_scanned_packing", "Check", None),
		("custom_scanned_grading", "Check", None),
		("custom_bunch_id", "Data", None),
		("custom_bunched_by", "Link", "UOM"),
		("custom_graded_by", "Link", "Employee"),
		("custom_harvest_entry", "Link", "Stock Entry"),
		("custom_grading_entry", "Link", "Stock Entry"),
		("custom_stem_length", "Link", "Stem Length"),
		("custom_receiving_batch_id", "Data", None),
		("custom_cut_stage", "Link", "Cut Stage"),
		("custom_harvester", "Link", "Employee"),
		("custom_receiving_entry", "Link", "Stock Entry"),
		("custom_bucket_id", "Link", "Bucket QR Code"),
		("custom_greenhouse", "Link", "Warehouse"),
	],
	"Stock Entry Detail": [
		("custom_stem_length", "Link", "Stem Length"),
		("custom_bucket_id", "Link", "Bucket QR Code"),
	],
}


def _ensure_foreign_fields():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	for doctype, fields in FOREIGN_FIELDS.items():
		meta = frappe.get_meta(doctype)
		for fieldname, fieldtype, options in fields:
			if meta.has_field(fieldname):
				continue
			if fieldtype == "Link" and not frappe.db.exists("DocType", options):
				fieldtype, options = "Data", None
			create_custom_field(
				doctype,
				{
					"fieldname": fieldname,
					"label": fieldname[7:].replace("_", " ").title(),
					"fieldtype": fieldtype,
					"options": options,
				},
			)


def _ensure_payment_terms(name):
	if not frappe.db.exists("Payment Term", name):
		frappe.get_doc(
			{
				"doctype": "Payment Term",
				"payment_term_name": name,
				"invoice_portion": 100,
				"due_date_based_on": "Day(s) after invoice date",
				"credit_days": 0,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Payment Terms Template", name):
		frappe.get_doc(
			{
				"doctype": "Payment Terms Template",
				"template_name": name,
				"terms": [
					{
						"payment_term": name,
						"invoice_portion": 100,
						"due_date_based_on": "Day(s) after invoice date",
						"credit_days": 0,
					}
				],
			}
		).insert(ignore_permissions=True)
