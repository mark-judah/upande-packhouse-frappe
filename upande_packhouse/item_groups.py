"""Shared Item Group tree helpers.

The Rose Item Group tree was reorganised: real varieties now live under
sub-groups ("Standard Roses - Retail", "Spray Roses - Regular", …) instead of
directly in the two root groups ("Spray Roses" / "Standard Roses") they used
to sit in. Several places downstream of an item's leaf Item Group tell spray
(scan bunch QR codes) from standard (typed in manually) off it -- packing
(mobile/api.py's get_pick_list_with_farm_pack_list) and the packhouse
dashboard's per-OPL stem split (api/dashboard.py) -- and all did it with a
flat string match against the root group name, which silently
misclassified almost every real variety once the tree changed (a leaf group
like "Spray Roses - Regular" is never == "Spray Roses"). Import
resolve_rose_item_groups from here wherever that classification is needed,
rather than re-deriving it.
"""

import frappe


def resolve_rose_item_groups(leaf_group_names):
    """Item Group -> "Spray Roses" | "Standard Roses" | itself, resolved via
    the Item Group nested set (lft/rgt) rather than a flat string match.

    Falls back to the leaf group name itself when it isn't under either
    root, so an unrelated Item Group still passes through unchanged rather
    than being coerced into one of the two.
    """
    leaf_group_names = {g for g in leaf_group_names if g}
    if not leaf_group_names:
        return {}

    roots = frappe.get_all(
        "Item Group",
        filters={"name": ["in", ["Spray Roses", "Standard Roses"]]},
        fields=["name", "lft", "rgt"],
    )
    if not roots:
        return {g: g for g in leaf_group_names}

    leaves = frappe.get_all(
        "Item Group",
        filters={"name": ["in", list(leaf_group_names)]},
        fields=["name", "lft", "rgt"],
    )

    resolved = {}
    for leaf in leaves:
        category = leaf.name
        for root in roots:
            if root.lft <= leaf.lft and leaf.rgt <= root.rgt:
                category = root.name
                break
        resolved[leaf.name] = category

    # A requested name that isn't an Item Group at all (shouldn't happen)
    # falls back to itself rather than being dropped.
    for g in leaf_group_names:
        resolved.setdefault(g, g)
    return resolved
