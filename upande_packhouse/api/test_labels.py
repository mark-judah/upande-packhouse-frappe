# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Test labels for one Order Pick List — every QR a tester needs to walk the OPL
# through the mobile app without the label printers. Remote transfers come first,
# one section per stage, each in scan order:
#
#   1 · Farm cold store  Bucket Requests app: a trolley per remote farm, then that
#                        farm's buckets still waiting, each followed by the spare
#                        bucket Replace would swap in (if one is shelved)
#   2 · Packhouse arrival Shelving app: a free shelf at the SALES farm, then the
#                        buckets to put on it (two per shelf — shelveBucket's limit)
#   Buckets / Current shelves  buckets not being transferred, and where every
#                        bucket sits now
#   Bunches              one per bunch on each row. A bunch only passes the packing
#                        scan (fetchStockEntryByBunch) if a Grading Stock Entry ties
#                        its bunch id to the bucket, so those can be created too.
#
# Loading to the truck needs no QR: the app picks the truck from a list.
#
# Payloads are the SAME JSON the production label printer encodes
# (server_scripts/gen_label_id.py): {"<bucket>": "bucket"}, {"shelf": "<id>"},
# {"bunch_id": "<id>"}, {"<trolley>": "trolley"} — so the app parses them
# exactly like real labels.
#
# Re-running is safe: existing graded bunches of a bucket are reused before any
# new one is created, and destination shelves are only picked while empty.

import base64
import json
import math
from io import BytesIO

import frappe
import qrcode
from frappe import _
from frappe.utils import cint, flt

from upande_packhouse.api.transfer_control import transfer_hub
from upande_packhouse.stock_movement import default_cost_center, opl_rows

BUCKETS_PER_SHELF = 2
TEST_SHELF_ROW = "TST"  # destination shelves are <farm prefix>-TST<n>M


def _png(payload):
	qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
	qr.add_data(json.dumps(payload))
	qr.make(fit=True)
	buf = BytesIO()
	qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
	return base64.b64encode(buf.getvalue()).decode()


def _label(kind, label_id, payload, lines):
	return {
		"kind": kind,
		"id": label_id,
		"payload": json.dumps(payload),
		"lines": lines,
		"png": _png(payload),
	}


def _farm_of(row):
	wh = (row.get("source_warehouse") or row.get("warehouse") or "").strip()
	return wh.split(" ")[0] if wh else (row.get("farm") or "")


def _shelf_prefix(farm):
	"""The prefix a farm's real shelves use (Kapkolia -> KPK, Torongo -> TRG)."""
	row = frappe.db.sql(
		"""SELECT SUBSTRING_INDEX(name, '-', 1) AS p, COUNT(*) AS n FROM `tabShelf`
		WHERE farm = %s AND name LIKE '%%-%%' GROUP BY p ORDER BY n DESC LIMIT 1""",
		farm,
		as_dict=True,
	)
	return row[0].p if row else (farm or "SHF")[:3].upper()


def _trolley_id(farm):
	"""Trolley ids are free text on the app side; one stable test trolley per farm."""
	return f"{_shelf_prefix(farm)}-TROLLEY-TST"


