import io
import json

import frappe
import qrcode

# Same QR settings as generate_qr_code_on_demand (mobile/api.py) -- every
# other scannable id in this system (bucket, bunch, shelf) uses this exact
# preset, so a Box Label's code looks and scans the same way.
QR_OPTIONS = {
	"version": 1,
	"error_correction": qrcode.constants.ERROR_CORRECT_L,
	"box_size": 10,
	"border": 2,
}


def generate_box_barcode(box_name):
	"""QR code PNG encoding a Box Label's own id -- the exact value
	staging/loading/dispatch already scan against
	(frappe.get_doc("Box Label", box_label_id) in mobile/api.py).

	Was a Code 128 barcode; switched to QR so it matches every other
	scannable code in this system (bucket/bunch/shelf all use
	generate_qr_code_on_demand's same preset) and so the mobile scanner's
	existing `{box_label:"…"}` extraction (karen-staging-store.ts /
	karen-loading-store.ts already accept this OR a bare string) just works
	with no client change. Field name stays `barcode` -- renaming it would
	touch every print format / report that reads it for no real benefit.

	Attaches via frappe.get_doc({"doctype": "File", "content": ...}) rather
	than hand-writing to disk and guessing the resulting file_url: File's own
	insert() is what decides the real on-disk path, so this is the only way
	to get a file_url that is guaranteed to resolve.
	"""
	payload = json.dumps({"box_label": box_name}, separators=(",", ":"))

	qr = qrcode.QRCode(**QR_OPTIONS)
	qr.add_data(payload)
	qr.make(fit=True)
	qr_img = qr.make_image(fill_color="black", back_color="white")

	buf = io.BytesIO()
	qr_img.save(buf, format="PNG")
	buf.seek(0)

	# Re-generating (e.g. a re-pack before staging) replaces the old image
	# outright rather than trying to rewrite it in place -- `content` is
	# only consumed by File's own before_insert, not a plain save(). Filter
	# by attached_to_field too: Frappe's own attach_files_to_document
	# on_update hook (frappe/core/doctype/file/utils.py) matches on that
	# same tuple and will otherwise create a second, "properly linked" File
	# the next time this Box Label is saved, thinking none exists yet.
	existing_name = frappe.db.get_value(
		"File",
		{
			"attached_to_doctype": "Box Label",
			"attached_to_name": box_name,
			"attached_to_field": "barcode",
			"is_folder": 0,
		},
		"name",
	)
	if existing_name:
		frappe.delete_doc("File", existing_name, ignore_permissions=True, delete_permanently=True)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{box_name}.png",
			"attached_to_doctype": "Box Label",
			"attached_to_name": box_name,
			"attached_to_field": "barcode",
			"content": buf.read(),
			"is_private": 0,
		}
	)
	file_doc.insert(ignore_permissions=True)

	return file_doc.file_url
