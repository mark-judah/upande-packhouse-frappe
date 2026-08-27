# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `stock-visibility` page API.
#
# The page is about *true* availability, so the shelf total is decomposed:
#     Available = Shelved - Allocated - Discard-requested   (floored at 0)
# because a bucket can sit physically on a shelf while its stems are already
# committed to an order (Bucket Allocation Status) or flagged for discard
# (a pending Discard Request). Counting the raw shelf quantity as "available"
# overstates cover, so allocated and discard-requested stems are surfaced
# alongside the shelved quantity and netted out.

import frappe


@frappe.whitelist()
def getStockVisibilityData():
    # Stock Visibility Data
    # API: getStockVisibilityData
    try:
        delivery_date = frappe.form_dict.get('delivery_date') or frappe.utils.today()

        # ── Shelved stock (physically on a shelf). item_group resolved from the
        # Item master so the page can classify EVERY variety (not just ordered
        # ones), and oldest_days gives freshness (age of the oldest stems in the
        # group) so aging stock can be surfaced before it becomes a discard. ──
        # Age bands (fresh 0-2d / aging 3-6d / old 7d+) computed per shelf row and
        # summed, so the grid can show the age composition of a stock figure, not
        # just its oldest day.
        stock_rows = frappe.db.sql("""
            SELECT
                si.variety,
                i.item_group,
                si.stem_length AS length,
                s.farm,
                SUM(si.stem_qty) AS stems,
                DATEDIFF(CURDATE(), MIN(COALESCE(si.receiving_date, si.date_added))) AS oldest_days,
                SUM(CASE WHEN DATEDIFF(CURDATE(), COALESCE(si.receiving_date, si.date_added)) <= 2
                    OR si.receiving_date IS NULL AND si.date_added IS NULL
                    THEN si.stem_qty ELSE 0 END) AS fresh,
                SUM(CASE WHEN DATEDIFF(CURDATE(), COALESCE(si.receiving_date, si.date_added)) BETWEEN 3 AND 6
                    THEN si.stem_qty ELSE 0 END) AS aging,
                SUM(CASE WHEN DATEDIFF(CURDATE(), COALESCE(si.receiving_date, si.date_added)) >= 7
                    THEN si.stem_qty ELSE 0 END) AS old
            FROM `tabShelf` s
            INNER JOIN `tabShelf Item` si ON s.name = si.parent
            LEFT JOIN `tabItem` i ON i.name = si.variety
            WHERE si.stem_qty > 0
                AND si.variety IS NOT NULL
                AND TRIM(si.variety) != ''
            GROUP BY si.variety, i.item_group, si.stem_length, s.farm
            ORDER BY si.variety, si.stem_length, s.farm
        """, as_dict=True)

        # ── Allocated: stems committed to orders on buckets that are STILL on a
        # shelf (Bucket Allocation Status keyed by bucket). Netted out of the
        # shelved figure to get true availability. ──
        allocated_rows = frappe.db.sql("""
            SELECT
                bas.item_code AS variety,
                bas.stem_length AS length,
                bas.shelf_farm AS farm,
                SUM(bas.allocated_quantity) AS stems
            FROM `tabBucket Allocation Status` bas
            WHERE IFNULL(bas.allocated_quantity, 0) > 0
                AND EXISTS (SELECT 1 FROM `tabShelf Item` si
                    WHERE si.bucket_id = bas.bucket_id AND si.stem_qty > 0)
            GROUP BY bas.item_code, bas.stem_length, bas.shelf_farm
            ORDER BY bas.item_code, bas.stem_length, bas.shelf_farm
        """, as_dict=True)

        # ── Discard-requested: stems on a shelf that are also in a pending
        # Discard Request (submittable, not cancelled; the bucket row is still
        # shelved and not yet discarded). This is the "on the shelf but also in a
        # discard request" overlap. ──
        discard_rows = frappe.db.sql("""
            SELECT
                drb.variety AS variety,
                drb.stem_length AS length,
                drb.farm AS farm,
                SUM(drb.stem_qty) AS stems
            FROM `tabDiscard Request Bucket` drb
            INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
            WHERE dr.docstatus < 2
                AND IFNULL(drb.is_shelved, 0) = 1
                AND IFNULL(drb.discarded, 0) = 0
                AND EXISTS (SELECT 1 FROM `tabShelf Item` si
                    WHERE si.bucket_id = drb.bucket_id AND si.stem_qty > 0)
            GROUP BY drb.variety, drb.stem_length, drb.farm
            ORDER BY drb.variety, drb.stem_length, drb.farm
        """, as_dict=True)

        # ── Coldroom: received (Receiving/Late Receipt) in the recent window whose
        # bucket is NOT on a shelf, NOT discarded, NOT issued => awaiting shelving.
        # Window kept short (bucket ids aren't indexed). Company is NOT hardcoded —
        # the shelf side isn't either, and hardcoding it blanked the coldroom on any
        # site whose farms belong to a different company. ──
        not_shelved_rows = frappe.db.sql("""
            SELECT
                sed.item_code AS variety,
                i.item_group,
                se.custom_stem_length AS length,
                se.custom_farm AS farm,
                SUM(sed.qty) AS stems,
                COUNT(DISTINCT se.custom_bucket_id) AS buckets
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            LEFT JOIN `tabItem` i ON i.name = sed.item_code
            WHERE se.docstatus = 1
                AND se.stock_entry_type IN ('Receiving', 'Late Receipt')
                AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
                AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id != ''
                AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si2
                    WHERE si2.bucket_id = se.custom_bucket_id AND si2.stem_qty > 0)
                AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` d
                    WHERE d.docstatus = 1 AND d.stock_entry_type = 'Discard'
                        AND d.custom_bucket_id = se.custom_bucket_id AND d.posting_date >= se.posting_date)
                AND NOT EXISTS (SELECT 1 FROM `tabPick List Item` pli
                    WHERE pli.bucket = se.custom_bucket_id AND pli.issued = 1)
            GROUP BY sed.item_code, i.item_group, se.custom_stem_length, se.custom_farm
            ORDER BY sed.item_code, se.custom_stem_length, se.custom_farm
        """, as_dict=True)

        # ── Orders for the delivery date. customer / delivery_point / farm carried
        # so the page can scope demand to a buyer, a drop-off, or a farm. ──
        order_rows = frappe.db.sql("""
            SELECT
                soi.item_code AS variety,
                soi.item_group,
                soi.custom_length AS length,
                so.customer,
                so.custom_delivery_point AS delivery_point,
                so.custom_farm AS farm,
                SUM(soi.qty * soi.conversion_factor) AS stems
            FROM `tabSales Order Item` soi
            INNER JOIN `tabSales Order` so ON so.name = soi.parent
            WHERE so.delivery_date = %(delivery_date)s
                AND so.docstatus = 1
                AND so.status NOT IN ('Cancelled', 'Closed', 'Completed')
                AND soi.item_code IS NOT NULL
            GROUP BY soi.item_code, soi.item_group, soi.custom_length,
                     so.customer, so.custom_delivery_point, so.custom_farm
            ORDER BY soi.item_code, soi.custom_length
        """, {'delivery_date': delivery_date}, as_dict=True)

        frappe.response['message'] = {
            'success': True,
            'delivery_date': delivery_date,
            'stock': stock_rows,
            'allocated': allocated_rows,
            'discard': discard_rows,
            'not_shelved': not_shelved_rows,
            'orders': order_rows
        }
    except Exception as e:
        frappe.log_error('getStockVisibilityData error: ' + str(e))
        frappe.response['message'] = {
            'success': False,
            'error': str(e),
            'stock': [],
            'allocated': [],
            'discard': [],
            'not_shelved': [],
            'orders': []
        }
