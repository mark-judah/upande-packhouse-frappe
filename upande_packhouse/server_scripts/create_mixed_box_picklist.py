from collections import defaultdict
import frappe
from frappe.utils import nowdate
from frappe import _


def _get_shelf_farm_for_location(location):
    """
    Derive the sales-shelf farm for a given location name
    by reading Production Settings shelf_locations.
    Returns the first enabled sales_shelf farm found for that location.
    """
    if not location:
        return None

    ps = frappe.get_cached_doc("Production Settings")
    enabled_sales_farms = [
        row.farm for row in (ps.shelf_locations or [])
        if row.enabled and row.sales_shelf
    ]

    if not enabled_sales_farms:
        return None

    placeholders = ", ".join(["%s"] * len(enabled_sales_farms))
    result = frappe.db.sql(f"""
        SELECT name FROM `tabFarm`
        WHERE name IN ({placeholders})
          AND location = %s
        LIMIT 1
    """, enabled_sales_farms + [location], as_dict=True)

    # v16 topology: remote locations have no local sales farm — fall back to the
    # global sales-shelf farm so remote allocations still route to its shelves.
    return result[0]["name"] if result else enabled_sales_farms[0]


def _get_confirmed_stems_for_location(sales_order, location):
    """
    Get confirmed stems for the farms at a given location.
    Returns: { so_item_name: confirmed_stems }
    """
    if not location:
        return {}

    ps = frappe.get_cached_doc("Production Settings")
    enabled_farms = [row.farm for row in (ps.shelf_locations or []) if row.enabled]
    if not enabled_farms:
        return {}

    placeholders = ", ".join(["%s"] * len(enabled_farms))
    farm_rows = frappe.db.sql(f"""
        SELECT name FROM `tabFarm`
        WHERE name IN ({placeholders})
          AND location = %s
    """, enabled_farms + [location], as_dict=True)

    location_farms = [r["name"] for r in farm_rows]
    if not location_farms:
        return {}

    farm_placeholders = ", ".join(["%s"] * len(location_farms))
    rows = frappe.db.sql(f"""
        SELECT cs.sales_order_item, SUM(cs.stems) AS total_stems
        FROM `tabConfirmed Stems` cs
        WHERE cs.parent = %s
          AND cs.parenttype = 'Sales Order'
          AND cs.parentfield = 'custom_confirmed_stems_table'
          AND cs.farm IN ({farm_placeholders})
          AND cs.stems > 0
        GROUP BY cs.sales_order_item
    """, [sales_order] + location_farms, as_dict=True)

    return {r["sales_order_item"]: r["total_stems"] or 0 for r in rows}


