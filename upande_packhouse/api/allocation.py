# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `sales-allocation-planning` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def fetchSalesAllocationPlanningData():
    # Sales Allocation Planning
    # API: fetchSalesAllocationPlanningData

    delivery_date = frappe.form_dict.get('delivery_date') or frappe.utils.today()
    customer = frappe.form_dict.get('customer')
    sales_order = frappe.form_dict.get('sales_order')
    variety = frappe.form_dict.get('variety')
    length = frappe.form_dict.get('length')
    farm = frappe.form_dict.get('farm')

    where_conditions = []
    where_conditions.append("so.docstatus = 1")
    where_conditions.append("so.status NOT IN ('Completed', 'Closed', 'Cancelled')")
    where_conditions.append("so.delivery_date = %(delivery_date)s")

    if customer:
        where_conditions.append(f"so.customer = '{customer}'")
    if sales_order:
        where_conditions.append(f"so.name = '{sales_order}'")
    if variety:
        where_conditions.append(f"soi.item_code = '{variety}'")
    if length:
        where_conditions.append(f"soi.custom_length = '{length}'")

    where_clause = " AND ".join(where_conditions)

    farm_filter = ""
    if farm:
        farm_filter = f"AND s.farm = '{farm}'"

    query = f"""
    WITH
    shelf_stock_by_farm AS (
        SELECT
            si.variety AS item_code,
            si.stem_length AS length,
            s.farm,
            SUM(si.stem_qty) AS available_stems,
            COUNT(DISTINCT si.bucket_id) AS bucket_count
        FROM `tabShelf` s
        INNER JOIN `tabShelf Item` si ON s.name = si.parent
        WHERE si.bucket_id IS NOT NULL
            AND TRIM(si.bucket_id) != ''
            AND si.stem_qty > 0
            {farm_filter}
        GROUP BY si.variety, si.stem_length, s.farm
    ),
    order_lines AS (
        SELECT
            so.name AS sales_order,
            so.customer,
            so.transaction_date,
            so.custom_order_name AS order_name,
            so.delivery_date,
            soi.name AS item_row_name,
            soi.idx AS line_no,
            soi.item_code,
            soi.custom_length AS length,
            soi.custom_number_of_boxes AS boxes_ordered,
            soi.custom_ordered_quantity AS stems_ordered,
            soi.custom_mixed_box AS mixed_box,
            soi.custom_mix_group AS mix_group,
            soi.custom_mixed_bunch AS mixed_bunch,
            soi.custom_bunch_group AS bunch_group
        FROM `tabSales Order` so
        INNER JOIN `tabSales Order Item` soi ON soi.parent = so.name
        WHERE {where_clause}
    ),
    all_farms AS (
        SELECT name AS farm
        FROM `tabFarm`
    ),
    order_farm_combinations AS (
        SELECT
            ol.*,
            af.farm AS farm,
            COALESCE(stock.available_stems, 0) AS stock_total,
            COALESCE(stock.bucket_count, 0) AS bucket_count
        FROM order_lines ol
        CROSS JOIN all_farms af
        LEFT JOIN shelf_stock_by_farm stock
            ON ol.item_code = stock.item_code
            AND ol.length = stock.length
            AND af.farm = stock.farm
    ),
    confirmed_stems AS (
        SELECT
            cs.sales_order_item,
            cs.farm,
            cs.stems
        FROM `tabConfirmed Stems` cs
        INNER JOIN `tabSales Order` so2 ON cs.parent = so2.name
        WHERE cs.parenttype = 'Sales Order'
            AND cs.parentfield = 'custom_confirmed_stems_table'
            AND cs.farm IS NOT NULL
            AND cs.stems > 0
            AND so2.docstatus = 1
            AND so2.delivery_date = %(delivery_date)s
    )
    SELECT
        ofc.customer,
        ofc.transaction_date,
        ofc.delivery_date,
        ofc.sales_order,
        ofc.order_name,
        ofc.item_row_name,
        ofc.line_no,
        ofc.item_code,
        ofc.length,
        ofc.boxes_ordered,
        ofc.stems_ordered,
        ofc.mixed_box,
        ofc.mix_group,
        ofc.mixed_bunch,
        ofc.bunch_group,
        ofc.farm AS farm_name,
        ofc.bucket_count,
        ofc.stock_total,
        CASE
            WHEN ofc.stock_total >= ofc.stems_ordered THEN 'Sufficient'
            WHEN ofc.stock_total > 0 THEN 'Partial'
            ELSE 'No Stock'
        END AS stock_status,
        CASE
            WHEN ofc.stems_ordered > 0 THEN ROUND((ofc.stock_total / ofc.stems_ordered) * 100, 1)
            ELSE 0
        END AS fulfillment_percentage
    FROM order_farm_combinations ofc
    ORDER BY ofc.customer, ofc.sales_order, ofc.line_no, ofc.farm
    LIMIT 3000
    """

    results = frappe.db.sql(query, {'delivery_date': delivery_date}, as_dict=True)

    # Fetch confirmed stems and attach to each row
    confirmed_stems_query = """
        SELECT
            cs.sales_order_item,
            cs.farm,
            cs.stems,
            cs.parent AS sales_order
        FROM `tabConfirmed Stems` cs
        INNER JOIN `tabSales Order` so2 ON cs.parent = so2.name
        WHERE cs.parenttype = 'Sales Order'
            AND cs.parentfield = 'custom_confirmed_stems_table'
            AND cs.farm IS NOT NULL
            AND cs.stems > 0
            AND so2.docstatus = 1
            AND so2.delivery_date = %(delivery_date)s
    """
    confirmed_stems_data = frappe.db.sql(confirmed_stems_query, {'delivery_date': delivery_date}, as_dict=True)

    # Build map: sales_order_item -> [{farm, stems}]
    confirmed_stems_map = {}
    for cs in confirmed_stems_data:
        key = cs['sales_order_item']
        if key not in confirmed_stems_map:
            confirmed_stems_map[key] = []
        confirmed_stems_map[key].append({
            'farm': cs['farm'],
            'stems': cs['stems']
        })

    # Attach confirmed stems to each result row by matching item_row_name
    for r in results:
        item_key = r.get('item_row_name')
        r['custom_confirmed_stems_table'] = confirmed_stems_map.get(item_key, [])

    aggregations = {}

    if results:
        unique_order_lines = {}
        total_stock = 0

        # Boxes are counted ONCE per group (mixed bunch -> custom_bunch_group,
        # mixed box -> custom_mix_group, else per straight line). Stems stay per line.
        box_groups = {}

        for r in results:
            order_key = f"{r['sales_order']}-{r['line_no']}"
            if order_key not in unique_order_lines:
                unique_order_lines[order_key] = {
                    'stems_ordered': r['stems_ordered'] or 0,
                    'boxes_ordered': r['boxes_ordered'] or 0,
                    'customer':      r['customer'],
                    'item_code':     r['item_code']
                }
                bg = str(r.get('bunch_group') or '').strip()
                mg = str(r.get('mix_group') or '').strip()
                if r.get('mixed_bunch') == 1 and bg != '':
                    gkey = f"{r['sales_order']}||bunch||{bg}"
                elif r.get('mixed_box') == 1 and mg != '':
                    gkey = f"{r['sales_order']}||mix||{mg}"
                else:
                    gkey = f"{r['sales_order']}||line||{r['line_no']}"
                if gkey not in box_groups:
                    box_groups[gkey] = {'boxes': r['boxes_ordered'] or 0, 'customer': r['customer']}
            total_stock = total_stock + (r['stock_total'] or 0)

        total_stems = sum(ol['stems_ordered'] for ol in unique_order_lines.values())
        total_boxes = sum(g['boxes'] for g in box_groups.values())

        aggregations['total'] = {
            'stems_ordered':      total_stems,
            'stock_total':        total_stock,
            'boxes_ordered':      total_boxes,
            'lines':              len(unique_order_lines),
            'farm_stock_entries': len(results)
        }

        by_customer = {}
        for order_key, data in unique_order_lines.items():
            c = data['customer']
            if not c:
                continue
            if c not in by_customer:
                by_customer[c] = {'stems_ordered': 0, 'boxes_ordered': 0, 'stock_total': 0, 'lines': 0}
            by_customer[c]['stems_ordered'] = by_customer[c]['stems_ordered'] + data['stems_ordered']
            by_customer[c]['lines']         = by_customer[c]['lines'] + 1
        # Boxes per customer from the deduped groups (not per line).
        for gkey, g in box_groups.items():
            c = g['customer']
            if c and c in by_customer:
                by_customer[c]['boxes_ordered'] = by_customer[c]['boxes_ordered'] + g['boxes']
        for r in results:
            c = r['customer']
            if c in by_customer:
                by_customer[c]['stock_total'] = by_customer[c]['stock_total'] + (r['stock_total'] or 0)
        aggregations['by_customer'] = by_customer

        by_farm = {}
        for r in results:
            fn = r['farm_name'] or 'Unknown'
            if fn not in by_farm:
                by_farm[fn] = {'stems_ordered': 0, 'stock_total': 0, 'boxes_ordered': 0, 'lines': 0}
            by_farm[fn]['stems_ordered'] = by_farm[fn]['stems_ordered'] + (r['stems_ordered'] or 0)
            by_farm[fn]['stock_total']   = by_farm[fn]['stock_total']   + (r['stock_total'] or 0)
            by_farm[fn]['boxes_ordered'] = by_farm[fn]['boxes_ordered'] + (r['boxes_ordered'] or 0)
            by_farm[fn]['lines']         = by_farm[fn]['lines'] + 1
        aggregations['by_farm'] = by_farm

    frappe.response['message'] = {
        'success': True,
        'data': results,
        'aggregations': aggregations,
        'filters': {
            'delivery_date': delivery_date,
            'customer':      customer,
            'sales_order':   sales_order,
            'variety':        variety,
            'length':        length,
            'farm':          farm
        },
        'breakdown_type': 'by_farm'
    }


