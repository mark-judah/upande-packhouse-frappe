# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `stock-visibility` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getStockVisibilityData():
    # Stock Visibility Data
    # API: getStockVisibilityData
    try:
        delivery_date = frappe.form_dict.get('delivery_date') or frappe.utils.today()

        stock_rows = frappe.db.sql("""
            SELECT
                si.variety,
                si.stem_length AS length,
                s.farm,
                SUM(si.stem_qty) AS stems
            FROM `tabShelf` s
            INNER JOIN `tabShelf Item` si ON s.name = si.parent
            WHERE si.stem_qty > 0
                AND si.variety IS NOT NULL
                AND TRIM(si.variety) != ''
            GROUP BY si.variety, si.stem_length, s.farm
            ORDER BY si.variety, si.stem_length, s.farm
        """, as_dict=True)

        # Not shelved (coldroom): received (Receiving/Late Receipt) in the recent window
        # whose bucket is NOT currently on a shelf, NOT discarded, and NOT issued => still
        # sitting in the coldroom awaiting shelving. Accumulates across the window (piles up
        # day over day) and self-clears once a bucket is shelved / issued / discarded.
        # Window kept short (bucket ids aren't indexed, so a long scan times out; a bucket
        # unshelved beyond a few days is not live stock anyway).
        not_shelved_rows = frappe.db.sql("""
            SELECT
                sed.item_code AS variety,
                se.custom_stem_length AS length,
                se.custom_farm AS farm,
                SUM(sed.qty) AS stems,
                COUNT(DISTINCT se.custom_bucket_id) AS buckets
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            INNER JOIN `tabFarm` f ON se.custom_farm = f.name
            WHERE se.docstatus = 1
                AND se.stock_entry_type IN ('Receiving', 'Late Receipt')
                AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
                AND f.company = 'Karen Roses'
                AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id != ''
                AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si2
                    WHERE si2.bucket_id = se.custom_bucket_id AND si2.stem_qty > 0)
                AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` d
                    WHERE d.docstatus = 1 AND d.stock_entry_type = 'Discard'
                        AND d.custom_bucket_id = se.custom_bucket_id AND d.posting_date >= se.posting_date)
                AND NOT EXISTS (SELECT 1 FROM `tabPick List Item` pli
                    WHERE pli.bucket = se.custom_bucket_id AND pli.issued = 1)
            GROUP BY sed.item_code, se.custom_stem_length, se.custom_farm
            ORDER BY sed.item_code, se.custom_stem_length, se.custom_farm
        """, as_dict=True)

        order_rows = frappe.db.sql("""
            SELECT
                soi.item_code AS variety,
                soi.item_group,
                soi.custom_length AS length,
                SUM(soi.qty * soi.conversion_factor) AS stems
            FROM `tabSales Order Item` soi
            INNER JOIN `tabSales Order` so ON so.name = soi.parent
            WHERE so.delivery_date = %(delivery_date)s
                AND so.docstatus = 1
                AND so.status NOT IN ('Cancelled', 'Closed', 'Completed')
                AND soi.item_code IS NOT NULL
            GROUP BY soi.item_code, soi.item_group, soi.custom_length
            ORDER BY soi.item_code, soi.custom_length
        """, {'delivery_date': delivery_date}, as_dict=True)

        frappe.response['message'] = {
            'success': True,
            'delivery_date': delivery_date,
            'stock': stock_rows,
            'not_shelved': not_shelved_rows,
            'orders': order_rows
        }
    except Exception as e:
        frappe.log_error('getStockVisibilityData error: ' + str(e))
        frappe.response['message'] = {
            'success': False,
            'error': str(e),
            'stock': [],
            'not_shelved': [],
            'orders': []
        }
