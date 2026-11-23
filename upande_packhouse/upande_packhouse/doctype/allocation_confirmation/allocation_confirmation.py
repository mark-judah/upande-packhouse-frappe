# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

#: stems a line may be over its order by rounding alone
TOLERANCE = 0.5


class AllocationConfirmation(Document):
	def validate(self):
		if flt(self.stems) <= 0:
			frappe.throw(_("Stems to plan must be more than 0."))
		self.flag = _flag(self)
		self.validate_one_team_per_line()
		self.validate_not_above_order()

	def _other_live(self):
		from upande_packhouse.allocation_plan import LIVE

		if not self.sales_order_item or self.status not in LIVE:
			return None
		return frappe.get_all(
			"Allocation Confirmation",
			filters={
				"sales_order_item": self.sales_order_item,
				"status": ("in", LIVE),
				"name": ("not in", [self.name or "", *(frappe.flags.get("ac_replacing") or ())]),
			},
			fields=["name", "team", "stems", "fulfilled_stems"],
		)

	def validate_one_team_per_line(self):
		"""A team takes a whole line (or a whole mix): every live confirmation of a line is for the same team. Giving
		the line to another team is a revision (the line confirmed again), never a second team beside the first."""
		others = self._other_live()
		if not others or not self.team:
			return
		other = next((c for c in others if c.team and c.team != self.team), None)
		if other:
			frappe.throw(
				_(
					"This order line is confirmed for {0}: a line is packed by one team. Confirm it again for {1}."
				).format(other.team, self.team),
				title=_("One team per line"),
			)

	def validate_not_above_order(self):
		"""What a line has confirmed and not yet allocated, and what it already has allocated, never comes to more
		than it ordered: a confirm sets the line's total, so a second confirm is a revision, never more stems."""
		others = self._other_live()
		if others is None:
			return
		ordered = flt(frappe.db.get_value("Sales Order Item", self.sales_order_item, "stock_qty"))
		if ordered <= 0:
			return
		allocated = flt(
			frappe.db.sql(
				"""SELECT SUM(quantity_allocated) FROM `tabBucket Allocations`
				WHERE sales_order_item = %s AND IFNULL(cancelled, 0) = 0""",
				self.sales_order_item,
			)[0][0]
		)
		outstanding = sum(max(0.0, flt(c.stems) - flt(c.fulfilled_stems)) for c in others)
		mine = max(0.0, flt(self.stems) - flt(self.fulfilled_stems))
		if allocated + outstanding + mine > ordered + TOLERANCE:
			frappe.throw(
				_(
					"This would plan {0} stems for an order line of {1}: {2} allocated, {3} already confirmed. "
					"Confirm the line's new total instead."
				).format(int(allocated + outstanding + mine), int(ordered), int(allocated), int(outstanding)),
				title=_("More than ordered"),
			)


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