@frappe.whitelist()
def confimSalesOrderItem():
    sales_order = frappe.form_dict.get('sales_order')
    line_no = frappe.form_dict.get('line_no')
    processing_location = frappe.form_dict.get('processing_location')
    action = frappe.form_dict.get('action', 'confirm')  # 'confirm' or 'unconfirm'
    stems = frappe.form_dict.get('stems')  # Number of stems to confirm (partial confirmation)

    def validate_inputs():
        """Validate input parameters"""
        if not sales_order:
            frappe.throw("Sales Order is required")

        if not line_no:
            frappe.throw("Line number is required")

        if action == 'confirm' and not processing_location:
            frappe.throw("Processing location is required for confirmation")

        if not frappe.db.exists("Sales Order", sales_order):
            frappe.throw(f"Sales Order {sales_order} does not exist")

    def update_processing_location():
        """Update the confirmed stems table on the Sales Order (parent level) for partial confirmation.

        The custom_confirmed_stems_table child table lives on Sales Order (parent).
        Each row has:
          - sales_order_item: references the Sales Order Item name (child row name)
          - farm: Link to Farm
          - stems: Float
        """
        try:
            sales_order_doc = frappe.get_doc("Sales Order", sales_order)
            line_no_int = int(line_no)

            # Find the specific item line by idx
            item_row = None
            for row in sales_order_doc.items:
                if row.idx == line_no_int:
                    item_row = row
                    break

            if not item_row:
                frappe.throw(f"Sales Order Item line {line_no} not found")

            # The Sales Order Item child row 'name' is the unique identifier
            item_row_name = item_row.name

            # Parse stems value
            stems_val = 0
            if stems is not None:
                try:
                    stems_val = float(stems)
                except (ValueError, TypeError):
                    stems_val = 0

            stems_ordered = item_row.custom_ordered_quantity or 0

            # Get existing confirmed stems entries for THIS line from the parent-level child table
            existing_entries = sales_order_doc.custom_confirmed_stems_table or []
            line_entries = [e for e in existing_entries if e.sales_order_item == item_row_name]

            if action == 'confirm':
                # Calculate total confirmed by OTHER farms for this line
                other_confirmed = sum(
                    (entry.stems or 0) for entry in line_entries
                    if entry.farm != processing_location
                )

                # Validate: total confirmed cannot exceed ordered
                if stems_val + other_confirmed > stems_ordered:
                    frappe.throw(
                        f"Cannot confirm {int(stems_val)} stems. "
                        f"Already {int(other_confirmed)} confirmed by other farms. "
                        f"Maximum available: {int(stems_ordered - other_confirmed)}"
                    )

                # Find existing entry for this farm + item
                existing_entry = None
                for entry in existing_entries:
                    if entry.sales_order_item == item_row_name and entry.farm == processing_location:
                        existing_entry = entry
                        break

                if stems_val == 0:
                    # Remove the entry if stems is 0
                    if existing_entry:
                        sales_order_doc.remove(existing_entry)
                    action_msg = "removed"
                elif existing_entry:
                    # Update existing entry
                    existing_entry.stems = stems_val
                    action_msg = "updated"
                else:
                    # Add new entry to the parent-level child table
                    sales_order_doc.append('custom_confirmed_stems_table', {
                        'farm': processing_location,
                        'stems': stems_val,
                        'sales_order_item': item_row_name
                    })
                    action_msg = "confirmed"

            else:
                # Unconfirm - remove the entry for this farm + item
                to_remove = []
                for entry in existing_entries:
                    if entry.sales_order_item == item_row_name and entry.farm == processing_location:
                        to_remove.append(entry)
                for entry in to_remove:
                    sales_order_doc.remove(entry)
                action_msg = "unconfirmed"

            # Recalculate total confirmed for this line
            remaining_for_line = [
                e for e in (sales_order_doc.custom_confirmed_stems_table or [])
                if e.sales_order_item == item_row_name and e.farm and (e.stems or 0) > 0
            ]

            new_total = sum((e.stems or 0) for e in remaining_for_line)

            # Save the document (allow saving submitted Sales Orders)
            sales_order_doc.flags.ignore_validate_update_after_submit = True
            sales_order_doc.save()
            frappe.db.commit()

            return {
                'success': True,
                'message': f'Line {line_no} successfully {action_msg}',
                'data': {
                    'sales_order': sales_order,
                    'line_no': line_no_int,
                    'sales_order_item': item_row_name,
                    'farm': processing_location,
                    'stems': stems_val if action == 'confirm' else 0,
                    'total_confirmed': new_total,
                    'stems_ordered': stems_ordered,
                    'action': action,
                    'updated_by': frappe.session.user,
                    'updated_at': frappe.utils.now(),
                    'confirmed_entries': [
                        {'farm': e.farm, 'stems': e.stems, 'sales_order_item': e.sales_order_item}
                        for e in remaining_for_line
                    ]
                }
            }

        except Exception as e:
            frappe.db.rollback()
            frappe.log_error(f"Processing Location Update Error: {str(e)}")
            frappe.throw(f"Failed to update processing location: {str(e)}")

    def get_current_bookings():
        """Get current booking status for all items in the order, including confirmed stems"""
        try:
            sales_order_doc = frappe.get_doc("Sales Order", sales_order)

            items = []
            for row in sales_order_doc.items:
                # Get confirmed stems for this line from the parent-level child table
                confirmed_stems = [
                    {'farm': e.farm, 'stems': e.stems, 'sales_order_item': e.sales_order_item}
                    for e in (sales_order_doc.custom_confirmed_stems_table or [])
                    if e.sales_order_item == row.name and e.farm and (e.stems or 0) > 0
                ]

                items.append({
                    'line_no': row.idx,
                    'item_row_name': row.name,
                    'item_code': row.item_code,
                    'length': row.custom_length,
                    'qty': row.qty,
                    'custom_ordered_quantity': row.custom_ordered_quantity,
                    'modified': str(row.modified),
                    'modified_by': row.modified_by,
                    'confirmed_stems': confirmed_stems,
                    'total_confirmed': sum(e.get('stems', 0) for e in confirmed_stems)
                })

            return items

        except Exception as e:
            frappe.log_error(f"Get Bookings Error: {str(e)}")
            frappe.throw(f"Failed to retrieve current bookings: {str(e)}")

    # Main execution
    try:
        validate_inputs()

        if action in ['confirm', 'unconfirm']:
            result = update_processing_location()

            # Also return current status of all items for frontend refresh
            current_bookings = get_current_bookings()
            result['current_bookings'] = current_bookings

            frappe.response['message'] = result

        elif action == 'get_confirmations':
            current_bookings = get_current_bookings()
            frappe.response['message'] = {
                'success': True,
                'data': current_bookings
            }
        else:
            frappe.throw("Invalid action. Use 'confirm', 'unconfirm', or 'get_confirmations'")

    except Exception as e:
        frappe.response['message'] = {
            'success': False,
            'error': str(e)
        }
