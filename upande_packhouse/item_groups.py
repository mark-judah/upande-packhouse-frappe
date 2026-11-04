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


# ---------------------------------------------------------------------------
# HS codes and crop names for the Roses sales invoice
# ---------------------------------------------------------------------------
# The HS code belongs to the ITEM (ERPNext's own Item > Customs Tariff Number),
# because the Item Group cannot tell a fresh stem from a dried or dyed one
# (Gypsophila and Mums come both ways, in the same group). Those defaults only
# fill an item that has none, so the invoice never prints a blank code; set the
# code on the item to override them (e.g. 060390 for dried or dyed).
HS_DEFAULT_BY_GROUP = {
	"Standard Roses": "060311",
	"Spray Roses": "060311",
	"Chrysanthemums": "060314",
	"Gypsophilla": "060319",
	"Lepidium": "060319",
	"Limonium": "060319",
	"Fillers - Eryngium": "060319",
	"Fillers - Solidago": "060319",
	"Fillers - Hypericum": "060319",
	"Fillers - Agapanthus": "060319",
	"Fillers - Hard Ruscus": "060319",
	"Fillers - Leather Fern": "060319",
	"Fillers - Limonium (F)": "060319",
}
HS_DESCRIPTIONS = {
	"060311": "Fresh cut roses",
	"060314": "Fresh cut chrysanthemums",
	"060319": "Other fresh cut flowers and buds",
	"060390": "Dried or dyed cut flowers",
}
_ROOTS = ("Cut Flowers", "All Item Groups", "")


def _group_chain(group):
	"""[group, parent, grandparent, ...] up to the root."""
	chain, seen = [], set()
	while group and group not in seen and len(chain) < 8:
		seen.add(group)
		chain.append(group)
		group = frappe.get_cached_value("Item Group", group, "parent_item_group")
	return chain


def default_hs_code(item_group):
	for g in _group_chain(item_group):
		if g in HS_DEFAULT_BY_GROUP:
			return HS_DEFAULT_BY_GROUP[g]
	return ""


def hs_code(item_code, item_group=None):
	"""The item's own Customs Tariff Number, else the default for its crop, else ''."""
	own = frappe.get_cached_value("Item", item_code, "customs_tariff_number") if item_code else ""
	if own:
		return own
	return default_hs_code(item_group or (frappe.get_cached_value("Item", item_code, "item_group") if item_code else ""))


def crop_name(item_group):
	"""Std Rose / Spr Rose for the two rose trees; every other crop is its parent Item Group in full
	("Lepidium", "Limonium", "Fillers" ...)."""
	chain = _group_chain(item_group)
	if "Standard Roses" in chain:
		return "Std Rose"
	if "Spray Roses" in chain:
		return "Spr Rose"
	if len(chain) >= 2 and chain[1] not in _ROOTS:
		return chain[1]
	return chain[0] if chain else ""