def _replacement_spare(pick_list_item):
	"""What Replace in the Bucket Requests app would swap in for this row, or None."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
		find_requested_bucket_replacement,
	)

	try:
		res = find_requested_bucket_replacement(pick_list_item)
	except frappe.ValidationError:
		frappe.clear_last_message()  # "already left the cold room" etc. — not a label error
		return None
	return res if res.get("found") else None


def _ensure(doctype, name, values):
	if not frappe.db.exists(doctype, name):
		frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)


RESERVED_KEY = "upande_packhouse:test_label_dest_shelf"  # bucket -> planned destination shelf


def _shelved_buckets(shelf_id):
	return {
		(b or "").upper()
		for b in frappe.get_all("Shelf Item", filters={"parent": shelf_id}, pluck="bucket_id")
	}


def _assign_dest_shelves(farm, buckets):
	"""{bucket: shelf} at `farm` for buckets still to arrive by truck.

	A bucket keeps the shelf it was given last time (labels stay stable across
	reruns). Otherwise it goes to the first test shelf with room, counting both
	buckets already shelved there AND buckets other OPLs' labels reserved it for —
	without the reservation two OPLs were both told to use KPK-TST1M. Reservations
	live in the cache (test aid only) and drop out once the bucket is shelved."""
	prefix = _shelf_prefix(farm)
	reserved = {
		k.upper() if isinstance(k, str) else k.decode().upper(): (v if isinstance(v, str) else v.decode())
		for k, v in (frappe.cache.hgetall(RESERVED_KEY) or {}).items()
	}
	occupancy = {}  # shelf -> set of buckets (shelved + still-reserved)
	for bucket, shelf in list(reserved.items()):
		# Done only once the bucket sits on ANOTHER shelf at the same (sales) farm —
		# its current shelf back at the remote farm doesn't count.
		farm_prefix = shelf.split("-", 1)[0] + "-"
		on_shelves = frappe.get_all("Shelf Item", filters={"bucket_id": bucket}, pluck="parent")
		if any(s.startswith(farm_prefix) and s != shelf for s in on_shelves):
			frappe.cache.hdel(RESERVED_KEY, bucket)
			reserved.pop(bucket)
			continue
		occupancy.setdefault(shelf, _shelved_buckets(shelf)).add(bucket)

	out = {}
	for bucket in buckets:
		key = bucket.upper()
		if key in reserved and reserved[key].startswith(prefix + "-"):
			out[bucket] = reserved[key]
			continue
		k = 0
		while k < 500:
			k += 1
			shelf_id = f"{prefix}-{TEST_SHELF_ROW}{k}M"
			taken = occupancy.setdefault(shelf_id, _shelved_buckets(shelf_id))
			if len(taken) < BUCKETS_PER_SHELF:
				taken.add(key)
				out[bucket] = shelf_id
				frappe.cache.hset(RESERVED_KEY, key, shelf_id)
				break
	for shelf_id in set(out.values()):
		_ensure(
			"Shelf QR Code",
			shelf_id,
			{
				"shelf_id": shelf_id,
				"row_id": f"{prefix}-{TEST_SHELF_ROW}",
				"position": int(shelf_id.rsplit(TEST_SHELF_ROW, 1)[1][:-1]),
			},
		)
	return out


def _current_warehouse(bucket, item_code):
	"""Where this bucket's stems of `item_code` are now: the target of its most
	recent stock movement (Receiving, Remote Transfers, the Sold leg, …)."""
	row = frappe.db.sql(
		"""
		SELECT sed.t_warehouse FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.docstatus = 1 AND sed.item_code = %(item)s AND sed.t_warehouse IS NOT NULL
		  AND COALESCE(sed.custom_bucket_id, se.custom_bucket_id) = %(bucket)s
		ORDER BY se.posting_date DESC, se.posting_time DESC, se.creation DESC LIMIT 1
		""",
		{"bucket": bucket, "item": item_code},
	)
	return row[0][0] if row else None


def _graded_bunches(bucket, item_code, stem_length):
	return [
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT DISTINCT se.custom_bunch_id FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
			WHERE se.docstatus = 1 AND se.stock_entry_type = 'Grading'
			  AND se.custom_bucket_id = %(bucket)s AND sed.item_code = %(item)s
			  AND COALESCE(se.custom_stem_length, '') = %(len)s
			  AND COALESCE(se.custom_bunch_id, '') != ''
			ORDER BY se.creation
			""",
			{"bucket": bucket, "item": item_code, "len": stem_length or ""},
		)
	]


def _next_bunch_ids(count):
	"""Reserve `count` ids from the same counter the label printer uses, so a test
	bunch can never collide with a real printed one."""
	seq = frappe.get_single("QR Sequence")
	start = cint(seq.bunch_counter)
	seq.bunch_counter = start + count
	seq.save(ignore_permissions=True)
	return [f"BUNCH-{start + i}" for i in range(1, count + 1)]


def _grade_bunch(bunch_id, bucket, item_code, stem_length, bunch_uom, per_bunch, farm, warehouse):
	"""A Grading entry shaped like the real ones (Material Transfer within one
	warehouse, bunch + bucket on the parent) — net zero on stock, but it is what
	fetchStockEntryByBunch resolves a scanned bunch to its bucket with."""
	company = frappe.db.get_value("Warehouse", warehouse, "company")
	cc = frappe.db.get_value("Warehouse", warehouse, "custom_cost_center") or default_cost_center(company)
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Grading",
			"purpose": "Material Transfer",
			"company": company,
			"custom_bunch_id": bunch_id,
			"custom_bucket_id": bucket,
			"custom_stem_length": stem_length,
			"farm": farm,
			"business_unit": "Roses",
			"cost_center": cc,
			"remarks": "Test grading (OPL test labels)",
			"items": [
				{
					"item_code": item_code,
					"qty": 1,
					"uom": bunch_uom,
					"conversion_factor": per_bunch,
					"s_warehouse": warehouse,
					"t_warehouse": warehouse,
					"farm": farm,
					"cost_center": cc,
					"allow_zero_valuation_rate": 1,
				}
			],
		}
	)
	se.insert(ignore_permissions=True)
	se.submit()


