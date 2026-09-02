"""Greenhouse cost-centre stamping for production Stock Entries.

Harvesting, grading, receiving, quarantine and the reject flows are all Stock
Entries that originate at / move through a greenhouse. Each greenhouse is a
Warehouse carrying a `custom_cost_center`, and every such entry must post to that
greenhouse's cost centre. Validated on the Stock Entry `validate` event so it
applies to every path — mobile APIs, the desk form, and server scripts.
"""

import frappe

# Stock Entry types that must post to their greenhouse's cost centre.
# Edit this set to add/remove covered flows.
GREENHOUSE_COST_CENTRE_TYPES = {
	"Harvesting",
	"Grading",
	"Grading Forecast",
	"Receiving",
	"Late Receipt",
	"Receiving Quarantined",
	"Remove From Quarantine",
	"Quarantine Rejects",
	"Packhouse Rejects",
	"Field Rejects",
}


def apply_greenhouse_cost_center(doc, method=None):
	"""Post greenhouse-related Stock Entries to the cost centre configured on the
	greenhouse Warehouse (Warehouse.custom_cost_center), and carry the order's
	Business Unit accounting dimension down onto every item row too — GL entries
	are generated per Stock Entry Detail row, so a dimension only set on the
	parent doc doesn't reach the ledger; it has to be on each row.

	The greenhouse is on `custom_greenhouse`. If that greenhouse has no cost centre
	set, the entry is blocked with a clear instruction rather than silently posting
	to the wrong (or a default) cost centre.

	Stock Entry has no legacy custom_farm / custom_business_unit fields —
	business_unit is set directly by the harvesting/grading/receiving flows
	and by this migration script.
	"""
	if doc.get("stock_entry_type") not in GREENHOUSE_COST_CENTRE_TYPES:
		return

	greenhouse = doc.get("custom_greenhouse")
	if not greenhouse:
		# No greenhouse on the entry — nothing to resolve the cost centre from.
		return

	cost_center = frappe.db.get_value("Warehouse", greenhouse, "custom_cost_center")
	if not cost_center:
		frappe.throw(
			frappe._("Please contact your IT administrator to add the cost center for greenhouse {0}").format(greenhouse)
		)

	doc.cost_center = cost_center
	business_unit = doc.get("business_unit")
	for row in doc.get("items") or []:
		row.cost_center = cost_center
		if business_unit:
			row.business_unit = business_unit
