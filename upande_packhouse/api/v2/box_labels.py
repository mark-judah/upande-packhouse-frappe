# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Box labels in the Label Print Manager (www/label-print-manager-v2.html, "Box labels" tab).

A Box Label is the sticker on a packed box. It is made when the box is packed
(Box Label doctype) and printed with the "Box Label" Print Format - Jinja plus
wkhtmltopdf, with logic of its own (QR codes, delivery point, mixed boxes). This
module never draws or edits a label: it finds them, renders them through that
same print format so a preview is exactly what prints, and records whether each
one has been printed (`Box Label.printed`).

Boxes are found through their Order Pick List -> Sales Order, so "delivery date"
means the order's delivery date, the same date every other v2 page uses.
"""

import base64
from collections import defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, today

DEFAULT_FORMAT = "Box Label"
MAX_PREVIEW = 150  # one PDF at a time: wkhtmltopdf renders a page per box
IMAGE_PAGES = 24  # pages drawn as images in the preview; the PDF has them all


def _check(ptype="read"):
	frappe.has_permission("Box Label", ptype, throw=True)


def _window(from_date, to_date):
	"""Today and tomorrow unless asked otherwise."""
	t = getdate(today())
	a = getdate(from_date) if from_date else t
	b = getdate(to_date) if to_date else getdate(add_days(t, 1))
	return (b, a) if a > b else (a, b)


_FROM = """
	FROM `tabBox Label` bl
	INNER JOIN `tabOrder Pick List` o ON o.name = bl.order_pick_list AND o.docstatus < 2
	INNER JOIN `tabSales Order` so ON so.name = o.sales_order AND so.docstatus = 1
