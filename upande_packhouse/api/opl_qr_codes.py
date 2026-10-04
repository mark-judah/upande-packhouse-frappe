# OPL QR Codes -- the "Generate QR Codes" action on the Order Pick List.
#
# Produces, for ONE Order Pick List, every QR code its buckets move through:
#   * buckets  -> the BUCKET QR code of each bucket on the pick rows (one per
#                 physical bucket, even when it is split across rows).
#   * bunches  -> the BUNCH QR codes of every bunch graded from those buckets.
#                 A bunch is tied to a bucket by its Grading Stock Entry, which
#                 carries both custom_bunch_id and custom_bucket_id.
#   * shelves  -> the SHELF QR code of each shelf those buckets sit on.
#   * trolleys -> one TROLLEY QR code per farm the OPL's buckets come from
#                 (noting how many still await transfer). Trolleys have no records -- the app takes
#                 whatever id the scanned code carries -- so each farm gets one
#                 fixed id (<shelf prefix>-TROLLEY-1) and every OPL prints the
#                 same label for the same physical trolley.
#
# plan() reads only. generate() draws the missing codes here with qrcode/PIL
# and stores them on the Bucket/Bunch/Shelf QR Code records. Images already
# present are never replaced: they are the codes already printed on physical
# labels. Trolley codes (and shelves with no Shelf QR Code record) have nowhere
# to be stored, so they are drawn fresh every time -- same id, same code.
#
# Payloads are what the label printers write and every scanner already parses:
#   bucket  {"35e82c":"bucket"}      (the id is the KEY -- never {"bucket_id": ...})
#   bunch   {"bunch_id":"BUNCH-123"}
#   shelf   {"shelf": "KPK-A1M"}     (json.dumps default spacing, as gen_label_id)
#   trolley {"KPK-TROLLEY-1": "trolley"}
# Bucket and bunch payloads have no spaces after the colons: that is what the
# existing batch labels carry, so a code generated here is byte-for-byte the
# pre-printed one.

import base64
import json
from io import BytesIO

import frappe
import qrcode
from frappe import _
from frappe.utils import cint, scrub

LABEL_DOCTYPES = ("Bucket QR Code", "Bunch QR Code", "Shelf QR Code")


@frappe.whitelist()
def plan(opl: str):
	return _plan(_get_opl(opl))


@frappe.whitelist(methods=["POST"])
def generate(opl: str):
	opl_doc = _get_opl(opl)
	result = _plan(opl_doc)

	saved = 0
	failed = []
	for label in _all_labels(result):
		if label["has_image"]:
			continue
		if not label["doctype"]:
			label["image"] = "data:image/png;base64," + _draw(label["payload"])
			continue
		try:
			label["image"] = _store_image(label)
			saved += 1
		except Exception:
			frappe.log_error(title=f"OPL QR Codes: {label['name']}")
			failed.append(label["name"])

	result["saved"] = saved
	result["failed"] = failed
	return result


def _all_labels(result):
	return (
		result["bucket_labels"] + result["bunch_labels"] + result["shelf_labels"] + result["trolley_labels"]
	)


def _get_opl(opl_name):
	if not opl_name:
		frappe.throw(_("An Order Pick List name is required."))
	if not frappe.db.exists("Order Pick List", opl_name):
		frappe.throw(_("Order Pick List {0} not found.").format(opl_name))
	opl = frappe.get_doc("Order Pick List", opl_name)
	opl.check_permission("read")
	return opl


def _draw(payload):
	qr = qrcode.QRCode(
		version=1,
		error_correction=qrcode.constants.ERROR_CORRECT_L,
		box_size=10,
		border=2,
	)
	qr.add_data(payload)
	qr.make(fit=True)
	buffered = BytesIO()
	qr.make_image(fill_color="black", back_color="white").save(buffered, format="PNG")
	return base64.b64encode(buffered.getvalue()).decode()


