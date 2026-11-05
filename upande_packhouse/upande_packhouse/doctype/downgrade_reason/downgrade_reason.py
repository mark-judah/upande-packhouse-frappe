# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class DowngradeReason(Document):
	"""Why an order line is filled with longer stems than it ordered: picked from this list, not typed."""
