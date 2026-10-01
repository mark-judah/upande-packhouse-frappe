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
		# A shared platform (Customer Differentiator Column set) resolves each order
		# group to a customer by this value alone - two customers both claiming the
		# same raw value (e.g. both saying they're "JZF") would make that resolution
		# ambiguous, so it's refused here rather than silently picking one.
		if self.differentiator_value:
			clash = frappe.db.exists(
				"Customer Order Mapping",
				{
					"platform": self.platform,
					"differentiator_value": self.differentiator_value,
					"name": ["!=", self.name],
				},
			)
			if clash:
				frappe.throw(
					_("{0} on {1} is already mapped to a different customer ({2}).").format(
						self.differentiator_value, self.platform, clash
					)
				)
