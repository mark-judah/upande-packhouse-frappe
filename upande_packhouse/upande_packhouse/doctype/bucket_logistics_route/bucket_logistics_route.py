# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from datetime import timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, get_datetime, getdate


def route_window(from_datetime, to_datetime, route_date=None):
	"""(from, to) as datetimes; a route saved before the window existed covers its whole day."""
	if from_datetime and to_datetime:
		return get_datetime(from_datetime), get_datetime(to_datetime)
	day = get_datetime(getdate(route_date))
	return day, day + timedelta(days=1)


class BucketLogisticsRoute(Document):
	def autoname(self):
		# One truck can run several routes a day: the first keeps BLR-{date}-{vehicle},
		# later ones get -2, -3, …
		self.set_route_date()
		base = "BLR-{0}-{1}".format(self.route_date, self.vehicle)
		name, n = base, 1
		while frappe.db.exists("Bucket Logistics Route", name):
			n += 1
			name = "{0}-{1}".format(base, n)
		self.name = name

	def set_route_date(self):
		if self.from_datetime:
			self.route_date = getdate(self.from_datetime)

	def validate(self):
		self.set_route_date()
		start, end = route_window(self.from_datetime, self.to_datetime, self.route_date)
		if start >= end:
			frappe.throw(_("To must be after From."))
		for other in frappe.get_all(
			"Bucket Logistics Route",
			filters={
				"vehicle": self.vehicle,
				"name": ["!=", self.name],
				"route_date": ["between", [add_days(start.date(), -7), end.date()]],
			},
			fields=["name", "from_datetime", "to_datetime", "route_date"],
		):
			o_start, o_end = route_window(other.from_datetime, other.to_datetime, other.route_date)
			if start < o_end and o_start < end:
				frappe.throw(
					_("{0} already has route {1} from {2} to {3}.").format(
						self.vehicle,
						other.name,
						o_start.strftime("%Y-%m-%d %H:%M"),
						o_end.strftime("%Y-%m-%d %H:%M"),
					)
				)