def create_mixed_box_pick_list_for_allocated_items(sales_order_doc, allocations, submit=True, location=None):
    """
    Create OPL(s) for allocated MIXED BOX items.
    - Derives shelf farm dynamically from Production Settings + Farm location
    - Uses confirmed stems to determine required quantity (not full order qty)
    - Appends to existing draft OPLs instead of overwriting
    - Sets awaiting_transfer per row for remote-farm buckets
    - OPL stays draft if any row has awaiting_transfer = 1
    """

    if not allocations:
        frappe.throw("No allocations provided to create Pick List")

    if sales_order_doc.docstatus != 1:
        frappe.throw("An Order Pick List can only be created for submitted Sales Orders.")

    # Derive shelf farm from location via Production Settings
    shelf_farm = _get_shelf_farm_for_location(location)
    if not shelf_farm:
        shelf_farm = sales_order_doc.farm or "Karen"

    # Get confirmed stems for this location
    confirmed_stems = _get_confirmed_stems_for_location(sales_order_doc.name, location)

    frappe.log_error(
        title="Mixed Box OPL - Farm",
        message=f"Location: {location} → Shelf Farm: {shelf_farm}"
    )

    shelf_names = frappe.get_all("Shelf", filters={"farm": shelf_farm}, pluck="name")
    if not shelf_names:
        frappe.throw(f"No shelves found for farm {shelf_farm} (location: {location})")

    # ── Group allocations by SO item ──
    allocations_by_so_item = defaultdict(list)
    for alloc in allocations:
        allocations_by_so_item[alloc['sales_order_item']].append(alloc)

    # ── Group by mix_group, mixed boxes only ──
    mixed_box_allocations = defaultdict(list)

    for so_item_name, alloc_list in allocations_by_so_item.items():
        so_item = frappe.get_doc("Sales Order Item", so_item_name)

        if so_item.get('custom_mixed_box') != 1:
            continue

        mix_group = so_item.get('custom_mix_group')
        if not mix_group:
            frappe.throw(f"Mixed box item {so_item.item_code} is missing mix_group")

        total_qty = sum(a['qty'] for a in alloc_list)

        allocations_list = []
        for alloc in alloc_list:
            wh = alloc.get('warehouse') or alloc.get('s_warehouse') or ''
            allocations_list.append({
                'bucket_id': alloc['bucket_id'],
                'qty': alloc['qty'],
                'warehouse': wh,
                'stem_length': alloc.get('stem_length'),
                'harvest_batch_no': alloc.get('harvest_batch_no'),
                'downgrade_reason': alloc.get('downgrade_reason', ''),
                'available_exact_stems': alloc.get('available_exact_stems', 0),
                '_shelf_farm': alloc.get('_shelf_farm') or shelf_farm,
                '_is_sales_shelf': alloc.get('_is_sales_shelf', 1),
            })

        mixed_box_allocations[mix_group].append({
            'alloc': {
                'sales_order_item': so_item_name,
                'item_code': so_item.item_code,
                'qty': total_qty,
                'allocations_list': allocations_list,
                'uom': so_item.uom,
                'stock_uom': so_item.stock_uom,
                'conversion_factor': so_item.conversion_factor,
                'stem_length': alloc_list[0].get('stem_length') if alloc_list else None,
                'downgrade_reason': alloc_list[0].get('downgrade_reason', '') if alloc_list else '',
                'available_exact_stems': alloc_list[0].get('available_exact_stems', 0) if alloc_list else 0,
            },
            'so_item': so_item,
            'sales_order_item_name': so_item_name,
        })

    order_pick_list_names = []

    for mix_group, box_items in mixed_box_allocations.items():

        # Check if any allocation in this group is from a non-sales-shelf farm
        any_awaiting = any(
            not alloc_entry.get('_is_sales_shelf', 1)
            for item in box_items
            for alloc_entry in item['alloc'].get('allocations_list', [])
        )

        all_items_in_mix = frappe.get_all(
            "Sales Order Item",
            filters={
                "parent": sales_order_doc.name,
                "custom_mix_group": mix_group,
                "custom_mixed_box": 1
            },
            fields=["name", "item_code", "item_name", "qty", "delivered_qty", "conversion_factor", "stock_qty"]
        )

        existing_opl = frappe.get_all(
            "Order Pick List",
            filters={
                "sales_order": sales_order_doc.name,
                "mix_group": mix_group,
                "docstatus": ["!=", 2]
            },
            order_by="creation desc",
            limit=1
        )

        if existing_opl:
            opl_name = existing_opl[0]['name']
            order_pick_list = frappe.get_doc("Order Pick List", opl_name)

            if order_pick_list.docstatus != 0:
                frappe.msgprint(f"OPL {opl_name} already submitted", indicator="orange")
                order_pick_list_names.append(opl_name)
                continue

            # Remove rows for items in current batch, keep others
            current_batch_so_items = {item['sales_order_item_name'] for item in box_items}
            filtered_locations = [
                loc for loc in order_pick_list.table_ytkc
                if loc.sales_order_item not in current_batch_so_items
            ]
            order_pick_list.table_ytkc = []
            for loc in filtered_locations:
                order_pick_list.append("table_ytkc", loc)

            # Append new rows
            for item in box_items:
                box_locs = _generate_box_locations(
                    item['alloc'], item['so_item'],
                    sales_order_doc.name, item['sales_order_item_name'],
                    shelf_farm=shelf_farm,
                    confirmed_qty=confirmed_stems.get(item['sales_order_item_name'], 0)
                )
                for loc in box_locs:
                    order_pick_list.append("table_ytkc", loc)

            total_stems = sum(loc.stock_qty for loc in order_pick_list.table_ytkc)
            order_pick_list.custom_total_stems = total_stems
            order_pick_list.flags.ignore_permissions = True
            order_pick_list.save()

            # Update SO item links BEFORE the helper checks cumulative coverage
            for item in box_items:
                frappe.db.set_value(
                    'Sales Order Item', item['sales_order_item_name'],
                    'custom_opl', opl_name,
                    update_modified=False
                )

            # Delegate submit decision to the central helper. It checks cumulative
            # coverage across all OPLs/sessions for every SO item in this mix.
            from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
                _try_submit_opl_if_complete,
            )
            confirmed_map = {
                item['sales_order_item_name']: confirmed_stems.get(item['sales_order_item_name'])
                for item in box_items
                if confirmed_stems.get(item['sales_order_item_name'])
            }
            submitted = _try_submit_opl_if_complete(opl_name, confirmed_by_item=confirmed_map or None)

            if submitted:
                frappe.msgprint(f"OPL {opl_name} submitted (fully allocated)", indicator="green")
            else:
                reason = "awaiting transfer" if any_awaiting else "partial allocation"
                frappe.msgprint(f"OPL {opl_name} updated (draft — {reason})", indicator="blue")

            order_pick_list_names.append(opl_name)

        else:
            # Create new OPL
            order_pick_list = frappe.new_doc("Order Pick List")
            order_pick_list.sales_order = sales_order_doc.name
            order_pick_list.customer = sales_order_doc.customer
            order_pick_list.order_name = sales_order_doc.custom_order_name
            order_pick_list.date_created = nowdate()
            order_pick_list.farm = shelf_farm
            order_pick_list.custom_business_unit = sales_order_doc.business_unit
            order_pick_list.mix_group = mix_group
            order_pick_list.custom_allocated_pick_list = 1

            total_stems = sum(item['alloc']['qty'] for item in box_items)
            order_pick_list.custom_total_stems = total_stems

            for item in box_items:
                box_locs = _generate_box_locations(
                    item['alloc'], item['so_item'],
                    sales_order_doc.name, item['sales_order_item_name'],
                    shelf_farm=shelf_farm,
                    confirmed_qty=confirmed_stems.get(item['sales_order_item_name'], 0)
                )
                for loc in box_locs:
                    order_pick_list.append("table_ytkc", loc)

            order_pick_list.flags.ignore_permissions = True
            order_pick_list.save()

            # Update SO item links BEFORE the helper checks cumulative coverage
            for item in box_items:
                frappe.db.set_value(
                    'Sales Order Item', item['sales_order_item_name'],
                    'custom_opl', order_pick_list.name,
                    update_modified=False
                )

            # Delegate submit decision to the central helper
            from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
                _try_submit_opl_if_complete,
            )
            confirmed_map = {
                item['sales_order_item_name']: confirmed_stems.get(item['sales_order_item_name'])
                for item in box_items
                if confirmed_stems.get(item['sales_order_item_name'])
            }
            submitted = _try_submit_opl_if_complete(
                order_pick_list.name, confirmed_by_item=confirmed_map or None
            )

            if submitted:
                frappe.msgprint(f"OPL {order_pick_list.name} created and submitted", indicator="green")
            else:
                reason = "awaiting transfer" if any_awaiting else "partial allocation"
                frappe.msgprint(f"OPL {order_pick_list.name} created (draft — {reason})", indicator="blue")

            order_pick_list_names.append(order_pick_list.name)

    return order_pick_list_names[0] if order_pick_list_names else None


