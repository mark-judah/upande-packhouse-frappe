# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""A Packing Team member working in another team for a date range. The team they belong to (Packing Team Member)
stays as it is; on the days covered they count for `to_team`."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class PackingTeamMove(Document):
	def validate(self):
		if getdate(self.to_date) < getdate(self.from_date):
			frappe.throw(_("To Date cannot be before From Date."))
		self.from_team = home_team(self.employee) or None
		if self.from_team and self.from_team == self.to_team:
			frappe.throw(
				_("{0} is already in {1}.").format(self.employee_name or self.employee, self.to_team)
			)
		clash = frappe.db.get_value(
			"Packing Team Move",
			{
				"employee": self.employee,
				"name": ("!=", self.name or ""),
				"from_date": ("<=", self.to_date),
				"to_date": (">=", self.from_date),
			},
			["name", "to_team"],
			as_dict=True,
		)
		if clash:
			frappe.throw(
				_("{0} is already moved to {1} for part of these dates ({2}).").format(
					self.employee_name or self.employee, clash.to_team, clash.name
				)
			)


def home_team(employee):
	return frappe.db.get_value(
		"Packing Team Member", {"employee": employee, "parenttype": "Packing Teams"}, "parent"
	)


def moves_on(day, employees=None):
	"""{employee: {name, to_team, from_team, from_date, to_date}} of the moves covering the day."""
	filters = {"from_date": ("<=", day), "to_date": (">=", day)}
	if employees is not None:
		if not employees:
			return {}
		filters["employee"] = ("in", list(employees))
	return {
		m.employee: m
		for m in frappe.get_all(
			"Packing Team Move",
			filters=filters,
			fields=["name", "employee", "to_team", "from_team", "from_date", "to_date"],
		)
	}
