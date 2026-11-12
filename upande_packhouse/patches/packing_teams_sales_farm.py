import frappe


def execute():
	"""Every Packing Team on a sales farm (Production Settings > Shelf Locations, Sales Shelf): the sales farm
	whose Farm Location is the team's location, the one named like the location first. Teams no sales farm
	matches are left unset and listed."""
	from upande_packhouse.api.v2.allocation_teams import sales_farm_for_location, sales_farm_names

	frappe.reload_doc("upande_packhouse", "doctype", "packing_teams")
	frappe.clear_cache(doctype="Packing Teams")
	names = set(sales_farm_names())
	unmapped = []
	for t in frappe.get_all("Packing Teams", fields=["name", "location", "sales_farm"]):
		if t.sales_farm in names:
			continue
		# none, or a farm that is not a sales farm (the team could not be saved with it)
		farm = sales_farm_for_location(t.location)
		if farm or t.sales_farm:
			frappe.db.set_value("Packing Teams", t.name, "sales_farm", farm, update_modified=False)
		if not farm:
			unmapped.append(f"{t.name} ({t.location or 'no location'})")
	if unmapped:
		message = "Packing Teams with no sales farm (set one on the team): " + ", ".join(unmapped)
		print(message)
		frappe.log_error("Packing Teams: no sales farm", message)
