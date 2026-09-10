# One-time backfill: populate Specifications.approved_varieties (Spec Approved
# Variety) from each spec's own box_items -- (colour, variety) pairs, deduped,
# skipping rows missing either colour or variety and pairs already present.
# Run via:
#   bench --site kaitet.local execute upande_packhouse.populate_approved_varieties.run
import frappe


def run():
    specs = frappe.get_all("Specifications", pluck="name")
    updated, added_rows, skipped_no_pairs = 0, 0, 0

    for name in specs:
        doc = frappe.get_doc("Specifications", name)

        existing = {(r.colour, r.variety) for r in doc.approved_varieties}
        pairs = []
        seen = set()
        for bi in doc.box_items:
            if not bi.colour or not bi.variety:
                continue
            key = (bi.colour, bi.variety)
            if key in seen or key in existing:
                continue
            seen.add(key)
            pairs.append(key)

        if not pairs:
            skipped_no_pairs += 1
            continue

        for colour, variety in pairs:
            doc.append("approved_varieties", {"colour": colour, "variety": variety})
        doc.save(ignore_permissions=True)
        updated += 1
        added_rows += len(pairs)

    frappe.db.commit()
    print(f"updated={updated} added_rows={added_rows} specs_with_no_colour_variety_pairs={skipped_no_pairs}")
    return {"updated": updated, "added_rows": added_rows, "skipped_no_pairs": skipped_no_pairs}
