# One-time migration: port v15's Specifications (kaitet-group.c.frappe.cloud)
# into v16, fixing the schema gaps found (Colors master + Spec
# Consumable.consumable_type) and backfilling data for specs that already exist
# here from an earlier, incomplete migration attempt (box_items/consumables
# were stubbed with missing length/box_type/pack_rate/consumable_type on
# those). v15 is treated as the source of truth for header fields + both child
# tables. colour/variety live ONLY in approved_varieties now (Spec Box Item
# carries neither) -- both are derived here straight from v15's own box_items
# (remapped, deduped by (colour, variety)), since that's the only place left
# they can come from on a fresh run. Run via:
#   bench --site kaitet.local execute upande_packhouse.migrate_v15_specs.run
import json

import frappe

SOURCE_FILE = "/tmp/claude-1000/-home-jk-Projects-upande-production/53c93643-4f69-4a92-98cd-628867bb89ec/scratchpad/v15_specs_all.json"

# v15 -> v16 Item name drift (same variety, different casing/spelling in v16's
# Item master -- confirmed by hand, not a guess).
VARIETY_REMAP = {
    "Classic Sensation": "Classic sensation",
    "Fair Flow": "Fair flow",
    "Good Mood": "Good mood",
    "Happy Wedding": "Happy wedding",
    "Pina colada Yellow": "Pinacolada Yellow",
    "ROYAL BLUSH": "Royal Blush",
}

# v15 Colors master (free-text values) -> v16 Colors doctype record (Title Case,
# normalized per user's explicit instruction).
COLOUR_REMAP = {
    "LEMONADE": "Lemonade", "DARK PINK": "Dark Pink", "PALE PINK": "Pale Pink",
    "BABY PINK": "Baby Pink", "LIGHT PEACH": "Light Peach", "CREAM PEACH": "Cream Peach",
    "LIGHT PINK": "Light Pink", "CREAM": "Cream", "LILAC": "Lilac", "PINK BI": "Pink Bi",
    "GREEN": "Green", "BLUE": "Blue", "ORANGE BI": "Orange Bi", "ORANGE": "Orange",
    "PEACH": "Peach", "CORAL": "Coral", "CERISE": "Cerise", "YELLOW": "Yellow",
    "Pink": "Pink", "White": "White", "Red": "Red",
}

HEADER_FIELDS = [
    "customer", "consumables_charge", "documentation_charge", "certificate_of_origin",
    "category_code", "ftnft", "spec_type", "valid_from", "expiry_date", "status",
    "cut_stage", "defoliation_length", "rubber_band_type", "rubber_band_distance_1",
    "rubber_band_distance_2", "box_assortment",
]

# cut_stage is now a Link to the real Cut Stage master ("1.5-2.0",
# "2.0-2.5", "2.0-3.0", "2.5-3.0", "Budwood") -- v15's own values already use
# that exact format, so no remap is needed (a stale Select-era remap used to
# live here, converting them the WRONG way to fit the old hardcoded options).
CUT_STAGE_REMAP = {}

# One spec ("BBBB BOMBASTIC 72CM") recorded a range ("15-20") instead of one
# of v16's discrete values -- per explicit instruction, take the upper bound.
DEFOLIATION_LENGTH_REMAP = {
    "15-20": "20",
}

# Completely empty v15 shells (no customer, no ftnft, no box_items, no
# consumables -- nothing) -- confirmed there is nothing to migrate, skipped
# per explicit instruction rather than created as blank placeholders.
SKIP = {"SPEC-13038", "SPEC-1522", "SPEC-1524"}


def _remap_colour(v):
    if not v:
        return v
    return COLOUR_REMAP.get(v, COLOUR_REMAP.get(v.strip(), v))


def _remap_variety(v):
    if not v:
        return v
    return VARIETY_REMAP.get(v, v)


