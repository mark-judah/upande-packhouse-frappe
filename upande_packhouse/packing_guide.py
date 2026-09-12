"""Builds the Packing Guide -- one row per (box, variety) -- straight off the
Sales Order itself, not off allocation's bucket-sourcing details. Allocation
is simply the trigger (call sync_packing_guide once stock is confirmed for a
line); the CONTENT is fully deterministic from the order: how many boxes,
what packrate, what variety/colour goes in each. This is the single source
of truth packing should read instead of recomputing box math -- see
mobile/api.py's get_pick_list_with_farm_pack_list, which used to reinvent
this from scratch (and inconsistently) every time it ran.

Reuses sales_order_engine's own packrate/UOM helpers so this can never
disagree with what the Sales Order itself already computed and validated at
save time (_line_packrate, _uom_factor) -- one formula, not a second one.
"""

import frappe

from upande_packhouse.sales_order_engine import _line_packrate, _uom_factor


def _box_kind(it):
    if it.get("custom_mixed_bunch"):
        return "Mixed Bunch"
    if it.get("custom_mixed_box"):
        return "Mixed Box"
    return "Straight Box"


def _group_key(it):
    """Mixed lines are grouped (and must share one box_number sequence) by
    bunch_group first, then mix_group -- a line can't be both, but this
    matches _box_kind's own precedence."""
    if it.get("custom_mixed_bunch"):
        return it.get("custom_bunch_group") or it.get("custom_line") or it.name
    if it.get("custom_mixed_box"):
        return it.get("custom_mix_group") or it.name
    return it.name  # straight: each line is its own group, never shared


def _rows_per_item(it):
    """One Packing Guide row's worth of numbers for this Sales Order Item --
    everything EXCEPT which box_number it lands on (that's assigned by the
    caller, per group, so mixed colours line up across boxes)."""
    pack_rate = _line_packrate(it)
    stems_per_bunch = _uom_factor(it.uom) or 0
    num_boxes = int(it.get("custom_number_of_boxes") or 0)

    if not pack_rate or not stems_per_bunch or not num_boxes:
        frappe.throw(
            frappe._(
                "Cannot build the Packing Guide for {0}: Packrate, Stems/Bunch (from UOM) and "
                "Number of Boxes must all be set. Fix the Sales Order line first."
            ).format(frappe.bold(it.item_code)),
            title=frappe._("Incomplete Line"),
        )

    if pack_rate % stems_per_bunch != 0:
        frappe.throw(
            frappe._(
                "{0}'s Packrate ({1} stems/box) is not a whole number of {2}-stem bunches -- "
                "the box math doesn't divide evenly. Fix the Packrate or the bunch size before "
                "packing can be planned."
            ).format(frappe.bold(it.item_code), pack_rate, stems_per_bunch),
            title=frappe._("Packrate / Bunch Size Mismatch"),
        )

    bunches = pack_rate // stems_per_bunch
    return {
        "box_kind": _box_kind(it),
        "sales_order_item": it.name,
        "colour": frappe.db.get_value("Item", it.item_code, "custom_color"),
        "variety": it.item_code,
        "stems_per_bunch": stems_per_bunch,
        "bunches": bunches,
        # Computed here, not left to PackingGuide.validate() -- a child row
        # appended via `parent.append(...)` doesn't get its own controller's
        # validate() called just because the parent later saves (confirmed:
        # it stayed 0 until set explicitly). validate() still recomputes it
        # too, as a defensive backstop for anyone editing bunches by hand
        # in the Desk grid afterwards.
        "stems": bunches * stems_per_bunch,
        "length": it.get("custom_length"),
        "pack_rate": pack_rate,
        "box_type": it.get("custom_box_type"),
    }, num_boxes


def sync_packing_guide(order_pick_list, so_items):
    """(Re)build every Packing Guide row this OPL should have for `so_items`
    (a list of Sales Order Item docs/dicts, all belonging to the same OPL --
    for a straight line this is a single item; for a mixed box/bunch group
    it's every colour line sharing that group).

    Every colour in one mix/bunch group is required to have booked the SAME
    Number of Boxes -- otherwise "box 3" wouldn't mean the same thing across
    colours, which is exactly the gap that let mixed-bunch packing drift out
    of sync with what was actually ordered. This is where that's enforced,
    not left to three different downstream guesses.

    Wipes and rebuilds table_nade wholesale each call (this OPL's own rows
    only -- so_items always covers the OPL's complete group) rather than
    trying to patch it incrementally; the source data (the Sales Order) is
    small and cheap to re-derive from, so there's no reason to reconcile
    diffs by hand.
    """
    if not so_items:
        return

    groups = {}
    for it in so_items:
        key = _group_key(it)
        groups.setdefault(key, []).append(it)

    rows = []
    for key, items in groups.items():
        box_counts = set()
        per_item_rows = []
        for it in items:
            row, num_boxes = _rows_per_item(it)
            per_item_rows.append(row)
            box_counts.add(num_boxes)

        if len(box_counts) > 1:
            names = ", ".join(sorted(frappe.bold(it.item_code) + " (" + str(int(it.get("custom_number_of_boxes") or 0)) + ")" for it in items))
            frappe.throw(
                frappe._(
                    "Every colour in the same mixed group must book the same Number of Boxes, "
                    "so box numbers line up across colours -- {0} disagree. Fix Number of Boxes "
                    "on the Sales Order before packing can be planned."
                ).format(names),
                title=frappe._("Inconsistent Box Count in Mixed Group"),
            )

        num_boxes = box_counts.pop()
        for row in per_item_rows:
            for box_number in range(1, num_boxes + 1):
                rows.append({**row, "box_number": box_number})

    order_pick_list.set("table_nade", [])
    for row in rows:
        order_pick_list.append("table_nade", row)
