"""Lookup helpers for the Sales Order consignee picker.

A Consignee now carries a `customers` multiselect (child doctype Consignee Customer),
so one consignee can serve many customers. Client-side get_list can't reliably read a
child table's `parent`, so resolve the mapping server-side.
"""

import frappe


@frappe.whitelist()
def consignees_for_customer(customer=None):
	"""Consignees whose `customers` multiselect contains this customer.

	Falls back to every consignee when the customer has no mapping yet, so the
	picker is never empty and the user is not blocked.
	"""
	names = []
	if customer:
		names = frappe.get_all(
			"Consignee Customer",
			filters={"customer": customer, "parenttype": "Consignee"},
			pluck="parent",
		)
		names = list(dict.fromkeys(names))  # dedupe, keep order
	if not names:
		names = frappe.get_all("Consignee", pluck="name")
	return names
