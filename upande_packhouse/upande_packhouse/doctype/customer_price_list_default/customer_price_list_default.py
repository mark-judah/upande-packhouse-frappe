# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CustomerPriceListDefault(Document):
	def validate(self):
		self.validate_unique_customer_currency()

	def validate_unique_customer_currency(self):
		"""Customer.default_price_list only holds one price list total -- this
		doctype adds the currency axis on top of it, so (customer, currency)
		is the real key: one default per currency a customer might be quoted in."""
		existing = frappe.db.exists(
			"Customer Price List Default",
			{
				"customer": self.customer,
				"currency": self.currency,
				"name": ["!=", self.name],
			},
		)
		if existing:
			frappe.throw(
				_("{0} already has a default price list for {1} ({2})").format(
					self.customer, self.currency, existing
				)
			)