def _check_mix_group_complete(order_pick_list, all_items_in_mix, confirmed_stems=None):
    """
    Returns True if all items in the mix group are fully allocated in the OPL.
    Uses confirmed stems as the target if available.
    """
    item_totals = defaultdict(float)
    for loc in order_pick_list.table_ytkc:
        if loc.sales_order_item:
            item_totals[loc.sales_order_item] += (loc.stock_qty or 0)

    for item_info in all_items_in_mix:
        # Use confirmed stems if available, otherwise full order qty
        confirmed_qty = confirmed_stems.get(item_info['name'], 0) if confirmed_stems else 0
        if confirmed_qty > 0:
            required = confirmed_qty
        else:
            required = (item_info['qty'] - item_info['delivered_qty']) * (item_info['conversion_factor'] or 1)

        allocated = item_totals.get(item_info['name'], 0)
        if abs(allocated - required) > 1:
            return False
    return True


def _generate_box_locations(alloc, so_item, sales_order_name, sales_order_item_name, shelf_farm=None, confirmed_qty=0):
    """
    Split a single allocation into box-level OPL rows based on packrate.
    Handles multi-bucket allocations and sets awaiting_transfer per row.

    When confirmed_qty > 0, uses it as the target instead of packrate * num_boxes.
    This supports partial confirmation where a location only handles a portion of the order.
    """
    item_code = alloc['item_code']
    allocations_list = alloc.get('allocations_list', [])

    if not allocations_list:
        wh = alloc.get('warehouse') or alloc.get('s_warehouse') or ''
        allocations_list = [{
            'bucket_id': alloc.get('bucket_id'),
            'qty': alloc['qty'],
            'warehouse': wh,
            'stem_length': alloc.get('stem_length'),
            'downgrade_reason': alloc.get('downgrade_reason', ''),
            'available_exact_stems': alloc.get('available_exact_stems', 0),
            '_shelf_farm': shelf_farm,
            '_is_sales_shelf': 1,
        }]

    if so_item.get('custom_mixed_box') == 1:
        stems_per_box = int(so_item.get("custom_packrate_mixed_box") or 10)
        num_boxes = int(so_item.get("custom_number_of_boxes") or 1)
    else:
        stems_per_box = int(so_item.get("custom_packrate") or 10)
        num_boxes = int(so_item.get("custom_number_of_boxes") or 1)

    conversion_factor = so_item.conversion_factor or 1
    sales_uom = so_item.uom
    stock_uom = so_item.stock_uom

    total_stems_needed = stems_per_box * num_boxes
    total_allocated = sum(a['qty'] for a in allocations_list)

    # If confirmed_qty is set, use it as the target instead of full order
    # This handles partial confirmations where a location only manages a portion
    if confirmed_qty > 0 and confirmed_qty < total_stems_needed:
        # Partial confirmation — don't validate against full box count
        # Instead, create rows per bucket without the box-splitting logic
        # since the location may not fill complete boxes
        total_stems_needed = confirmed_qty

    if total_allocated < total_stems_needed - 0.001:
        frappe.log_error(
            title="Box Location - Allocation Check",
            message=f"Item: {item_code}, Allocated: {total_allocated}, "
                    f"Needed: {total_stems_needed}, Confirmed: {confirmed_qty}, "
                    f"Full order: {stems_per_box * num_boxes}"
        )
        # For partial confirmations, allow the allocation if it matches what was allocated
        # (the validation already happened in allocate_stock_with_buckets)
        if confirmed_qty > 0:
            total_stems_needed = total_allocated
        else:
            frappe.throw(
                f"Insufficient allocation for {item_code}: "
                f"allocated {total_allocated}, needed {total_stems_needed}"
            )

    # Determine if we should do box-level splitting or flat rows
    # Box splitting only makes sense when we have enough stems for complete boxes
    use_box_splitting = total_allocated >= stems_per_box and confirmed_qty == 0

    if use_box_splitting:
        # Original box-splitting logic for full allocations
        return _generate_box_locations_with_splitting(
            alloc, so_item, sales_order_name, sales_order_item_name,
            allocations_list, stems_per_box, num_boxes, conversion_factor,
            sales_uom, stock_uom, shelf_farm, item_code
        )
    else:
        # Flat rows — one row per bucket contribution, no box splitting
        # Used for partial confirmations
        return _generate_flat_locations(
            alloc, so_item, sales_order_name, sales_order_item_name,
            allocations_list, conversion_factor, sales_uom, stock_uom,
            shelf_farm, item_code
        )


