import re

import frappe

from upande_packhouse.box_label import label_filters, mix_names


def execute():
	"""Fill Box Label's order name, variety and mix name (the print filters) on
	labels made before those fields existed."""
	opl_names = {}
	mixes = {}
	for box in frappe.get_all(
		"Box Label",
		filters={"order_name": ["in", ["", None]]},
		fields=["name", "order_pick_list", "customer_purchase_order"],
	):
		opl = box.order_pick_list
		if opl and opl not in opl_names:
			opl_names[opl] = frappe.db.get_value("Order Pick List", opl, ["order_name", "sales_order"], as_dict=True) or {}
		info = opl_names.get(opl) or {}
		so = info.get("sales_order") or box.customer_purchase_order
		if so not in mixes:
			mixes[so] = mix_names(so)
		order_name = info.get("order_name") or (frappe.db.get_value("Sales Order", so, "custom_order_name") if so else "")
		rows = frappe.get_all(
			"Box Label Item", filters={"parent": box.name}, fields=["variety", "qty", "uom"], order_by="idx asc"
		)
		# Box Label Item holds bunches: weight by stems where the UOM says "(10)".
		def stems(r):
			m = re.search(r"\((\d+)\)", r.uom or "")
			return (r.qty or 0) * (int(m.group(1)) if m else 1)

		frappe.db.set_value(
			"Box Label",
			box.name,
			label_filters(order_name, [(r.variety, stems(r)) for r in rows], mixes[so]),
			update_modified=False,
		)
