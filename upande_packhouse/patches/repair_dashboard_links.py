"""Re-apply this app's customizations, then drop dashboard links that cannot resolve.

`custom/sales_order.json` ships a "Packhouse" dashboard link group with four
rows, two of which point at a field this app also ships:

    Order Pick List . sales_order
    Farm Pack List  . sales_order
    Delivery Note   . custom_so      <- from custom/delivery_note.json
    Sales Invoice   . custom_so      <- from custom/sales_invoice.json

The link rows live in `custom/sales_order.json` while the fields they point at
live in sibling files, so a site can end up with the links applied and the
fields not. When that happens the Sales Order form fails twice over:

    Document Links Row #3: Invalid doctype or fieldname.
    (1054, "Unknown column 'custom_so' in 'WHERE'")

— the first from `DocType.validate_links_table_fieldnames()` on any save of the
DocType, the second from the connections panel querying a column that is not
there.

This patch closes both halves: sync the customizations so the fields exist, then
delete any link row that still cannot resolve, so a stale row degrades into a
missing connection instead of an unusable form.
"""

import frappe
from frappe.modules.utils import sync_customizations

APP = "upande_packhouse"

# DocTypes whose dashboard links this app ships.
LINK_OWNERS = ("Sales Order",)


def execute():
    try:
        sync_customizations(APP)
    except Exception:
        # A bad customization file must not leave the form broken — log it and
        # still prune, so the doctype comes back usable either way.
        frappe.log_error(
            f"sync_customizations({APP}) failed during repair_dashboard_links",
            frappe.get_traceback(),
        )

    for doctype in LINK_OWNERS:
        prune_unresolvable_links(doctype)


def prune_unresolvable_links(doctype):
    """Delete DocType Link rows whose target field no longer exists.

    Rows are removed with a direct delete rather than by saving the DocType:
    saving re-runs the very validation that is failing, and a standard DocType
    is not editable outside developer mode.
    """
    rows = frappe.get_all(
        "DocType Link",
        filters={"parent": doctype, "parentfield": "links"},
        fields=["name", "idx", "link_doctype", "link_fieldname", "group"],
        order_by="idx",
        parent_doctype="DocType",  # DocType Link is a child table
    )

    dropped = []
    for row in rows:
        if not row.link_doctype or not row.link_fieldname:
            continue
        if not frappe.db.exists("DocType", row.link_doctype):
            dropped.append((row, "doctype missing"))
            continue
        if not frappe.get_meta(row.link_doctype).has_field(row.link_fieldname):
            dropped.append((row, "fieldname missing"))

    for row, reason in dropped:
        frappe.db.delete("DocType Link", {"name": row.name})
        print(
            f"{doctype}: dropped dashboard link row #{row.idx} "
            f"{row.link_doctype}.{row.link_fieldname} ({reason})"
        )

    if dropped:
        frappe.clear_cache(doctype=doctype)

    return [
        {"link_doctype": r.link_doctype, "link_fieldname": r.link_fieldname,
         "reason": reason}
        for r, reason in dropped
    ]