# A production feature (the Order Pick List "test labels" button, System Manager
# only), not a test-only endpoint: @whitelist_for_tests would disable it outside
# test runs, which is what the semgrep rule below keys on the word "test" for.
# nosemgrep: frappe-semgrep-rules.rules.frappe-test-whitelist-missing-protection
@frappe.whitelist(methods=["POST"])
def generate_opl_test_labels(
	opl: str,
	buckets: int | str = 1,
	shelves: int | str = 1,
	bunches: int | str = 1,
	grade_bunches: int | str = 1,
):
	"""QR labels (base64 PNG + caption) for testing one Order Pick List.

	System Manager only: with `grade_bunches` it submits Grading Stock Entries."""
	frappe.only_for("System Manager")
	if not opl or not frappe.db.exists("Order Pick List", opl):
		frappe.throw(_("Order Pick List {0} not found").format(opl))

	doc = frappe.get_doc("Order Pick List", opl)
	rows = [r for r in opl_rows(doc) if r.get("bucket")]
	if not rows:
		frappe.throw(_("{0} has no buckets allocated yet.").format(opl))

	sales_farm = doc.get("farm") or transfer_hub()
	sections, warnings = [], []
	summary = {
		"buckets": 0,
		"shelves": 0,
		"trolleys": 0,
		"spares": 0,
		"bunches": 0,
		"grading_created": 0,
		"bunches_reused": 0,
	}

	# one entry per physical bucket (a bucket spans several rows)
	by_bucket = {}
	for r in rows:
		b = by_bucket.setdefault(
			r.bucket.upper(),
			{
				"bucket": r.bucket,
				"pli": r.name,
				"farm": _farm_of(r),
				"shelf": r.get("shelf"),
				"items": {},
				"stems": 0.0,
				"transfer": 0,
				"loaded": 0,
				"shelved": 0,
			},
		)
		b["items"].setdefault((r.item_code, r.get("stem_length") or ""), 0.0)
		b["items"][(r.item_code, r.get("stem_length") or "")] += flt(r.stock_qty)
		b["stems"] += flt(r.stock_qty)
		if (cint(r.get("awaiting_transfer")) or cint(r.get("in_transit"))) and not cint(r.get("shelved")):
			b["transfer"] = 1
		if cint(r.get("loaded_in_trolley")) or cint(r.get("in_transit")):
			b["loaded"] = 1
		if cint(r.get("shelved")):
			b["shelved"] = 1

	def bucket_label(b):
		_ensure("Bucket QR Code", b["bucket"], {"id": b["bucket"], "status": "In Use"})
		varieties = ", ".join(f"{i} {length}".strip() for (i, length) in b["items"])
		return _label(
			"bucket",
			b["bucket"],
			{b["bucket"]: "bucket"},
			[
				varieties,
				f"{int(b['stems'])} stems · {b['farm'] or '—'}",
				f"shelf {b['shelf'] or '—'}" + (" · to transfer" if b["transfer"] else ""),
			],
		)

	transfer = [b for b in by_bucket.values() if b["transfer"]]
	shelf_labels = 0

	# ── Stage 1: farm cold store (Bucket Requests) — trolley, then buckets + spares ──
	waiting = [b for b in transfer if not b["loaded"]]
	if cint(buckets) and waiting:
		labels = []
		by_farm = {}
		for b in waiting:
			by_farm.setdefault(b["farm"], []).append(b)
		for farm, bl in sorted(by_farm.items()):
			trolley = _trolley_id(farm)
			labels.append(
				_label(
					"trolley",
					trolley,
					{trolley: "trolley"},
					[f"{farm} · scan first", f"{len(bl)} bucket(s) to load"],
				)
			)
			summary["trolleys"] += 1
			for b in bl:
				labels.append(bucket_label(b))
				spare = _replacement_spare(b["pli"])
				if not spare:
					continue
				nb = spare["new_bucket"]
				_ensure("Bucket QR Code", nb, {"id": nb, "status": "Available"})
				labels.append(
					_label(
						"spare",
						nb,
						{nb: "bucket"},
						[
							f"{spare.get('variety') or ''} {spare.get('stem_length') or ''}".strip(),
							f"{int(flt(spare.get('available_qty')))} stems · shelf {spare.get('shelf') or '—'}",
							f"Replace spare for {b['bucket']}",
						],
					)
				)
				summary["spares"] += 1
		sections.append(
			{
				"key": "stage_farm",
				"title": _("1 · Farm cold store — Bucket Requests"),
				"hint": _(
					"Scan the trolley, then each bucket. A spare is what Replace swaps in — scan it "
					"instead after replacing. Then Load to truck (picked in the app, no QR)."
				),
				"labels": labels,
			}
		)

	# ── Stage 2: packhouse arrival (Shelving) — shelf, then the buckets for it ──
	if cint(shelves) and transfer:
		labels = []
		dest = {}
		for bucket, shelf_id in _assign_dest_shelves(sales_farm, [b["bucket"] for b in transfer]).items():
			dest.setdefault(shelf_id, []).append(bucket)
		for shelf_id, bl in sorted(dest.items()):
			labels.append(
				_label(
					"shelf",
					shelf_id,
					{"shelf": shelf_id},
					[f"{sales_farm} · shelve on arrival", ", ".join(bl)],
				)
			)
			shelf_labels += 1
			if cint(buckets):
				labels += [bucket_label(by_bucket[x.upper()]) for x in bl]
		sections.append(
			{
				"key": "stage_arrival",
				"title": _("2 · Packhouse arrival — Shelving"),
				"hint": _("Once the truck arrives at {0}: scan the shelf, then the buckets under it.").format(
					sales_farm
				),
				"labels": labels,
			}
		)

	if cint(buckets):
		summary["buckets"] = len(by_bucket)
		local = [bucket_label(b) for b in by_bucket.values() if not b["transfer"]]
		if local:
			sections.append({"key": "buckets", "title": _("Buckets"), "labels": local})

	if cint(shelves):
		labels = []
		current = {}
		for b in by_bucket.values():
			if b["shelf"]:
				current.setdefault(b["shelf"], []).append(b["bucket"])
		for shelf_id, bl in sorted(current.items()):
			farm = frappe.db.get_value("Shelf", shelf_id, "farm") or ""
			labels.append(
				_label("shelf", shelf_id, {"shelf": shelf_id}, [f"{farm} · current shelf", ", ".join(bl)])
			)
		summary["shelves"] = shelf_labels + len(labels)
		sections.append({"key": "shelves", "title": _("Current shelves"), "labels": labels})

	if cint(bunches):
		labels = []
		for b in by_bucket.values():
			for (item_code, length), stems in b["items"].items():
				row = next(
					r for r in rows if r.bucket.upper() == b["bucket"].upper() and r.item_code == item_code
				)
				bunch_uom = row.get("uom") if (row.get("uom") or "").lower().startswith("bunch") else None
				bunch_uom = bunch_uom or frappe.db.get_value("Item", item_code, "sales_uom")
				per_bunch = cint(
					frappe.db.get_value(
						"UOM Conversion Detail", {"parent": item_code, "uom": bunch_uom}, "conversion_factor"
					)
				)
				if per_bunch <= 0:
					warnings.append(_("{0}: no bunch size for {1} — skipped").format(b["bucket"], item_code))
					continue
				need = math.ceil(stems / per_bunch)
				ids = _graded_bunches(b["bucket"], item_code, length)[:need]
				summary["bunches_reused"] += len(ids)
				missing = need - len(ids)
				if missing and cint(grade_bunches):
					warehouse = _current_warehouse(b["bucket"], item_code)
					if not warehouse:
						warnings.append(
							_("{0}: no stock movement found — bunches not graded").format(b["bucket"])
						)
					else:
						for bunch_id in _next_bunch_ids(missing):
							_ensure(
								"Bunch QR Code",
								bunch_id,
								{
									"id": bunch_id,
									"item_code": item_code,
									"bunch_size": bunch_uom,
									"stem_length": length
									if frappe.db.exists("Stem Length", length)
									else None,
									"farm": b["farm"] or None,
									"farm_code": b["farm"],
								},
							)
							_grade_bunch(
								bunch_id,
								b["bucket"],
								item_code,
								length,
								bunch_uom,
								per_bunch,
								b["farm"],
								warehouse,
							)
							ids.append(bunch_id)
							summary["grading_created"] += 1
				elif missing:
					warnings.append(
						_(
							"{0} {1}: {2} bunch(es) not graded — tick 'Create grading entries' to make them scannable"
						).format(b["bucket"], item_code, missing)
					)
				for bunch_id in ids:
					labels.append(
						_label(
							"bunch",
							bunch_id,
							{"bunch_id": bunch_id},
							[f"{item_code} {length}".strip(), f"{bunch_uom} · bucket {b['bucket']}"],
						)
					)
		summary["bunches"] = len(labels)
		sections.append({"key": "bunches", "title": _("3 · Packing — Bunches"), "labels": labels})

	return {
		"opl": doc.name,
		"sales_order": doc.get("sales_order"),
		"customer": doc.get("customer"),
		"order_name": doc.get("order_name"),
		"sales_farm": sales_farm,
		"sections": sections,
		"summary": summary,
		"warnings": warnings,
	}
