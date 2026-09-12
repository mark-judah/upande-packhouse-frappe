from collections import defaultdict
import frappe
from frappe.utils import nowdate
from frappe import _
from upande_packhouse.server_scripts.opl_qr_code_gen import generate_qr_code
from upande_packhouse.packing_guide import sync_packing_guide
from upande_packhouse.server_scripts.create_mixed_box_picklist import _generate_box_locations


def _get_shelf_farm_for_location(location):
    """
    Derive the sales-shelf farm for a given location name
    by reading Production Settings shelf_locations.
    Returns the first enabled sales_shelf farm found for that location.
    Falls back to querying Farm doctype if needed.
    """
    if not location:
        return None

    # Get all enabled farms from Production Settings
    ps = frappe.get_cached_doc("Production Settings")
    enabled_sales_farms = [
        row.farm for row in (ps.shelf_locations or [])
        if row.enabled and row.sales_shelf
    ]

    if not enabled_sales_farms:
        return None

    # Find which of those farms belongs to this location
    placeholders = ", ".join(["%s"] * len(enabled_sales_farms))
    result = frappe.db.sql(f"""
        SELECT name FROM `tabFarm`
        WHERE name IN ({placeholders})
          AND location = %s
        LIMIT 1
    """, enabled_sales_farms + [location], as_dict=True)

    # v16 topology: remote locations have no local sales farm. All packing happens
    # at the global sales-shelf farm, so fall back to it (Timau) rather than None —
    # remote buckets are then flagged awaiting_transfer against that farm's shelves.
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

    # Get farms at this location
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


