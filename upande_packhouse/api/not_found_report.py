# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Bucket report: before a requested bucket is reported -- missing (not found), damaged, the wrong variety or
issued offline -- at the farm (Bucket Requests) or at issuing, the QC scans the shelf the system put it on and
photographs that shelf. Only then is it replaced, marked not found or marked issued, and the shelf and photo stay on
the record: the pick row (not found or issued offline, no replacement) or the Bucket Replacement.

A different shelf is refused: the bucket is looked for where the system says it is.
"""

import json

import frappe
from frappe import _

#: A shelf photo counts for a report this many hours after it was taken.
PHOTO_HOURS = 12


def parse_shelf(scan):
	"""A shelf QR (`{"shelf": "KPK-G3M"}`) or the shelf typed in, upper case."""
	text = (scan or "").strip()
	if text.startswith("{"):
		try:
			text = str((json.loads(text) or {}).get("shelf") or "")
		except (ValueError, AttributeError):
			text = ""
	return text.strip().upper()


def expected_shelf(pick_list_item):
	"""The shelf the system has the row's bucket on: its shelf now, else the shelf it was allocated from."""
	row = frappe.db.get_value(
		"Pick List Item", pick_list_item, ["bucket", "shelf", "item_code"], as_dict=True
	)
	if not row:
		return ""
	# a bucket id is reused: only its shelf row of this variety is this bucket
	now = (
		frappe.db.sql(
			"""SELECT parent FROM `tabShelf Item` WHERE bucket_id = %s AND variety = %s
			   ORDER BY modified DESC LIMIT 1""",
			(row.bucket, row.item_code),
		)
		if row.bucket
		else None
	)
	return ((now[0][0] if now else "") or row.shelf or "").strip().upper()


def _row(pick_list_item=None, opl_name=None, bucket=None, sale_order_item=None):
	if pick_list_item:
		return pick_list_item
	from upande_packhouse.api import offline_issue

	return offline_issue._anchor_row(opl_name, bucket, sale_order_item)


def shelf_check(pick_list_item, scan):
	"""(ok, expected, scanned, message)."""
	expected = expected_shelf(pick_list_item)
	scanned = parse_shelf(scan)
	bucket = frappe.db.get_value("Pick List Item", pick_list_item, "bucket") or ""
	if not scanned:
		# something was scanned, but not a shelf QR (a bucket's, a trolley's ...)
		if (scan or "").strip():
			return (
				False,
				expected,
				scanned,
				_("That is not a shelf QR: scan shelf {0}.").format(expected)
				if expected
				else _("That is not a shelf QR: scan the shelf."),
			)
		return (
			False,
			expected,
			scanned,
			_("Scan the shelf {0} first.").format(expected) if expected else _("Scan the shelf first."),
		)
	if not frappe.db.exists("Shelf", scanned) and not frappe.db.exists("Shelf", {"name": ("like", scanned)}):
		return (
			False,
			expected,
			scanned,
			_("{0} is not a shelf: scan shelf {1}.").format(scanned, expected)
			if expected
			else _("{0} is not a shelf.").format(scanned),
		)
	if expected and scanned != expected:
		return (
			False,
			expected,
			scanned,
			_("{0} was put on {1}, not {2}: look on {1} and scan it.").format(bucket, expected, scanned),
		)
	return True, expected, scanned, ""


@frappe.whitelist(methods=["POST"])
def checkNotFoundShelf(
	pick_list_item: str | None = None,
	opl_name: str | None = None,
	bucket: str | None = None,
	sale_order_item: str | None = None,
	scan: str | None = None,
):
	"""Step one of a not-found report: the shelf scanned is the one the system put the bucket on."""
	try:
		pli = _row(pick_list_item, opl_name, bucket, sale_order_item)
	except frappe.ValidationError as e:
		frappe.local.message_log = []
		return {"ok": False, "message": str(e)}
	ok, expected, scanned, message = shelf_check(pli, scan)
	return {"ok": ok, "expected": expected, "shelf": scanned, "message": message}


def evidence(pick_list_item, scanned_shelf, shelf_photo):
	"""The shelf of a not-found report, checked: the shelf the bucket was put on, scanned -- that is the evidence.
	A photo is optional (older app builds still send one): kept only when this user uploaded it for this report.
	Throws when the shelf is missing or wrong."""
	ok, expected, scanned, message = shelf_check(pick_list_item, scanned_shelf)
	if not ok:
		frappe.throw(message, title=_("Scan the shelf"))
	photo = (shelf_photo or "").strip()
	# a shelf photo this user uploaded for this report -- not any file on the site, not an old report's
	fresh = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-PHOTO_HOURS)
	if photo and not frappe.db.exists(
		"File",
		{
			"file_url": photo,
			"file_name": ("like", "shelf-%"),
			"owner": frappe.session.user,
			"creation": (">=", fresh),
		},
	):
		photo = ""
	return {"scanned_shelf": scanned, "shelf_photo": photo}


