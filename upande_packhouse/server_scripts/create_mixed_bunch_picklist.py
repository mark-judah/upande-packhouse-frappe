from collections import defaultdict
import frappe
from frappe.utils import nowdate

# Mixed BUNCH pick-list builder — sibling of create_mixed_box_picklist.py.
#
# A "mixed bunch" is a bouquet whose single bunch is composed of several
# colour/variety components (each with its own stems_per_bunch), defined on the
# Specification's box_items rows where bunch_type == "Mixed Bunch".
#
# This mirrors the mixed-BOX flow (group the SO Item colour-lines that share a
# bunch_group into one OPL, split into box-level rows, submit when the
# whole group is cumulatively allocated) with two differences:
#   1. stems-per-box comes from custom_packrate_mixed_box (as for mixed boxes).
#   2. the OPL's bouquet packing guide (table_nade) child table is populated
#      from the spec so packers know the per-bunch composition
#      (colour · variety · stems).
#
# The assortment-agnostic farm/shelf/confirmed helpers are reused (not copied)
# from create_mixed_box_picklist so there is a single source of truth for them.

from upande_packhouse.server_scripts.create_mixed_box_picklist import (
    _get_shelf_farm_for_location,
    _get_confirmed_stems_for_location,
    _lookup_shelf,
)


def _spec_bunch_rows(spec_name):
    """Mixed-bunch composition rows from a Specification:
    [{colour, variety, stems_per_bunch, length}, ...]."""
    if not spec_name:
        return []
    try:
        spec = frappe.get_doc("Specifications", spec_name)
    except Exception:
        return []
    rows = []
    for bi in (spec.box_items or []):
        if bi.get("bunch_type") == "Mixed Bunch" and bi.get("variety"):
            rows.append({
                "colour": bi.get("colour"),
                "variety": bi.get("variety"),
                "stems_per_bunch": int(bi.get("stems_per_bunch") or 0),
                "length": bi.get("length"),
            })
    return rows


def _populate_bouquet_guide(order_pick_list, spec_name):
    """(Re)fill the OPL bouquet packing guide from the spec's mixed-bunch rows."""
    rows = _spec_bunch_rows(spec_name)
    if not rows:
        return
    order_pick_list.set("table_nade", [])
    for r in rows:
        order_pick_list.append("table_nade", r)


