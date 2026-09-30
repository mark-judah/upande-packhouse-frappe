# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Logistics Route gained a mandatory From/To window (a truck can run several
# routes a day). Routes saved before it covered their whole day — say so explicitly.

import frappe


def execute():
	route = frappe.qb.DocType("Bucket Logistics Route")
	(
		frappe.qb.update(route)
		.set(route.from_datetime, frappe.query_builder.functions.Timestamp(route.route_date, "00:00:00"))
		.set(route.to_datetime, frappe.query_builder.functions.Timestamp(route.route_date, "23:59:59"))
		.where(route.from_datetime.isnull() | route.to_datetime.isnull())
		.where(route.route_date.isnotnull())
		.run()
	)