def for_replacement():
	"""The report's fields for the Bucket Replacement being recorded (set by the endpoint around the swap)."""
	return dict(frappe.flags.get("not_found_evidence") or {})


def reports(pairs):
	"""{(pick list, bucket): report} for buckets reported on these pick lists -- not found, damaged, wrong variety,
	issued offline, or the bucket replacing one: the shelf scanned, photo, who and when, from the pick rows (not
	found, no replacement) and the Bucket Replacements. Only the same pick list's: a bucket id is reused, and an
	old report belongs to another bucket."""
	pairs = {(o, b) for o, b in pairs or [] if o and b}
	if not pairs:
		return {}
	opls = tuple({o for o, _b in pairs})
	buckets = tuple({b for _o, b in pairs})
	out = {}
	for r in frappe.db.sql(
		"""SELECT parent AS opl, bucket, report_shelf AS shelf, report_photo AS photo, not_found_by AS by_user,
		          not_found_at AS at, farm, 'Missing' AS reason
		   FROM `tabPick List Item`
		   WHERE parenttype = 'Order Pick List' AND not_found = 1 AND parent IN %(o)s AND bucket IN %(b)s
		   ORDER BY not_found_at DESC""",
		{"o": opls, "b": buckets},
		as_dict=True,
	):
		if (r.opl, r.bucket) in pairs:
			out.setdefault((r.opl, r.bucket), r)
	for r in frappe.db.sql(
		"""SELECT order_pick_list AS opl, old_bucket AS bucket, new_bucket, scanned_shelf AS shelf,
		          shelf_photo AS photo, reported_by AS by_user, reported_at AS at, farm, expected_shelf, reason
		   FROM `tabBucket Replacement`
		   WHERE order_pick_list IN %(o)s AND (old_bucket IN %(b)s OR new_bucket IN %(b)s)
		   ORDER BY reported_at DESC""",
		{"o": opls, "b": buckets},
		as_dict=True,
	):
		# a replacement shows the report of the bucket it stands in for
		for key in ((r.opl, r.bucket), (r.opl, r.new_bucket)):
			if key in pairs and key not in out:
				out[key] = r
	names = {}
	result = {}
	for key, r in out.items():
		if r.by_user and r.by_user not in names:
			names[r.by_user] = frappe.db.get_value("User", r.by_user, "full_name") or r.by_user
		result[key] = {
			"bucket": r.bucket,
			"replaced_by": r.get("new_bucket") or "",
			"shelf": r.shelf or r.get("expected_shelf") or "",
			"photo": r.photo or "",
			"by": names.get(r.by_user, ""),
			"at": str(r.at or ""),
			"farm": r.farm or "",
			"reason": r.get("reason") or "Missing",
		}
	return result


def save_on_rows(opl_name, bucket, report):
	"""The report's shelf and photo on the bucket's rows of a pick list (marked issued offline: no replacement)."""
	if not report:
		return
	frappe.db.sql(
		"""UPDATE `tabPick List Item` SET report_shelf = %(s)s, report_photo = %(p)s
		   WHERE parent = %(o)s AND parenttype = 'Order Pick List' AND bucket = %(b)s""",
		{
			"s": report.get("scanned_shelf") or "",
			"p": report.get("shelf_photo") or "",
			"o": opl_name,
			"b": bucket,
		},
	)
	attach_photo(report.get("shelf_photo"), "Order Pick List", opl_name)


def attach_photo(photo, doctype, name, field=None):
	"""Attach an uploaded shelf photo to the record it belongs to (it shows in that record's attachments). A photo
	already attached elsewhere -- a bucket shared by several pick lists -- stays where it is."""
	if not photo or not name:
		return
	for f in frappe.get_all(
		"File",
		filters=[["file_url", "=", photo], ["attached_to_name", "is", "not set"]],
		pluck="name",
	):
		frappe.db.set_value(
			"File",
			f,
			{"attached_to_doctype": doctype, "attached_to_name": name, "attached_to_field": field or ""},
			update_modified=False,
		)


def clear_loose_photos():
	"""Daily: shelf photos taken for a report that was then cancelled -- attached to nothing after a day -- go."""
	for name in frappe.get_all(
		"File",
		filters=[
			["file_name", "like", "shelf-%"],
			["attached_to_name", "is", "not set"],
			["creation", "<", frappe.utils.add_days(frappe.utils.now_datetime(), -1)],
		],
		pluck="name",
	):
		# still named on a record (attached elsewhere first): keep it
		url = frappe.db.get_value("File", name, "file_url")
		if frappe.db.exists("Bucket Replacement", {"shelf_photo": url}) or frappe.db.exists(
			"Pick List Item", {"report_photo": url}
		):
			continue
		frappe.delete_doc("File", name, ignore_permissions=True, force=True)
