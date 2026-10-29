# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BucketReservation(Document):
	"""One row per bucket held for a planned order line. Allocation reads these rows by bucket_id and
	status; see upande_packhouse.availability.reserved_by_bucket."""
