# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BucketTransferEvent(Document):
	"""One step (or refused attempt) of a bucket's remote transfer — append-only, for
	traceability. Written by upande_packhouse.api.transfer_control.log_transfer_event."""