"""


def formats():
	"""The print formats a Box Label can use, default first (read-only: they are never edited here)."""
	names = frappe.get_all(
		"Print Format", {"doc_type": "Box Label", "disabled": 0}, pluck="name", order_by="name asc"
	)
	if DEFAULT_FORMAT in names:
		names.remove(DEFAULT_FORMAT)
		names.insert(0, DEFAULT_FORMAT)
	return names


@frappe.whitelist(methods=["GET"])
def orders(
	from_date: Any = None,
	to_date: Any = None,
	customer: Any = None,
	q: Any = None,
	printed: Any = None,
):
	"""Sales orders delivering in the window that have box labels, with how many are printed."""
	_check()
	f, t = _window(from_date, to_date)
	params = {"f": f, "t": t}
	where = ["so.delivery_date BETWEEN %(f)s AND %(t)s"]
	if customer:
		where.append("(so.customer = %(c)s OR so.customer_name = %(c)s)")
		params["c"] = customer
	if (q or "").strip():
		where.append(
			"(so.name LIKE %(q)s OR so.customer_name LIKE %(q)s OR IFNULL(so.po_no, '') LIKE %(q)s OR bl.name LIKE %(q)s)"
		)
		params["q"] = f"%{q.strip()}%"
	having = ""
	if printed == "todo":
		having = "HAVING SUM(IFNULL(bl.printed, 0)) < COUNT(bl.name)"
	elif printed == "done":
		having = "HAVING SUM(IFNULL(bl.printed, 0)) >= COUNT(bl.name)"
	# nosemgrep: frappe-sql-format-injection -- the interpolated parts are fixed SQL fragments; every value is a bound parameter
	rows = frappe.db.sql(
		f"""
		SELECT so.name AS sales_order, so.customer, so.customer_name, so.delivery_date,
		       so.custom_delivery_point AS delivery_point, so.po_no,
		       COUNT(bl.name) AS boxes, SUM(IFNULL(bl.printed, 0)) AS printed,
		       GROUP_CONCAT(DISTINCT bl.order_pick_list ORDER BY bl.order_pick_list) AS opls,
		       MAX(bl.printed_at) AS last_printed
		{_FROM}
		WHERE {" AND ".join(where)}
		GROUP BY so.name, so.customer, so.customer_name, so.delivery_date, so.custom_delivery_point, so.po_no
		{having}
		ORDER BY so.delivery_date, so.customer_name, so.name
		LIMIT 600
		""",
		params,
		as_dict=True,
	)
	for r in rows:
		r["boxes"] = cint(r["boxes"])
		r["printed"] = cint(r["printed"])
		r["opls"] = [x for x in (r["opls"] or "").split(",") if x]
		r["delivery_date"] = str(r["delivery_date"])
		r["last_printed"] = str(r["last_printed"]) if r["last_printed"] else None
	# nosemgrep: frappe-sql-format-injection -- the interpolated parts are fixed SQL fragments; every value is a bound parameter
	customers = frappe.db.sql(
		f"""SELECT DISTINCT so.customer, so.customer_name {_FROM}
		    WHERE so.delivery_date BETWEEN %(f)s AND %(t)s ORDER BY so.customer_name""",
		{"f": f, "t": t},
		as_dict=True,
	)
	return {
		"success": True,
		"from_date": str(f),
		"to_date": str(t),
		"orders": rows,
		"totals": {
			"orders": len(rows),
			"boxes": sum(r["boxes"] for r in rows),
			"printed": sum(r["printed"] for r in rows),
		},
		"customers": [{"customer": c.customer, "name": c.customer_name or c.customer} for c in customers],
		"formats": formats(),
		"can_mark": bool(frappe.has_permission("Box Label", "write")),
	}


@frappe.whitelist(methods=["GET"])
def boxes(sales_order: str):
	"""The box labels of one sales order, with what is in each box."""
	_check()
	# nosemgrep: frappe-sql-format-injection -- the interpolated parts are fixed SQL fragments; every value is a bound parameter
	rows = frappe.db.sql(
		f"""
		SELECT bl.name, bl.box_number, bl.order_pick_list, bl.printed, bl.printed_at, bl.pack_rate,
		       bl.box_total_count, bl.delivery_point, bl.staged, bl.loaded, bl.delivered, bl.mix_name, bl.team
		{_FROM}
		WHERE so.name = %(so)s
		ORDER BY bl.order_pick_list, bl.box_number
		""",
		{"so": sales_order},
		as_dict=True,
	)
	if not rows:
		return {"success": True, "boxes": []}
	items = defaultdict(list)
	for it in frappe.get_all(
		"Box Label Item",
		{"parent": ["in", [r.name for r in rows]]},
		["parent", "variety", "qty", "length", "uom"],
		order_by="idx asc",
		limit_page_length=0,
	):
		items[it.parent].append(
			{"variety": it.variety, "qty": cint(it.qty), "length": it.length, "uom": it.uom}
		)
	for r in rows:
		r["printed"] = cint(r["printed"])
		r["printed_at"] = str(r["printed_at"]) if r["printed_at"] else None
		r["items"] = items.get(r.name, [])
		r["stage"] = (
			"Delivered"
			if r["delivered"]
			else "Loaded"
			if r["loaded"]
			else "Staged"
			if r["staged"]
			else "Packed"
		)
	return {"success": True, "boxes": rows}


def _resolve(names, sales_order):
	if names:
		names = list(dict.fromkeys(names))
		# nosemgrep: frappe-sql-format-injection -- the interpolated parts are fixed SQL fragments; every value is a bound parameter
		found = frappe.db.sql(
			f"""SELECT bl.name {_FROM} WHERE bl.name IN %(n)s ORDER BY bl.order_pick_list, bl.box_number""",
			{"n": tuple(names)},
			as_dict=True,
		)
	elif sales_order:
		# nosemgrep: frappe-sql-format-injection -- the interpolated parts are fixed SQL fragments; every value is a bound parameter
		found = frappe.db.sql(
			f"""SELECT bl.name {_FROM} WHERE so.name = %(so)s ORDER BY bl.order_pick_list, bl.box_number""",
			{"so": sales_order},
			as_dict=True,
		)
	else:
		frappe.throw(_("Choose a sales order or some box labels."))
	if not found:
		frappe.throw(_("No box labels found."))
	return [r.name for r in found]


@frappe.whitelist(methods=["POST"])
def preview(sales_order: str | None = None, names: Any = None, print_format: str | None = None):
	"""The labels as the print format draws them, as one PDF (base64). Nothing is changed."""
	_check()
	from frappe.utils.pdf import get_file_data_from_writer
	from pypdf import PdfWriter

	names = frappe.parse_json(names) if isinstance(names, str) else names
	allowed = formats()
	fmt = print_format or (allowed[0] if allowed else DEFAULT_FORMAT)
	if fmt not in allowed:
		frappe.throw(_("{0} is not a print format for Box Label.").format(fmt))
	todo = _resolve(names, sales_order)
	if len(todo) > MAX_PREVIEW:
		frappe.throw(
			_("That is {0} labels; preview up to {1} at a time (pick a single order or some boxes).").format(
				len(todo), MAX_PREVIEW
			)
		)
	t0 = now_datetime()
	writer = PdfWriter()
	try:
		for n in todo:
			frappe.get_print("Box Label", n, fmt, as_pdf=True, output=writer)
		pdf = get_file_data_from_writer(writer)
	finally:
		writer.close()
	import pymupdf

	doc = pymupdf.open(stream=pdf, filetype="pdf")
	pages = [
		"data:image/png;base64," + base64.b64encode(pg.get_pixmap(dpi=100).tobytes("png")).decode()
		for i, pg in enumerate(doc)
		if i < IMAGE_PAGES
	]
	page_mm = (
		[round(doc[0].rect.width * 25.4 / 72), round(doc[0].rect.height * 25.4 / 72)]
		if doc.page_count
		else [0, 0]
	)
	page_count = doc.page_count
	doc.close()
	return {
		"success": True,
		"pdf_base64": base64.b64encode(pdf).decode(),
		"pages": pages,
		"page_count": page_count,
		"page_mm": page_mm,
		"names": todo[:IMAGE_PAGES],
		"count": len(todo),
		"print_format": fmt,
		"filename": f"{sales_order or 'box-labels'}-box-labels.pdf",
		"seconds": round((now_datetime() - t0).total_seconds(), 1),
	}


@frappe.whitelist(methods=["POST"])
def set_printed(names: Any = None, printed: Any = 1, sales_order: str | None = None):
	"""Tick or untick `printed` on box labels (a whole order with `sales_order`)."""
	_check("write")
	names = frappe.parse_json(names) if isinstance(names, str) else names
	flag = 1 if cint(printed) else 0
	todo = _resolve(names, sales_order)
	ts = now_datetime() if flag else None
	for part in [todo[i : i + 500] for i in range(0, len(todo), 500)]:
		frappe.db.sql(
			"UPDATE `tabBox Label` SET printed = %s, printed_at = %s WHERE name IN %s",
			(flag, ts, tuple(part)),
		)
	return {"success": True, "updated": len(todo), "printed": flag}
