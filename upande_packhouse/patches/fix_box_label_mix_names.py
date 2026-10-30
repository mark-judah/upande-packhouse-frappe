import re

import frappe

from upande_packhouse.box_label import label_filters, mix_names


def execute():
	"""Re-derive the mix name on labels that picked up another pick list's mix.

	A variety on several lines of one order (Dominica in a 60cm and a 70cm mix)
	used to take its first line's mix, so a box could carry two or three mix
	names. Only the mix name is rewritten: it is printed text, so boxes already
	staged or loaded are corrected too."""
	mixes = {}
	for box in frappe.get_all(
		"Box Label",
		filters={"mix_name": ["like", "%, %"]},
		fields=["name", "order_pick_list", "customer_purchase_order", "order_name"],
	):
		opl = box.order_pick_list
		so = (
			frappe.db.get_value("Order Pick List", opl, "sales_order") if opl else None
		) or box.customer_purchase_order
		if (so, opl) not in mixes:
			mixes[(so, opl)] = mix_names(so, opl)
		rows = frappe.get_all(
			"Box Label Item",
			filters={"parent": box.name},
			fields=["variety", "qty", "uom"],
			order_by="idx asc",
		)

		# Box Label Item holds bunches: weight by stems where the UOM says "(10)".
		def stems(r):
			m = re.search(r"\((\d+)\)", r.uom or "")
			return (r.qty or 0) * (int(m.group(1)) if m else 1)

		filters = label_filters(box.order_name, [(r.variety, stems(r)) for r in rows], mixes[(so, opl)])
		frappe.db.set_value("Box Label", box.name, "mix_name", filters["mix_name"], update_modified=False)
