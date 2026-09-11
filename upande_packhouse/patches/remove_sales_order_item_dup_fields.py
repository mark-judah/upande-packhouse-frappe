import frappe


def execute():
    """One-time cleanup, ships with the fixture changes to Sales Order Item's
    field_order / in_list_view (see custom/sales_order_item.json) -- fixture
    sync only upserts what's listed there, it never deletes a Custom Field
    that's no longer in the file, so removing these two needs an explicit
    patch to actually take effect on any site that migrates this in.

    - custom_source_warehouse: redundant with the native `warehouse` field
      (see warehouse_routing.js / spec_autofill.py, both already updated to
      read/write `warehouse` directly instead).
    - custom_pack_rate: a stray, unused (0 rows had it set), never-wired-up
      duplicate of custom_packrate (the real, actually-used Packrate link).
    """
    for fieldname in ("custom_source_warehouse", "custom_pack_rate"):
        cf_name = frappe.db.get_value("Custom Field", {"dt": "Sales Order Item", "fieldname": fieldname})
        if cf_name:
            frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True, force=True)

        column_exists = frappe.db.sql(
            "SHOW COLUMNS FROM `tabSales Order Item` LIKE %s", fieldname
        )
        if column_exists:
            # ALTER TABLE implicitly commits in MySQL; Frappe's own DDL guard
            # refuses to run one while anything from the delete_doc() above
            # is still uncommitted, rather than let that implicit commit
            # hide the transaction boundary. Commit explicitly first.
            frappe.db.commit()
            frappe.db.sql(f"ALTER TABLE `tabSales Order Item` DROP COLUMN `{fieldname}`")

    frappe.clear_cache(doctype="Sales Order Item")
