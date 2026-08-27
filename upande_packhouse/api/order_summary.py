# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `order-summary` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getPackhouseLocations():
    # Locations a packing farm can map to (Farm.farm_location -> Location tree).
    # Used to populate the Order Summary location filter independent of the date.
    rows = frappe.db.sql("""
        SELECT DISTINCT f.farm_location AS location
        FROM `tabFarm` f
        WHERE f.farm_location IS NOT NULL AND f.farm_location != ''
        ORDER BY f.farm_location
    """, as_dict=True)
    frappe.response['message'] = {
        'success': True,
        'locations': [r['location'] for r in rows]
    }


@frappe.whitelist()
def fetchOrderSummaryData():
    # Order Summary Dashboard
    # API: fetchOrderSummaryData

    delivery_date       = frappe.form_dict.get('delivery_date') or frappe.utils.today()
    # Location = the packing farm's location (Karen vs Ravine), derived from the OPL's
    # farm. Accept either param name for backward compatibility with the old client.
    location            = frappe.form_dict.get('location') or frappe.form_dict.get('processing_location', '')

    where_conditions = [
        "so.docstatus = 1",
        "so.status NOT IN ('Cancelled', 'Closed')",
        "so.delivery_date = %(delivery_date)s"
    ]

    if location:
        # Location = the packing farm's Location (Karen / Ravine / Naivasha),
        # via Farm.farm_location. Accept the farm name too for back-compat.
        where_conditions.append("(opl_farm.farm_location = %(location)s OR opl.farm = %(location)s)")

    where_clause = ' AND '.join(where_conditions)

    query = f"""
    SELECT
        so.customer,
        so.name                          AS sales_order,
        so.custom_order_name             AS order_name,
        so.delivery_date,
        soi.name                         AS so_item_id,
        soi.idx                          AS line_no,
        soi.item_code                    AS variety,
        soi.item_group,
        soi.custom_length                AS length,
        soi.custom_box_type              AS box_type,
        soi.custom_packrate              AS pack_rate,
        soi.custom_number_of_boxes       AS boxes_ordered,
        soi.custom_mixed_box             AS mixed_box,
        soi.custom_mix_group             AS mix_group,
        soi.custom_mixed_bunch           AS mixed_bunch,
        soi.custom_bunch_group           AS bunch_group,
        soi.stock_qty                    AS stems_ordered,
        /* Processing location = the packing farm's location (Karen vs Ravine).
           soi.custom_processing_location is not populated, so derive it from the
           OPL's farm via Farm.custom_location. */
        opl.farm         AS processing_location,
        opl_farm.farm_location           AS location,
        soi.custom_opl                   AS opl_id,

        opl.docstatus                    AS opl_docstatus,
        opl.team                  AS team,

        /* ── Allocated & Issued: from Pick List Item child table via stock_qty ── */
        /* Grouped by OPL + sales_order_item so each SO line gets precise totals  */
        IFNULL(pli_agg.allocated_stems, 0)  AS allocated_stems,
        IFNULL(pli_agg.issued_stems, 0)     AS issued_stems,
        IFNULL(pli_agg.issued_count, 0)     AS issued_count,
        IFNULL(pli_agg.total_count, 0)      AS total_count,

        fpl_agg.fpl_id                   AS fpl_id,
        IFNULL(fpl_agg.packed_stems, 0)  AS packed_stems,
        IFNULL(fpl_agg.packed_boxes, 0)  AS boxes_packed,

        IFNULL(bl.box_count, 0)          AS box_labels_count,

        /* ── Staged: Box Labels with loaded=1 (staged at cold room) ── */
        IFNULL(bl.staged_count, 0)       AS staged_boxes,

        /* ── Loaded: Loading Sheet Items with loaded=1 ── */
        IFNULL(ls.loaded_count, 0)       AS loaded_boxes,

        IFNULL(dsp.dispatched_stems, 0)  AS dispatched_stems,
        dsp.dispatch_ref                 AS dispatch_ref,

        /* ── Confirmed Stems ── */
        IFNULL(cs.confirmed_stems, 0)    AS confirmed_stems,

        /* ── Takt: minutes from OPL issued -> staged (final-stage cycle time) ── */
        /* No dedicated timestamps exist for the issued/staged flags, so we use   */
        /* the row `modified` time when each flag was set: issued = latest issued  */
        /* Pick List Item; staged = earliest staged Box Label.                     */
        CASE
            WHEN iss.issued_at IS NOT NULL AND stg.staged_at IS NOT NULL
                 AND stg.staged_at >= iss.issued_at
            THEN TIMESTAMPDIFF(MINUTE, iss.issued_at, stg.staged_at)
        END                              AS takt_mins,

        /* ── Invoiced: submitted Sales Invoice(s) linked to this Sales Order ── */
        inv.invoice_ref                  AS invoice_ref

    FROM `tabSales Order` so
    INNER JOIN `tabSales Order Item` soi ON soi.parent = so.name
    LEFT JOIN `tabOrder Pick List` opl ON opl.name = soi.custom_opl
    LEFT JOIN `tabFarm` opl_farm ON opl_farm.name = opl.farm

    /* ── Allocated & Issued per SO line item ──────────────────────────────── */
    /* Uses stock_qty from Pick List Item (= stems after UOM conversion).     */
    /* sales_order_item links each PLI row back to the specific SO Item,      */
    /* so we get per-line precision instead of OPL-level totals.              */
    LEFT JOIN (
        SELECT
            pli.parent                   AS opl_name,
            pli.sales_order_item         AS so_item,
            SUM(pli.stock_qty)           AS allocated_stems,
            SUM(CASE WHEN pli.issued = 1 THEN pli.stock_qty ELSE 0 END) AS issued_stems,
            COUNT(*)                     AS total_count,
            SUM(CASE WHEN pli.issued = 1 THEN 1 ELSE 0 END) AS issued_count
        FROM `tabPick List Item` pli
        WHERE pli.parenttype = 'Order Pick List'
        GROUP BY pli.parent, pli.sales_order_item
    ) pli_agg ON pli_agg.opl_name = opl.name
             AND pli_agg.so_item = soi.name

    /* ── Farm Pack List: packed boxes = COUNT of pack_list_item child rows,     */
    /* grouped per OPL so multiple FPLs per OPL do NOT fan out the SO-line rows.  */
    /* (Old version joined tabFarm Pack List directly + counted Dispatch Form     */
    /*  Item rows — both wrong: fan-out inflation + wrong child table.)           */
    LEFT JOIN (
        SELECT
            fpl_p.order_pick_list                 AS opl_name,
            MIN(fpl_p.name)                              AS fpl_id,
            COUNT(DISTINCT pli_p.box_id)                 AS packed_boxes,
            SUM(IFNULL(pli_p.stock_qty, 0)) AS packed_stems
        FROM `tabFarm Pack List` fpl_p
        INNER JOIN `tabFarm Packlist Item` pli_p
            ON pli_p.parent = fpl_p.name
            AND pli_p.parenttype = 'Farm Pack List'
            AND pli_p.parentfield = 'pack_list_item'
        WHERE fpl_p.docstatus != 2
          AND fpl_p.sales_order IN (
              SELECT so_f.name
              FROM `tabSales Order` so_f
              WHERE so_f.docstatus = 1
                AND so_f.delivery_date = %(delivery_date)s
                AND so_f.status NOT IN ('Cancelled', 'Closed')
          )
        GROUP BY fpl_p.order_pick_list
    ) fpl_agg ON fpl_agg.opl_name = opl.name

    /* ── Box Labels: total + staged ── */
    LEFT JOIN (
        SELECT
            bl_inner.order_pick_list     AS opl_name,
            COUNT(bl_inner.name)         AS box_count,
            SUM(CASE WHEN bl_inner.staged = 1 THEN 1 ELSE 0 END) AS staged_count
        FROM `tabBox Label` bl_inner
        GROUP BY bl_inner.order_pick_list
    ) bl ON bl.opl_name = opl.name

    /* ── Loading Sheet: loaded onto truck ── */
    LEFT JOIN (
        SELECT
            bl_ls.order_pick_list        AS opl_name,
            COUNT(lsi.name) AS loaded_count
        FROM `tabLoading Sheet Item` lsi
        INNER JOIN `tabBox Label` bl_ls
            ON bl_ls.name = lsi.box_label_link
        GROUP BY bl_ls.order_pick_list
    ) ls ON ls.opl_name = opl.name

    /* ── Dispatched ── */
    LEFT JOIN (
        SELECT
            soi_d.custom_opl             AS opl_name,
            SUM(dni.stock_qty)           AS dispatched_stems,
            GROUP_CONCAT(DISTINCT dn.name ORDER BY dn.creation SEPARATOR ', ') AS dispatch_ref
        FROM `tabDelivery Note Item` dni
        INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
        INNER JOIN `tabSales Order Item` soi_d ON soi_d.name = dni.so_detail
        WHERE dn.docstatus = 1 AND soi_d.custom_opl IS NOT NULL AND soi_d.custom_opl != ''
        GROUP BY soi_d.custom_opl
    ) dsp ON dsp.opl_name = opl.name

    /* ── Confirmed Stems ── */
    LEFT JOIN (
        SELECT
            cs_inner.sales_order_item    AS so_item_id,
            SUM(cs_inner.stems)          AS confirmed_stems
        FROM `tabConfirmed Stems` cs_inner
        INNER JOIN `tabSales Order` so_cs
            ON so_cs.name = cs_inner.parent
        WHERE cs_inner.parenttype = 'Sales Order'
          AND so_cs.docstatus = 1
          AND so_cs.delivery_date = %(delivery_date)s
        GROUP BY cs_inner.sales_order_item
    ) cs ON cs.so_item_id = soi.name

    /* ── Takt endpoints per OPL (see takt_mins above) ── */
    LEFT JOIN (
        SELECT pli_t.parent AS opl_name, MAX(pli_t.modified) AS issued_at
        FROM `tabPick List Item` pli_t
        WHERE pli_t.parenttype = 'Order Pick List' AND pli_t.issued = 1
        GROUP BY pli_t.parent
    ) iss ON iss.opl_name = opl.name
    LEFT JOIN (
        SELECT bl_t.order_pick_list AS opl_name, MIN(bl_t.modified) AS staged_at
        FROM `tabBox Label` bl_t
        WHERE bl_t.staged = 1
        GROUP BY bl_t.order_pick_list
    ) stg ON stg.opl_name = opl.name

    /* ── Invoiced: submitted Sales Invoices linked via custom_so ── */
    LEFT JOIN (
        SELECT si.custom_so AS so_name,
               GROUP_CONCAT(DISTINCT si.name ORDER BY si.creation SEPARATOR ', ') AS invoice_ref
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.custom_so IS NOT NULL AND si.custom_so != ''
        GROUP BY si.custom_so
    ) inv ON inv.so_name = so.name

    WHERE {where_clause}
    ORDER BY so.customer, so.name, soi.idx
    """

    try:
        results = frappe.db.sql(query, {
            'delivery_date': delivery_date,
            'location':      location
        }, as_dict=True)

        for r in results:
            opl_id          = r.get('opl_id')
            opl_docstatus   = r.get('opl_docstatus')
            allocated       = int(r.get('allocated_stems') or 0)
            issued_stems    = float(r.get('issued_stems') or 0)
            total_count     = int(r.get('total_count') or 0)
            issued_count    = int(r.get('issued_count') or 0)
            packed_stems    = float(r.get('packed_stems') or 0)
            boxes_packed    = int(r.get('boxes_packed') or 0)
            stems_ordered   = float(r.get('stems_ordered') or 0)
            box_count       = int(r.get('box_labels_count') or 0)
            boxes_ordered   = int(r.get('boxes_ordered') or 0)
            dispatched      = float(r.get('dispatched_stems') or 0)
            confirmed_stems = int(r.get('confirmed_stems') or 0)
            staged_boxes    = int(r.get('staged_boxes') or 0)
            loaded_boxes    = int(r.get('loaded_boxes') or 0)

            r['allocated_stems']  = allocated
            r['issued_stems']     = issued_stems
            r['packed_stems']     = packed_stems
            r['boxes_packed']     = boxes_packed
            r['dispatched_stems'] = dispatched
            r['stems_ordered']    = stems_ordered
            r['box_labels_count'] = box_count
            r['boxes_ordered']    = boxes_ordered
            r['confirmed_stems']  = confirmed_stems
            r['staged_boxes']     = staged_boxes
            r['loaded_boxes']     = loaded_boxes
            r['takt_mins']        = int(r['takt_mins']) if r.get('takt_mins') is not None else None

            # ── Status determination (most complete first) ──
            if not opl_id:
                status = 'Not Allocated'
            elif dispatched > 0:
                status = 'Dispatched'
            elif loaded_boxes > 0 and loaded_boxes >= box_count and box_count > 0:
                status = 'Loaded'
            elif loaded_boxes > 0:
                status = 'Partially Loaded'
            elif staged_boxes > 0 and staged_boxes >= box_count and box_count > 0:
                status = 'Staged'
            elif staged_boxes > 0:
                status = 'Partially Staged'
            elif box_count > 0:
                status = 'Box Labels Generated'
            elif packed_stems > 0 and packed_stems >= stems_ordered:
                status = 'Packed'
            elif packed_stems > 0:
                status = 'Partially Packed'
            elif total_count > 0 and issued_count >= total_count:
                status = 'Issued'
            elif issued_count > 0:
                status = 'Partially Issued'
            elif opl_docstatus == 1:
                status = 'Allocated'
            else:
                status = 'Partially Allocated'

            r['status'] = status

        frappe.response['message'] = {
            'success': True,
            'data': results
        }

    except Exception as e:
        frappe.log_error('fetchOrderSummaryData error: ' + str(e))
        frappe.response['message'] = {
            'success': False,
            'error': str(e),
            'data': []
        }
