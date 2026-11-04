"""Auto Repeat override: standing orders (a Sales Order that repeats) appear in
advance of their delivery. Stock Auto Repeat makes the copy ON its schedule
date and gives it that same date as its delivery date, so the order shows up
already due. This subclass makes the schedule the day the order APPEARS, sets
the delivery date a chosen number of days later and wipes what belongs to the
source order's own fulfilment. The reminder email is core's Notify by Email. Every other
document type repeats exactly as in core -- see standing_orders.py.

If a future Frappe upgrade changes AutoRepeat.set_dates / update_doc /
send_notification / before_insert, re-diff this file
against frappe/automation/doctype/auto_repeat/auto_repeat.py.
"""

import frappe
from frappe import _
from frappe.automation.doctype.auto_repeat.auto_repeat import AutoRepeat
from frappe.core.doctype.communication.email import make
from frappe.utils import getdate, nowdate

from upande_packhouse import standing_orders as so


class PackhouseAutoRepeat(AutoRepeat):
	def before_insert(self):
		if so.is_standing(self):
			# Core moves a past start date up to today, which would silently change
			# the day the first order appears. Ask instead.
			if self.start_date and getdate(self.start_date) < getdate(nowdate()):
				frappe.throw(_("The first order cannot appear on a day that has passed."))
			return
		super().before_insert()

	def validate(self):
		super().validate()
		if so.is_standing(self):
			so.validate_standing(self)

	def set_dates(self):
		if not so.is_standing(self):
			return super().set_dates()
		if self.disabled:
			self.next_schedule_date = None
			return
		nxt = so.next_appearance(self, skip_today_if_made=True)
		if nxt is None:
			# nothing left before the end date
			self.next_schedule_date = None
			if self.end_date and getdate(self.end_date) >= getdate(self.start_date):
				return
		self.next_schedule_date = nxt
		if nxt and self.end_date and getdate(self.end_date) < getdate(nxt):
			frappe.throw(_("The Next Scheduled Date cannot be later than the End Date."))

	def update_doc(self, new_doc, reference_doc):
		super().update_doc(new_doc, reference_doc)
		if so.is_standing(self):
			so.apply_standing_dates(self, new_doc, reference_doc)

	def send_notification(self, new_doc):
		"""Core's Notify by Email, except that a standing order's email does not
		attach the draft's print unless a Print Format was chosen: the email is
		the reminder (its template shows the order), and a draft print can be
		refused by Print Settings, which would replace the whole message."""
		if not so.is_standing(self) or self.print_format:
			return super().send_notification(new_doc)
		ctx = {"doc": new_doc}
		subject = frappe.render_template(self.subject, ctx) if self.subject else _("New {0}: {1}").format(new_doc.doctype, new_doc.name)
		message = frappe.render_template(self.message or "", ctx) if self.message else _("A recurring {0} {1} has been created for you.").format(new_doc.doctype, new_doc.name)
		make(
			doctype=new_doc.doctype,
			name=new_doc.name,
			recipients=self.recipients,
			subject=subject,
			content=message,
			send_email=1,
		)