def _store_image(label):
	doctype, name = label["doctype"], label["name"]
	if doctype not in LABEL_DOCTYPES:
		frappe.throw(_("Unexpected label type {0}.").format(doctype))

	# Re-checked here: a scanner or another user may have stored one since
	# the plan was read.
	existing = frappe.db.get_value(doctype, name, "qr_code_image")
	if existing:
		return existing

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"qr_{scrub(name)}.png",
			"attached_to_doctype": doctype,
			"attached_to_name": name,
			"attached_to_field": "qr_code_image",
			"is_private": 0,
			"content": _draw(label["payload"]),
			"decode": 1,
		}
	)
	file_doc.insert(ignore_permissions=True)

	# set_value, not a full save: these records are written by the scanners
	# mid-flow (status, last_stock_entry) and a save here would stamp
	# modified over whatever a concurrent scan just wrote.
	frappe.db.set_value(doctype, name, "qr_code_image", file_doc.file_url, update_modified=False)
	return file_doc.file_url


def _label(kind, doctype, name, payload, image, lines):
	return {
		"kind": kind,
		"doctype": doctype,
		"name": name,
		"payload": payload,
		"has_image": 1 if image else 0,
		"image": image or None,
		"lines": [line for line in lines if line],
	}


def _shelf_prefix(farm):
	"""The prefix a farm's shelves use (Kapkolia -> KPK, Torongo -> TRG)."""
	row = frappe.db.sql(
		"""SELECT SUBSTRING_INDEX(name, '-', 1) AS p, COUNT(*) AS n FROM `tabShelf`
		WHERE farm = %s AND name LIKE '%%-%%' GROUP BY p ORDER BY n DESC LIMIT 1""",
		farm,
		as_dict=True,
	)
	return row[0].p if row else (farm or "TRL")[:3].upper()


