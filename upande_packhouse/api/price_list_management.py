# Backend for the two new "Reference" pages: price-lists (the gap tree) and
# new-customer-price-list (create-a-list-then-fill-it-in). Both pages share
# this one module rather than duplicating tree logic.
#
# Variety/length scope deliberately mirrors variety_tree.py's own Cut Flowers
# traversal and the May-price-list convention's 6 lengths -- same taxonomy,
# same lengths, so "which varieties/lengths have no price" means the same
# thing here as it does everywhere else this session.
#
# Pricing mechanism is the same one sales_order_engine.sales_order_price
# actually reads: Item Price with the custom_length Link field (see
# upande_packhouse/overrides/item_price.py for the duplicate-check override
# that makes two Item Price rows differing only by custom_length legal) --
# nothing new invented here.
import frappe

LENGTHS = ["42cm", "52cm", "62cm", "72cm", "82cm", "92cm"]


@frappe.whitelist()
def getPriceLists():
    """Every enabled selling Price List, for the page's dropdown."""
    rows = frappe.get_all(
        "Price List",
        filters={"enabled": 1, "selling": 1},
        fields=["name", "currency"],
        order_by="name",
    )
    frappe.response["message"] = {"success": True, "price_lists": rows}


@frappe.whitelist()
def getPriceTree(price_list):
    """Same Line -> Category -> Item shape as variety_tree.getVarietyTree,
    but each item carries a `lengths` dict (one of the 6 lengths -> rate or
    None) for the given price_list instead of demand/production stats.
    """
    root = "Cut Flowers"
    try:
        if not price_list or not frappe.db.exists("Price List", price_list):
            frappe.response["message"] = {"success": False, "error": "Unknown price list: " + str(price_list)}
            return
        if not frappe.db.exists("Item Group", root):
            frappe.response["message"] = {"success": True, "root": root, "lines": [], "lengths": LENGTHS}
            return

        lines = frappe.get_all("Item Group", filters={"parent_item_group": root}, fields=["name"], order_by="name")
        line_names = [ln.name for ln in lines if ln.name != "Cut Flowers - Legacy"]

        cats_by_line = {}
        if line_names:
            for c in frappe.get_all("Item Group", filters={"parent_item_group": ["in", line_names]},
                                     fields=["name", "parent_item_group"], order_by="name"):
                cats_by_line.setdefault(c.parent_item_group, []).append(c.name)

        group_line = {}
        category_groups = []
        for ln in line_names:
            for cg in (cats_by_line.get(ln) or [ln]):
                category_groups.append(cg)
                group_line[cg] = ln

        items_by_group = {}
        all_item_codes = []
        if category_groups:
            for it in frappe.get_all(
                    "Item", filters={"item_group": ["in", category_groups]},
                    fields=["name", "item_name", "item_group", "disabled"],
                    order_by="item_name"):
                items_by_group.setdefault(it.item_group, []).append(it)
                all_item_codes.append(it.name)

        # ---- ALL Item Price rows for ALL these items, ONE query ----
        rates_by_item = {}
        if all_item_codes:
            for r in frappe.get_all(
                    "Item Price",
                    filters={"item_code": ["in", all_item_codes], "price_list": price_list,
                             "selling": 1, "custom_length": ["in", LENGTHS]},
                    fields=["item_code", "custom_length", "price_list_rate"]):
                rates_by_item.setdefault(r.item_code, {})[r.custom_length] = r.price_list_rate

        priced_cells = 0
        total_cells = 0

        out = []
        for ln in line_names:
            ocats = []
            for cname in (cats_by_line.get(ln) or [ln]):
                items = items_by_group.get(cname)
                if not items:
                    continue
                disp = cname[len(ln) + 3:] if cname.startswith(ln + " - ") else cname
                oitems = []
                for it in items:
                    rates = rates_by_item.get(it.name, {})
                    lengths = {}
                    for length in LENGTHS:
                        rate = rates.get(length)
                        lengths[length] = rate
                        total_cells += 1
                        if rate is not None:
                            priced_cells += 1
                    oitems.append({
                        "n": it.item_name or it.name,
                        "code": it.name,
                        "s": "Inactive" if it.disabled else "Active",
                        "lengths": lengths,
                    })
                ocats.append({"name": disp, "group": cname, "items": oitems})
            if ocats:
                out.append({"name": ln, "cats": ocats})

        pl = frappe.db.get_value("Price List", price_list, "currency")
        frappe.response["message"] = {
            "success": True, "root": root, "lines": out, "lengths": LENGTHS,
            "currency": pl, "priced_cells": priced_cells, "total_cells": total_cells,
        }
    except Exception as e:
        frappe.log_error("getPriceTree error: " + str(e))
        frappe.response["message"] = {"success": False, "error": str(e), "lines": []}


