# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Backend for the web Sales Order ledger (www/sales-order.html) -- CRUD over
# the real Sales Order doctype for the Roses business unit.
#
# This deliberately does NOT reimplement pricing, box-math, or validation:
# saveSalesOrder builds a real `Sales Order` doc and calls .insert()/.save(),
# which fires upande_packhouse.sales_order_engine's before_validate/validate
# hooks (price-list resolution, length-aware pricing, packrate/UOM/colour-
# limit checks) exactly as the Desk form would. "Add from Spec" rows are
# built by calling the real upande_packhouse.spec_autofill.get_spec_fill_data
# / build_spec_rows from the client directly -- this module only shapes the
# MANUAL (non-spec) rows, which the engine can't build on its own.

import frappe

from upande_packhouse.api.specifications import _cut_flower_item_groups
from upande_packhouse.spec import _ensure_bunch_uom, _ensure_packrate


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


@frappe.whitelist()
def getSalesOrderOptions():
	try:
		stem_lengths = frappe.get_all("Stem Length", pluck="name", order_by="name asc")
		box_types = frappe.get_all("Box Type", pluck="name", order_by="name asc")
		warehouses = frappe.get_all(
			"Warehouse", filters={"is_group": 0, "disabled": 0}, pluck="name", order_by="name asc"
		)
		currencies = frappe.get_all(
			"Currency", filters={"enabled": 1}, pluck="name", order_by="name asc"
		)
		frappe.response["message"] = {
			"success": True,
			"stem_lengths": stem_lengths,
			"box_types": box_types,
			"warehouses": warehouses,
			"currencies": currencies,
		}
	except Exception as e:
		frappe.log_error("getSalesOrderOptions error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def searchOrderVarieties():
	"""Same Cut Flowers scope as the Specifications page's variety picker,
	plus sales_uom -- sales_order_engine.sales_order_validate blocks the
	save of any manually-added line whose item has none, so the picker
	surfaces it immediately instead of at save time."""
	try:
		query = frappe.form_dict.get("query") or ""
		rows = frappe.get_all(
			"Item",
			filters={
				"name": ["like", "%" + query + "%"],
				"disabled": 0,
				"item_group": ["in", _cut_flower_item_groups()],
			},
			fields=["name", "item_group", "custom_color", "sales_uom"],
			order_by="name asc",
			limit_page_length=20,
		)
		frappe.response["message"] = {"success": True, "varieties": rows}
	except Exception as e:
		frappe.log_error("searchOrderVarieties error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "varieties": []}


@frappe.whitelist()
def searchConsignees():
	"""Scoped to consignees linked to the given customer (via the Consignee
	Customer child table) when one is provided; unscoped otherwise -- a
	consignee not yet linked to any customer still needs to be findable."""
	try:
		query = frappe.form_dict.get("query") or ""
		customer = frappe.form_dict.get("customer") or ""
		if customer:
			names = frappe.get_all(
				"Consignee Customer", filters={"customer": customer}, pluck="parent"
			)
			if names:
				rows = frappe.get_all(
					"Consignee",
					filters={"name": ["in", names], "consignee": ["like", "%" + query + "%"]},
					fields=["name", "consignee"],
					order_by="consignee asc",
					limit_page_length=20,
				)
				frappe.response["message"] = {"success": True, "consignees": rows}
				return
		rows = frappe.get_all(
			"Consignee",
			filters={"consignee": ["like", "%" + query + "%"]},
			fields=["name", "consignee"],
			order_by="consignee asc",
			limit_page_length=20,
		)
		frappe.response["message"] = {"success": True, "consignees": rows}
	except Exception as e:
		frappe.log_error("searchConsignees error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "consignees": []}


@frappe.whitelist()
def searchDeliveryPoints():
	try:
		query = frappe.form_dict.get("query") or ""
		customer = frappe.form_dict.get("customer") or ""
		filters = {"name": ["like", "%" + query + "%"]}
		rows = []
		if customer:
			rows = frappe.get_all(
				"Delivery Point",
				filters={**filters, "customer": customer},
				fields=["name", "description"],
				order_by="name asc",
				limit_page_length=20,
			)
		if not rows:
			rows = frappe.get_all(
				"Delivery Point",
				filters=filters,
				fields=["name", "description"],
				order_by="name asc",
				limit_page_length=20,
			)
		frappe.response["message"] = {"success": True, "delivery_points": rows}
	except Exception as e:
		frappe.log_error("searchDeliveryPoints error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "delivery_points": []}


@frappe.whitelist()
def searchShippingAgents():
	try:
		query = frappe.form_dict.get("query") or ""
		rows = frappe.get_all(
			"Shipping Agent",
			filters={"name": ["like", "%" + query + "%"]},
			fields=["name", "description"],
			order_by="name asc",
			limit_page_length=20,
		)
		frappe.response["message"] = {"success": True, "shipping_agents": rows}
	except Exception as e:
		frappe.log_error("searchShippingAgents error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "shipping_agents": []}


@frappe.whitelist()
def resolvePriceList():
	"""Customer + currency -> price list, for pre-filling the header the
	moment both are picked. Customer Price List Default (this app's own
	mapping, see api/sales_settings.py) wins when it has an entry for this
	exact pair; Customer.default_price_list (no currency axis) is the
	fallback -- same fallback sales_order_engine._resolve_price_list uses,
	so this only ever pre-fills a value the real engine would also accept."""
	try:
		customer = frappe.form_dict.get("customer")
		currency = frappe.form_dict.get("currency")
		price_list = None
		if customer and currency:
			price_list = frappe.db.get_value(
				"Customer Price List Default",
				{"customer": customer, "currency": currency, "disabled": 0},
				"price_list",
			)
		if not price_list and customer:
			price_list = frappe.db.get_value("Customer", customer, "default_price_list")
		frappe.response["message"] = {"success": True, "price_list": price_list}
	except Exception as e:
		frappe.log_error("resolvePriceList error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "price_list": None}


@frappe.whitelist()
def previewRate():
	"""Read-only mirror of sales_order_engine.sales_order_price's Item Price
	lookup, for the ledger's live total before save -- never the value that
	actually gets persisted (the real hook recomputes it authoritatively on
	save, independent of whatever this returned)."""
	try:
		item_code = frappe.form_dict.get("item_code")
		price_list = frappe.form_dict.get("price_list")
		length = frappe.form_dict.get("length")
		if not (item_code and price_list and length):
			frappe.response["message"] = {"success": True, "rate": 0}
			return
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": price_list, "custom_length": length, "selling": 1},
			"price_list_rate",
		)
		frappe.response["message"] = {"success": True, "rate": float(rate or 0)}
	except Exception as e:
		frappe.log_error("previewRate error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "rate": 0}


@frappe.whitelist()
def setItemSalesUom():
	"""Quick-add for sales_order_validate's 'No Sales UOM set on: X' gate."""
	try:
		data = _json_payload()
		item_code = data.get("item_code")
		uom = data.get("uom")
		if not item_code or not uom:
			frappe.response["message"] = {"success": False, "error": "Item and UOM are required"}
			return
		if not frappe.db.exists("Item", item_code):
			frappe.response["message"] = {"success": False, "error": "Item not found"}
			return
		frappe.db.set_value("Item", item_code, "sales_uom", uom)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("setItemSalesUom error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def createItemPrice():
	"""Quick-add for sales_order_validate's 'No price found for: X' gate --
	same (item_code, price_list, custom_length, selling=1) shape sales_order_price
	reads back, so a save retried right after this succeeds."""
	try:
		data = _json_payload()
		item_code = data.get("item_code")
		price_list = data.get("price_list")
		length = data.get("length")
		rate = data.get("rate")
		if not (item_code and price_list and length and rate):
			frappe.response["message"] = {
				"success": False,
				"error": "Item, Price List, Length and Rate are all required",
			}
			return
		currency = frappe.db.get_value("Price List", price_list, "currency")
		doc = frappe.new_doc("Item Price")
		doc.item_code = item_code
		doc.price_list = price_list
		doc.custom_length = length
		doc.selling = 1
		doc.currency = currency
		doc.price_list_rate = rate
		doc.uom = "Stems"
		doc.insert()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": doc.name}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("createItemPrice error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def listSalesOrders():
	try:
		query = frappe.form_dict.get("query") or ""
		customer = frappe.form_dict.get("customer") or ""
		filters = {"business_unit": "Roses"}
		if query:
			filters["name"] = ["like", "%" + query + "%"]
		if customer:
			filters["customer"] = customer
		rows = frappe.get_all(
			"Sales Order",
			filters=filters,
			fields=[
				"name", "customer", "transaction_date", "delivery_date",
				"currency", "status", "docstatus", "grand_total",
				"custom_total_boxes", "custom_total_stems", "modified",
			],
			order_by="modified desc",
			limit_page_length=200,
		)
		frappe.response["message"] = {"success": True, "orders": rows}
	except Exception as e:
		frappe.log_error("listSalesOrders error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "orders": []}


def _row_out(it):
	return {
		"name": it.name,
		"item_code": it.item_code,
		"item_name": it.item_name,
		"qty": it.qty,
		"uom": it.uom,
		"rate": it.rate,
		"amount": it.amount,
		"warehouse": it.warehouse,
		"custom_length": it.get("custom_length"),
		"custom_box_type": it.get("custom_box_type"),
		"custom_number_of_boxes": it.get("custom_number_of_boxes"),
		"custom_packrate": it.get("custom_packrate"),
		"custom_packrate_mixed_box": it.get("custom_packrate_mixed_box"),
		"custom_mixed_box": it.get("custom_mixed_box"),
		"custom_mix_group": it.get("custom_mix_group"),
		"custom_mix_name": it.get("custom_mix_name"),
		"custom_mixed_bunch": it.get("custom_mixed_bunch"),
		"custom_bunch_group": it.get("custom_bunch_group"),
		"custom_line": it.get("custom_line"),
		"custom_cut_stage": it.get("custom_cut_stage"),
		"custom_ordered_quantity": it.get("custom_ordered_quantity"),
	}


@frappe.whitelist()
def getSalesOrder():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Sales Order", name):
			frappe.response["message"] = {"success": False, "error": "Sales Order not found"}
			return
		doc = frappe.get_doc("Sales Order", name)
		frappe.response["message"] = {
			"success": True,
			"order": {
				"name": doc.name,
				"docstatus": doc.docstatus,
				"status": doc.status,
				"customer": doc.customer,
				"transaction_date": str(doc.transaction_date or ""),
				"delivery_date": str(doc.delivery_date or ""),
				"currency": doc.currency,
				"selling_price_list": doc.selling_price_list,
				"custom_consignee": doc.get("custom_consignee"),
				"custom_delivery_point": doc.get("custom_delivery_point"),
				"custom_shipping_agent": doc.get("custom_shipping_agent"),
				"custom_s_number": doc.get("custom_s_number"),
				"custom_truck_details": doc.get("custom_truck_details"),
				"custom_total_boxes": doc.get("custom_total_boxes"),
				"custom_total_stems": doc.get("custom_total_stems"),
				"grand_total": doc.grand_total,
				"items": [_row_out(it) for it in doc.items],
			},
		}
	except Exception as e:
		frappe.log_error("getSalesOrder error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


def _shape_manual_row(r, default_warehouse):
	"""A manual (non-spec) row from the client's own card UI -> a Sales
	Order Item dict. Mirrors spec_autofill.build_spec_rows's row shape
	exactly (same fieldset, same custom_packrate vs custom_packrate_mixed_box
	split) so manual and spec-derived rows behave identically once saved --
	qty/stock_qty/conversion_factor/rate are deliberately NOT set here, the
	real before_validate/validate hooks (sales_order_engine.py) recompute
	those authoritatively from custom_packrate(_mixed_box) x
	custom_number_of_boxes on save."""
	stems_per_bunch = int(r.get("stems_per_bunch") or 0)
	bunches_per_box = int(r.get("bunches_per_box") or 0)
	boxes = int(r.get("boxes") or 0)
	stems_per_box = stems_per_bunch * bunches_per_box
	uom = _ensure_bunch_uom(stems_per_bunch) or ""

	mixed_box = 1 if r.get("mixed_box") else 0
	mixed_bunch = 1 if r.get("mixed_bunch") else 0

	row = {
		"item_code": r.get("item_code"),
		"uom": uom,
		"custom_length": r.get("length"),
		"custom_box_type": r.get("box_type"),
		"custom_number_of_boxes": boxes,
		"custom_mixed_box": mixed_box,
		"custom_mix_group": r.get("mix_group") if mixed_box else "",
		"custom_mixed_bunch": mixed_bunch,
		"custom_bunch_group": r.get("bunch_group") if mixed_bunch else "",
		"warehouse": r.get("warehouse") or default_warehouse,
	}
	if mixed_box or mixed_bunch:
		row["custom_packrate_mixed_box"] = stems_per_box
	else:
		pr = _ensure_packrate(stems_per_box)
		if pr:
			row["custom_packrate"] = pr
	return row


@frappe.whitelist()
def saveSalesOrder():
	try:
		data = _json_payload()
		name = data.get("name")

		if name and frappe.db.exists("Sales Order", name):
			doc = frappe.get_doc("Sales Order", name)
			if doc.docstatus != 0:
				frappe.response["message"] = {"success": False, "error": "Only a draft order can be edited here."}
				return
		else:
			doc = frappe.new_doc("Sales Order")
			doc.business_unit = "Roses"

		header_fields = [
			"customer", "transaction_date", "delivery_date", "currency", "selling_price_list",
			"custom_consignee", "custom_delivery_point", "custom_shipping_agent",
			"custom_s_number", "custom_truck_details",
		]
		for f in header_fields:
			if f in data:
				doc.set(f, data.get(f))

		default_warehouse = data.get("default_warehouse")

		doc.set("items", [])
		for r in data.get("spec_rows") or []:
			# Already fully shaped by upande_packhouse.spec_autofill.build_spec_rows
			# on the client -- appended as-is.
			doc.append("items", r)
		for r in data.get("manual_rows") or []:
			doc.append("items", _shape_manual_row(r, default_warehouse))

		if doc.is_new():
			doc.insert()
		else:
			doc.save()

		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": doc.name}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("saveSalesOrder error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def submitSalesOrder():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Sales Order", name):
			frappe.response["message"] = {"success": False, "error": "Sales Order not found"}
			return
		doc = frappe.get_doc("Sales Order", name)
		doc.submit()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("submitSalesOrder error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def deleteSalesOrder():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Sales Order", name):
			frappe.response["message"] = {"success": False, "error": "Sales Order not found"}
			return
		if frappe.db.get_value("Sales Order", name, "docstatus") != 0:
			frappe.response["message"] = {"success": False, "error": "Only a draft order can be deleted here."}
			return
		frappe.delete_doc("Sales Order", name, ignore_permissions=False)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("deleteSalesOrder error: " + str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}
