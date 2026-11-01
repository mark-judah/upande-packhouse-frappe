import frappe

from upande_packhouse.box_label import purchase_order, specification


def execute():
	"""Fill customer name, specification, customer's PO and item names on labels
	made before those fields existed, so the label prints from its own fields."""
	frappe.db.sql(
		"""UPDATE `tabBox Label Item` bi JOIN `tabItem` i ON i.name = bi.variety
		SET bi.item_name = i.item_name WHERE IFNULL(bi.item_name, '') = ''"""
	)
	frappe.db.sql(
		"""UPDATE `tabBox Label` bl JOIN `tabCustomer` c ON c.name = bl.customer
		SET bl.customer_name = c.customer_name WHERE IFNULL(bl.customer_name, '') = ''"""
	)

	orders = {}
	for box in frappe.get_all(
		"Box Label",
		filters={"specification": ["in", ["", None]]},
		fields=["name", "order_pick_list", "customer_purchase_order", "order_name"],
	):
		so = (
			frappe.db.get_value("Order Pick List", box.order_pick_list, "sales_order")
			if box.order_pick_list
			else None
		) or box.customer_purchase_order
		if so not in orders:
			orders[so] = (
				frappe.get_doc("Sales Order", so) if so and frappe.db.exists("Sales Order", so) else None
			)
		so_doc = orders[so]
		varieties = frappe.get_all("Box Label Item", filters={"parent": box.name}, pluck="variety")
		frappe.db.set_value(
			"Box Label",
			box.name,
			{
				"specification": specification(
					(so_doc.get("items") or []) if so_doc else [],
					box.order_pick_list,
					varieties,
					box.order_name,
				),
				"po_no": purchase_order(so_doc) if so_doc else "",
			},
			update_modified=False,
		)
