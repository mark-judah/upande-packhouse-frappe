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
                SUM(si.stem_qty) AS stems,
                TIMESTAMPDIFF(DAY, MIN(si.date_added), NOW()) AS oldest_days
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
                AND EXISTS (SELECT 1 FROM `tabShelf Item` si
                    WHERE si.bucket_id = bas.bucket_id AND si.stem_qty > 0)
            GROUP BY bas.item_code, bas.stem_length, bas.shelf_farm
            ORDER BY bas.item_code, bas.stem_length, bas.shelf_farm
        """, as_dict=True)

        # Discard-requested stems still on a shelf (a bucket can be shelved AND in a
        # pending discard request) — netted out of availability, same as stock-visibility.
        discard_rows = frappe.db.sql("""
            SELECT
                drb.variety      AS variety,
                drb.stem_length  AS length,
                drb.farm         AS farm,
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

        # Variety images for the PDF export / cards — Item.image first, else the
        # first image File attached to the Item (the field the office keeps photos in).
        variety_names = list({r['variety'] for r in shelf_rows if r.get('variety')})
        images = {}
        if variety_names:
            for it in frappe.get_all("Item", filters={"name": ["in", variety_names]},
                                     fields=["name", "image"]):
                if it.image:
                    images[it.name] = it.image
            missing = [v for v in variety_names if v not in images]
            if missing:
                for f in frappe.get_all(
                    "File",
                    filters={"attached_to_doctype": "Item", "attached_to_name": ["in", missing]},
                    fields=["attached_to_name", "file_url"],
                    order_by="creation asc",
                ):
                    url = (f.file_url or "").lower()
                    if f.file_url and url.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        images.setdefault(f.attached_to_name, f.file_url)

        # Variety colours (Item.custom_color -> Color.color hex) for the export's
        # fallback banner when a variety has no photo.
        colours = {}
        if variety_names:
            for r in frappe.db.sql("""
                SELECT i.name AS v, i.custom_color AS cname, c.color AS hex
                FROM `tabItem` i LEFT JOIN `tabColor` c ON c.name = i.custom_color
                WHERE i.name IN %(v)s AND i.custom_color IS NOT NULL AND i.custom_color != ''
            """, {'v': variety_names}, as_dict=True):
                colours[r['v']] = {'name': r['cname'], 'hex': r['hex'] or '#cccccc'}

        frappe.response['message'] = {
            'success': True,
            'delivery_date': delivery_date,
            'shelf': shelf_rows,
            'allocated': alloc_rows,
            'discard': discard_rows,
            'orders': order_rows,
            'images': images,
            'colours': colours,
        }
    except Exception as e:
        frappe.log_error('getAvailsData error: ' + str(e))
        frappe.response['message'] = {
            'success': False,
            'error': str(e),
            'shelf': [],
            'allocated': [],
            'discard': [],
            'orders': [],
            'images': {}
        }
