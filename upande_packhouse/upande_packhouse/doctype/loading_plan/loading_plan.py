# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class LoadingPlan(Document):
	pass


# Ported from a live-only Desk "Server Script" (api_method
# fetch_loading_plan_orders) that was never in the codebase -- confirmed
# broken there (2026-09-12): it filtered Farm Pack List on a
# `custom_order_pick_list` field that doesn't exist (the real field is
# `order_pick_list`), and read pack quantities from a `Dispatch Form Item`
# child doctype that isn't even Farm Pack List's own child table (that's
# `Farm Packlist Item`, whose stems field is `stock_qty`, not
# `custom_number_of_stems`). Both fixed here; logic otherwise unchanged.
@frappe.whitelist()
def fetch_loading_plan_orders(delivery_date=None, location=None):
	"""One row per Sales Order line for a delivery date/location, with
	packed stems/boxes matched per (Order Pick List, item_code, stem
	length) -- an OPL usually spans several order lines, so a whole-OPL
	total would double-count across them. Called by the Loading Plan
	form's "Fetch Orders" button (loading_plan.js).
	"""
	location_farm = {"Karen": "Karen", "Ravine": "Kapkolia"}
	farm = location_farm.get(location) if location else None

	def lenkey(v):
		# normalise stem length to digits only so "62cm" == "62"
		return "".join(c for c in str(v or "") if c.isdigit())

	if not delivery_date:
		return {"status": "error", "message": "delivery_date is required"}

	so_filters = {"delivery_date": delivery_date, "docstatus": 1}
	if farm:
		so_filters["custom_farm"] = farm

	sales_orders = frappe.get_all(
		"Sales Order",
		filters=so_filters,
		fields=["name", "customer", "custom_delivery_point", "custom_order_name"],
		order_by="custom_delivery_point asc, customer asc",
	)

	# ---- One entry per Sales Order line ----
	line_entries = []
	opl_set = set()
	for so in sales_orders:
		customer = so.customer or ""
		if not customer:
			continue
		so_delivery_point = so.custom_delivery_point or ""

		so_items = frappe.get_all(
			"Sales Order Item",
			filters={"parent": so.name},
			fields=[
				"item_code",
				"custom_length",
				"custom_delivery_point",
				"custom_box_type",
				"custom_number_of_boxes",
				"custom_box_quantity",
				"custom_opl",
			],
		)
		for item in so_items:
			delivery_point = item.custom_delivery_point or so_delivery_point
			if not delivery_point:
				continue
			opl = item.custom_opl or ""
			if opl:
				opl_set.add(opl)
			line_entries.append({
				"customer": customer,
				"delivery_point": delivery_point,
				"sales_order": so.name,
				"order_name": so.custom_order_name or so.name,
				"box_type": item.custom_box_type or "",
				"number_of_boxes": item.custom_number_of_boxes or item.custom_box_quantity or 0,
				"opl": opl,
				"item_code": item.item_code or "",
				"len_key": lenkey(item.custom_length),
			})

	# ---- Packed stems/boxes matched per (OPL, item_code, length) ----
	packed_by_key = {}
	if opl_set:
		fpls = frappe.get_all(
			"Farm Pack List",
			filters={"order_pick_list": ["in", list(opl_set)], "docstatus": ["!=", 2]},
			fields=["name", "order_pick_list"],
		)
		fpl_to_opl = {f.name: f.order_pick_list for f in fpls}
		if fpl_to_opl:
			rows = frappe.get_all(
				"Farm Packlist Item",
				filters={
					"parenttype": "Farm Pack List",
					"parentfield": "pack_list_item",
					"parent": ["in", list(fpl_to_opl.keys())],
				},
				fields=["parent", "item_code", "stem_length", "stock_qty"],
			)
			for r in rows:
				opl = fpl_to_opl.get(r.parent)
				if not opl:
					continue
				key = (opl, r.item_code or "", lenkey(r.stem_length))
				agg = packed_by_key.setdefault(key, {"packed_stems": 0, "packed_boxes": 0})
				agg["packed_stems"] = agg["packed_stems"] + (r.stock_qty or 0)
				agg["packed_boxes"] = agg["packed_boxes"] + 1

	# ---- Sort, assign loading position, build response ----
	line_entries.sort(key=lambda x: (x["delivery_point"], x["customer"], x["order_name"], x["box_type"]))

	items = []
	position = 1
	for e in line_entries:
		packed = packed_by_key.get((e["opl"], e["item_code"], e["len_key"]), {})
		items.append({
			"customer": e["customer"],
			"delivery_point": e["delivery_point"],
			"loading_position": position,
			"sales_order": e["sales_order"],
			"order_name": e["order_name"],
			"box_type": e["box_type"],
			"number_of_boxes": e["number_of_boxes"],
			"packed_stems": packed.get("packed_stems", 0),
			"packed_boxes": packed.get("packed_boxes", 0),
		})
		position = position + 1

	return {
		"status": "success",
		"message": str(len(items)) + " lines found",
		"items": items,
	}
