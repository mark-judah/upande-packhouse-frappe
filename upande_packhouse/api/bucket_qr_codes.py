# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# "Regenerate QR Codes" on the Bucket QR Code list: redraw the QR image of the
# selected buckets and replace the one stored on the record (a missing, broken or
# outdated image). The payload is the one every label and scanner uses —
# {"<bucket id>":"bucket"}, compact JSON (opl_qr_codes._draw) — so a regenerated code
# scans exactly like the printed label.

import json

import frappe
from frappe import _
from frappe.utils import scrub

from upande_packhouse.api.opl_qr_codes import _draw

DOCTYPE = "Bucket QR Code"
#: More than this many buckets regenerate in the background (a long request times out).
INLINE_LIMIT = 100


@frappe.whitelist(methods=["POST"])
def regenerate(names: str | list | None = None):
	names = frappe.parse_json(names) if isinstance(names, str) else names
	names = [n for n in dict.fromkeys(names or []) if n]
	if not names:
		frappe.throw(_("Select the buckets to regenerate."))
	frappe.has_permission(DOCTYPE, "write", throw=True)
	if len(names) > INLINE_LIMIT:
		frappe.enqueue(_regenerate, queue="long", timeout=3600, names=names, user=frappe.session.user)
		return {"queued": len(names)}
	return _regenerate(names)


def _regenerate(names, user=None):
	done, failed = [], []
	for name in names:
		try:
			_redraw(name)
			done.append(name)
		except Exception:
			frappe.log_error(title=f"Regenerate bucket QR: {name}")
			failed.append(name)
		if done and len(done) % 50 == 0:
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- long runs keep what's done
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	result = {"regenerated": len(done), "failed": failed}
	if user:  # ran in the background: tell the person who started it
		frappe.publish_realtime(
			"msgprint",
			_("Regenerated {0} bucket QR code(s){1}.").format(
				len(done), _(", {0} failed (see Error Log)").format(len(failed)) if failed else ""
			),
			user=user,
		)
	return result


def _redraw(name):
	bucket_id = frappe.db.get_value(DOCTYPE, name, "id") or name
	payload = json.dumps({bucket_id: "bucket"}, separators=(",", ":"), ensure_ascii=False)
	# The old image goes: one current QR file per bucket.
	for old in frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": DOCTYPE,
			"attached_to_name": name,
			"attached_to_field": "qr_code_image",
		},
		pluck="name",
	):
		frappe.delete_doc("File", old, ignore_permissions=True, force=1)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"qr_{scrub(name)}.png",
			"attached_to_doctype": DOCTYPE,
			"attached_to_name": name,
			"attached_to_field": "qr_code_image",
			"is_private": 0,
			"content": _draw(payload),
			"decode": 1,
		}
	)
	file_doc.insert(ignore_permissions=True)
	# set_value, not a save: scanners write status / last_stock_entry on these records
	# mid-flow, and a save would stamp over a concurrent scan.
	frappe.db.set_value(DOCTYPE, name, "qr_code_image", file_doc.file_url, update_modified=False)
	return file_doc.file_url
