# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PackingTeams(Document):
	def onload(self):
		# Today's On Leave / Off for each member, worked out on opening (not stored).
		from frappe.utils import getdate, nowdate

		from upande_packhouse.api.v2.allocation_people import _employees, _leave, _off_days

		ids = [m.employee for m in self.members or [] if m.employee]
		if not ids:
			return
		day = getdate(nowdate())
		staff = [e for e in _employees(day, ids) if e.employee in ids]
		off, leave = _off_days(staff, day), _leave(ids, day)
		for m in self.members:
			reason = off.get(m.employee) or leave.get(m.employee) or ""
			m.on_leave_off = 1 if reason else 0
			m.leave_off_reason = reason

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
		# The customers this team packs for: their orders go to this team first.
		served = set()
		for row in self.customers or []:
			if row.customer in served:
				frappe.throw(_("{0} is listed twice.").format(row.customer))
			served.add(row.customer)
		# The team packs at one sales farm (Production Settings > Shelf Locations, Sales Shelf); its
		# location is that farm's.
		from upande_packhouse.api.v2.allocation_teams import sales_farm_names

		if self.sales_farm and self.sales_farm not in sales_farm_names():
			frappe.throw(_("{0} is not a sales farm.").format(self.sales_farm))
		if self.sales_farm:
			self.location = frappe.db.get_value("Farm", self.sales_farm, "farm_location") or self.location
		# A person packs in one team.
		seen = set()
		for row in self.members or []:
			if row.employee in seen:
				frappe.throw(_("{0} is listed twice.").format(row.employee_name or row.employee))
			seen.add(row.employee)
			other = frappe.db.get_value(
				"Packing Team Member",
				{"employee": row.employee, "parenttype": "Packing Teams", "parent": ("!=", self.name)},
				"parent",
			)
			if other:
				frappe.throw(_("{0} is already in {1}.").format(row.employee_name or row.employee, other))

	def on_update(self):
		# team_sales_farms() is read once per request
		frappe.local._team_sales_farms = None

	def on_trash(self):
		frappe.local._team_sales_farms = None
