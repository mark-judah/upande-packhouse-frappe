# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class Specifications(Document):
	def validate(self):
		self.validate_colour_range()
		self.validate_temporary_dates()
		self.validate_approved_varieties()
		self.warn_bunch_shape()

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

	def validate_approved_varieties(self):
		"""A Colour is a (bunch_id, colour) group of Approved Variety rows --
		several varieties substituting for each other in the same slot, never
		delivered together. Exactly one of them has to be the primary/default
		pick, or nothing (a Sales Order autofill, a picker) knows which variety
		to reach for first when several are equally 'approved'."""
		groups = {}
		for av in self.approved_varieties or []:
			groups.setdefault((av.bunch_id or "", av.colour or ""), []).append(av)

		for (bunch_id, colour), rows in groups.items():
			primaries = [r for r in rows if r.is_primary]
			label = _("Bunch {0}, colour {1}").format(bunch_id or "—", colour or "—")
			if len(primaries) == 0:
				# Promote rather than throw. "No primary" is not a decision the
				# operator made -- it is what a freshly added row looks like,
				# and the commonest slot has exactly one variety in it, where
				# there is nothing to choose between. Throwing there would make
				# every new colour row an error to be dismissed before it could
				# be saved. Which one leads only becomes a real question once a
				# slot has substitutes, and the first row is the same answer the
				# backfill patch gives an existing spec.
				rows[0].is_primary = 1
				continue
			if len(primaries) > 1:
				frappe.throw(
					_("{0}: only one variety can be marked Primary — the rest are substitutes").format(label)
				)

	def warn_bunch_shape(self):
		"""Same bunch_id pairing rule the Sales Order autofill enforces
		(spec_autofill._bunch_shape): each bunch_id's Box Item rows must be
		one per colour, or one per Approved Variety row. A spec that breaks it
		still SAVES -- a spec is built up row by row, and blocking every
		intermediate save would make the tables impossible to edit -- but the
		person editing it is told here, rather than the person building an
		order finding out from a refused autofill."""
		from upande_packhouse.spec_autofill import bunch_shape_issues

		issues = bunch_shape_issues(self)
		if issues:
			frappe.msgprint(
				_(
					"This spec's bunch_id grouping won't autofill onto a Sales Order until it's fixed:<br>{0}"
				).format("<br>".join(issues)),
				title=_("Bunch ID Mismatch"),
				indicator="orange",
			)