def _generate_flat_locations(alloc, so_item, sales_order_name, sales_order_item_name,
                              allocations_list, conversion_factor, sales_uom, stock_uom,
                              shelf_farm, item_code):
    """Generate one OPL row per bucket — no box splitting. For partial allocations."""
    locations = []
    box_counter = 1

    for alloc_entry in allocations_list:
        bucket_id = alloc_entry['bucket_id']
        stems = alloc_entry['qty']
        actual_farm = alloc_entry.get('_shelf_farm') or shelf_farm
        is_sales_shelf = alloc_entry.get('_is_sales_shelf', 1)
        awaiting_transfer = 0 if is_sales_shelf else 1

        shelf_str = _lookup_shelf(actual_farm, item_code, bucket_id, shelf_farm)

        loc = {
            "item_code": item_code,
            "bucket": bucket_id,
            "custom_sale_order_item": sales_order_item_name,
            "item_name": so_item.item_name,
            "stock_uom": stock_uom,
            "uom": sales_uom,
            "qty": stems / conversion_factor,
            "stem_length": alloc.get('stem_length') or so_item.custom_length,
            "stock_qty": stems,
            "conversion_factor": conversion_factor,
            "source_warehouse": alloc_entry.get('warehouse') or '',
            "sales_order_item": sales_order_item_name,
            "farm": actual_farm,
            "custom_consignee": so_item.get("custom_consignee"),
            "custom_box_label": so_item.get("custom_box_label"),
            "transit_truck": so_item.get("custom_truck"),
            "custom_box_id": box_counter,
            "packrate": (
                so_item.get("custom_packrate_mixed_box")
                if so_item.get('custom_mixed_box') == 1
                else so_item.get("custom_packrate")
            ),
            "custom_flower_food": so_item.get("custom_flower_food"),
            "custom_ready_for_packing": 0,
            "shelf": shelf_str,
            "downgrade_reason": alloc_entry.get('downgrade_reason', ''),
            "available_stems_of_exact_length": alloc_entry.get('available_exact_stems', 0),
            "awaiting_transfer": awaiting_transfer,
        }

        locations.append(loc)
        box_counter += 1

    return locations