def _box_item_row(bi):
    # colour/variety are NOT copied onto Spec Box Item -- that redundancy was
    # removed; both live solely in Specifications.approved_varieties (see
    # populate_approved_varieties.py, which derives them from these same v15
    # box_items instead).
    return {
        "bunch_type": bi.get("bunch_type"),
        "hz_bud_count_range": bi.get("hz_bud_count_range"),
        "stems_per_bunch": bi.get("stems_per_bunch"),
        "length": bi.get("length"),
        "box_type": bi.get("box_type"),
        "bunches_per_box": bi.get("bunches_per_box"),
        "pack_rate": bi.get("pack_rate"),
    }


def _consumable_row(c):
    qty_per_box = c.get("qty_per_box") or 0
    qty_per_bunch = c.get("qty_per_bunch") or 0
    # v15 had no application_level concept -- infer it from which qty column is
    # actually populated (v16's own default is "Bunch" when neither is set).
    if qty_per_box and not qty_per_bunch:
        application_level = "Box"
    else:
        application_level = "Bunch"
    return {
        "consumable_type": c.get("consumable_type"),
        "item": c.get("consumable_item"),
        "description": c.get("description"),
        "qty_per_bunch": qty_per_bunch,
        "qty_per_box": qty_per_box,
        "price_inclusive": c.get("price_inclusive"),
        "application_level": application_level,
    }


def run():
    data = json.loads(open(SOURCE_FILE).read())
    created, updated, errors = [], [], []

    for name, src in data.items():
        if name in SKIP:
            continue
        try:
            if src.get("cut_stage") in CUT_STAGE_REMAP:
                src["cut_stage"] = CUT_STAGE_REMAP[src["cut_stage"]]
            if src.get("defoliation_length") in DEFOLIATION_LENGTH_REMAP:
                src["defoliation_length"] = DEFOLIATION_LENGTH_REMAP[src["defoliation_length"]]
            box_items = [_box_item_row(bi) for bi in (src.get("box_items") or [])]
            consumables = [_consumable_row(c) for c in (src.get("consumables") or [])]

            # approved_varieties: derive (colour, variety) straight from v15's
            # own box_items (remapped, deduped) -- box_items itself no longer
            # carries either field, so this is the only place they can still
            # come from on a fresh run (e.g. the live-site pass).
            seen_pairs, approved_varieties = set(), []
            for bi in (src.get("box_items") or []):
                colour = _remap_colour(bi.get("colour"))
                variety = _remap_variety(bi.get("variety"))
                if not colour or not variety:
                    continue
                key = (colour, variety)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                approved_varieties.append({"colour": colour, "variety": variety})

            if frappe.db.exists("Specifications", name):
                doc = frappe.get_doc("Specifications", name)
                for f in HEADER_FIELDS:
                    doc.set(f, src.get(f))
                doc.set("box_items", [])
                for row in box_items:
                    doc.append("box_items", row)
                doc.set("consumables", [])
                for row in consumables:
                    doc.append("consumables", row)
                existing_pairs = {(r.colour, r.variety) for r in doc.approved_varieties}
                for row in approved_varieties:
                    if (row["colour"], row["variety"]) not in existing_pairs:
                        doc.append("approved_varieties", row)
                doc.save(ignore_permissions=True)
                updated.append(name)
            else:
                doc = frappe.new_doc("Specifications")
                doc.spec_name = src.get("spec_name") or name
                for f in HEADER_FIELDS:
                    doc.set(f, src.get(f))
                for row in box_items:
                    doc.append("box_items", row)
                for row in consumables:
                    doc.append("consumables", row)
                for row in approved_varieties:
                    doc.append("approved_varieties", row)
                doc.insert(ignore_permissions=True)
                created.append(name)
        except Exception as e:
            errors.append((name, str(e)[:300]))
            frappe.db.rollback()

    frappe.db.commit()
    print(f"created={len(created)} updated={len(updated)} errors={len(errors)}")
    if errors:
        print("ERRORS:")
        for n, e in errors:
            print(" ", n, "->", e)
    return {"created": len(created), "updated": len(updated), "errors": errors}