def create_straight_box_pick_list_for_allocated_items(sales_order_doc, allocations, submit=True, location=None):
    """
    Create one OPL per Sales Order Item for straight (non-mixed) box allocations.
    - Derives shelf farm dynamically from Production Settings + Farm location
    - Always saves the OPL as draft first.
    - After save, delegates to _try_submit_opl_if_complete (in sales_allocation.py),
      which checks CUMULATIVE coverage across all OPLs/sessions before submitting.
    - The `submit` arg is now advisory: when False, we skip the auto-submit attempt.
    """

    if not allocations:
        frappe.throw("No allocations provided to create Pick List")

    if sales_order_doc.docstatus != 1:
        frappe.throw("An Order Pick List can only be created for submitted Sales Orders.")

    # Derive shelf farm from location via Production Settings
    shelf_farm = _get_shelf_farm_for_location(location)
    if not shelf_farm:
        shelf_farm = sales_order_doc.farm or "Karen"

    # Get confirmed stems for this location to determine required quantities
    confirmed_stems = _get_confirmed_stems_for_location(sales_order_doc.name, location)

    frappe.log_error(
        title="Straight OPL Creation - Farm",
        message=f"Location: {location} → Shelf Farm: {shelf_farm}"
    )

    # Group allocations by Sales Order Item
    allocations_by_so_item = defaultdict(list)
    for alloc in allocations:
        so_item_name = alloc.get('sales_order_item')
        if not so_item_name:
            frappe.throw("Missing sales_order_item in allocation")
        allocations_by_so_item[so_item_name].append(alloc)

    order_pick_list_names = []

    try:
        for sales_order_item_name, allocs in allocations_by_so_item.items():

            so_item = next(
                (i for i in sales_order_doc.items if i.name == sales_order_item_name), None
            )
            if not so_item:
                frappe.throw(
                    f"Sales Order Item {sales_order_item_name} not found in {sales_order_doc.name}"
                )

            total_allocated_stems = sum(alloc['qty'] for alloc in allocs)

            # Coverage and submission are no longer decided here; the central helper
            # (_try_submit_opl_if_complete) checks cumulative state after save.
            any_awaiting = any(not alloc.get('_is_sales_shelf', 1) for alloc in allocs)

            # Verify shelves exist for the sales shelf farm
            shelf_names = frappe.get_all("Shelf", filters={"farm": shelf_farm}, pluck="name")
            if not shelf_names:
                frappe.throw(f"No shelves found for farm {shelf_farm} (location: {location})")

            order_pick_list = frappe.new_doc("Order Pick List")
            order_pick_list.sales_order = sales_order_doc.name
            order_pick_list.customer = sales_order_doc.customer
            order_pick_list.order_name = sales_order_doc.custom_order_name
            order_pick_list.date_created = nowdate()
            order_pick_list.farm = shelf_farm
            order_pick_list.custom_business_unit = sales_order_doc.business_unit
            order_pick_list.custom_total_stems = total_allocated_stems
            order_pick_list.custom_allocated_pick_list = 1

            # Real box-splitting by packrate capacity, not one row per bucket.
            # This used to just stamp box_id = 1, 2, 3... per bucket regardless
            # of how many stems that bucket held -- so custom_box_id never
            # represented an actual capacity-bound box for a straight line,
            # only a bucket sequence number. _generate_box_locations (shared
            # with mixed box -- it already reads custom_packrate for a
            # non-mixed item) is the same, single splitting engine used
            # everywhere else now: it fills box 1 to custom_packrate stems
            # before starting box 2, throws instead of silently dropping any
            # stems beyond what custom_number_of_boxes x custom_packrate can
            # hold, and its row shelf-lookup has the same farm-then-fallback
            # behaviour this loop used to do by hand.
            combined_allocations_list = [{
                'bucket_id': alloc['bucket_id'],
                'qty': alloc['qty'],
                'warehouse': alloc.get('warehouse') or alloc.get('s_warehouse') or '',
                'stem_length': alloc.get('stem_length'),
                'downgrade_reason': alloc.get('downgrade_reason', ''),
                'available_exact_stems': alloc.get('available_exact_stems', 0),
                '_shelf_farm': alloc.get('_shelf_farm') or shelf_farm,
                '_is_sales_shelf': alloc.get('_is_sales_shelf', 1),
            } for alloc in allocs]

            combined_alloc = {
                'item_code': so_item.item_code,
                'allocations_list': combined_allocations_list,
                'stem_length': allocs[0].get('stem_length') if allocs else None,
            }

            box_locs = _generate_box_locations(
                combined_alloc, so_item, sales_order_doc.name, sales_order_item_name,
                shelf_farm=shelf_farm,
                confirmed_qty=confirmed_stems.get(sales_order_item_name, 0),
            )
            for loc in box_locs:
                order_pick_list.append("table_ytkc", loc)

            # One Sales Order Item = one OPL here, so it's its own group --
            # every box (1..custom_number_of_boxes) is this one variety.
            sync_packing_guide(order_pick_list, [so_item])

            order_pick_list.flags.ignore_permissions = True

            try:
                order_pick_list.save()

                # QR code
                opl_url = f"{frappe.utils.get_url()}/app/order-pick-list/{order_pick_list.name}"
                qr_code_url = generate_qr_code(opl_url, order_pick_list.name)

                order_pick_list = frappe.get_doc("Order Pick List", order_pick_list.name)
                order_pick_list.custom_qr_code = qr_code_url
                order_pick_list.custom_allocated_pick_list = 1
                order_pick_list.save()

                # Link OPL to the SO item BEFORE the submission helper looks at coverage
                frappe.db.set_value(
                    'Sales Order Item', sales_order_item_name,
                    'custom_opl', order_pick_list.name,
                    update_modified=False
                )

                # Delegate submit/draft decision to the central helper.
                # Cumulative coverage across all OPLs/sessions is checked there.
                submitted = False
                if submit:
                    from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
                        _try_submit_opl_if_complete,
                    )
                    confirmed_map = (
                        {sales_order_item_name: confirmed_stems.get(sales_order_item_name)}
                        if confirmed_stems.get(sales_order_item_name)
                        else None
                    )
                    submitted = _try_submit_opl_if_complete(
                        order_pick_list.name, confirmed_by_item=confirmed_map
                    )

                # When the OPL got submitted, mark its rows as ready for packing
                if submitted:
                    frappe.db.sql(
                        "UPDATE `tabPick List Item` SET custom_ready_for_packing = 1 WHERE parent = %s",
                        order_pick_list.name,
                    )
                    frappe.msgprint(
                        f"Pick List {order_pick_list.name} created and submitted.",
                        indicator="green"
                    )
                else:
                    reason = "awaiting transfer" if any_awaiting else "partial allocation"
                    frappe.msgprint(
                        f"Pick List {order_pick_list.name} created as draft ({reason}).",
                        indicator="blue"
                    )

                order_pick_list_names.append(order_pick_list.name)

            except Exception as e:
                frappe.log_error(
                    f"Failed to create OPL for SO Item {sales_order_item_name}: {e}"
                )
                raise

    except Exception as e:
        frappe.log_error(
            f"Error creating straight OPLs for {sales_order_doc.name}: {e}"
        )
        raise

    if order_pick_list_names:
        frappe.msgprint(
            f"Created {len(order_pick_list_names)} Pick List(s) for {sales_order_doc.name}",
            indicator="green"
        )
        try:
            frappe.db.set_value(
                'Sales Order', sales_order_doc.name,
                'custom_opl', ', '.join(order_pick_list_names),
                update_modified=False
            )
        except Exception as e:
            frappe.log_error(f"Failed to update SO custom_opl: {e}")

    return order_pick_list_names
