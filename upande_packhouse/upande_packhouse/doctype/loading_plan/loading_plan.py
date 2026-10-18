# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class LoadingPlan(Document):
	def fill_from_orders(self):
		"""Replace the rows with the delivery date/location's orders, as the form's
		"Fetch Orders" button does. Returns how many rows were added."""
		res = fetch_loading_plan_orders(str(self.delivery_date), self.location)
		if res.get("status") != "success":
			frappe.throw(res.get("message") or frappe._("Could not fetch orders."))
		self.set("loading_plan_items", [])
		for item in res.get("items") or []:
			self.append(
				"loading_plan_items",
				{
					"customer": item["customer"],
					"delivery_point": item["delivery_point"],
					"loading_position": item["loading_position"],
					"box_type": item["box_type"],
					"number_of_boxes": item["number_of_boxes"],
					"sales_order": item["sales_order"],
					"order_pick_list": item["opl"],
				},
			)
		return len(self.loading_plan_items)


# Ported from a live-only Desk "Server Script" (api_method
# fetch_loading_plan_orders) that was never in the codebase -- confirmed
# broken there (2026-09-12): it filtered Farm Pack List on a
# `custom_order_pick_list` field that doesn't exist (the real field is
# `order_pick_list`), and read pack quantities from a `Dispatch Form Item`
# child doctype that isn't even Farm Pack List's own child table (that's
# `Farm Packlist Item`, whose stems field is `stock_qty`, not
# `custom_number_of_stems`). Both fixed here; logic otherwise unchanged.
def _box_kind_key(item):
	"""The packing group a mixed line belongs to (packing_guide._group_key), namespaced
	by kind so a mix group and a bunch group with the same number stay apart."""
	from upande_packhouse.packing_guide import _group_key

	return ("bunch" if item.get("custom_mixed_bunch") else "box", _group_key(item))


def _opl_boxes_by_type(opl_name):
	"""{box_type: real boxes} for one OPL, from its Packing Guide: a box is a
	(packing group, box_number) -- a mixed box/bunch group's colours share their boxes,
	each straight line is its own group (the same count real_box_count_for_opl makes)."""
	rows = frappe.get_all(
		"Packing Guide",
		filters={"parent": opl_name},
		fields=["sales_order_item", "box_number", "box_type"],
	)
	if not rows:
		return {}
	sois = {
		d.name: d
		for d in frappe.get_all(
			"Sales Order Item",
			filters={"name": ["in", list({r.sales_order_item for r in rows if r.sales_order_item})]},
			fields=[
				"name",
				"custom_mixed_box",
				"custom_mixed_bunch",
				"custom_mix_group",
				"custom_bunch_group",
				"custom_line",
				"custom_box_type",
			],
		)
	}
	boxes = {}
	for r in rows:
		soi = sois.get(r.sales_order_item)
		group = (
			_box_kind_key(soi)
			if soi and (soi.custom_mixed_box or soi.custom_mixed_bunch)
			else r.sales_order_item
		)
		box_type = r.box_type or (soi.custom_box_type if soi else "") or ""
		boxes.setdefault(box_type, set()).add((group, r.box_number))
	return {bt: len(b) for bt, b in boxes.items()}


@frappe.whitelist()
def fetch_loading_plan_orders(delivery_date: str | None = None, location: str | None = None):
	"""The orders to load for a delivery date/location, from their Order Pick Lists --
	the orders that have stock allocated -- one row per OPL and box type, with the
	OPL's real box count (its Packing Guide; a mixed box/bunch's colours share boxes)
	and what is packed on it. Sales Orders with no OPL (nothing allocated) are left out;
	so are cancelled orders and OPLs. Called by the Loading Plan form's "Fetch Orders"
	button (loading_plan.js).
	"""
	location_farm = {"Karen": "Karen", "Ravine": "Kapkolia"}
	farm = location_farm.get(location) if location else None

	if not delivery_date:
		return {"status": "error", "message": "delivery_date is required"}

	so_filters = {"delivery_date": delivery_date, "docstatus": 1}
	if farm:
		so_filters["custom_farm"] = farm
	sales_orders = {
		so.name: so
		for so in frappe.get_all(
			"Sales Order",
			filters=so_filters,
			fields=["name", "customer", "custom_delivery_point", "custom_order_name"],
		)
	}
	if not sales_orders:
		return {"status": "success", "message": "0 orders found", "items": []}

	opls = frappe.get_all(
		"Order Pick List",
		filters={"sales_order": ["in", list(sales_orders)], "docstatus": ["<", 2]},
		fields=["name", "sales_order", "creation"],
		order_by="creation asc",
	)

	# The delivery point a line names wins over its order's (as before).
	line_dp = {}
	for it in frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", list(sales_orders)]},
		fields=["parent", "custom_opl", "custom_delivery_point"],
	):
		if it.custom_opl and it.custom_delivery_point:
			line_dp.setdefault(it.custom_opl, it.custom_delivery_point)

	# Packed boxes per OPL: each box (box_id) once, however many varieties it holds.
	packed = {}
	opl_names = [o.name for o in opls]
	if opl_names:
		fpl_to_opl = {
			f.name: f.order_pick_list
			for f in frappe.get_all(
				"Farm Pack List",
				filters={"order_pick_list": ["in", opl_names], "docstatus": ["!=", 2]},
				fields=["name", "order_pick_list"],
			)
		}
		if fpl_to_opl:
			for r in frappe.get_all(
				"Farm Packlist Item",
				filters={
					"parenttype": "Farm Pack List",
					"parentfield": "pack_list_item",
					"parent": ["in", list(fpl_to_opl)],
				},
				fields=["parent", "stock_qty", "box_id"],
			):
				agg = packed.setdefault(fpl_to_opl[r.parent], {"stems": 0, "boxes": set()})
				agg["stems"] = agg["stems"] + (r.stock_qty or 0)
				agg["boxes"].add(str(r.box_id or "1"))

	entries = []
	for o in opls:
		so = sales_orders.get(o.sales_order)
		if not so or not so.customer:
			continue
		delivery_point = line_dp.get(o.name) or so.custom_delivery_point or ""
		if not delivery_point:
			continue
		by_type = _opl_boxes_by_type(o.name) or {"": 0}
		for box_type, boxes in by_type.items():
			entries.append(
				{
					"customer": so.customer,
					"delivery_point": delivery_point,
					"sales_order": so.name,
					"order_name": so.custom_order_name or so.name,
					"opl": o.name,
					"box_type": box_type,
					"number_of_boxes": boxes,
					"packed_stems": (packed.get(o.name) or {}).get("stems", 0),
					"packed_boxes": len((packed.get(o.name) or {}).get("boxes") or ()),
				}
			)

	entries.sort(key=lambda x: (x["delivery_point"], x["customer"], x["order_name"], x["box_type"]))
	items = []
	for position, e in enumerate(entries, start=1):
		items.append({**e, "loading_position": position})

	return {
		"status": "success",
		"message": str(len(items)) + " orders found",
		"items": items,
	}
