# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `order-fulfilment` page API — ported verbatim from kaitet-group v15 LIVE
# Server Scripts (these were never migrated to the v16 bench). Bodies keep
# frappe.form_dict / frappe.response exactly as the live scripts set them.

import frappe


@frappe.whitelist()
def getOrderFulfilment():
    # Order Fulfilment — one row per Sales Order LINE (Sales Order Item): stems
    # ordered vs confirmed vs PACKED (from Farm Pack List), with the customer's
    # account manager. Client groups: account manager -> sales order -> line.
    # Fulfilment = packed / ordered. Scope: delivery_date (default today).
    fd = frappe.form_dict
    delivery_date = fd.get('delivery_date') or frappe.utils.today()

    rows = frappe.db.sql("""
        SELECT
            so.name               AS sales_order,
            so.customer           AS customer,
            so.custom_order_name  AS order_name,
            (SELECT MIN(o.name) FROM `tabOrder Pick List` o WHERE o.sales_order = so.name) AS opl,
            soi.name              AS soi,
            soi.idx               AS idx,
            soi.item_code         AS variety,
            soi.custom_length     AS length,
            c.account_manager     AS manager_email,
            COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), c.account_manager, '') AS manager_name,
            COALESCE(soi.qty * soi.conversion_factor, 0) AS ordered,
            COALESCE((SELECT SUM(cs.stems) FROM `tabConfirmed Stems` cs
                      WHERE cs.parent = so.name AND cs.parenttype = 'Sales Order'
                        AND cs.sales_order_item = soi.name), 0) AS confirmed,
            COALESCE((SELECT SUM(fpi.stock_qty)
                      FROM `tabFarm Pack List` fpl
                      JOIN `tabFarm Packlist Item` fpi
                        ON fpi.parent = fpl.name AND fpi.parenttype = 'Farm Pack List'
                           AND fpi.parentfield = 'pack_list_item'
                      WHERE fpl.sales_order = so.name AND fpl.docstatus != 2
                        AND fpi.item_code = soi.item_code
                        AND (fpi.stem_length = soi.custom_length OR soi.custom_length IS NULL OR soi.custom_length = '')
                     ), 0) AS packed,
            COALESCE((SELECT COUNT(*)
                      FROM `tabPick List Item` pli
                      JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
                      WHERE opl.sales_order = so.name AND pli.parenttype = 'Order Pick List'
                        AND pli.item_code = soi.item_code
                        AND (pli.stem_length = soi.custom_length OR soi.custom_length IS NULL OR soi.custom_length = '')
                        AND (pli.awaiting_transfer = 1 OR pli.shelved = 1)
                     ), 0) AS transferred
        FROM `tabSales Order Item` soi
        JOIN `tabSales Order` so ON so.name = soi.parent
        LEFT JOIN `tabCustomer` c ON c.name = so.customer
        LEFT JOIN `tabUser` u ON u.name = c.account_manager
        WHERE so.docstatus = 1
          AND so.delivery_date = %(d)s
          AND so.status NOT IN ('Cancelled', 'Closed')
        ORDER BY manager_name, so.customer, so.name, soi.idx
    """, {'d': delivery_date}, as_dict=True)

    for r in rows:
        for k in ['ordered', 'confirmed', 'packed', 'transferred']:
            r[k] = int(r.get(k) or 0)
        if not (r.get('manager_name') or '').strip():
            r['manager_name'] = 'Unassigned'

    frappe.response['message'] = {'delivery_date': str(delivery_date), 'lines': rows}
