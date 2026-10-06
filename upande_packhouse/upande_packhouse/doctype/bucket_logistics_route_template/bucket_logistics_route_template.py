# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BucketLogisticsRouteTemplate(Document):
	# A truck's route with no date: its trips. A new day route (Bucket Logistics Route)
	# can start from it on the Truck routes tab; it is never put on a day by itself.
	pass
