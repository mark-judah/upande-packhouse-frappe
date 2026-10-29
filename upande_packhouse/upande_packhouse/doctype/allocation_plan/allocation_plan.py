# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AllocationPlan(Document):
	def validate(self):
		# One plan per delivery day and farm (the name is built from both, so this is also a clear message).
		other = frappe.db.get_value(
			"Allocation Plan",
			{"delivery_date": self.delivery_date, "farm": self.farm, "name": ("!=", self.name)},
		)
		if other:
			frappe.throw(_("There is already a plan for {0} on {1}.").format(self.farm, self.delivery_date))
