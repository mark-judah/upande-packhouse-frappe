# One-time backfill: Consignee.customers (Table MultiSelect -> Consignee
# Customer) was added as an empty structure and never populated, so
# consignee_api.consignees_for_customer's real query always came back empty
# and silently fell back to "every consignee" for every order -- the exact
# "all consignees appear in the popup" bug reported live. Derives real
# (customer, consignee) pairs straight from Sales Order history (the only
# source of truth left for this relationship, since it was never tracked
# anywhere else), deduped, skipping pairs already present.
#
# Already run once against the local bench (kaitet.local, 2026-09-11): 33
# pairs seen, 33 added, 0 already present, 0 unmatched consignee names --
# but that local history is almost entirely this session's own test Sales
# Orders, not real customer data. The real fix for live users needs this run
# against kaitet-group's actual Sales Order history instead.
#
# Run via: bench --site <site> execute upande_packhouse.populate_consignee_customers.run
import frappe


def run():
    pairs = frappe.db.sql("""
        SELECT DISTINCT customer, custom_consignee
        FROM `tabSales Order`
        WHERE customer IS NOT NULL AND customer != ''
          AND custom_consignee IS NOT NULL AND custom_consignee != ''
    """, as_dict=True)

    updated, added_rows, missing_consignee, already_present = 0, 0, set(), 0

    for p in pairs:
        consignee, customer = p.custom_consignee, p.customer
        if not frappe.db.exists("Consignee", consignee):
            missing_consignee.add(consignee)
            continue
        if frappe.db.exists("Consignee Customer", {"parent": consignee, "customer": customer}):
            already_present += 1
            continue
        doc = frappe.get_doc("Consignee", consignee)
        doc.append("customers", {"customer": customer})
        doc.save(ignore_permissions=True)
        updated += 1
        added_rows += 1

    frappe.db.commit()
    print(f"pairs seen={len(pairs)} consignees_updated={updated} rows_added={added_rows} "
          f"already_present={already_present} missing_consignee_doc={len(missing_consignee)}")
    if missing_consignee:
        print("Sales Order custom_consignee values with no matching Consignee record:", sorted(missing_consignee))
    return {"updated": updated, "added_rows": added_rows, "missing_consignee": sorted(missing_consignee)}
