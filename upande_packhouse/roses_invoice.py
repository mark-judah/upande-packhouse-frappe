"""Delivery Note -> Sales Invoice for the Roses packhouse flow.

Replaces the old Dispatch-Form-builds-invoice path. A Delivery Note carries all
the packhouse/dispatch data on its "Roses Packhouse" tab (copied on at creation),
and on submit we generate the Sales Invoice with ERPNext's native mapper, then
fill the same-named custom fields from the DN so nothing is lost.
"""

import frappe

try:
	from erpnext.stock.doctype.delivery_note.mapper import make_sales_invoice
except ImportError:  # older ERPNext layout
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

HEADER_FIELDS = [
	"custom_so", "custom_farm", "custom_business_unit", "custom_flo_id", "custom_flo_id_2",
	"custom_freight", "custom_transport_mode", "custom_brn_ref", "custom_consignee",
	"custom_delivery_point", "custom_dispatch_form", "custom_truck_details", "custom_total_boxes",
]
ITEM_FIELDS = [
	"custom_length", "custom_total_boxes", "custom_total_stems", "custom_stems_per_box",
	"custom_farm_codes", "custom_source_farm", "custom_hsc", "custom_crop_type",
]


def sync_accounting_dimensions(doc, method=None):
	"""Keep the native Farm / Business Unit accounting-dimension fields and the
	legacy packhouse custom_farm / custom_business_unit fields in lockstep.

	`farm` / `business_unit` (the accounting dimensions) are now the source of
	truth — harvesting and grading set them directly. This hook mirrors each
	pair in whichever direction has a value, so:
	  * new records that set only `farm` still populate `custom_farm`, keeping
	    the many dashboards that still read custom_farm working during the
	    migration, and
	  * older records / integrations that set only `custom_farm` still populate
	    the accounting dimension for GL + reports.
	The accounting-dimension value wins if both are set.
	"""
	def mirror(dim_field, legacy_field):
		if not (doc.meta.get_field(dim_field) and doc.meta.get_field(legacy_field)):
			return
		dim_val = doc.get(dim_field)
		legacy_val = doc.get(legacy_field)
		if dim_val:
			if legacy_val != dim_val:
				doc.set(legacy_field, dim_val)
		elif legacy_val:
			doc.set(dim_field, legacy_val)

	mirror("farm", "custom_farm")
	mirror("business_unit", "custom_business_unit")


def delivery_note_on_submit(doc, method=None):
	# Only Roses-business-unit deliveries generate a packhouse Sales Invoice.
	if (doc.get("custom_business_unit") or "") != "Roses":
		return

	# Idempotent: skip if a Sales Invoice already references this Delivery Note.
	existing = frappe.db.sql(
		"SELECT parent FROM `tabSales Invoice Item` WHERE delivery_note = %s LIMIT 1", doc.name
	)
	if existing:
		return

	si = make_sales_invoice(doc.name)  # native mapper: items, rate, warehouse, SO/DN linkage

	# copy header custom fields DN -> SI (same fieldnames)
	for f in HEADER_FIELDS:
		val = doc.get(f)
		if val is not None and val != "":
			si.set(f, val)
	si.custom_dispatch_form = doc.name

	# copy item custom fields, matched by the SI item's dn_detail (= DN item name)
	dn_items = {it.name: it for it in doc.items}
	for si_it in si.items:
		dn_it = dn_items.get(si_it.get("dn_detail"))
		if not dn_it:
			dn_it = next((d for d in doc.items if d.item_code == si_it.item_code), None)
		if dn_it:
			for f in ITEM_FIELDS:
				val = dn_it.get(f)
				if val is not None and val != "":
					si_it.set(f, val)

	si.flags.ignore_permissions = True
	si.insert(ignore_permissions=True)  # leave as Draft, matching the old dispatch flow
	frappe.msgprint("Sales Invoice " + si.name + " created from Delivery Note " + doc.name)
