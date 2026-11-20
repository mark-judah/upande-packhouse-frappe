"""Delivery Note -> Sales Invoice for the Roses packhouse flow.

Replaces the old Dispatch-Form-builds-invoice path. Dispatch builds one
Delivery Note per Sales Order (mobile/api.py's createOrUpdateDispatch); the
per-order packhouse/dispatch data (consignee/delivery point/freight/transport
mode/BRN/truck) lives on the Delivery Note ITEM rows.

Whether a Sales Invoice is made on submit is a Production Settings choice
(Dispatch and Invoicing > Create Sales Invoice When a Delivery Note Is
Submitted). Ticked: each submitted Roses Delivery Note gets its own draft
invoice, built with ERPNext's native mapper, then the same-named ITEM custom
fields are filled from each matching DN item; only the genuinely
document-wide fields (farm, business_unit, the manually-filled FLO IDs) copy
at header level. Unticked: nothing is created, and users pick the submitted
Delivery Notes they want billed together (Sales Invoice > Get Items From >
Delivery Note), which is how a customer's orders are consolidated.
"""

import frappe
from frappe import _

try:
	from erpnext.stock.doctype.delivery_note.mapper import make_sales_invoice
except ImportError:  # older ERPNext layout
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

HEADER_FIELDS = ["farm", "business_unit", "custom_flo_id", "custom_flo_id_2"]
ITEM_FIELDS = [
	"custom_length",
	"custom_total_boxes",
	"custom_total_stems",
	"custom_stems_per_box",
	"custom_farm_codes",
	"custom_source_farm",
	"custom_hsc",
	"custom_crop_type",
	"custom_consignee",
	"custom_delivery_point",
	"custom_freight",
	"custom_transport_mode",
	"custom_brn_ref",
	"custom_truck_details",
]


def sync_sales_order_accounting_dimensions(doc, method=None):
	"""Sales Order only. ecommerce_integration (Floriday/Biflorica) owns
	custom_farm / custom_business_unit on Sales Order for its own marketplace
	sync — it force-recreates them every migrate via its own after_migrate
	hook, and floriday_sales_order.py writes farm/business-unit data into
	THOSE fields, not the real accounting dimensions. Bridge that write into
	the real farm / business_unit dimension fields so the rest of this app
	(which reads only the dimension fields now, not the legacy ones) sees
	Floriday-origin orders correctly. Sales Invoice and Delivery Note have no
	legacy fields left at all (removed outright, no other app depends on
	them there), so this hook is not needed for those.
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
	if (doc.get("business_unit") or "") != "Roses":
		return

	# Opt-in: left off, users select the Delivery Notes to consolidate themselves.
	if not frappe.db.get_single_value("Production Settings", "auto_invoice_on_delivery_note"):
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
	frappe.msgprint(_("Sales Invoice {0} created from Delivery Note {1}").format(si.name, doc.name))
