"""Box Label generation -- the step between a fully-packed Farm Pack List
and staging/loading. Nothing in this codebase ever created a Box Label
before this (confirmed: every reference elsewhere is get_doc/exists/
get_value/set_value against an ALREADY-EXISTING one) -- staging
(createStagingEntry), loading (createLoadingEntry) and dispatch
(createOrUpdateDispatch) all assume Box Labels already exist, but nothing
ever made that first one real.

One Box Label per physical box (box_number), sourced from the Farm Pack
List's OWN packed rows (ground truth of what was actually packed), not the
Packing Guide plan -- so a label always reflects reality even if a pack
deviated slightly from plan. Header fields (consignee/delivery point/
freight agent/truck details) are carried straight from the Sales Order,
mirroring exactly what createOrUpdateDispatch already does when it later
builds the Delivery Note from these same fields -- so a box label and the
delivery note it feeds never disagree.
"""

import frappe


def sync_box_labels_for_fpl(fpl_doc, opl_doc, so_doc):
    """Create (or refresh, pre-staging) one Box Label per box_number packed
    on this Farm Pack List. Idempotent: an existing label for a box is
    updated in place rather than duplicated, but only while it's still
    untouched by the physical flow (not yet staged/loaded/delivered) --
    once a box has left packing, its label is left alone even if the FPL
    is amended.
    """
    by_box = {}
    for row in fpl_doc.pack_list_item:
        box_no = int(row.box_id or 0) or 1
        by_box.setdefault(box_no, []).append(row)

    total_boxes = len(by_box)
    farm_code = frappe.db.get_value("Farm", fpl_doc.farm, "farm_code") if fpl_doc.farm else None

    created, updated, skipped = [], [], []

    for box_no, rows in by_box.items():
        name = "BOX-{0}-{1}".format(opl_doc.name, box_no)
        existing = frappe.db.exists("Box Label", name)
        if existing:
            box = frappe.get_doc("Box Label", name)
            if box.staged or box.loaded or box.delivered:
                # Already moving through the physical flow -- a re-pack/
                # amendment must not silently rewrite a label that may
                # already be printed and stuck on a real box.
                skipped.append(name)
                continue
        else:
            box = frappe.new_doc("Box Label")
            box.order_pick_list = opl_doc.name
            box.box_number = box_no

        total_stems = sum(int(r.stock_qty or 0) for r in rows)

        box.farm = fpl_doc.farm
        box.farm_code = farm_code
        box.customer = fpl_doc.customer
        box.length = rows[0].stem_length
        box.pack_rate = total_stems
        box.farm_pack_lis = fpl_doc.name
        box.date = frappe.utils.today()
        # customer_purchase_order is (despite its label) the field
        # createOrUpdateDispatch already reads as the SALES ORDER name when
        # grouping loaded boxes into a Delivery Note -- match that, don't
        # invent a second convention.
        box.customer_purchase_order = so_doc.name
        box.consignee = so_doc.get("custom_consignee")
        box.truck_details = so_doc.get("custom_truck_details")
        box.freight_agent = so_doc.get("custom_shipping_agent")
        box.delivery_point = so_doc.get("custom_delivery_point")
        box.box_total_count = total_boxes

        box.set("box_item", [])
        for r in rows:
            box.append("box_item", {
                "variety": r.item_code,
                "qty": r.bunch_qty,
                "uom": r.bunch_uom,
                "length": r.stem_length,
                "source_farm": fpl_doc.farm,
            })

        box.flags.ignore_permissions = True
        if existing:
            box.save(ignore_permissions=True)
            updated.append(box.name)
        else:
            box.insert(ignore_permissions=True)
            created.append(box.name)

    return {"created": created, "updated": updated, "skipped": skipped}