def _plan(opl):
	rows = opl.get("table_ytkc") or []

	# One bucket = one physical bucket = ONE label, even when it is split
	# across several pick rows (two boxes off one bucket is routine).
	buckets = {}
	rows_without_bucket = []
	for row in rows:
		if not row.bucket:
			rows_without_bucket.append(row.idx)
			continue
		bucket = buckets.setdefault(
			row.bucket,
			{
				"variety": row.item_code,
				"stem_length": row.stem_length,
				"farm": row.farm or opl.farm,
				"shelf": row.shelf,
				"awaiting_transfer": 0,
			},
		)
		if (cint(row.get("awaiting_transfer")) or cint(row.get("in_transit"))) and not cint(
			row.get("shelved")
		):
			bucket["awaiting_transfer"] = 1

	farms = list({b["farm"] for b in buckets.values() if b["farm"]})
	farm_code = {}
	if farms:
		for farm in frappe.get_all("Farm", filters={"name": ["in", farms]}, fields=["name", "farm_code"]):
			farm_code[farm.name] = farm.farm_code

	# ---- buckets
	bucket_image = {}
	if buckets:
		for qr in frappe.get_all(
			"Bucket QR Code",
			filters={"name": ["in", list(buckets)]},
			fields=["name", "qr_code_image"],
			limit_page_length=0,
		):
			bucket_image[qr.name] = qr.qr_code_image

	bucket_labels = []
	for name in sorted(buckets):
		b = buckets[name]
		bucket_labels.append(
			_label(
				"bucket",
				"Bucket QR Code",
				name,
				json.dumps({name: "bucket"}, separators=(",", ":")),
				bucket_image.get(name),
				[name, b["variety"], b["stem_length"], farm_code.get(b["farm"]) or b["farm"]],
			)
		)

	# ---- bunches, via grading. A bunch scanned more than once (re-grading/
	# amendment) is still one physical bunch: count it once.
	bunch_labels = []
	buckets_without_bunches = []
	unknown_bunches = []

	if buckets:
		bunch_of_bucket = {}
		for entry in frappe.get_all(
			"Stock Entry",
			filters={
				"docstatus": ["!=", 2],
				"stock_entry_type": "Grading",
				"custom_bucket_id": ["in", list(buckets)],
			},
			fields=["custom_bucket_id", "custom_bunch_id"],
			limit_page_length=0,
		):
			if not entry.custom_bunch_id:
				continue
			found = bunch_of_bucket.setdefault(entry.custom_bucket_id, [])
			if entry.custom_bunch_id not in found:
				found.append(entry.custom_bunch_id)

		all_bunches = []
		for name in sorted(buckets):
			found = bunch_of_bucket.get(name) or []
			if not found:
				buckets_without_bunches.append(name)
			all_bunches.extend(found)

		known = {}
		if all_bunches:
			for qr in frappe.get_all(
				"Bunch QR Code",
				filters={"name": ["in", all_bunches]},
				fields=[
					"name",
					"item_code",
					"bunch_size",
					"stem_length",
					"farm",
					"farm_code",
					"qr_code_image",
				],
				limit_page_length=0,
			):
				known[qr.name] = qr

		for name in sorted(buckets):
			for bunch in bunch_of_bucket.get(name) or []:
				qr = known.get(bunch)
				if not qr:
					# Graded against an id with no Bunch QR Code record.
					# Reported, never invented.
					unknown_bunches.append(bunch)
					continue
				label = _label(
					"bunch",
					"Bunch QR Code",
					bunch,
					json.dumps({"bunch_id": bunch}, separators=(",", ":")),
					qr.qr_code_image,
					[qr.item_code, qr.bunch_size, qr.stem_length, qr.farm_code or qr.farm],
				)
				label["bucket"] = name
				bunch_labels.append(label)

	# ---- shelves the buckets sit on
	buckets_on_shelf = {}
	for name in sorted(buckets):
		if buckets[name]["shelf"]:
			buckets_on_shelf.setdefault(buckets[name]["shelf"], []).append(name)

	shelf_record = {}
	shelf_farm = {}
	if buckets_on_shelf:
		for qr in frappe.get_all(
			"Shelf QR Code",
			filters={"name": ["in", list(buckets_on_shelf)]},
			fields=["name", "qr_code_image"],
			limit_page_length=0,
		):
			shelf_record[qr.name] = qr
		for shelf in frappe.get_all(
			"Shelf", filters={"name": ["in", list(buckets_on_shelf)]}, fields=["name", "farm"]
		):
			shelf_farm[shelf.name] = shelf.farm

	shelf_labels = []
	for shelf, on_it in sorted(buckets_on_shelf.items()):
		record = shelf_record.get(shelf)
		shelf_labels.append(
			_label(
				"shelf",
				# No Shelf QR Code record -> nothing to store it on; drawn fresh.
				"Shelf QR Code" if record else None,
				shelf,
				json.dumps({"shelf": shelf}),
				record.qr_code_image if record else None,
				[shelf, shelf_farm.get(shelf), _("Buckets: {0}").format(", ".join(on_it))],
			)
		)

	# ---- one trolley per farm the buckets come from
	buckets_by_farm = {}
	for name in sorted(buckets):
		b = buckets[name]
		farm = b["farm"] or opl.farm
		if farm:
			buckets_by_farm.setdefault(farm, []).append(b)

	trolley_labels = []
	for farm, farm_buckets in sorted(buckets_by_farm.items()):
		trolley = f"{_shelf_prefix(farm)}-TROLLEY-1"
		waiting = sum(1 for b in farm_buckets if b["awaiting_transfer"])
		trolley_labels.append(
			_label(
				"trolley",
				None,
				trolley,
				json.dumps({trolley: "trolley"}),
				None,
				[
					trolley,
					farm,
					_("{0} bucket(s) to load").format(waiting)
					if waiting
					else _("{0} bucket(s) on this pick list").format(len(farm_buckets)),
				],
			)
		)

	return {
		"opl": opl.name,
		"customer": opl.customer,
		"order_name": opl.order_name,
		"bucket_labels": bucket_labels,
		"bunch_labels": bunch_labels,
		"shelf_labels": shelf_labels,
		"trolley_labels": trolley_labels,
		"rows_without_bucket": rows_without_bucket,
		"buckets_without_bunches": buckets_without_bunches,
		"unknown_bunches": unknown_bunches,
	}
