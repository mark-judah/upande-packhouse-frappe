# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Bucket Watch: one row per bucket somebody wants to know about the next time it appears
anywhere in the process. Logic lives in api/v2/rt_watchlist.py."""

import frappe
from frappe.model.document import Document


class BucketWatch(Document):
	def before_validate(self):
		self.bucket_id = (self.bucket_id or "").strip().upper()
		if not self.bucket_id:
			frappe.throw("Bucket ID is required.")