@frappe.whitelist()
def setItemPrice(item_code, price_list, length, rate=None):
    """Upsert (or clear, if rate is blank) the Item Price for
    (item_code, price_list, length, selling=1). Same shape upload_may_pricelist
    used -- find-or-create via frappe.get_doc()/insert(), never raw SQL,
    since CustomItemPrice's duplicate-check override only runs through the
    normal document controller.
    """
    try:
        if not frappe.db.exists("Item", item_code):
            return _err("Unknown item: " + str(item_code))
        if not frappe.db.exists("Price List", price_list):
            return _err("Unknown price list: " + str(price_list))
        if length not in LENGTHS:
            return _err("Unknown length: " + str(length))

        filters = {"item_code": item_code, "price_list": price_list, "custom_length": length, "selling": 1}
        existing = frappe.db.get_value("Item Price", filters, ["name"], as_dict=True)

        rate = (str(rate).strip() if rate not in (None, "") else "")
        if not rate:
            # Blank rate = clear the price entirely, not "set to 0".
            if existing:
                frappe.delete_doc("Item Price", existing.name, ignore_permissions=True)
                frappe.db.commit()
            frappe.response["message"] = {"success": True, "cleared": True}
            return

        rate = float(rate)
        if rate < 0:
            return _err("Rate can't be negative")

        if existing:
            frappe.db.set_value("Item Price", existing.name, "price_list_rate", rate)
        else:
            currency = frappe.db.get_value("Price List", price_list, "currency")
            doc = frappe.get_doc({
                "doctype": "Item Price",
                "item_code": item_code,
                "price_list": price_list,
                "currency": currency,
                "custom_length": length,
                "uom": "Stems",
                "selling": 1,
                "price_list_rate": rate,
            })
            doc.insert(ignore_permissions=True)
        frappe.db.commit()
        frappe.response["message"] = {"success": True, "rate": rate}
    except Exception as e:
        frappe.log_error("setItemPrice error: " + str(e))
        frappe.response["message"] = {"success": False, "error": str(e)}


def _err(msg):
    frappe.response["message"] = {"success": False, "error": msg}


@frappe.whitelist()
def createCustomerPriceList(customer, prefix, currency):
    """Create a new selling Price List named "<prefix> <Customer Name>" and
    set it as that Customer's default_price_list (the only real,
    functionally-meaningful way to "link" a Price List to a Customer --
    Price List itself has no such field; ERPNext resolves an order's price
    list from Customer.default_price_list, so this is what actually makes
    the new list active for them). Returns the created name so the caller
    can immediately load its (empty) price grid.
    """
    try:
        if not customer or not frappe.db.exists("Customer", customer):
            return _err("Unknown customer: " + str(customer))
        prefix = (prefix or "").strip()
        if not prefix:
            return _err("Prefix is required")
        if not currency or not frappe.db.exists("Currency", currency):
            return _err("Unknown currency: " + str(currency))

        name = "{0} {1}".format(prefix, customer)
        if frappe.db.exists("Price List", name):
            return _err("A Price List named " + repr(name) + " already exists")

        doc = frappe.get_doc({
            "doctype": "Price List",
            "price_list_name": name,
            "currency": currency,
            "selling": 1,
            "buying": 0,
            "enabled": 1,
        })
        doc.insert(ignore_permissions=True)
        frappe.db.set_value("Customer", customer, "default_price_list", doc.name)
        frappe.db.commit()
        frappe.response["message"] = {"success": True, "price_list": doc.name}
    except Exception as e:
        frappe.log_error("createCustomerPriceList error: " + str(e))
        frappe.response["message"] = {"success": False, "error": str(e)}


