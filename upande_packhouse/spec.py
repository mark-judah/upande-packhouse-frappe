"""Specifications lifecycle helpers.

Temporary specs carry an expiry date; once past it they must stop appearing in
the Sales Order spec picker. Rather than filter on dates everywhere, a daily job
flips expired Temporary specs to status = Inactive (history preserved).
"""

import frappe


def expire_temporary_specs():
	"""Daily: deactivate Temporary specs whose expiry_date has passed."""
	today = frappe.utils.nowdate()
	names = frappe.get_all(
		"Specifications",
		filters={
			"spec_type": "Temporary",
			"status": "Active",
			"expiry_date": ["<", today],
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Specifications", name, "status", "Inactive", update_modified=False)
	if names:
		frappe.db.commit()
	return names