def _generate_box_locations_with_splitting(alloc, so_item, sales_order_name, sales_order_item_name,
                                            allocations_list, stems_per_box, num_boxes, conversion_factor,
                                            sales_uom, stock_uom, shelf_farm, item_code):
    """Original box-splitting logic for full allocations."""
    locations = []
    bucket_index = 0
    current_bucket_remaining = 0
    current_bucket = None

    for box_num in range(1, num_boxes + 1):
        stems_needed = stems_per_box
        contributions = []

        while stems_needed > 0:
            if current_bucket_remaining == 0:
                if bucket_index >= len(allocations_list):
                    break
                current_bucket = allocations_list[bucket_index]
                current_bucket_remaining = current_bucket['qty']
                bucket_index += 1

            take = min(stems_needed, current_bucket_remaining)
            contributions.append({
                'bucket_id': current_bucket['bucket_id'],
                'stems': take,
                'warehouse': current_bucket.get('warehouse') or current_bucket.get('s_warehouse') or '',
                'downgrade_reason': current_bucket.get('downgrade_reason', ''),
                'available_exact_stems': current_bucket.get('available_exact_stems', 0),
                '_shelf_farm': current_bucket.get('_shelf_farm') or shelf_farm,
                '_is_sales_shelf': current_bucket.get('_is_sales_shelf', 1),
            })
            stems_needed -= take
            current_bucket_remaining -= take

        if stems_needed > 0:
            frappe.throw(f"Cannot fill box {box_num} for {item_code}: missing {stems_needed} stems.")

        for contrib in contributions:
            bucket_id = contrib['bucket_id']
            actual_farm = contrib.get('_shelf_farm') or shelf_farm
            is_sales_shelf = contrib.get('_is_sales_shelf', 1)
            awaiting_transfer = 0 if is_sales_shelf else 1

            shelf_str = _lookup_shelf(actual_farm, item_code, bucket_id, shelf_farm)

            loc = {
                "item_code": item_code,
                "bucket": bucket_id,
                "custom_sale_order_item": sales_order_item_name,
                "item_name": so_item.item_name,
                "stock_uom": stock_uom,
                "uom": sales_uom,
                "qty": contrib['stems'] / conversion_factor,
                "stem_length": alloc.get('stem_length') or so_item.custom_length,
                "stock_qty": contrib['stems'],
                "conversion_factor": conversion_factor,
                "source_warehouse": contrib['warehouse'],
                "sales_order_item": sales_order_item_name,
                "farm": actual_farm,
                "custom_consignee": so_item.get("custom_consignee"),
                "custom_box_label": so_item.get("custom_box_label"),
                "transit_truck": so_item.get("custom_truck"),
                "custom_box_id": box_num,
                "packrate": (
                    so_item.get("custom_packrate_mixed_box")
                    if so_item.get('custom_mixed_box') == 1
                    else so_item.get("custom_packrate")
                ),
                "custom_flower_food": so_item.get("custom_flower_food"),
                "custom_ready_for_packing": 0,
                "shelf": shelf_str,
                "downgrade_reason": contrib.get('downgrade_reason', ''),
                "available_stems_of_exact_length": contrib.get('available_exact_stems', 0),
                "awaiting_transfer": awaiting_transfer,
            }

            locations.append(loc)

    return locations


