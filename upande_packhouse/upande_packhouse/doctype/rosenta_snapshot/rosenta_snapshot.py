# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RosentaSnapshot(Document):
	"""One Rosenta report for one delivery date, as last pulled by api/v2/rosenta_sync.py. TEMPORARY."""
