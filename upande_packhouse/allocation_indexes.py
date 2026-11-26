# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Indexes Sales Allocation and its release read by, made on every migrate (named, so a schema sync keeps them).

Every allocation step and every release looks rows up by order line (Bucket Allocations, Pick List Item), by
order (Order Pick List, Bucket Allocations) and by variety (Shelf Item). None of those columns was indexed, so
each lookup read the whole table -- once per line, often per bucket -- which is most of why Allocate's steps were
slow and a release ran past the request's time before its pick lists were made.

The desk's unread-notification count (every page load) filters Notification Log by user and read, and stock
reports filter Stock Ledger Entry by posting date alone; both scanned millions of rows."""

import frappe

INDEXES = (
	("Bucket Allocations", ["sales_order_item", "cancelled"], "idx_ba_line"),
	("Bucket Allocations", ["sales_order"], "idx_ba_order"),
	("Pick List Item", ["sales_order_item"], "idx_pli_line"),
	("Pick List Item", ["custom_sale_order_item"], "idx_pli_custom_line"),
	("Order Pick List", ["sales_order"], "idx_opl_order"),
	("Shelf Item", ["variety"], "idx_shelf_item_variety"),
	("Bucket Allocation Status", ["item_code", "bucket_id"], "idx_bas_item"),
	("Notification Log", ["for_user", "read"], "idx_nl_user_read"),
	("Stock Ledger Entry", ["posting_date"], "idx_sle_posting_date"),
	# Stock Visibility's forecast reads the active weeks by year and week number, and its in-transit read looks
	# for pick rows with one of three flags set: each scanned the whole table (the three flag indexes let the
	# database take the matching rows of each and join them)
	("Production Forecast Week", ["iso_year", "week_number"], "idx_pfw_week"),
	("Pick List Item", ["loaded_in_trolley"], "idx_pli_in_trolley"),
	("Pick List Item", ["loaded_on_truck"], "idx_pli_on_truck"),
	("Pick List Item", ["in_transit"], "idx_pli_in_transit"),
	("Pick List Item", ["not_found"], "idx_pli_not_found"),
)


def ensure_allocation_indexes():
	for doctype, columns, name in INDEXES:
		if not frappe.db.table_exists(doctype) or not all(frappe.db.has_column(doctype, c) for c in columns):
			continue
		# nosemgrep: frappe-sql-format-injection -- the doctype is one of INDEXES' fixed names, never request data
		existing = {r[2] for r in frappe.db.sql(f"SHOW INDEX FROM `tab{doctype}`")}
		if name not in existing:
			frappe.db.add_index(doctype, [f"`{c}`" for c in columns], name)
