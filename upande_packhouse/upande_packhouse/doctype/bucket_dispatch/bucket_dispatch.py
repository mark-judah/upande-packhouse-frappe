# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BucketDispatch(Document):
	def validate(self):
		if not self.dispatched_by:
			self.dispatched_by = frappe.session.user
		if not self.dispatch_datetime:
			self.dispatch_datetime = frappe.utils.now()

		self.total_dispatched = len(self.buckets or [])
		self.total_received = len([b for b in (self.buckets or []) if b.received])

		if self.total_dispatched and self.total_received >= self.total_dispatched:
			self.status = "Fully Received"
		elif self.total_received:
			self.status = "Partially Received"
		else:
			self.status = "Dispatched"
