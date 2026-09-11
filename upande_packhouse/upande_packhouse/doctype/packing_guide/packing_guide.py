# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PackingGuide(Document):
	def validate(self):
		# Always derived, never hand-typed -- the one place this box row's
		# stem contribution is computed, so it can never drift from
		# bunches x stems_per_bunch.
		self.stems = (self.bunches or 0) * (self.stems_per_bunch or 0)
