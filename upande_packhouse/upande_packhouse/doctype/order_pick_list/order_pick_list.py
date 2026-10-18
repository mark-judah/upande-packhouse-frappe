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
		from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
			opl_submit_blockers,
		)

		if self.flags.get("ignore_opl_submit_checks"):
			return

		blockers = opl_submit_blockers(self)
		if blockers:
			frappe.throw(
				_("{0} is not ready to submit yet -- {1}.").format(self.name, "; ".join(blockers)),
				title=_("Allocation Incomplete"),
			)

	def on_trash(self):
		# A pick list whose buckets are moving (awaiting transfer at a farm, on a truck,
		# in transit, or planned on a trip) disappears from the farm's Bucket Requests and
		# its trips for good when deleted. Paths that release it on purpose delete with
		# ignore_on_trash, or set flags.ignore_transfer_check.
		if self.flags.get("ignore_transfer_check") or frappe.flags.get("ignore_opl_transfer_check"):
			return
		from upande_packhouse.api.remote_transfer.transfer_scheduling import opl_delete_blockers

		blockers = opl_delete_blockers(self.name)
		if blockers:
			frappe.throw(
				_(
					"{0} can't be deleted while its buckets are moving: {1}. Finish or cancel the transfer first."
				).format(self.name, "; ".join(blockers)),
				title=_("Pick List In Transfer"),
			)
