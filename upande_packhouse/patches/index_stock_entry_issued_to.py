# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Index Stock Entry.custom_issued_to.

Releasing (or replacing) an allocation looks up the Stock Entries that carry the
sales order item (stock_movement.reverse_allocation_movement and its callers).
Without an index that is a full scan of the Stock Entry table - about 15 seconds a
release on a 1.7M-row site - so a bulk release crawled. With it, the lookup is instant."""

import frappe


def execute():
	if frappe.db.has_column("Stock Entry", "custom_issued_to"):
		frappe.db.add_index("Stock Entry", ["custom_issued_to"])
