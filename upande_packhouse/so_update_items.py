# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Update Items on a submitted Sales Order, with the order's own item fields.

ERPNext's Update Items (update_child_qty_rate) adds and changes lines of a submitted order but only knows its
standard fields: item, qty, rate, UOM, delivery date. A rose line also carries its Specification, stem length, box
type, boxes, pack rate, mix and bunch groups, source warehouse ... -- what the spec picker fills on a draft. Here:
  1. ERPNext's own update runs on the standard fields (its checks: what is delivered, billed, reserved),
  2. each new or changed line gets its other fields from the dialog,
  3. the order engine sets those lines as a draft save would (stems from boxes x pack rate, the price by stem
     length, the source warehouse and truck) -- every other line is left exactly as it was,
  4. one update-after-submit save: the allocation plan sees the changed lines (release_changed_lines).
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

# what ERPNext's update reads; everything else on a dialog row is set here
STANDARD = ("docname", "item_code", "qty", "rate", "uom", "conversion_factor", "delivery_date", "description")
# never taken from the dialog: the order engine or the system owns them
SYSTEM = {
	"name",
	"idx",
	"parent",
	"parenttype",
	"parentfield",
	"doctype",
	"docstatus",
	"stock_qty",
	"amount",
	"base_amount",
	"base_rate",
	"net_rate",
	"net_amount",
	"delivered_qty",
	"billed_amt",
	"custom_opl",
	"custom_fully_allocated",
	# an old field labelled like the real one (warehouse, "Source Warehouse")
	"custom_source_warehouse",
}


def _editable_fields():
	"""The fields a line takes from the dialog besides ERPNext's own: the order's custom item fields (Specification,
	stem length, boxes, pack rate, mix, source warehouse ...) and the warehouse -- not read-only, not the system's."""
	meta = frappe.get_meta("Sales Order Item")
	skip = {
		"Section Break",
		"Column Break",
		"Tab Break",
		"HTML",
		"Button",
		"Fold",
		"Table",
		"Table MultiSelect",
	}
	return {
		f.fieldname
		for f in meta.fields
		if (f.get("is_custom_field") or f.fieldname == "warehouse")
		and f.fieldtype not in skip
		and not f.read_only
		and f.fieldname not in SYSTEM
	}


@frappe.whitelist(methods=["POST"])
def update_items(sales_order: str, trans_items: str):
	"""Add and change lines of submitted `sales_order`: `trans_items` = the dialog's rows (docname for an
	existing line), with any Sales Order Item field."""
	from erpnext.controllers.accounts_controller import update_child_qty_rate

	items = [
		t
		for t in (json.loads(trans_items) if isinstance(trans_items, str) else trans_items)
		if t.get("item_code")
	]
	so = frappe.get_doc("Sales Order", sales_order)
	so.check_permission("write")
	if so.docstatus != 1:
		frappe.throw(_("{0} is not submitted: edit its lines on the form.").format(sales_order))
	before = {r.name for r in so.items}

	# 1. ERPNext's own update: what it allows on a submitted order, on its own fields
	update_child_qty_rate(
		"Sales Order",
		json.dumps([{k: t.get(k) for k in STANDARD if k in t} for t in items], default=str),
		sales_order,
		"items",
	)

	# 2. each new or changed line gets its other fields; new lines in the dialog's order
	so = frappe.get_doc("Sales Order", sales_order)
	added = sorted((r for r in so.items if r.name not in before), key=lambda r: r.idx)
	new_items = [t for t in items if not t.get("docname")]
	by_name = {r.name: r for r in so.items}
	pairs = [(by_name[t["docname"]], t) for t in items if t.get("docname") in by_name]
	pairs += list(zip(added, new_items, strict=False))
	editable = _editable_fields()
	touched = set()
	for row, t in pairs:
		changed = False
		for field in editable:
			if field in t and (t.get(field) or None) != (row.get(field) or None):
				row.set(field, t.get(field))
				changed = True
		if changed or row.name not in before:
			touched.add(row.name)
	if not touched:
		return {"updated": 0}

	# 3. the order engine on those lines only: every other line stays exactly as it was
	kept = {r.name: r.as_dict() for r in so.items if r.name not in touched}
	price_list = so.selling_price_list
	from upande_packhouse import roses_warehouse_map, sales_order_engine

	sales_order_engine.sales_order_before_validate(so)
	roses_warehouse_map.sales_order_apply_routing(so)
	sales_order_engine.sales_order_price(so)
	so.selling_price_list = price_list  # the order's price list stays as submitted
	for r in so.items:
		if r.name in kept:
			r.update({k: v for k, v in kept[r.name].items() if k not in ("name", "idx", "parent")})
	so.calculate_taxes_and_totals()

	# 4. one save as an update after submit: the allocation plan sees the changed lines
	so.flags.ignore_validate_update_after_submit = True
	so.flags.ignore_permissions = True
	so.save()
	# the planning page reads the new lines now, not after its minute of shared reads
	from upande_packhouse.api.v2 import allocation_playground

	allocation_playground._changed()
	return {"updated": len(touched), "added": len(added)}
