import frappe


def execute():
	"""Fill the new Delivery Date on existing pick lists from their Sales Order."""
	frappe.db.sql(
		"""UPDATE `tabOrder Pick List` o JOIN `tabSales Order` so ON so.name = o.sales_order
		SET o.delivery_date = so.delivery_date WHERE o.delivery_date IS NULL"""
	)
