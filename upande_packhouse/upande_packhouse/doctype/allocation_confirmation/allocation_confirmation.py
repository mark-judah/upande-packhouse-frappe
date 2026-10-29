# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class AllocationConfirmation(Document):
	def validate(self):
		if flt(self.stems) <= 0:
			frappe.throw(_("Stems to plan must be more than 0."))
		self.flag = _flag(self)


def _flag(doc):
	"""Longer than ordered / other variety / both: set from what was planned against what was ordered."""
	from upande_packhouse.availability import length_cm

	longer = length_cm(doc.length) > length_cm(doc.ordered_length) > 0
	other = bool(doc.ordered_variety and doc.variety and doc.variety != doc.ordered_variety)
	if longer and other:
		return "Both"
	if longer:
		return "Longer than ordered"
	if other:
		return "Other variety"
	return ""
