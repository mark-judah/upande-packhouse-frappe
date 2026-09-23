# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Backend for the web Sales Settings page (www/sales-settings.html):
# currency enablement (Frappe's real Currency master -- never a hardcoded
# list) and the Customer x Currency -> Price List default mapping that
# Customer.default_price_list can't express on its own (that field holds
# exactly one price list, with no currency axis).

import frappe


def _json_payload():
	"""See specifications.py's _json_payload for why this exists instead of
	frappe.request.get_json(): frappe.call() form-encodes its request body
	(it's jQuery.ajax under the hood), so a complex arg only ever arrives as
	a JSON-stringified `data` form field, never as a raw JSON request body."""
	raw = frappe.form_dict.get("data")
	if raw is None:
		return {}
	if isinstance(raw, str):
		return frappe.parse_json(raw) or {}
	return raw


# Every ISO currency ships as a Currency record already; most start
# disabled. These three are switched on the first time anyone opens Sales
# Settings, purely so the page isn't empty on a fresh site -- nothing stops
# the user disabling them again or enabling any other real Currency record.
DEFAULT_CURRENCIES = ["EUR", "USD", "GBP"]


def _ensure_default_currencies():
	for code in DEFAULT_CURRENCIES:
		if not frappe.db.exists("Currency", code):
			frappe.get_doc({"doctype": "Currency", "currency_name": code, "enabled": 1}).insert(
				ignore_permissions=True
			)
		elif not frappe.db.get_value("Currency", code, "enabled"):
			frappe.db.set_value("Currency", code, "enabled", 1)


@frappe.whitelist()
def listCurrencies():
	try:
		_ensure_default_currencies()
		rows = frappe.get_all(
			"Currency",
			fields=["name", "enabled", "symbol", "fraction"],
			order_by="enabled desc, name asc",
		)
		frappe.response["message"] = {"success": True, "currencies": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listCurrencies error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "currencies": []}


@frappe.whitelist()
def setCurrencyEnabled():
	try:
		data = _json_payload()
		currency = data.get("currency")
		enabled = 1 if data.get("enabled") else 0
		if not currency or not frappe.db.exists("Currency", currency):
			frappe.response["message"] = {"success": False, "error": "Currency not found"}
			return
		frappe.db.set_value("Currency", currency, "enabled", enabled)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="setCurrencyEnabled error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def searchPriceLists():
	try:
		query = frappe.form_dict.get("query") or ""
		rows = frappe.get_all(
			"Price List",
			filters={"price_list_name": ["like", "%" + query + "%"], "enabled": 1, "selling": 1},
			fields=["name", "currency"],
			order_by="name asc",
			limit_page_length=20,
		)
		frappe.response["message"] = {"success": True, "price_lists": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="searchPriceLists error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "price_lists": []}


@frappe.whitelist()
def listPriceLists():
	"""Every Selling Price List, enabled or not -- unlike searchPriceLists
	(which only returns enabled=1 ones, for the Link-field pickers in Desk
	and the dashboard), this is Sales Settings' own management view: the
	one place an enabled/disabled toggle actually gets exposed at all."""
	try:
		rows = frappe.get_all(
			"Price List",
			filters={"selling": 1},
			fields=["name", "currency", "enabled"],
			order_by="enabled desc, name asc",
		)
		frappe.response["message"] = {"success": True, "price_lists": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listPriceLists error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "price_lists": []}


@frappe.whitelist()
def setPriceListEnabled():
	try:
		data = _json_payload()
		price_list = data.get("price_list")
		enabled = 1 if data.get("enabled") else 0
		if not price_list or not frappe.db.exists("Price List", price_list):
			frappe.response["message"] = {"success": False, "error": "Price List not found"}
			return
		frappe.db.set_value("Price List", price_list, "enabled", enabled)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="setPriceListEnabled error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def createPriceList():
	"""Quick-add: a Selling Price List in a given currency, created inline
	from wherever a save/preview first discovers one is missing."""
	try:
		data = _json_payload()
		price_list_name = (data.get("price_list_name") or "").strip()
		currency = data.get("currency")
		if not price_list_name or not currency:
			frappe.response["message"] = {
				"success": False,
				"error": "Price List name and currency are required",
			}
			return
		if frappe.db.exists("Price List", price_list_name):
			frappe.response["message"] = {
				"success": False,
				"error": 'Price List "%s" already exists' % price_list_name,
			}
			return
		doc = frappe.new_doc("Price List")
		doc.price_list_name = price_list_name
		doc.currency = currency
		doc.enabled = 1
		doc.selling = 1
		doc.insert()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": doc.name}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="createPriceList error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def listCustomerPriceListDefaults():
	try:
		rows = frappe.get_all(
			"Customer Price List Default",
			fields=["name", "customer", "currency", "price_list", "disabled"],
			order_by="customer asc, currency asc",
		)
		frappe.response["message"] = {"success": True, "defaults": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listCustomerPriceListDefaults error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "defaults": []}


@frappe.whitelist()
def saveCustomerPriceListDefault():
	try:
		data = _json_payload()
		name = data.get("name")
		if name and frappe.db.exists("Customer Price List Default", name):
			doc = frappe.get_doc("Customer Price List Default", name)
		else:
			doc = frappe.new_doc("Customer Price List Default")
		for f in ("customer", "currency", "price_list"):
			if f in data:
				doc.set(f, data.get(f))
		doc.disabled = 1 if data.get("disabled") else 0
		if doc.is_new():
			doc.insert()
		else:
			doc.save()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": doc.name}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="saveCustomerPriceListDefault error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def deleteCustomerPriceListDefault():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Customer Price List Default", name):
			frappe.response["message"] = {"success": False, "error": "Not found"}
			return
		frappe.delete_doc("Customer Price List Default", name, ignore_permissions=False)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="deleteCustomerPriceListDefault error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


# ----------------------------- default warehouse -----------------------------


@frappe.whitelist()
def getSalesSettings():
	try:
		default_warehouse = frappe.db.get_single_value("Sales Settings", "default_warehouse")
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"is_group": 0, "disabled": 0, "company": "Karen Roses"},
			pluck="name",
			order_by="name asc",
		)
		frappe.response["message"] = {
			"success": True,
			"default_warehouse": default_warehouse,
			"warehouses": warehouses,
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getSalesSettings error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def setDefaultWarehouse():
	try:
		data = _json_payload()
		warehouse = data.get("default_warehouse") or None
		if warehouse and not frappe.db.exists("Warehouse", warehouse):
			frappe.response["message"] = {"success": False, "error": "Warehouse not found"}
			return
		doc = frappe.get_single("Sales Settings")
		doc.default_warehouse = warehouse
		doc.save()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="setDefaultWarehouse error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}
