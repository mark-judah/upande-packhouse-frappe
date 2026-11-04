"""Seed the three order sources the packhouse imports today as ordinary
`Order Import Platform` records, so every site starts from the same standard setup:

- XPOL Cloud   - one flat Excel export, one customer, an order per harvest date, cancelled lines left out.
- Flamingo Cloud - a wide matrix workbook (PO groups side by side), one customer.
- DFG Cloud    - one CSV that mixes several customers, told apart by its Buyer column.

Records that already exist are never touched (a site may have edited them), and a customer
link is only made when that customer exists on the site. Safe to run again.
"""

import frappe

ORDER = "Sales Order"
ITEM = "Sales Order Item"


def _m(source, doctype, field):
	return {"source_column": source, "target_doctype": doctype, "target_fieldname": field}


PLATFORMS = [
	{
		"platform_name": "XPOL Cloud",
		"file_type": "Excel",
		"layout_type": "Standard Rows",
		"header_row": 1,
		"multi_order_column": "Harvest date",
		"skip_rows_column": "Status",
		"skip_rows_values": "CANCELLED",
		"po_no_template": "XPOL {delivery_date}",
		"notes": (
			"XPOL Cloud order export (.xls). One order per Harvest date. Cancelled lines are skipped. "
			"The Description is matched to a Specification; when it is not one the Line code is used to "
			"find it, and anything still unclear goes to the import queue, which remembers the answer. "
			"The file carries no PO number, so each order is referenced as XPOL plus its harvest date. "
			"The file's price, line code and received/shipped counts are not used."
		),
		"mappings": [
			_m("Harvest date", ORDER, "delivery_date"),
			_m("Description", ITEM, "custom_line"),
			_m("Line code", ITEM, "_line_code"),
			_m("Box quantity", ITEM, "custom_number_of_boxes"),
		],
		"customers": [{"customer": "Xpol"}],
	},
	{
		"platform_name": "Flamingo Cloud",
		"file_type": "Excel",
		"layout_type": "Wide Matrix",
		"header_row": 1,
		"multi_order_column": "PO Number",
		"matrix": {
			"matrix_group_label_row": 2,
			"matrix_order_date_row": 3,
			"matrix_delivery_date_row": 4,
			"matrix_po_row": 5,
			"matrix_items_start_row": 6,
			"matrix_code_column": 0,
			"matrix_description_column": 1,
			"matrix_bunch_rate_column": 3,
			"matrix_first_group_column": 4,
			"matrix_group_column_width": 2,
			"matrix_stop_value": "Total",
		},
		"notes": (
			"Flamingo (KARE01) PO lines by date workbook: one group of columns per PO. The ordered "
			"bunches column drives the quantity and is converted to boxes through the Specification. "
			"The file's rate is never used."
		),
		"mappings": [
			_m("Specification", ITEM, "custom_line"),
			_m("Bunches Ordered", ITEM, "_bunches_ordered"),
			_m("Bunch Rate", ITEM, "_bunch_rate"),
			_m("PO Number", ORDER, "po_no"),
			_m("Order Date", ORDER, "transaction_date"),
			_m("Delivery Date", ORDER, "delivery_date"),
			_m("Line Code", ITEM, "_line_code"),
		],
		"customers": [{"customer": "FLAMINGO  UK"}],
	},
	{
		"platform_name": "DFG Cloud",
		"file_type": "CSV",
		"layout_type": "Standard Rows",
		"header_row": 1,
		"multi_order_column": "PO nr",
		"customer_differentiator_column": "Buyer",
		"notes": "DFG Cloud CSV export. The Buyer column says which customer an order is for.",
		"mappings": [
			_m("Handover date", ORDER, "delivery_date"),
			_m("PO nr", ORDER, "po_no"),
			_m("Delivery addr", ORDER, "custom_consignee"),
			_m("Article descr", ITEM, "custom_line"),
			_m("St/ Bu", ITEM, "uom"),
			_m("Present boxes", ITEM, "custom_number_of_boxes"),
			_m("Packrate", ITEM, "custom_packrate"),
			_m("Price", ITEM, "rate"),
			_m("Lgth", ITEM, "custom_length"),
			_m("Fairtrade", ITEM, "custom_ftnft"),
		],
		"customers": [
			{"customer": "UK & IE Flora Group Limited", "differentiator_value": "JZF"},
			{"customer": "Superflora B.V.(IRELAND)", "differentiator_value": "SFL"},
		],
	},
]


def ensure_platform(spec):
	"""Create one platform (and its customer links) unless it already exists. Returns True when created."""
	name = spec["platform_name"]
	if frappe.db.exists("Order Import Platform", name):
		return False
	doc = frappe.new_doc("Order Import Platform")
	for f in (
		"platform_name",
		"file_type",
		"layout_type",
		"header_row",
		"multi_order_column",
		"customer_differentiator_column",
		"skip_rows_column",
		"skip_rows_values",
		"date_format",
		"po_no_template",
		"notes",
	):
		if spec.get(f) is not None:
			doc.set(f, spec[f])
	for f, v in (spec.get("matrix") or {}).items():
		doc.set(f, v)
	for m in spec["mappings"]:
		doc.append("default_field_mappings", m)
	doc.insert(ignore_permissions=True)

	for c in spec.get("customers") or []:
		if not frappe.db.exists("Customer", c["customer"]):
			continue
		link = frappe.new_doc("Customer Order Mapping")
		link.customer = c["customer"]
		link.platform = name
		if c.get("differentiator_value"):
			link.differentiator_value = c["differentiator_value"]
		link.insert(ignore_permissions=True)
	return True


# Earlier names of the same sources, renamed in place so their customer links and learned mappings follow.
LEGACY_NAMES = {"DFG Clear": "DFG Cloud", "Flamingo UK": "Flamingo Cloud"}


def execute():
	for old, new in LEGACY_NAMES.items():
		if frappe.db.exists("Order Import Platform", old) and not frappe.db.exists(
			"Order Import Platform", new
		):
			frappe.rename_doc("Order Import Platform", old, new, force=True)
			frappe.db.set_value("Order Import Platform", new, "platform_name", new)
	for spec in PLATFORMS:
		ensure_platform(spec)
