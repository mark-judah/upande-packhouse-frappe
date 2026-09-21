# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class Specifications(Document):
	def validate(self):
		self.validate_colour_range()
		self.validate_temporary_dates()

	def validate_colour_range(self):
		"""Min/Max Colours Per Box only apply to Mixed Box. Their
		mandatory_depends_on eval only drives the Desk form's own UI --
		Frappe never enforces mandatory_depends_on in Python (grep the
		framework: it isn't there), so "required for Mixed Box" has to be
		a real check here or it's just a suggestion the API can ignore."""
		if self.box_assortment != "Mixed Box":
			return
		if not self.min_colours_per_box or not self.max_colours_per_box:
			frappe.throw(_("Min Colours Per Box and Max Colours Per Box are required for a Mixed Box spec"))
		if self.min_colours_per_box > self.max_colours_per_box:
			frappe.throw(
				_("Min Colours Per Box ({0}) cannot be greater than Max Colours Per Box ({1})").format(
					self.min_colours_per_box, self.max_colours_per_box
				)
			)

	def validate_temporary_dates(self):
		"""Same story as above: Valid From/Expiry Date's mandatory_depends_on
		on Temporary is Desk-UI-only, so it has to be re-checked here to
		actually be a rule rather than an easily-bypassed suggestion."""
		if self.spec_type != "Temporary":
			return
		if not self.valid_from or not self.expiry_date:
			frappe.throw(_("Valid From and Expiry Date are required for a Temporary spec"))
		if self.valid_from > self.expiry_date:
			frappe.throw(_("Valid From cannot be after Expiry Date"))
