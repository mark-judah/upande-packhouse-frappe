# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `avails` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getAvailsData():
    # Avails Data
    # API: getAvailsData
    # Shelf stock (Shelf/Shelf Item, live) minus allocations (Bucket Allocation
    # Status, live) => avail per variety per length per farm. Orders/Confirmed are
    # scoped to a delivery_date (default today). Client pivots + filters by farm/variety.
    try:
        delivery_date = frappe.form_dict.get('delivery_date') or frappe.utils.today()

        shelf_rows = frappe.db.sql("""
            SELECT
                si.variety       AS variety,
                si.stem_length   AS length,
                s.farm           AS farm,
                SUM(si.stem_qty) AS stems
            FROM `tabShelf` s
            INNER JOIN `tabShelf Item` si ON s.name = si.parent
            WHERE si.stem_qty > 0
                AND si.variety IS NOT NULL
                AND TRIM(si.variety) != ''
            GROUP BY si.variety, si.stem_length, s.farm
            ORDER BY si.variety, si.stem_length, s.farm
        """, as_dict=True)

        alloc_rows = frappe.db.sql("""
            SELECT
                bas.item_code                AS variety,
                bas.stem_length              AS length,
                bas.shelf_farm               AS farm,
                SUM(bas.allocated_quantity)  AS stems
            FROM `tabBucket Allocation Status` bas
            WHERE bas.item_code IS NOT NULL
                AND TRIM(bas.item_code) != ''
            GROUP BY bas.item_code, bas.stem_length, bas.shelf_farm
            ORDER BY bas.item_code, bas.stem_length, bas.shelf_farm
        """, as_dict=True)

        order_rows = frappe.db.sql("""
            SELECT
                soi.item_code                          AS variety,
                soi.custom_length                      AS length,
                SUM(soi.qty * soi.conversion_factor)   AS ordered_stems,
                SUM(IFNULL(cs.confirmed, 0))           AS confirmed_stems
            FROM `tabSales Order Item` soi
            INNER JOIN `tabSales Order` so ON so.name = soi.parent
            LEFT JOIN (
                SELECT sales_order_item, SUM(stems) AS confirmed
                FROM `tabConfirmed Stems`
                WHERE parenttype = 'Sales Order'
                GROUP BY sales_order_item
            ) cs ON cs.sales_order_item = soi.name
            WHERE so.delivery_date = %(delivery_date)s
                AND so.docstatus = 1
                AND so.status NOT IN ('Cancelled', 'Closed', 'Completed')
                AND soi.item_code IS NOT NULL
            GROUP BY soi.item_code, soi.custom_length
            ORDER BY soi.item_code, soi.custom_length
        """, {'delivery_date': delivery_date}, as_dict=True)

        frappe.response['message'] = {
            'success': True,
            'delivery_date': delivery_date,
            'shelf': shelf_rows,
            'allocated': alloc_rows,
            'orders': order_rows
        }
    except Exception as e:
        frappe.log_error('getAvailsData error: ' + str(e))
        frappe.response['message'] = {
            'success': False,
            'error': str(e),
            'shelf': [],
            'allocated': [],
            'orders': []
        }