def _lookup_shelf(actual_farm, item_code, bucket_id, fallback_farm):
    """Look up shelf for a bucket, trying actual farm first then fallback."""
    matching_shelves = frappe.db.sql("""
        SELECT s.name AS shelf
        FROM `tabShelf Item` si
        INNER JOIN `tabShelf` s ON s.name = si.parent
        WHERE s.farm = %s AND si.variety = %s AND si.bucket_id = %s
        LIMIT 3
    """, [actual_farm, item_code, bucket_id], as_dict=True)

    if not matching_shelves and actual_farm != fallback_farm:
        matching_shelves = frappe.db.sql("""
            SELECT s.name AS shelf
            FROM `tabShelf Item` si
            INNER JOIN `tabShelf` s ON s.name = si.parent
            WHERE s.farm = %s AND si.variety = %s AND si.bucket_id = %s
            LIMIT 3
        """, [fallback_farm, item_code, bucket_id], as_dict=True)

    if not matching_shelves:
        matching_shelves = frappe.db.sql("""
            SELECT s.name AS shelf
            FROM `tabShelf Item` si
            INNER JOIN `tabShelf` s ON s.name = si.parent
            WHERE si.bucket_id = %s
            LIMIT 3
        """, [bucket_id], as_dict=True)

    if not matching_shelves:
        frappe.throw(
            f"Bucket '{bucket_id}' for item '{item_code}' not found on any shelf. "
            "Please refresh and try again."
        )

    return ", ".join(s["shelf"] for s in matching_shelves)
