import io

import barcode
import frappe
from barcode.writer import ImageWriter

# "Medium" size preset: module_width/module_height control the bar
# dimensions (mm), quiet_zone the margin either side. write_text=True prints
# the human-readable value under the bars (Code 128 requirement per spec).
MEDIUM_WRITER_OPTIONS = {
	"format": "PNG",
	"module_width": 0.3,
	"module_height": 18.0,
	"quiet_zone": 4.0,
	"font_size": 10,
	"text_distance": 3.0,
	"write_text": True,
	"dpi": 300,
}


def generate_box_barcode(box_name):
	"""Code 128 barcode PNG (medium size, human-readable text) encoding a
	Box Label's own ID -- the exact value staging/loading/dispatch already
	scan against (frappe.get_doc("Box Label", box_label_id) in mobile/api.py).

	Attaches via frappe.get_doc({"doctype": "File", "content": ...}) rather
	than hand-writing to disk and guessing the resulting file_url: File's own
	insert() is what decides the real on-disk path, so this is the only way
	to get a file_url that is guaranteed to resolve.
	"""
	code128 = barcode.get_barcode_class("code128")
	barcode_obj = code128(box_name, writer=ImageWriter())

	buf = io.BytesIO()
	barcode_obj.write(buf, options=MEDIUM_WRITER_OPTIONS)
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
