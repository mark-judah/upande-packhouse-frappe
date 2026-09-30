# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import base64
import io
import json

import frappe
import qrcode
from frappe import _
from frappe.model.document import Document


class BoxLabel(Document):
	pass


@frappe.whitelist()
def qr_codes(names=None, name=None):
	"""QR codes for each Box Label, drawn in memory (nothing is attached):

	* `box_qr` -- the label's own id as `{"box_label":"<id>"}`, the payload
	  staging/loading/dispatch scan (same as box_label_barcode_gen on develop).
	* `delivery_point_qr` -- the delivery point exactly as stored, no JSON
	  wrapper, no trimming; None when the label has no delivery point.

	`name` is still accepted for a form opened before `names` existed."""
	names = frappe.parse_json(names) if names else name
	if isinstance(names, str):
		names = [names]
	if not names:
		frappe.throw(_("Select at least one Box Label."))

	labels = []
	for label_name in names:
		doc = frappe.get_doc("Box Label", label_name)
		doc.check_permission("read")
		labels.append(
			{
				"name": doc.name,
				"box_qr": _qr_image(json.dumps({"box_label": doc.name}, separators=(",", ":"))),
				"delivery_point": doc.delivery_point,
				"delivery_point_qr": _qr_image(doc.delivery_point) if doc.delivery_point else None,
			}
		)
	return {"labels": labels}


def _qr_image(value):
	# Same preset as every other scannable code here (bucket, bunch, shelf, box).
	qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=2)
	qr.add_data(value)
	qr.make(fit=True)
	buf = io.BytesIO()
	qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
	return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
