import frappe


def execute():
	"""Saved routes (Bucket Logistics Route Template) start from each truck's most recent
	day of routes: one saved route per distinct window + stops that day. Routes dated
	from today on that match one are linked to it so they aren't copied a second time."""
	from upande_packhouse.api.transfer_control import TEMPLATE, _dt

	frappe.reload_doc("upande_packhouse", "doctype", "bucket_logistics_route_template")
	frappe.reload_doc("upande_packhouse", "doctype", "bucket_logistics_route")
	if frappe.db.count(TEMPLATE):
		return
	today = frappe.utils.today()
	latest = frappe.db.sql(
		"""SELECT vehicle, MAX(route_date) AS d FROM `tabBucket Logistics Route`
		   WHERE route_date IS NOT NULL GROUP BY vehicle""",
		as_dict=True,
	)
	for row in latest:
		seen = {}
		for name in frappe.get_all(
			"Bucket Logistics Route",
			filters={"vehicle": row.vehicle, "route_date": row.d},
			pluck="name",
			order_by="from_datetime asc",
		):
			src = frappe.get_doc("Bucket Logistics Route", name)
			if not src.legs:
				continue
			start, end = (_dt(src.from_datetime)[11:] or "00:00"), (_dt(src.to_datetime)[11:] or "23:59")
			key = (start, end, tuple(l.to_farm for l in src.legs))
			if key in seen:
				continue
			t = frappe.new_doc(TEMPLATE)
			t.vehicle, t.active = src.vehicle, 1
			t.from_time, t.to_time = start + ":00", end + ":00"
			for l in src.legs:
				t.append(
					"legs",
					{"leg": l.leg, "run": l.run, "from_farm": l.from_farm, "to_farm": l.to_farm, "distance_km": l.distance_km},
				)
			t.total_km = src.total_km
			t.insert(ignore_permissions=True)
			seen[key] = t.name
		# Day routes from today on that match a saved route are its copies already.
		for name in frappe.get_all(
			"Bucket Logistics Route",
			filters={"vehicle": row.vehicle, "route_date": [">=", today]},
			pluck="name",
		):
			r = frappe.get_doc("Bucket Logistics Route", name)
			key = (_dt(r.from_datetime)[11:], _dt(r.to_datetime)[11:], tuple(l.to_farm for l in r.legs))
			if key in seen:
				frappe.db.set_value("Bucket Logistics Route", name, "template", seen[key], update_modified=False)
