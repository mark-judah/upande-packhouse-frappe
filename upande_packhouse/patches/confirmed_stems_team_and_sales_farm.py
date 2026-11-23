import frappe


def execute():
	"""Fill each Confirmed Stems row's variety, team and processing sales farm from its line's confirmation."""
	from upande_packhouse.allocation_plan import _confirmed_row_details

	frappe.reload_doc("upande_packhouse", "doctype", "confirmed_stems")
	for name, soi in frappe.db.sql(
		"""SELECT name, sales_order_item FROM `tabConfirmed Stems`
		WHERE parenttype = 'Sales Order' AND IFNULL(sales_order_item, '') != ''"""
	):
		details = {k: v for k, v in _confirmed_row_details(soi).items() if v}
		if details:
			frappe.db.set_value("Confirmed Stems", name, details, update_modified=False)
