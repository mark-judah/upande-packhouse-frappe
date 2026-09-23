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

import frappe

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
	"Section Break", "Column Break", "Tab Break", "HTML", "Button",
	"Table", "Table MultiSelect", "Fold", "Heading", "Image",
}
IGNORED_FIELDNAMES = {
	"name", "owner", "creation", "modified", "modified_by", "docstatus",
	"idx", "parent", "parentfield", "parenttype", "naming_series",
}
# Recomputed by sales_order_engine on save -- mappable (nothing stops it,
# per "map to any field"), but flagged in the catalog so the UI can warn
# that it's usually not necessary.
ENGINE_COMPUTED_FIELDS = {
	"qty", "stock_qty", "conversion_factor", "rate", "price_list_rate", "amount",
	"custom_ordered_quantity", "custom_packrate", "custom_packrate_mixed_box", "uom",
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
			doc.append("default_field_mappings", {
				"source_column": m.get("source_column"),
				"target_doctype": m.get("target_doctype"),
				"target_fieldname": m.get("target_fieldname"),
				"default_value": m.get("default_value"),
			})
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
					{"source_item_code": m.source_item_code, "item": m.item}
					for m in doc.item_code_mapping
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
			"customer", "platform", "default_currency", "default_price_list",
			"default_warehouse", "default_consignee", "default_delivery_point",
			"default_shipping_agent",
		):
			if f in data:
				doc.set(f, data.get(f))
		doc.set("field_mapping_overrides", [])
		for m in data.get("field_mapping_overrides") or []:
			doc.append("field_mapping_overrides", {
				"source_column": m.get("source_column"),
				"target_doctype": m.get("target_doctype"),
				"target_fieldname": m.get("target_fieldname"),
				"default_value": m.get("default_value"),
			})
		doc.set("item_code_mapping", [])
		for m in data.get("item_code_mapping") or []:
			doc.append("item_code_mapping", {
				"source_item_code": m.get("source_item_code"),
				"item": m.get("item"),
			})
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


# ----------------------------- preview build -----------------------------


def _resolve_mapping(customer, platform):
	platform_doc = frappe.get_doc("Order Import Platform", platform)
	mapping = {}
	for m in platform_doc.default_field_mappings:
		mapping[m.source_column] = {
			"target_doctype": m.target_doctype,
			"target_fieldname": m.target_fieldname,
			"default_value": m.default_value,
		}

	cust_name = frappe.db.get_value(
		"Customer Order Mapping", {"customer": customer, "platform": platform}
	)
	cust_doc = frappe.get_doc("Customer Order Mapping", cust_name) if cust_name else None
	if cust_doc:
		for m in cust_doc.field_mapping_overrides:
			mapping[m.source_column] = {
				"target_doctype": m.target_doctype,
				"target_fieldname": m.target_fieldname,
				"default_value": m.default_value,
			}

	item_code_map = {}
	defaults = {}
	if cust_doc:
		for im in cust_doc.item_code_mapping:
			item_code_map[im.source_item_code] = im.item
		for f in (
			"default_currency", "default_price_list", "default_warehouse",
			"default_consignee", "default_delivery_point", "default_shipping_agent",
		):
			v = cust_doc.get(f)
			if v:
				defaults[f] = v

	return platform_doc, cust_doc, mapping, item_code_map, defaults


_HEADER_DEFAULT_TARGETS = {
	"default_currency": "currency",
	"default_price_list": "selling_price_list",
	"default_consignee": "custom_consignee",
	"default_delivery_point": "custom_delivery_point",
	"default_shipping_agent": "custom_shipping_agent",
}


@frappe.whitelist()
def buildPreview():
	"""header: [column names in file order]. rows: [[cell, cell, ...], ...].
	Both already parsed client-side (CSV/XLSX -> plain arrays) -- this only
	resolves mapping and shapes rows, it never touches a file."""
	try:
		data = _json_payload()
		customer = data.get("customer")
		platform = data.get("platform")
		header = data.get("header") or []
		rows = data.get("rows") or []
		if not customer or not platform:
			frappe.response["message"] = {"success": False, "error": "Customer and Platform are required"}
			return
		if not frappe.db.exists("Order Import Platform", platform):
			frappe.response["message"] = {"success": False, "error": "Platform not found"}
			return

		platform_doc, cust_doc, mapping, item_code_map, defaults = _resolve_mapping(customer, platform)
		col_index = {name: i for i, name in enumerate(header)}
		split_col = platform_doc.multi_order_column

		def cell(row, col_name):
			i = col_index.get(col_name)
			if i is None or i >= len(row):
				return None
			v = row[i]
			return v.strip() if isinstance(v, str) else v

		groups = []
		group_index = {}
		for row in rows:
			if not any((c is not None and c != "") for c in row):
				continue
			key = cell(row, split_col) if split_col else "__all__"
			if key not in group_index:
				group_index[key] = len(groups)
				groups.append([])
			groups[group_index[key]].append(row)

		previews = []
		for group_rows in groups:
			header_vals = {}
			item_rows = []
			unresolved_items = []
			for row in group_rows:
				item_row = {}
				for col_name, target in mapping.items():
					raw = cell(row, col_name)
					if (raw is None or raw == "") and target.get("default_value"):
						raw = target["default_value"]
					if raw is None or raw == "":
						continue
					if target["target_doctype"] == "Sales Order":
						if not header_vals.get(target["target_fieldname"]):
							header_vals[target["target_fieldname"]] = raw
					else:
						item_row[target["target_fieldname"]] = raw

				raw_item = item_row.get("item_code")
				if raw_item:
					real_item = item_code_map.get(raw_item)
					if real_item:
						item_row["item_code"] = real_item
					elif not frappe.db.exists("Item", raw_item):
						item_row["_unresolved_item"] = True
						unresolved_items.append(raw_item)
				if item_row:
					item_rows.append(item_row)

			for f, v in defaults.items():
				target_field = _HEADER_DEFAULT_TARGETS.get(f)
				if target_field and not header_vals.get(target_field):
					header_vals[target_field] = v
			header_vals["customer"] = customer

			previews.append({
				"header": header_vals,
				"rows": item_rows,
				"unresolved_items": sorted(set(unresolved_items)),
				"default_warehouse": defaults.get("default_warehouse"),
			})

		frappe.response["message"] = {
			"success": True,
			"orders": previews,
			"unmapped_columns": [c for c in header if c not in mapping],
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="buildPreview error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}
