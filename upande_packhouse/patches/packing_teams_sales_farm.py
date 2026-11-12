import frappe


def execute():
	"""Every Packing Team on a sales farm: the farm whose Farm Location is the team's location."""
	frappe.reload_doc("upande_packhouse", "doctype", "packing_teams")
	frappe.clear_cache(doctype="Packing Teams")
	by_location = {}
	for f in frappe.get_all("Farm", fields=["name", "farm_location"]):
		if f.farm_location:
			by_location.setdefault(f.farm_location, []).append(f.name)
	for t in frappe.get_all("Packing Teams", fields=["name", "location", "sales_farm"]):
		if t.sales_farm or not t.location:
			continue
		farms = by_location.get(t.location) or []
		farm = t.location if t.location in farms else (farms[0] if len(farms) == 1 else None)
		if farm or frappe.db.exists("Farm", t.location):
			frappe.db.set_value("Packing Teams", t.name, "sales_farm", farm or t.location, update_modified=False)
