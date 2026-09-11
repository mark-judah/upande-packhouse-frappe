# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class OrderPickList(Document):
	def before_submit(self):
		# Doctype-level backstop: every programmatic path that submits an OPL
		# (allocation-time, shelving-time) already checks opl_submit_blockers
		# before calling .submit() -- but several roles (System Manager, Sales
		# Manager, Sales Master Manager, Sales Representative, Sales User) also
		# hold plain desk "Submit" permission on this doctype, and nothing at
		# the doctype level enforced the same rules against a manual click.
		# This re-runs the identical check unconditionally, so a draft OPL
		# that's short on allocated stems, still has buckets in transit, or
		# belongs to an incomplete mixed-box/bunch group can never actually
		# reach docstatus=1 -- via automation OR the Desk UI -- no matter who
		# clicks Submit or which code path got there.
		from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import opl_submit_blockers

		if self.flags.get("ignore_opl_submit_checks"):
			return

		blockers = opl_submit_blockers(self)
		if blockers:
			frappe.throw(
				_("{0} is not ready to submit yet -- {1}.").format(self.name, "; ".join(blockers)),
				title=_("Allocation Incomplete"),
			)
