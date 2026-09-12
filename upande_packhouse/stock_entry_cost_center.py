"""Warehouse-driven cost-centre stamping for production Stock Entries.

Harvesting, grading, receiving, quarantine and the reject flows are all Stock
Entries that originate at / move through a greenhouse. Each greenhouse is a
Warehouse carrying a `custom_cost_center`, and every such entry must post to that
greenhouse's cost centre. Post-harvest flows (issuing from the cold store to a
sales order) have no greenhouse, but the cold store they issue FROM is a
Warehouse too and carries the exact same `custom_cost_center` field -- reused
directly rather than a separate hardcoded cost centre, so setting one up for a
new cold store is the same one-step admin task ("set custom_cost_center on the
warehouse") as it already is for a greenhouse. Both are validated on the Stock
Entry `validate` event so they apply to every path — mobile APIs, the desk
form, and server scripts.
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

# Stock Entry types for the post-harvest stage -- no greenhouse involved (the
# bucket has already left the farm), so the cost centre comes from the item
# row's own source warehouse (Warehouse.custom_cost_center) instead of
# Stock Entry.custom_greenhouse. Add more post-harvest stock entry types here
# as needed.
POST_HARVEST_COST_CENTRE_TYPES = {
	"Issue From The Cold Store",
	# Farm Pack List submit -> Ungraded Sold -> Graded Sold (see
	# farm_pack_list.py). Same post-harvest situation: no greenhouse, cost
	# centre comes from the item row's own source warehouse (this time the
	# farm's Ungraded Sold warehouse rather than its coldstore).
	"Move To Graded Sold",
	# Shelving a bucket at a different farm than it was received at (see
	# roses_warehouse_map.transfer_to_farm_warehouse) -- same situation
	# again: no greenhouse, cost centre comes from the coldstore it's
	# being moved OUT of.
	"Farm Transfer",
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


def apply_post_harvest_cost_center(doc, method=None):
	"""Post-harvest Stock Entries (issuing a bucket from the cold store to a
	sales order, currently the only such flow) have no greenhouse to derive a
	cost centre from -- mobile/api.py's issueBucketToSaleOrderItem builds this
	Stock Entry with a bare item row and no cost_center at all, which ERPNext's
	own accounting-dimension check then blocks on submit/insert with "Cost
	Center is mandatory for Item <x>".

	Same fix as apply_greenhouse_cost_center, just keyed by warehouse instead
	of Stock Entry.custom_greenhouse: the item row's own s_warehouse (the cold
	store the bucket is being issued FROM) is itself a Warehouse and already
	carries custom_cost_center. If it isn't set, block with the identical
	"contact your IT administrator" message the greenhouse flow uses, naming
	the warehouse instead of a greenhouse -- same one-step fix, same message.
	"""
	if doc.get("stock_entry_type") not in POST_HARVEST_COST_CENTRE_TYPES:
		return

	business_unit = doc.get("business_unit")
	for row in doc.get("items") or []:
		s_warehouse = row.get("s_warehouse")
		if not s_warehouse:
			continue

		cost_center = frappe.db.get_value("Warehouse", s_warehouse, "custom_cost_center")
		if not cost_center:
			frappe.throw(
				frappe._("Please contact your IT administrator to add the cost center for warehouse {0}").format(s_warehouse)
			)

		row.cost_center = cost_center
		if business_unit:
			row.business_unit = business_unit
		if not doc.get("cost_center"):
			doc.cost_center = cost_center
