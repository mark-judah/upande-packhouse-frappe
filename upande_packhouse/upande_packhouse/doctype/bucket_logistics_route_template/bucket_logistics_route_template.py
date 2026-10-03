# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BucketLogisticsRouteTemplate(Document):
	# A truck's route with no date: its trips and its From/To time of day. Every active
	# template is copied onto each day (a dated Bucket Logistics Route) the first time
	# that day is planned — see transfer_control.ensure_day_routes.
	pass
