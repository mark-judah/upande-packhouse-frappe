# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Indexes Sales Allocation and its release read by, made on every migrate (named, so a schema sync keeps them).

Every allocation step and every release looks rows up by order line (Bucket Allocations, Pick List Item), by
order (Order Pick List, Bucket Allocations) and by variety (Shelf Item). None of those columns was indexed, so
each lookup read the whole table -- once per line, often per bucket -- which is most of why Allocate's steps were
slow and a release ran past the request's time before its pick lists were made."""

import frappe

INDEXES = (
	("Bucket Allocations", ["sales_order_item", "cancelled"], "idx_ba_line"),
	("Bucket Allocations", ["sales_order"], "idx_ba_order"),
	("Pick List Item", ["sales_order_item"], "idx_pli_line"),
	("Pick List Item", ["custom_sale_order_item"], "idx_pli_custom_line"),
	("Order Pick List", ["sales_order"], "idx_opl_order"),
	("Shelf Item", ["variety"], "idx_shelf_item_variety"),
	("Bucket Allocation Status", ["item_code", "bucket_id"], "idx_bas_item"),
)


def ensure_allocation_indexes():
	for doctype, columns, name in INDEXES:
		if not frappe.db.table_exists(doctype) or not all(frappe.db.has_column(doctype, c) for c in columns):
			continue
		existing = {r[2] for r in frappe.db.sql(f"SHOW INDEX FROM `tab{doctype}`")}  # nosemgrep: fixed names
		if name not in existing:
			frappe.db.add_index(doctype, columns, name)
