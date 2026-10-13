import frappe


def execute():
	"""Pick List Item.transit_truck is the remote-transfer truck only. Allocation used to
	copy the order's freight truck (Sales Order custom_truck, e.g. "SIM Truck") into it;
	clear every value that is not an internal transfer Vehicle."""
	frappe.db.sql(
		"""UPDATE `tabPick List Item` pli SET pli.transit_truck = NULL
		WHERE IFNULL(pli.transit_truck, '') != ''
		  AND NOT EXISTS (SELECT 1 FROM `tabVehicle` v
		                  WHERE v.name = pli.transit_truck AND IFNULL(v.custom_dispatch_truck, 0) = 0)"""
	)