def create_mixed_bunch_pick_list_for_allocated_items(sales_order_doc, allocations, submit=True, location=None):
    """Create OPL(s) for allocated MIXED BUNCH items (grouped by custom_bunch_group)."""

    if not allocations:
        frappe.throw("No allocations provided to create Pick List")

    if sales_order_doc.docstatus != 1:
        frappe.throw("An Order Pick List can only be created for submitted Sales Orders.")

    shelf_farm = _get_shelf_farm_for_location(location)
    if not shelf_farm:
        shelf_farm = sales_order_doc.custom_farm or "Karen"

    confirmed_stems = _get_confirmed_stems_for_location(sales_order_doc.name, location)

    shelf_names = frappe.get_all("Shelf", filters={"farm": shelf_farm}, pluck="name")
    if not shelf_names:
        frappe.throw(f"No shelves found for farm {shelf_farm} (location: {location})")

    # ── Group allocations by SO item ──
    allocations_by_so_item = defaultdict(list)
    for alloc in allocations:
        allocations_by_so_item[alloc['sales_order_item']].append(alloc)

    # ── Group by bunch_group, mixed bunches only ──
    bunch_allocations = defaultdict(list)

    for so_item_name, alloc_list in allocations_by_so_item.items():
        so_item = frappe.get_doc("Sales Order Item", so_item_name)

        if so_item.get('custom_mixed_bunch') != 1:
            continue

        bunch_group = so_item.get('custom_bunch_group') or so_item.get('custom_line')
        if not bunch_group:
            frappe.throw(f"Mixed bunch item {so_item.item_code} is missing bunch_group")

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

        bunch_allocations[bunch_group].append({
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

    for bunch_group, box_items in bunch_allocations.items():

        any_awaiting = any(
            not alloc_entry.get('_is_sales_shelf', 1)
            for item in box_items
            for alloc_entry in item['alloc'].get('allocations_list', [])
        )

        # The bouquet recipe comes from the spec (custom_line) shared by the group's lines.
        spec_name = box_items[0]['so_item'].get('custom_line')

        existing_opl = frappe.get_all(
            "Order Pick List",
            filters={
                "sales_order": sales_order_doc.name,
                "bunch_group": bunch_group,
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

            for item in box_items:
                box_locs = _generate_bunch_locations(
                    item['alloc'], item['so_item'],
                    sales_order_doc.name, item['sales_order_item_name'],
                    shelf_farm=shelf_farm,
                    confirmed_qty=confirmed_stems.get(item['sales_order_item_name'], 0)
                )
                for loc in box_locs:
                    order_pick_list.append("table_ytkc", loc)

            _populate_bouquet_guide(order_pick_list, spec_name)

            total_stems = sum(loc.stock_qty for loc in order_pick_list.table_ytkc)
            order_pick_list.custom_total_stems = total_stems
            order_pick_list.flags.ignore_permissions = True
            order_pick_list.save()

            for item in box_items:
                frappe.db.set_value(
                    'Sales Order Item', item['sales_order_item_name'],
                    'custom_opl', opl_name,
                    update_modified=False
                )

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
            order_pick_list = frappe.new_doc("Order Pick List")
            order_pick_list.sales_order = sales_order_doc.name
            order_pick_list.customer = sales_order_doc.customer
            order_pick_list.order_name = sales_order_doc.custom_order_name
            order_pick_list.date_created = nowdate()
            order_pick_list.farm = shelf_farm
            order_pick_list.custom_business_unit = sales_order_doc.custom_business_unit
            order_pick_list.bunch_group = bunch_group
            order_pick_list.custom_allocated_pick_list = 1

            total_stems = sum(item['alloc']['qty'] for item in box_items)
            order_pick_list.custom_total_stems = total_stems

            for item in box_items:
                box_locs = _generate_bunch_locations(
                    item['alloc'], item['so_item'],
                    sales_order_doc.name, item['sales_order_item_name'],
                    shelf_farm=shelf_farm,
                    confirmed_qty=confirmed_stems.get(item['sales_order_item_name'], 0)
                )
                for loc in box_locs:
                    order_pick_list.append("table_ytkc", loc)

            _populate_bouquet_guide(order_pick_list, spec_name)

            order_pick_list.flags.ignore_permissions = True
            order_pick_list.save()

            for item in box_items:
                frappe.db.set_value(
                    'Sales Order Item', item['sales_order_item_name'],
                    'custom_opl', order_pick_list.name,
                    update_modified=False
                )

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


def _generate_bunch_locations(alloc, so_item, sales_order_name, sales_order_item_name, shelf_farm=None, confirmed_qty=0):
    """Split a single mixed-bunch allocation into box-level OPL rows.
    Uses custom_packrate_mixed_box as stems-per-box (like mixed boxes)."""
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

    stems_per_box = int(so_item.get("custom_packrate_mixed_box") or 10)
    num_boxes = int(so_item.get("custom_number_of_boxes") or 1)

    conversion_factor = so_item.conversion_factor or 1
    sales_uom = so_item.uom
    stock_uom = so_item.stock_uom

    total_stems_needed = stems_per_box * num_boxes
    total_allocated = sum(a['qty'] for a in allocations_list)

    if confirmed_qty > 0 and confirmed_qty < total_stems_needed:
        total_stems_needed = confirmed_qty

    if total_allocated < total_stems_needed - 0.001:
        frappe.log_error(
            title="Bunch Location - Allocation Check",
            message=f"Item: {item_code}, Allocated: {total_allocated}, "
                    f"Needed: {total_stems_needed}, Confirmed: {confirmed_qty}, "
                    f"Full order: {stems_per_box * num_boxes}"
        )
        if confirmed_qty > 0:
            total_stems_needed = total_allocated
        else:
            frappe.throw(
                f"Insufficient allocation for {item_code}: "
                f"allocated {total_allocated}, needed {total_stems_needed}"
            )

    use_box_splitting = total_allocated >= stems_per_box and confirmed_qty == 0

    if use_box_splitting:
        return _generate_bunch_locations_with_splitting(
            alloc, so_item, sales_order_name, sales_order_item_name,
            allocations_list, stems_per_box, num_boxes, conversion_factor,
            sales_uom, stock_uom, shelf_farm, item_code
        )
    else:
        return _generate_bunch_flat_locations(
            alloc, so_item, sales_order_name, sales_order_item_name,
            allocations_list, conversion_factor, sales_uom, stock_uom,
            shelf_farm, item_code
        )


def _bunch_row(so_item, item_code, sales_order_name, sales_order_item_name, alloc,
               bucket_id, stems, warehouse, downgrade_reason, available_exact_stems,
               awaiting_transfer, shelf_str, actual_farm, conversion_factor, sales_uom, stock_uom, box_id):
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
        "source_warehouse": warehouse or '',
        "sales_order_item": sales_order_item_name,
        "farm": actual_farm,
        "custom_consignee": so_item.get("custom_consignee"),
        "custom_box_label": so_item.get("custom_box_label"),
        "transit_truck": so_item.get("custom_truck"),
        "custom_box_id": box_id,
        # Mixed bunches use the mixed-box packrate field.
        "packrate": so_item.get("custom_packrate_mixed_box"),
        "custom_flower_food": so_item.get("custom_flower_food"),
        "custom_ready_for_packing": 0,
        "shelf": shelf_str,
        "downgrade_reason": downgrade_reason or '',
        "available_stems_of_exact_length": available_exact_stems or 0,
        "awaiting_transfer": awaiting_transfer,
    }
    return loc


def _generate_bunch_flat_locations(alloc, so_item, sales_order_name, sales_order_item_name,
                                   allocations_list, conversion_factor, sales_uom, stock_uom,
                                   shelf_farm, item_code):
    """One OPL row per bucket — no box splitting. For partial allocations."""
    locations = []
    box_counter = 1
    for alloc_entry in allocations_list:
        bucket_id = alloc_entry['bucket_id']
        stems = alloc_entry['qty']
        actual_farm = alloc_entry.get('_shelf_farm') or shelf_farm
        is_sales_shelf = alloc_entry.get('_is_sales_shelf', 1)
        awaiting_transfer = 0 if is_sales_shelf else 1
        shelf_str = _lookup_shelf(actual_farm, item_code, bucket_id, shelf_farm)
        locations.append(_bunch_row(
            so_item, item_code, sales_order_name, sales_order_item_name, alloc,
            bucket_id, stems, alloc_entry.get('warehouse'),
            alloc_entry.get('downgrade_reason', ''), alloc_entry.get('available_exact_stems', 0),
            awaiting_transfer, shelf_str, actual_farm, conversion_factor, sales_uom, stock_uom, box_counter
        ))
        box_counter += 1
    return locations


def _generate_bunch_locations_with_splitting(alloc, so_item, sales_order_name, sales_order_item_name,
                                             allocations_list, stems_per_box, num_boxes, conversion_factor,
                                             sales_uom, stock_uom, shelf_farm, item_code):
    """Box-splitting logic for full allocations — fills boxes across buckets."""
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
            locations.append(_bunch_row(
                so_item, item_code, sales_order_name, sales_order_item_name, alloc,
                bucket_id, contrib['stems'], contrib['warehouse'],
                contrib.get('downgrade_reason', ''), contrib.get('available_exact_stems', 0),
                awaiting_transfer, shelf_str, actual_farm, conversion_factor, sales_uom, stock_uom, box_num
            ))

    return locations
