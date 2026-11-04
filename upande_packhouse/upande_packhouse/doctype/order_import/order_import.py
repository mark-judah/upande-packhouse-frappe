# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OrderImport(Document):
	"""One uploaded order file and how far its import has got. The wizard saves it as the person works so an
	import can be resumed later without uploading the file again, and every Sales Order it creates links back."""
