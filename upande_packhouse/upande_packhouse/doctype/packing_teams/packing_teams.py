# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PackingTeams(Document):
	def validate(self):
		# One default crop: the one the team is planned for first. The others are its fall-back crops,
		# planned when the default has no orders for the day.
		seen = set()
		for row in self.crops or []:
			if row.crop in seen:
				frappe.throw(_("{0} is listed twice.").format(row.crop))
			seen.add(row.crop)
		defaults = [r for r in self.crops or [] if r.is_default]
		if len(defaults) > 1:
			frappe.throw(_("Only one crop can be the default."))
		if self.crops and not defaults:
			self.crops[0].is_default = 1
