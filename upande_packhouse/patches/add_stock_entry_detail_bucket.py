"""Add `custom_bucket_id` to Stock Entry Detail.

Packhouse stock entries are grouped per variety per stem length, so one entry
carries several buckets as separate lines. The bucket therefore has to live on
the line, not only on the parent Stock Entry — otherwise a grouped entry cannot
say which bucket contributed which stems, and `stock_movement.bucket_balance()`
loses the only handle it has on a bucket's whereabouts.

Historical entries (Harvesting, Receiving, and the per-bucket transfers posted
before grouping) keep the bucket on the parent; every read in `stock_movement`
coalesces line over parent, so both shapes stay queryable.

Until this patch runs, `stock_movement` detects the missing column and falls
back to one entry per bucket.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
    "Stock Entry Detail": [
        {
            "fieldname": "custom_bucket_id",
            "label": "Bucket",
            "fieldtype": "Link",
            "options": "Bucket QR Code",
            "insert_after": "custom_stem_length",
            "in_list_view": 1,
            "module": "Upande Packhouse",
            "is_system_generated": 1,
        }
    ]
}


def execute():
    if not frappe.db.exists("DocType", "Bucket QR Code"):
        # Nothing to link to on a site without the harvest doctypes.
        return

    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)
    frappe.clear_cache(doctype="Stock Entry Detail")
