# Live Cut Flowers taxonomy for the variety-tree page — reads the real
# Item Group tree + Items so the page reflects adds/removes automatically.
import frappe


@frappe.whitelist()
def getVarietyTree():
    root = "Cut Flowers"
    try:
        if not frappe.db.exists("Item Group", root):
            frappe.response["message"] = {"success": True, "lines": [], "root": root}
            return

        colours = {c.name: c.color for c in frappe.get_all("Color", fields=["name", "color"])}

        # per-variety averages over the last 8 weeks
        WKS = 8
        start = frappe.utils.add_days(frappe.utils.nowdate(), -7 * WKS)
        demand = {r["item_code"]: (r["q"] or 0) / WKS for r in frappe.db.sql("""
            SELECT soi.item_code, SUM(soi.stock_qty) q
            FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
            WHERE so.docstatus = 1 AND so.transaction_date >= %(s)s
            GROUP BY soi.item_code""", {"s": start}, as_dict=True)}
        prod = {r["item_code"]: (r["q"] or 0) / WKS for r in frappe.db.sql("""
            SELECT sed.item_code, SUM(sed.qty) q
            FROM `tabStock Entry Detail` sed JOIN `tabStock Entry` se ON se.name = sed.parent
            WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus = 1 AND se.posting_date >= %(s)s
            GROUP BY sed.item_code""", {"s": start}, as_dict=True)}

        # ── Item Group tree in TWO queries, not 1 + N ──
        # Lines directly under the root, then every category under those lines in a
        # single IN query (was one query per line).
        lines = frappe.get_all("Item Group", filters={"parent_item_group": root},
                               fields=["name"], order_by="name")
        line_names = [ln.name for ln in lines if ln.name != "Cut Flowers - Legacy"]

        cats_by_line = {}
        if line_names:
            for c in frappe.get_all("Item Group", filters={"parent_item_group": ["in", line_names]},
                                    fields=["name", "parent_item_group"], order_by="name"):
                cats_by_line.setdefault(c.parent_item_group, []).append(c.name)

        # The category groups we pull items from: each line's sub-categories, or the
        # line itself when it holds items directly. Track which line each belongs to.
        group_line = {}
        category_groups = []
        for ln in line_names:
            for cg in (cats_by_line.get(ln) or [ln]):
                category_groups.append(cg)
                group_line[cg] = ln

        # ── ALL items for ALL those groups in ONE query (was one query per category) ──
        items_by_group = {}
        if category_groups:
            for it in frappe.get_all(
                    "Item", filters={"item_group": ["in", category_groups]},
                    fields=["name", "item_name", "image", "custom_color", "stock_uom", "disabled", "item_group"],
                    order_by="item_name"):
                items_by_group.setdefault(it.item_group, []).append(it)

        out = []
        for ln in line_names:
            ocats = []
            for cname in (cats_by_line.get(ln) or [ln]):
                items = items_by_group.get(cname)
                if not items:
                    continue
                disp = cname[len(ln) + 3:] if cname.startswith(ln + " - ") else cname
                ocats.append({
                    "name": disp, "group": cname,
                    "items": [{
                        "n": it.item_name or it.name,
                        "code": it.name,                # Item docname — used for edits
                        "img": it.image or "",
                        "s": "Inactive" if it.disabled else "Active",
                        "colour": it.custom_color or "",
                        "hex": colours.get(it.custom_color, "") if it.custom_color else "",
                        "uom": it.stock_uom or "",
                        "demand": round(demand.get(it.name, 0)),
                        "prod": round(prod.get(it.name, 0)),
                    } for it in items],
                })
            if ocats:
                out.append({"name": ln, "cats": ocats})

        frappe.response["message"] = {"success": True, "root": root, "lines": out}
    except Exception as e:
        frappe.log_error("getVarietyTree error: " + str(e))
        frappe.response["message"] = {"success": False, "error": str(e), "lines": []}


@frappe.whitelist()
def getColors():
    # The defined Colour palette (name + hex) offered by the variety-tree picker.
    colors = frappe.get_all("Color", fields=["name", "color"], order_by="name")
    frappe.response["message"] = {
        "success": True,
        "colors": [{"name": c.name, "hex": c.color or ""} for c in colors],
    }


@frappe.whitelist()
def setVarietyColor(item, color=None):
    # Assign (or clear) an Item's custom_color. `color` must be an existing Color.
    try:
        if not frappe.db.exists("Item", item):
            frappe.response["message"] = {"success": False, "error": "Item not found"}
            return
        color = (color or "").strip()
        if color and not frappe.db.exists("Color", color):
            frappe.response["message"] = {"success": False, "error": "Unknown colour: " + color}
            return
        frappe.db.set_value("Item", item, "custom_color", color or None)
        frappe.db.commit()
        hexval = frappe.db.get_value("Color", color, "color") if color else ""
        frappe.response["message"] = {"success": True, "colour": color, "hex": hexval or ""}
    except Exception as e:
        frappe.log_error("setVarietyColor error: " + str(e))
        frappe.response["message"] = {"success": False, "error": str(e)}

