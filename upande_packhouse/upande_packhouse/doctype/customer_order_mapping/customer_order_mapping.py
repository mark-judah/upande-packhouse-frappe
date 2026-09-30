# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CustomerOrderMapping(Document):
	def validate(self):
		existing = frappe.db.exists(
			"Customer Order Mapping",
			{"customer": self.customer, "platform": self.platform, "name": ["!=", self.name]},
		)
		if existing:
			frappe.throw(
				_("{0} already has a mapping for {1} ({2})").format(self.customer, self.platform, existing)
			)
