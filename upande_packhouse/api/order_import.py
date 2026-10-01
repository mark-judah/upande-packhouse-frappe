# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Backend for the Sales Order page's "Import" flow -- customer order files
# from platforms (XPOL Cloud, etc.) mapped onto real Sales Order / Sales
# Order Item fields.
#
# File parsing (CSV/XLSX -> a plain header + rows array) happens entirely
# client-side; this module only resolves mapping and shapes rows. The
# resulting per-order payload is deliberately shaped to match
# api/sales_order.py's `manual_rows` input exactly (item_code,
# stems_per_bunch, bunches_per_box, boxes, length, box_type, mixed_box,
# mixed_bunch, mix_group, bunch_group, warehouse) -- an imported order is
# saved through the exact same saveSalesOrder path (and therefore the same
# real validation/pricing engine) as one built by hand in the ledger, not a
# separate write path.

import json

import frappe

from upande_packhouse import spec_autofill

# Sales Order Item has no native "stems per bunch" / "bunches per box"
# fields (those are Specifications-only concepts) -- these two virtual
# targets let a column map onto them anyway; buildPreview's row-shaping
# recognizes them specially to derive custom_packrate(_mixed_box) and the
# "Bunch (N)" UOM, exactly like a manual ledger card would.
VIRTUAL_ITEM_FIELDS = [
	{"fieldname": "_stems_per_bunch", "label": "Stems Per Bunch (auto Pack Rate + UOM)", "fieldtype": "Int"},
	{"fieldname": "_bunches_per_box", "label": "Bunches Per Box (auto Pack Rate + UOM)", "fieldtype": "Int"},
]

IGNORED_FIELDTYPES = {
	"Section Break",
	"Column Break",
	"Tab Break",
	"HTML",
	"Button",
	"Table",
	"Table MultiSelect",
	"Fold",
	"Heading",
	"Image",
}
IGNORED_FIELDNAMES = {
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
	"naming_series",
}
# Recomputed by sales_order_engine on save -- mappable (nothing stops it,
# per "map to any field"), but flagged in the catalog so the UI can warn
# that it's usually not necessary.
ENGINE_COMPUTED_FIELDS = {
	"qty",
	"stock_qty",
	"conversion_factor",
	"rate",
	"price_list_rate",
	"amount",
	"custom_ordered_quantity",
	"custom_packrate",
	"custom_packrate_mixed_box",
	"uom",
}


def _json_payload():
	"""See specifications.py's _json_payload: frappe.call() form-encodes its
	request body, so a complex arg only ever arrives as a JSON-stringified
	`data` form field, never as a raw JSON request body."""
	raw = frappe.form_dict.get("data")
	if raw is None:
		return {}
	if isinstance(raw, str):
		return frappe.parse_json(raw) or {}
	return raw


def _catalog_for(doctype):
	meta = frappe.get_meta(doctype)
	out = []
	for df in meta.fields:
		if df.fieldtype in IGNORED_FIELDTYPES or df.fieldname in IGNORED_FIELDNAMES:
			continue
		out.append(
			{
				"fieldname": df.fieldname,
				"label": df.label or df.fieldname,
				"fieldtype": df.fieldtype,
				"computed": df.fieldname in ENGINE_COMPUTED_FIELDS,
			}
		)
	return out


