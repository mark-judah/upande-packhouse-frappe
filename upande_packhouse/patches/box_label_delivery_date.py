import frappe


def execute():
	frappe.reload_doc("upande_packhouse", "doctype", "box_label")
	frappe.db.sql(
		"""UPDATE `tabBox Label` bl
		JOIN `tabSales Order` so ON so.name = bl.customer_purchase_order
		SET bl.delivery_date = so.delivery_date
		WHERE bl.delivery_date IS NULL OR bl.delivery_date != so.delivery_date"""
	)