@frappe.whitelist()
def getFieldCatalog():
	"""Every real, mappable field on Sales Order and Sales Order Item, read
	live from doctype meta -- never a hardcoded list, so a custom field
	added later (by this app or any other sharing these doctypes) just
	shows up as a mapping target automatically."""
	try:
		so_fields = _catalog_for("Sales Order")
		soi_fields = VIRTUAL_ITEM_FIELDS + _catalog_for("Sales Order Item")
		frappe.response["message"] = {
			"success": True,
			"sales_order_fields": so_fields,
			"sales_order_item_fields": soi_fields,
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getFieldCatalog error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# ----------------------------- platforms -----------------------------


@frappe.whitelist()
def listPlatforms():
	try:
		rows = frappe.get_all(
			"Order Import Platform",
			fields=["name", "platform_name", "file_type", "multi_order_column"],
			order_by="platform_name asc",
		)
		frappe.response["message"] = {"success": True, "platforms": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listPlatforms error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "platforms": []}


@frappe.whitelist()
def getPlatform():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Order Import Platform", name):
			frappe.response["message"] = {"success": False, "error": "Platform not found"}
			return
		doc = frappe.get_doc("Order Import Platform", name)
		frappe.response["message"] = {
			"success": True,
			"platform": {
				"name": doc.name,
				"platform_name": doc.platform_name,
				"file_type": doc.file_type,
				"header_row": doc.header_row,
				"multi_order_column": doc.multi_order_column,
				"notes": doc.notes,
				"default_field_mappings": [
					{
						"source_column": m.source_column,
						"target_doctype": m.target_doctype,
						"target_fieldname": m.target_fieldname,
						"default_value": m.default_value,
					}
					for m in doc.default_field_mappings
				],
			},
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getPlatform error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def savePlatform():
	try:
		data = _json_payload()
		name = data.get("name")
		if name and frappe.db.exists("Order Import Platform", name):
			doc = frappe.get_doc("Order Import Platform", name)
		else:
			doc = frappe.new_doc("Order Import Platform")
		for f in ("platform_name", "file_type", "header_row", "multi_order_column", "notes"):
			if f in data:
				doc.set(f, data.get(f))
		doc.set("default_field_mappings", [])
		for m in data.get("default_field_mappings") or []:
			doc.append(
				"default_field_mappings",
				{
					"source_column": m.get("source_column"),
					"target_doctype": m.get("target_doctype"),
					"target_fieldname": m.get("target_fieldname"),
					"default_value": m.get("default_value"),
				},
			)
		if doc.is_new():
			doc.insert()
		else:
			doc.save()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": doc.name}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="savePlatform error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def deletePlatform():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Order Import Platform", name):
			frappe.response["message"] = {"success": False, "error": "Not found"}
			return
		frappe.delete_doc("Order Import Platform", name, ignore_permissions=False)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="deletePlatform error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# ------------------------ customer order mappings ------------------------


@frappe.whitelist()
def listCustomerMappings():
	try:
		customer = frappe.form_dict.get("customer") or ""
		filters = {}
		if customer:
			filters["customer"] = customer
		rows = frappe.get_all(
			"Customer Order Mapping",
			filters=filters,
			fields=["name", "customer", "platform"],
			order_by="customer asc",
		)
		frappe.response["message"] = {"success": True, "mappings": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listCustomerMappings error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "mappings": []}


@frappe.whitelist()
def getCustomerMapping():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Customer Order Mapping", name):
			frappe.response["message"] = {"success": False, "error": "Mapping not found"}
			return
		doc = frappe.get_doc("Customer Order Mapping", name)
		frappe.response["message"] = {
			"success": True,
			"mapping": {
				"name": doc.name,
				"customer": doc.customer,
				"platform": doc.platform,
				"default_currency": doc.default_currency,
				"default_price_list": doc.default_price_list,
				"default_warehouse": doc.default_warehouse,
				"default_consignee": doc.default_consignee,
				"default_delivery_point": doc.default_delivery_point,
				"default_shipping_agent": doc.default_shipping_agent,
				"field_mapping_overrides": [
					{
						"source_column": m.source_column,
						"target_doctype": m.target_doctype,
						"target_fieldname": m.target_fieldname,
						"default_value": m.default_value,
					}
					for m in doc.field_mapping_overrides
				],
				"item_code_mapping": [
					{"source_item_code": m.source_item_code, "item": m.item} for m in doc.item_code_mapping
				],
			},
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getCustomerMapping error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def saveCustomerMapping():
	try:
		data = _json_payload()
		name = data.get("name")
		if name and frappe.db.exists("Customer Order Mapping", name):
			doc = frappe.get_doc("Customer Order Mapping", name)
		else:
			doc = frappe.new_doc("Customer Order Mapping")
		for f in (
			"customer",
			"platform",
			"default_currency",
			"default_price_list",
			"default_warehouse",
			"default_consignee",
			"default_delivery_point",
			"default_shipping_agent",
		):
			if f in data:
				doc.set(f, data.get(f))
		doc.set("field_mapping_overrides", [])
		for m in data.get("field_mapping_overrides") or []:
			doc.append(
				"field_mapping_overrides",
				{
					"source_column": m.get("source_column"),
					"target_doctype": m.get("target_doctype"),
					"target_fieldname": m.get("target_fieldname"),
					"default_value": m.get("default_value"),
				},
			)
		doc.set("item_code_mapping", [])
		for m in data.get("item_code_mapping") or []:
			doc.append(
				"item_code_mapping",
				{
					"source_item_code": m.get("source_item_code"),
					"item": m.get("item"),
				},
			)
		if doc.is_new():
			doc.insert()
		else:
			doc.save()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": doc.name}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="saveCustomerMapping error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def deleteCustomerMapping():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Customer Order Mapping", name):
			frappe.response["message"] = {"success": False, "error": "Not found"}
			return
		frappe.delete_doc("Customer Order Mapping", name, ignore_permissions=False)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="deleteCustomerMapping error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# --------------------- one-screen customer setup (auto platform) ---------------------
#
# The wizard used to make the user create an "Order Import Platform" by hand first
# (name it, set a header row, type a multi-order split column, type source column
# names from memory) before they could map anything. That's the part that was
# "too manual" - this collapses it into one save behind step 3 (mapping): one
# customer gets one auto-owned, auto-named platform, created or updated here,
# never shown to the user as a thing they manage separately.


def _platform_name_for_customer(customer: str) -> str:
	return f"{customer} Import"


@frappe.whitelist()
def getCustomerImportSetup():
	"""What step 3 pre-fills itself from for a customer who's been set up before -
	the same merged platform + customer-mapping view buildPreview already computes,
	handed back as a flat list of {source_column, target_doctype, target_fieldname,
	default_value} rows instead of a platform name the user never has to see."""
	try:
		customer = frappe.form_dict.get("customer")
		if not customer:
			frappe.response["message"] = {"success": False, "error": "Customer is required"}
			return
		platform = _platform_name_for_customer(customer)
		if not frappe.db.exists("Order Import Platform", platform):
			frappe.response["message"] = {"success": True, "exists": False}
			return
		platform_doc, cust_doc, mapping, item_code_map, defaults = _resolve_mapping(customer, platform)
		frappe.response["message"] = {
			"success": True,
			"exists": True,
			"multi_order_column": platform_doc.multi_order_column,
			"field_mappings": [{"source_column": col, **target} for col, target in mapping.items()],
			"item_code_mapping": [{"source_item_code": k, "item": v} for k, v in item_code_map.items()],
			"defaults": defaults,
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getCustomerImportSetup error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def saveCustomerImportSetup():
	"""file_type/header_row are fixed, not user input - CSV and Excel are already
	told apart by file extension client-side, and the header is always row 1 (see
	applyParsedGrid, which never reads anything else - the old Header Row field
	promised a configurability nothing downstream of it actually honoured)."""
	try:
		data = _json_payload()
		customer = data.get("customer")
		if not customer:
			frappe.response["message"] = {"success": False, "error": "Customer is required"}
			return
		platform_name = _platform_name_for_customer(customer)

		if frappe.db.exists("Order Import Platform", platform_name):
			platform_doc = frappe.get_doc("Order Import Platform", platform_name)
		else:
			platform_doc = frappe.new_doc("Order Import Platform")
			platform_doc.platform_name = platform_name
			platform_doc.file_type = "CSV"
			platform_doc.header_row = 1
		platform_doc.multi_order_column = data.get("multi_order_column") or ""
		platform_doc.set("default_field_mappings", [])
		for m in data.get("field_mappings") or []:
			if not m.get("source_column") or not m.get("target_fieldname"):
				continue
			platform_doc.append(
				"default_field_mappings",
				{
					"source_column": m.get("source_column"),
					"target_doctype": m.get("target_doctype"),
					"target_fieldname": m.get("target_fieldname"),
					"default_value": m.get("default_value"),
				},
			)
		if platform_doc.is_new():
			platform_doc.insert()
		else:
			platform_doc.save()

		cust_name = frappe.db.get_value(
			"Customer Order Mapping", {"customer": customer, "platform": platform_name}
		)
		cust_doc = (
			frappe.get_doc("Customer Order Mapping", cust_name)
			if cust_name
			else frappe.new_doc("Customer Order Mapping")
		)
		cust_doc.customer = customer
		cust_doc.platform = platform_name
		defaults = data.get("defaults") or {}
		for f in (
			"default_currency",
			"default_price_list",
			"default_warehouse",
			"default_consignee",
			"default_delivery_point",
			"default_shipping_agent",
		):
			if f in defaults:
				cust_doc.set(f, defaults.get(f))
		# Everything now lives directly on this customer's own platform - no separate
		# override layer left to drift out of sync with it.
		cust_doc.set("field_mapping_overrides", [])
		cust_doc.set("item_code_mapping", [])
		for m in data.get("item_code_mapping") or []:
			if not m.get("source_item_code") or not m.get("item"):
				continue
			cust_doc.append(
				"item_code_mapping", {"source_item_code": m.get("source_item_code"), "item": m.get("item")}
			)
		if cust_doc.is_new():
			cust_doc.insert()
		else:
			cust_doc.save()

		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "platform": platform_name}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="saveCustomerImportSetup error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# ------------------- shared platforms (one source, several customers) -------------------
#
# Some platforms aren't one customer's own - a single export genuinely contains several
# real customers' orders at once, told apart by one of the file's own columns (a buyer
# code, say). Those need an actual, user-named, reusable Platform (unlike the single-
# customer case, which never surfaces "platform" as a concept at all) plus one Customer
# Order Mapping row per customer resolved against it, each carrying the raw value that
# means them.


@frappe.whitelist()
def listSharedPlatforms():
	"""Only platforms already set up as shared - step 2's "which source is this"
	picker, so a returning import reuses what was mapped last time instead of
	starting the buyer-code -> customer table over from scratch."""
	try:
		rows = frappe.get_all(
			"Order Import Platform",
			filters={"customer_differentiator_column": ["is", "set"]},
			fields=["name", "platform_name", "customer_differentiator_column", "multi_order_column"],
			order_by="platform_name asc",
		)
		frappe.response["message"] = {"success": True, "platforms": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listSharedPlatforms error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "platforms": []}


@frappe.whitelist()
def getSharedPlatformSetup():
	"""What step 3 pre-fills itself from for a shared platform picked in step 2 -
	the field mapping plus one overlay per customer already mapped to it."""
	try:
		platform = frappe.form_dict.get("platform")
		if not platform or not frappe.db.exists("Order Import Platform", platform):
			frappe.response["message"] = {"success": False, "error": "Platform not found"}
			return
		platform_doc = frappe.get_doc("Order Import Platform", platform)
		base_mapping = _platform_field_mapping(platform_doc)
		cust_names = frappe.get_all("Customer Order Mapping", filters={"platform": platform}, pluck="name")
		customers = []
		for name in cust_names:
			cust_doc = frappe.get_doc("Customer Order Mapping", name)
			customers.append(
				{
					"customer": cust_doc.customer,
					"differentiator_value": cust_doc.differentiator_value,
					"item_code_mapping": [
						{"source_item_code": m.source_item_code, "item": m.item}
						for m in cust_doc.item_code_mapping
					],
					"defaults": {f: cust_doc.get(f) for f in _CUSTOMER_DEFAULT_FIELDS if cust_doc.get(f)},
				}
			)
		frappe.response["message"] = {
			"success": True,
			"multi_order_column": platform_doc.multi_order_column,
			"customer_differentiator_column": platform_doc.customer_differentiator_column,
			"field_mappings": [{"source_column": col, **target} for col, target in base_mapping.items()],
			"customers": customers,
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getSharedPlatformSetup error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def saveSharedPlatformSetup():
	"""One shared platform, saved once (header_row fixed at 1, same reasoning as
	saveCustomerImportSetup), plus one Customer Order Mapping row per customer the
	user resolved in step 2 - the buyer-code -> customer table built there becomes
	real, reusable data here, not just something held in the browser for this run."""
	try:
		data = _json_payload()
		platform_name = (data.get("platform_name") or "").strip()
		differentiator_column = (data.get("customer_differentiator_column") or "").strip()
		customers = data.get("customers") or []
		if not platform_name:
			frappe.response["message"] = {"success": False, "error": "This source needs a name"}
			return
		if not differentiator_column:
			frappe.response["message"] = {
				"success": False,
				"error": "Pick the column that tells customers apart",
			}
			return
		if not customers:
			frappe.response["message"] = {"success": False, "error": "Map at least one customer"}
			return

		if frappe.db.exists("Order Import Platform", platform_name):
			platform_doc = frappe.get_doc("Order Import Platform", platform_name)
		else:
			platform_doc = frappe.new_doc("Order Import Platform")
			platform_doc.platform_name = platform_name
			platform_doc.file_type = "CSV"
			platform_doc.header_row = 1
		platform_doc.multi_order_column = data.get("multi_order_column") or ""
		platform_doc.customer_differentiator_column = differentiator_column
		platform_doc.set("default_field_mappings", [])
		for m in data.get("field_mappings") or []:
			if not m.get("source_column") or not m.get("target_fieldname"):
				continue
			platform_doc.append(
				"default_field_mappings",
				{
					"source_column": m.get("source_column"),
					"target_doctype": m.get("target_doctype"),
					"target_fieldname": m.get("target_fieldname"),
					"default_value": m.get("default_value"),
				},
			)
		if platform_doc.is_new():
			platform_doc.insert()
		else:
			platform_doc.save()

		for c in customers:
			customer = c.get("customer")
			differentiator_value = c.get("differentiator_value")
			if not customer or not differentiator_value:
				continue
			cust_name = frappe.db.get_value(
				"Customer Order Mapping", {"customer": customer, "platform": platform_doc.name}
			)
			cust_doc = (
				frappe.get_doc("Customer Order Mapping", cust_name)
				if cust_name
				else frappe.new_doc("Customer Order Mapping")
			)
			cust_doc.customer = customer
			cust_doc.platform = platform_doc.name
			cust_doc.differentiator_value = differentiator_value
			defaults = c.get("defaults") or {}
			for f in _CUSTOMER_DEFAULT_FIELDS:
				if f in defaults:
					cust_doc.set(f, defaults.get(f))
			cust_doc.set("field_mapping_overrides", [])
			cust_doc.set("item_code_mapping", [])
			for m in c.get("item_code_mapping") or []:
				if not m.get("source_item_code") or not m.get("item"):
					continue
				cust_doc.append(
					"item_code_mapping",
					{"source_item_code": m.get("source_item_code"), "item": m.get("item")},
				)
			if cust_doc.is_new():
				cust_doc.insert()
			else:
				cust_doc.save()

		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "platform": platform_doc.name}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="saveSharedPlatformSetup error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# ----------------------------- preview build -----------------------------


_CUSTOMER_DEFAULT_FIELDS = (
	"default_currency",
	"default_price_list",
	"default_warehouse",
	"default_consignee",
	"default_delivery_point",
	"default_shipping_agent",
)


def _platform_field_mapping(platform_doc):
	"""The platform's own default column mapping, shared by every customer on it,
	before any customer-specific override is layered on."""
	mapping = {}
	for m in platform_doc.default_field_mappings:
		mapping[m.source_column] = {
			"target_doctype": m.target_doctype,
			"target_fieldname": m.target_fieldname,
			"default_value": m.default_value,
		}
	return mapping


def _customer_overlay(cust_doc, base_mapping):
	"""Field mapping overrides + item code map + defaults for one Customer Order
	Mapping row, layered onto the platform's own shared base mapping."""
	mapping = dict(base_mapping)
	item_code_map = {}
	defaults = {}
	if cust_doc:
		for m in cust_doc.field_mapping_overrides:
			mapping[m.source_column] = {
				"target_doctype": m.target_doctype,
				"target_fieldname": m.target_fieldname,
				"default_value": m.default_value,
			}
		for im in cust_doc.item_code_mapping:
			item_code_map[im.source_item_code] = im.item
		for f in _CUSTOMER_DEFAULT_FIELDS:
			v = cust_doc.get(f)
			if v:
				defaults[f] = v
	return mapping, item_code_map, defaults


def _resolve_mapping(customer, platform):
	"""Single-customer platform - exactly one customer owns the whole thing, so
	there's exactly one Customer Order Mapping row to layer on."""
	platform_doc = frappe.get_doc("Order Import Platform", platform)
	base_mapping = _platform_field_mapping(platform_doc)
	cust_name = frappe.db.get_value("Customer Order Mapping", {"customer": customer, "platform": platform})
	cust_doc = frappe.get_doc("Customer Order Mapping", cust_name) if cust_name else None
	mapping, item_code_map, defaults = _customer_overlay(cust_doc, base_mapping)
	return platform_doc, cust_doc, mapping, item_code_map, defaults


def _shared_customer_overlays(platform_doc):
	"""Shared platform - every Customer Order Mapping row under it, keyed by the
	raw differentiator value it was mapped to (e.g. "JZF" -> UK IE's own overlay),
	so a group can be resolved to the right customer by that value alone."""
	base_mapping = _platform_field_mapping(platform_doc)
	rows = frappe.get_all("Customer Order Mapping", filters={"platform": platform_doc.name}, pluck="name")
	by_value = {}
	for name in rows:
		cust_doc = frappe.get_doc("Customer Order Mapping", name)
		if not cust_doc.differentiator_value:
			continue
		mapping, item_code_map, defaults = _customer_overlay(cust_doc, base_mapping)
		by_value[cust_doc.differentiator_value] = {
			"customer": cust_doc.customer,
			"mapping": mapping,
			"item_code_map": item_code_map,
			"defaults": defaults,
		}
	return by_value


_HEADER_DEFAULT_TARGETS = {
	"default_currency": "currency",
	"default_price_list": "selling_price_list",
	"default_consignee": "custom_consignee",
	"default_delivery_point": "custom_delivery_point",
	"default_shipping_agent": "custom_shipping_agent",
}

# Which imported item_row field each Spec Box Item field autofills/compares
# against, keyed by the item_row fieldname so buildPreview can walk it directly.
_SPEC_FIELD_MAP = [
	("_stems_per_bunch", "stems_per_bunch"),
	("_bunches_per_box", "bunches_per_box"),
	("custom_length", "length"),
]


def _normalize_length(v):
	"""Stem Length records are named with a trailing 'cm' ('40cm') but import
	files and manual entry both carry bare numbers ('40') - compare on digits
	only so that formatting difference is never flagged as a real conflict."""
	digits = "".join(ch for ch in str(v or "") if ch.isdigit())
	return digits or None


def _same_spec_value(item_field, file_val, spec_val):
	if item_field == "custom_length":
		return _normalize_length(file_val) == _normalize_length(spec_val)
	return str(file_val).strip() == str(spec_val).strip()


def _find_spec_match(customer, item_code):
	"""The same data an order built from a Specification would carry for this
	variety - found read-only via the bunch_id that links a Spec Approved
	Variety row to its own Spec Box Item row, without touching the interactive
	selection machinery in spec_autofill.build_spec_rows (that's built around
	a human picking colour-lines/box-items in the "Add from Specification"
	popup, not a one-shot per-row lookup like this).

	The Spec Box Item join is LEFT, not INNER - a Specification can approve a
	variety (Spec Approved Variety) without ever having entered pack data for
	it (Spec Box Item), e.g. a bunch_id typo between the two tables, or a
	variety added to the spec but never priced/packed. Matching here and
	simply finding nothing to autofill would look identical to no spec
	existing at all, which is a materially different thing to tell the user:
	"no spec for this variety" vs "found the spec, but it's missing data."
	is_incomplete flags exactly that second case."""
	if not customer or not item_code:
		return None
	rows = frappe.db.sql(
		"""
		select s.name as spec_name, av.bunch_id as bunch_id,
		       bi.stems_per_bunch as stems_per_bunch, bi.length as length,
		       bi.bunches_per_box as bunches_per_box, bi.pack_rate as pack_rate
		from `tabSpecifications` s
		inner join `tabSpec Approved Variety` av on av.parent = s.name
		left join `tabSpec Box Item` bi on bi.parent = s.name and bi.bunch_id = av.bunch_id
		where s.customer = %s and av.variety = %s and s.status = 'Active'
		order by av.is_primary desc, s.modified desc
		limit 1
		""",
		(customer, item_code),
		as_dict=True,
	)
	if not rows:
		return None
	match = rows[0]
	match["is_incomplete"] = not all(
		match.get(f) not in (None, "") for f in ("stems_per_bunch", "length", "bunches_per_box")
	)
	return match


@frappe.whitelist()
def getSpecMatchForVariety():
	"""customer, item_code. Lets the ledger's own "pick a variety" popup
	(used both for a normal manual line and for fixing an import row inside
	the Review & Fix popup) autofill stems-per-bunch/length/bunches-per-box
	the moment a variety is chosen, the same data "+ Add from Spec" would
	have carried - without that popup's own interactive selection flow."""
	try:
		data = _json_payload()
		match = _find_spec_match(data.get("customer"), data.get("item_code"))
		frappe.response["message"] = {"success": True, "match": match}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="getSpecMatchForVariety error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


def _apply_spec_match(item_row, customer):
	"""Autofills item_row's blank pack fields from a matching Specification,
	exactly like picking that spec would - but flags (never silently
	overwrites) any field the file itself already carries a different value
	for, so the user decides which one is right instead of one quietly
	winning.

	Always sets _spec_status, so the preview can say which of three very
	different things happened for this row:
	  "no_match"   - no Specification approves this variety for this
	                 customer at all.
	  "incomplete" - one does, but it's missing pack data (_spec_missing_
	                 fields lists which of stems-per-bunch/length/bunches-
	                 per-box) - the spec itself needs finishing, not the row.
	  "matched"    - fully matched; every blank field was autofilled."""
	item_code = item_row.get("item_code")
	if not item_code or item_row.get("_unresolved_item"):
		return
	match = _find_spec_match(customer, item_code)
	if not match:
		item_row["_spec_status"] = "no_match"
		return
	item_row["_spec_name"] = match["spec_name"]
	item_row["_spec_bunch_id"] = match["bunch_id"]
	missing_fields = []
	conflicts = []
	for item_field, spec_field in _SPEC_FIELD_MAP:
		spec_val = match.get(spec_field)
		if spec_val in (None, ""):
			missing_fields.append(item_field)
			continue
		file_val = item_row.get(item_field)
		if file_val in (None, ""):
			item_row[item_field] = spec_val
		elif not _same_spec_value(item_field, file_val, spec_val):
			conflicts.append({"field": item_field, "file_value": file_val, "spec_value": spec_val})
	if missing_fields:
		item_row["_spec_status"] = "incomplete"
		item_row["_spec_missing_fields"] = missing_fields
	else:
		item_row["_spec_status"] = "matched"
	if conflicts:
		item_row["_spec_conflicts"] = conflicts


# ------------------- rows identified by Specification, not item code -------------------
# Some customer files never carry a variety/item code at all - only a column
# naming the Specification (maps onto custom_line, labelled "Specification")
# plus a box count. That's not a gap to work around with a cleverer item-code
# guess; it's exactly what build_spec_rows (spec_autofill.py) already exists
# to turn into real Sales Order Item rows, the same way "+ Add from Spec"
# does for a human keying one order in by hand. _resolve_spec_row reuses that
# function directly rather than re-deriving pack data/variety choice here, so
# an imported line and a manually spec-filled line are IDENTICAL once saved -
# same custom_line/custom_mix_name/detail-payload fields, same validate-time
# exemptions, no second code path for sales_order_engine.py to disagree with.
def _resolve_spec_row(item_row, group_customer, counters):
	"""item_row's custom_line names a Specification. Returns (rows, error):
	rows is build_spec_rows's own output - one or more fully shaped Sales
	Order Item dicts (a multi-bunch spec, e.g. a Mixed Box, expands one file
	row into one row per bunch) - or rows=None with a plain-English reason
	the spec couldn't be used, exactly the same checks build_spec_rows itself
	runs (an incomplete spec, a bunch_id mismatch), not re-invented here.

	item_row.get("item_code"), if the file also mapped a variety column, is
	honoured as the operator's own pick wherever that variety is one of the
	spec's approved candidates - otherwise (the common case for these files)
	every colour/bunch defaults to its Specification's primary approved
	variety, the exact same default build_spec_rows itself falls back to
	when a human's selection omits a pick."""
	spec_name = item_row.get("custom_line")
	boxes = int(item_row.get("custom_number_of_boxes") or 0)
	if boxes <= 0:
		return None, "needs Number of Boxes set on this row"
	if not frappe.db.exists("Specifications", spec_name):
		return None, "not found"
	spec_doc = frappe.get_doc("Specifications", spec_name)

	issues = spec_autofill._spec_issues(spec_doc)
	if issues:
		return None, "is incomplete (" + "; ".join(issues) + ")"
	bunch_aware, bunches, shape_issues = spec_autofill._bunch_shape(spec_doc)
	if shape_issues:
		return None, "has a bunch_id mismatch (" + "; ".join(shape_issues) + ")"

	picked = item_row.get("item_code") or None
	warehouse = item_row.get("warehouse")

	if bunch_aware:
		selections = []
		for b in bunches:
			picks = {}
			for slot in b["slots"]:
				candidate_names = {av.variety for av in slot["candidates"]}
				if picked and picked in candidate_names:
					picks[slot["colour"]] = picked
			selections.append({"bunch_id": b["bunch_id"], "boxes": boxes, "picks": picks})
	else:
		# _approved_by_colour discards is_primary (it only returns variety
		# names), so building selections straight from it would default to
		# whichever row happens to be first in the table - not necessarily
		# the primary one. Sorted here the same way _bunch_shape sorts its
		# own candidates, so the flat branch defaults to the primary variety
		# exactly as reliably as the bunch-aware one does.
		rows_by_colour = {}
		for av in spec_doc.approved_varieties or []:
			if av.variety:
				rows_by_colour.setdefault(av.colour or "", []).append(av)
		if not rows_by_colour:
			return None, "has no Approved Varieties configured"
		box_idxs = spec_autofill._box_idxs_by_variety(spec_doc)
		selections = []
		for _colour, av_rows in rows_by_colour.items():
			candidates = sorted(av_rows, key=lambda a: 0 if a.is_primary else 1)
			chosen = next((a for a in candidates if a.variety == picked), None) if picked else None
			variety = (chosen or candidates[0]).variety
			idxs = box_idxs.get(variety) or [0]
			selections.append({"box_idx": idxs[0], "variety": variety, "boxes": boxes})

	try:
		result = spec_autofill.build_spec_rows(
			spec=spec_doc.name,
			selections=json.dumps(selections),
			next_mix_group=counters["mix_group"],
			next_bunch_group=counters["bunch_group"],
			source_warehouse=warehouse,
		)
	except frappe.exceptions.ValidationError as e:
		return None, str(e)

	rows = result.get("rows") or []
	if not rows:
		return None, "produced no lines - check its Approved Varieties"
	# Bumped only once this spec-fill actually produced rows - the next one
	# in the SAME order (another file row, another Specification) needs a
	# value neither it nor any earlier fill on this order already used.
	counters["mix_group"] += 1
	counters["bunch_group"] += 1
	return rows, None


def _row_fingerprint(item_row):
	"""A short, human-readable list of whatever else this row carries (boxes,
	length...), so an unresolved-item message points at something findable
	back in the source file even when the item code itself is the one
	piece missing - "Row 7: no item code in this row" alone just sends the
	user hunting through the whole file for an unmarked line."""
	meta = frappe.get_meta("Sales Order Item")
	shown = [
		(meta.get_label(k) or k, v) for k, v in item_row.items() if not k.startswith("_") and k != "item_code"
	]
	if not shown:
		return ""
	bits = ", ".join(f"{label}: {v}" for label, v in shown[:3])
	return f" ({bits})"


@frappe.whitelist()
def buildPreview():
	"""header: [column names in file order]. rows: [[cell, cell, ...], ...].
	Both already parsed client-side (CSV/XLSX -> plain arrays) -- this only
	resolves mapping and shapes rows, it never touches a file.

	customer is only required for a single-customer platform. A shared one
	(customer_differentiator_column set) resolves each detected order group to
	its own customer instead, by reading that column's value out of the group's
	own rows and looking it up against that platform's Customer Order Mapping
	rows - see _shared_customer_overlays. A group whose value was never mapped
	to a customer is left out of the result and reported separately, rather
	than guessed at or silently dropped without a trace."""
	try:
		data = _json_payload()
		customer = data.get("customer")
		platform = data.get("platform")
		header = data.get("header") or []
		rows = data.get("rows") or []
		if not platform:
			frappe.response["message"] = {"success": False, "error": "Platform is required"}
			return
		if not frappe.db.exists("Order Import Platform", platform):
			frappe.response["message"] = {"success": False, "error": "Platform not found"}
			return

		platform_doc = frappe.get_doc("Order Import Platform", platform)
		shared = bool(platform_doc.customer_differentiator_column)
		if not shared and not customer:
			frappe.response["message"] = {"success": False, "error": "Customer is required"}
			return

		if shared:
			overlays_by_value = _shared_customer_overlays(platform_doc)
			mapping = item_code_map = defaults = None  # resolved per group below instead
		else:
			_platform_doc, _cust_doc, mapping, item_code_map, defaults = _resolve_mapping(customer, platform)

		col_index = {name: i for i, name in enumerate(header)}
		split_col = platform_doc.multi_order_column

		def cell(row, col_name):
			i = col_index.get(col_name)
			if i is None or i >= len(row):
				return None
			v = row[i]
			return v.strip() if isinstance(v, str) else v

		def coerce_mapped_value(target_doctype, target_fieldname, raw):
			"""A file's own date cells are whatever format that platform
			exports (commonly dd/mm/yyyy, matching this system's own
			date_format) - Frappe only parses a date string locale-aware
			through the Desk form's own input widget, not when a raw value is
			simply assigned via doc.set()/insert(), so an unconverted
			"14/09/2026" sails straight through to the database, where MySQL
			rejects it outright ("Incorrect date value"). Normalized here, at
			the point a column's raw cell value is actually assigned onto a
			real target field, rather than trusting that to have already
			happened somewhere upstream."""
			df = frappe.get_meta(target_doctype).get_field(target_fieldname)
			if not df or df.fieldtype not in ("Date", "Datetime"):
				return raw
			try:
				return str(frappe.utils.getdate(raw))
			except Exception:
				return raw

		# file_row_num is the row's own position in the file (1 = the first
		# data row, matching how the user would count it in their
		# spreadsheet - "row 1 is always the first", there's no separate
		# header-row setting). Carried through grouping so an unresolved item
		# can be reported as "Row 7", not just a value with no way to find it
		# back in the source file.
		groups = []
		group_index = {}
		for file_row_num, row in enumerate(rows, start=1):
			if not any((c is not None and c != "") for c in row):
				continue
			key = cell(row, split_col) if split_col else "__all__"
			if key not in group_index:
				group_index[key] = len(groups)
				groups.append([])
			groups[group_index[key]].append((file_row_num, row))

		previews = []
		unresolved_customers = set()
		for group_rows in groups:
			if shared:
				buyer_raw = next(
					(
						v
						for v in (cell(r, platform_doc.customer_differentiator_column) for _, r in group_rows)
						if v
					),
					None,
				)
				overlay = overlays_by_value.get(buyer_raw) if buyer_raw else None
				if not overlay:
					unresolved_customers.add(buyer_raw or "(blank)")
					continue
				group_customer = overlay["customer"]
				group_mapping = overlay["mapping"]
				group_item_code_map = overlay["item_code_map"]
				group_defaults = overlay["defaults"]
			else:
				group_customer = customer
				group_mapping = mapping
				group_item_code_map = item_code_map
				group_defaults = defaults

			header_vals = {}
			item_rows = []
			unresolved_items = []
			# Fresh per order - custom_mix_group/custom_bunch_group only need to
			# be distinct WITHIN this one Sales Order, same as "current max+1 on
			# the form" means for a human doing this interactively one order at
			# a time (see _resolve_spec_row / spec_autofill.build_spec_rows).
			spec_row_counters = {"mix_group": 1, "bunch_group": 1}
			for file_row_num, row in group_rows:
				item_row = {"_source_row": file_row_num}
				for col_name, target in group_mapping.items():
					raw = cell(row, col_name)
					if (raw is None or raw == "") and target.get("default_value"):
						raw = target["default_value"]
					if raw is None or raw == "":
						continue
					raw = coerce_mapped_value(target["target_doctype"], target["target_fieldname"], raw)
					if target["target_doctype"] == "Sales Order":
						if not header_vals.get(target["target_fieldname"]):
							header_vals[target["target_fieldname"]] = raw
					else:
						item_row[target["target_fieldname"]] = raw

				# A row identified by Specification (custom_line) rather than,
				# or in addition to, a raw item code is built the same way
				# "+ Add from Spec" builds it for a human - never forced through
				# the plain item-code path below, which has no way to resolve a
				# variety from a spec name at all. See _resolve_spec_row.
				spec_name = item_row.get("custom_line")
				if spec_name:
					spec_rows, spec_err = _resolve_spec_row(item_row, group_customer, spec_row_counters)
					if spec_rows:
						for sr in spec_rows:
							sr["_source_row"] = file_row_num
							sr["_is_spec_row"] = True
						item_rows.extend(spec_rows)
					else:
						item_row["_unresolved_item"] = True
						unresolved_items.append(
							f'Row {file_row_num}: Specification "{spec_name}" {spec_err}{_row_fingerprint(item_row)}'
						)
						item_rows.append(item_row)
					continue

				raw_item = item_row.get("item_code")
				# Every "no match" case names the exact file row (never just the
				# value) plus whatever else that row carries - a bare "(blank item
				# code)"/raw code gives no way to find the offending line back in
				# the source file, especially once several rows share the problem.
				if raw_item:
					real_item = group_item_code_map.get(raw_item)
					if real_item:
						item_row["item_code"] = real_item
					elif not frappe.db.exists("Item", raw_item):
						item_row["_unresolved_item"] = True
						unresolved_items.append(
							f'Row {file_row_num}: item code "{raw_item}" not recognised{_row_fingerprint(item_row)}'
						)
				elif len(item_row) > 1:  # more than just _source_row
					item_row["_unresolved_item"] = True
					unresolved_items.append(
						f"Row {file_row_num}: no item code in this row{_row_fingerprint(item_row)}"
					)
				if len(item_row) > 1:
					_apply_spec_match(item_row, group_customer)
					item_rows.append(item_row)

			for f, v in group_defaults.items():
				target_field = _HEADER_DEFAULT_TARGETS.get(f)
				if target_field and not header_vals.get(target_field):
					header_vals[target_field] = v
			header_vals["customer"] = group_customer
			# The manual ledger backfills currency/price list from the customer
			# the moment it's picked (see onCustomerOrCurrencyChange/
			# resolvePriceList) - an imported order never goes through that,
			# so without this it silently saves in the COMPANY's currency
			# instead of the customer's, which ERPNext then refuses outright
			# ("Accounting Entry for Customer: X can only be made in
			# currency: Y") the moment the order touches accounting. Only
			# fills what the file/platform/customer-mapping defaults above
			# left blank - never overrides an explicit value.
			if not header_vals.get("currency"):
				cust_currency = frappe.db.get_value("Customer", group_customer, "default_currency")
				if cust_currency:
					header_vals["currency"] = cust_currency
			# sales_order_engine._resolve_price_list (runs on every save anyway,
			# so this import-time fill is belt-and-braces for an accurate
			# PREVIEW, not the only thing standing between the order and a
			# wrong price list) treats "Standard Selling" the same as blank -
			# ERPNext's own set_missing_values quietly defaults a brand new
			# order to it before this app's hooks ever run, so a file that
			# genuinely never carries a price list column would otherwise
			# look "already set" and skip the customer's real default.
			if header_vals.get("selling_price_list") in (None, "", "Standard Selling"):
				cust_price_list = frappe.db.get_value("Customer", group_customer, "default_price_list")
				if cust_price_list:
					header_vals["selling_price_list"] = cust_price_list
			# Sales Order's own transaction_date field defaults to "Today" when
			# nothing sets it - these platform files never carry an order-date
			# column at all (only a handover/delivery date), so a backdated
			# delivery (the file was for a shipment that already happened,
			# only now being entered) left transaction_date at today's date
			# and ERPNext core's own "Expected Delivery Date should be after
			# Sales Order Date" rejected it outright. Order date isn't a
			# concept this kind of file expresses, so the safest default that
			# can never violate that check is "today, or the delivery date
			# itself if that's earlier" - never guessed later than the order
			# can actually be delivered.
			if not header_vals.get("transaction_date"):
				today = frappe.utils.getdate()
				delivery = header_vals.get("delivery_date")
				if delivery:
					try:
						header_vals["transaction_date"] = str(min(frappe.utils.getdate(delivery), today))
					except Exception:
						header_vals["transaction_date"] = str(today)
				else:
					header_vals["transaction_date"] = str(today)

			previews.append(
				{
					"header": header_vals,
					"rows": item_rows,
					"unresolved_items": sorted(set(unresolved_items)),
					"default_warehouse": group_defaults.get("default_warehouse"),
				}
			)

		if shared:
			mapped_cols = {col for ov in overlays_by_value.values() for col in ov["mapping"]}
		else:
			mapped_cols = set(mapping.keys())

		frappe.response["message"] = {
			"success": True,
			"orders": previews,
			"unmapped_columns": [c for c in header if c not in mapped_cols],
			"unresolved_customers": sorted(unresolved_customers),
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="buildPreview error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# ------------------- quick-add missing master values -------------------
# A row that fails to save with Frappe's own "Could not find {label}: {value}"
# Link-validation error almost always names one of these few simple,
# low-risk master doctypes (the file's own text didn't match an existing
# record). Rather than send the user out to the Desk to create it and back
# to retry, the wizard offers to create it right there - restricted to this
# explicit allowlist, each mapped to how its own name is supplied (a real
# field for a field:-autonamed doctype, or the bare .name itself for a
# prompt-autonamed one).
QUICK_ADD_DOCTYPES = {
	"Consignee": "consignee",
	"Stem Length": "length",
	"Delivery Point": None,
	"Shipping Agent": None,
	"Box Type": None,
}

# Optional fields worth letting the user fill in on the spot, beyond the bare
# name - everything else on these doctypes is either a system field or a
# child table too complex for a one-line quick-add (Delivery Point's own
# Shipping Agents table, Consignee's Customers - that one is handled
# separately below since it's auto-filled from the order, not user-entered).
QUICK_ADD_EXTRA_FIELDS = {
	"Consignee": [
		{"fieldname": "country", "fieldtype": "Link", "options": "Country", "label": "Country"},
	],
	"Stem Length": [
		{"fieldname": "company", "fieldtype": "Link", "options": "Company", "label": "Company"},
		{"fieldname": "price", "fieldtype": "Float", "label": "Price"},
	],
	"Delivery Point": [
		{"fieldname": "description", "fieldtype": "Data", "label": "Description"},
		{
			"fieldname": "business_unit",
			"fieldtype": "Link",
			"options": "Business Unit",
			"label": "Business Unit",
		},
	],
	"Shipping Agent": [
		{"fieldname": "description", "fieldtype": "Data", "label": "Description"},
	],
	"Box Type": [
		{"fieldname": "box_name", "fieldtype": "Data", "label": "Box Name"},
		{"fieldname": "length_cm", "fieldtype": "Float", "label": "Length (cm)"},
		{"fieldname": "width_cm", "fieldtype": "Float", "label": "Width (cm)"},
		{"fieldname": "height_cm", "fieldtype": "Float", "label": "Height (cm)"},
		{"fieldname": "weight_kg", "fieldtype": "Float", "label": "Weight (kg)"},
	],
}


def _ensure_master_value(doctype, value, customer=None, extra=None):
	"""Creates <doctype> named/keyed <value> if it doesn't already exist.
	Shared by quickAddMasterValue (explicit user action) and
	updateSpecFromImportConflict (so pushing the file's own Stem Length back
	into a spec never fails just because that exact length was never
	entered before). customer/extra are only used on an actual create - an
	existing record is left exactly as it already is.

	Returns the resolved docname, which the caller must use in place of the
	original value from here on - a quick-add normalizes "40" to "40cm" for
	Stem Length, so a row that still says bare "40" needs to be rewritten to
	the "40cm" this function actually created, or it would be right back to
	"Could not find Stem Length: 40" on retry."""
	if doctype not in QUICK_ADD_DOCTYPES or not value:
		return value
	value = str(value).strip()
	# Every real Stem Length is named "{n}cm" - a bare number (what files and
	# the file-vs-spec conflict compare both carry) would otherwise create a
	# one-off record that breaks that convention and won't match next time.
	if doctype == "Stem Length" and value.isdigit():
		value = f"{value}cm"
	if frappe.db.exists(doctype, value):
		return value
	name_field = QUICK_ADD_DOCTYPES[doctype]
	doc = frappe.new_doc(doctype)
	if name_field:
		doc.set(name_field, value)
	else:
		doc.name = value
	for f in QUICK_ADD_EXTRA_FIELDS.get(doctype, []):
		fn = f["fieldname"]
		if extra and extra.get(fn) not in (None, ""):
			doc.set(fn, extra[fn])
	# Consignee.consignees_for_customer only ever looks at this table (see
	# populate_consignee_customers.py) - a Consignee created without its
	# own customer attached would otherwise never resolve for this customer
	# again. Delivery Point's own (single-value) customer link is filled the
	# same way, unless the user already typed one into the extra-fields form.
	if doctype == "Consignee" and customer:
		doc.append("customers", {"customer": customer})
	if doctype == "Delivery Point" and customer and not (extra or {}).get("customer"):
		doc.set("customer", customer)
	doc.insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist()
def quickAddMasterValue():
	"""doctype, value, customer (the order's own customer, for Consignee's
	Customers table / Delivery Point's Customer link), extra (optional field
	values from the quick-add form). The only record-creation this import
	flow does on its own initiative, and only for QUICK_ADD_DOCTYPES - lets a
	failed queue item's missing Consignee/Stem Length/etc. be created inline
	and retried, instead of the user leaving the wizard to add it in the
	Desk."""
	try:
		data = _json_payload()
		doctype = data.get("doctype")
		value = (data.get("value") or "").strip()
		customer = data.get("customer")
		extra = data.get("extra") or {}
		if doctype not in QUICK_ADD_DOCTYPES:
			frappe.response["message"] = {
				"success": False,
				"error": "That isn't something this wizard can create.",
			}
			return
		if not value:
			frappe.response["message"] = {"success": False, "error": "A value is required."}
			return
		name = _ensure_master_value(doctype, value, customer=customer, extra=extra)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": name}
	except Exception as e:
		frappe.clear_messages()
		frappe.db.rollback()
		frappe.log_error(title="quickAddMasterValue error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def searchMasterValue():
	"""doctype, query. The "map to existing" side of a missing-value fix -
	e.g. a file's "40" should really have been "42" (a typo, a different
	length code) rather than a brand new Stem Length. Restricted to the same
	QUICK_ADD_DOCTYPES allowlist; every one of them is named by the very
	value a user would search for, so a plain name search is enough."""
	try:
		data = _json_payload()
		doctype = data.get("doctype")
		query = (data.get("query") or "").strip()
		if doctype not in QUICK_ADD_DOCTYPES:
			frappe.response["message"] = {"success": False, "error": "Not allowed."}
			return
		filters = {"name": ["like", f"%{query}%"]} if query else {}
		names = frappe.get_all(
			doctype, filters=filters, pluck="name", order_by="name asc", limit_page_length=20
		)
		frappe.response["message"] = {"success": True, "options": names}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="searchMasterValue error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# ------------------- pushing an import's values back into a Specification -------------------
@frappe.whitelist()
def updateSpecFromImportConflict():
	"""spec_name, bunch_id, updates: {item_row_field: raw_value}. The "update
	the spec directly" side of a spec-conflict choice - edits that one
	Spec Box Item row's fields and saves the Specification exactly as a
	human editing it by hand would, so ensure_spec_uoms_and_packrates
	(Specifications' own before_validate hook) recomputes pack_rate and
	provisions any new UOM/Packrate the same way it always does."""
	try:
		data = _json_payload()
		spec_name = data.get("spec_name")
		bunch_id = data.get("bunch_id")
		updates = data.get("updates") or {}
		if not spec_name or not bunch_id:
			frappe.response["message"] = {"success": False, "error": "Specification and bunch are required."}
			return
		if not frappe.db.exists("Specifications", spec_name):
			frappe.response["message"] = {"success": False, "error": "Specification not found"}
			return
		spec_doc = frappe.get_doc("Specifications", spec_name)
		box_item = next((bi for bi in spec_doc.box_items if bi.bunch_id == bunch_id), None)
		if not box_item:
			frappe.response["message"] = {
				"success": False,
				"error": "That bunch was not found on this Specification.",
			}
			return
		field_map = dict(_SPEC_FIELD_MAP)
		for item_field, raw_val in updates.items():
			spec_field = field_map.get(item_field)
			if not spec_field or raw_val in (None, ""):
				continue
			if spec_field == "length":
				raw_val = _ensure_master_value("Stem Length", raw_val)
			box_item.set(spec_field, raw_val)
		spec_doc.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()
		frappe.db.rollback()
		frappe.log_error(title="updateSpecFromImportConflict error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}
