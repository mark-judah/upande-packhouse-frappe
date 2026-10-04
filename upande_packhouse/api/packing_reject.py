# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packing rejects — a packer finds stems with quality issues while packing an OPL,
# so a box cannot be filled from what was issued.
#
#   1. The packer records the reject (Packing Screen → Packing issue → Quality
#      reject): which box, variety, stems, the QC issues and a reason. The stems go
#      to Rejects in stock, and a Quality Reporting is written so the reject shows
#      in QC's reports next to the Reject Recorder's.
#   2. A packing supervisor approves a replacement: stems from a matching bucket on
#      the sales farm's shelf (same variety and length, unallocated, oldest first).
#      Its stems are sold to the order line and issued to the packhouse in stock,
#      like any issued bucket, and leave its shelf.
#   3. The packer packs the replacement into the same box. With no bucket to take
#      from, the box is closed short instead (No Replacement).

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from upande_packhouse import stock_movement
from upande_packhouse.upande_packhouse.page.sales_allocation import sales_allocation as sa

REJECTS_WAREHOUSE = "Rejects - KR"
REJECTS_ENTRY_TYPE = "Packhouse Rejects"
BUSINESS_UNIT = "Roses"
#: Who may approve a replacement (also the doctype's write roles).
APPROVER_ROLES = ("Packing Supervisor", "Assistant Packhouse Manager", "System Manager")


def _can_approve():
	return bool(set(APPROVER_ROLES) & set(frappe.get_roles()))


def _opl(opl_name):
	opl = frappe.db.get_value(
		"Order Pick List",
		opl_name,
		["name", "docstatus", "farm", "team", "sales_order", "customer"],
		as_dict=True,
	)
	if not opl:
		frappe.throw(_("Order Pick List {0} not found.").format(opl_name))
	return opl


def _line(opl_name, variety, stem_length=None, bucket=None):
	"""The OPL row the rejected stems were issued on: its bucket's row when known,
	else the variety's (at that length when given)."""
	filters = {"parent": opl_name, "parenttype": "Order Pick List", "item_code": variety}
	if bucket:
		rows = frappe.get_all(
			"Pick List Item",
			filters={**filters, "bucket": bucket},
			fields=["name", "bucket", "stem_length", "custom_sale_order_item", "sales_order_item"],
			limit=1,
		)
		if rows:
			return rows[0]
	if stem_length:
		filters["stem_length"] = stem_length
	# Rejected while packing: the stems were issued, so an issued bucket first.
	rows = frappe.get_all(
		"Pick List Item",
		filters=filters,
		fields=["name", "bucket", "stem_length", "custom_sale_order_item", "sales_order_item"],
		order_by="issued desc, idx asc",
		limit=1,
	)
	if not rows:
		frappe.throw(_("{0} is not on {1}.").format(variety, opl_name))
	return rows[0]


def _bunch_bucket(bunch_id):
	"""The bucket a graded bunch came from (its Grading entry's bucket)."""
	if not bunch_id:
		return None
	return frappe.db.get_value(
		"Stock Entry",
		{"custom_bunch_id": bunch_id, "docstatus": 1},
		"custom_bucket_id",
		order_by="creation desc",
	)


def _describe(log):
	return {
		"name": log.name,
		"box_id": log.box_id,
		"variety": log.variety,
		"stem_length": log.stem_length,
		"stems_rejected": cint(log.stems_rejected),
		"bunches": cint(log.bunches),
		"reason": log.reason,
		"status": log.status,
		"donor_bucket": log.donor_bucket,
		"stems_replaced": cint(log.stems_replaced),
		"recorded_by": frappe.utils.get_fullname(log.recorded_by) if log.recorded_by else "",
		"approved_by": frappe.utils.get_fullname(log.approved_by) if log.approved_by else "",
	}


def _quality_reporting(log, opl, issues):
	"""The QC record of the reject, as Reject Recorder writes one, or None when this
	site has no Upande Quality. Never blocks the reject itself."""
	if not frappe.db.exists("DocType", "Quality Reporting"):
		return None
	try:
		farm = opl.farm or ""
		prefix = "Packhouse-" + (farm or "NA") + "-"
		seq = frappe.db.count("Quality Reporting", {"name": ["like", prefix + "%"]}) + 1
		name = prefix + str(seq).zfill(3)
		while frappe.db.exists("Quality Reporting", name):
			seq += 1
			name = prefix + str(seq).zfill(3)
		params = [
			{"parameter_name": i["parameter"], "count": cint(i.get("count")), "action": "Reject"}
			for i in issues
			if frappe.db.exists("QC Parameters", i["parameter"])
		]
		doc = frappe.get_doc(
			{
				"doctype": "Quality Reporting",
				"control_point": "Packhouse" if frappe.db.exists("QC Control Point", "Packhouse") else None,
				"farm": farm if farm and frappe.db.exists("Farm", farm) else None,
				"variety": log.variety,
				"control_action": "Rejected",
				"stems_checked": cint(log.stems_rejected),
				"stems_rejected": cint(log.stems_rejected),
				"stems_affected": cint(log.stems_rejected),
				"bunches_affected": cint(log.bunches),
				"quality_parameters": params,
				"inspection_type": "Online QC",
				"inspection_mode": "Packing Reject",
				"control_area": "Packing",
				"custom_order_pick_list": opl.name,
				"custom_length": log.stem_length or "",
				"custom_stems_affected": cint(log.stems_rejected),
				"custom_bunches_affected": cint(log.bunches),
				"custom_reason": log.reason or "",
				"custom_remarks": "Box {0}. {1}".format(log.box_id, log.remarks or "").strip(),
				"order_pick_list": opl.name,
			}
		)
		if opl.customer and frappe.db.exists("Customer", opl.customer):
			doc.customer = opl.customer
		if opl.team and frappe.db.exists("Packing Teams", opl.team):
			doc.team = opl.team
		if log.stem_length:
			doc.length = log.stem_length
		doc.name = name
		doc.flags.name_set = True
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(title="Packing reject: Quality Reporting failed", message=frappe.get_traceback())
		return None


def post_donor_stock(
	*, donor_bucket, variety, stem_length, stems, warehouse, farm, sales_order, so_item, opl
):
	"""Stems taken from a donor bucket on the shelf to make up an order's bunches:
	sold to the order line (shelf → Graded Sold) and issued to the packhouse
	(Graded Sold → Packhouse), as an issued bucket's are. Raises when either leg
	cannot be posted, so the caller's transaction rolls back. Returns the entries.

	Shared by packing rejects and the quality app's Grading QC replaceStems."""
	sold = stock_movement.move_allocation_to_sold(
		[
			{
				"bucket_id": donor_bucket,
				"item_code": variety,
				"qty": flt(stems),
				"warehouse": warehouse,
				"shelf_farm": farm,
				"stem_length": stem_length,
				"sales_order_item": so_item,
			}
		],
		BUSINESS_UNIT,
		sales_order=sales_order,
		opl=opl,
	)
	if sold.get("skipped") and not sold.get("posted"):
		frappe.throw(
			_("Bucket {0}'s stems could not be sold to the order: {1}").format(
				donor_bucket, "; ".join(str(x.get("reason")) for x in sold["skipped"])
			)
		)
	issued = stock_movement.post_issue_to_packhouse(
		bucket_id=donor_bucket,
		item_code=variety,
		qty=flt(stems),
		business_unit=BUSINESS_UNIT,
		farm=farm,
		stem_length=stem_length,
		so_item=so_item,
	)
	if not issued.get("moved"):
		frappe.throw(
			_("Bucket {0}'s stems could not be issued: {1}").format(donor_bucket, issued.get("reason"))
		)
	entries = [x.get("entry") for x in (sold.get("posted") or []) if isinstance(x, dict) and x.get("entry")]
	entries.append(issued["entry"])
	return entries


def _anchor(log):
	"""What sales_allocation._replacement_candidates matches a replacement against."""
	return frappe._dict(
		parent=log.order_pick_list,
		bucket=log.bucket or "",
		item_code=log.variety,
		stem_length=log.stem_length or "",
	)


def _donors(log, farm, limit=20):
	return sa._replacement_candidates(_anchor(log), farm, flt(log.stems_rejected), limit=limit)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@frappe.whitelist()
def get_reject_form(order_pick_list: str):
	"""Reasons, QC parameters and this OPL's rejects, for the Packing screen."""
	reasons = (
		frappe.get_all("Packhouse Rejection Reason", fields=["name", "reason"], order_by="reason asc")
		if frappe.db.exists("DocType", "Packhouse Rejection Reason")
		else []
	)
	parameters = (
		frappe.get_all("QC Parameters", fields=["name", "parameter"], order_by="parameter asc")
		if frappe.db.exists("DocType", "QC Parameters")
		else []
	)
	rejects = [
		_describe(frappe.get_doc("Packing Reject Log", n))
		for n in frappe.get_all(
			"Packing Reject Log",
			filters={"order_pick_list": order_pick_list},
			pluck="name",
			order_by="creation desc",
		)
	]
	return {
		"reasons": [{"name": r.name, "label": r.reason or r.name} for r in reasons],
		"parameters": [{"name": p.name, "label": p.parameter or p.name} for p in parameters],
		"rejects": rejects,
		"can_approve": _can_approve(),
	}


@frappe.whitelist(methods=["POST"])
def record_reject(
	order_pick_list: str,
	box_id: str,
	variety: str,
	stems_rejected: int,
	issues: str | list,
	stem_length: str | None = None,
	bunches: int | None = None,
	bunch_id: str | None = None,
	reason: str | None = None,
	remarks: str | None = None,
):
	"""Record stems rejected for quality while packing `box_id` of `order_pick_list`:
	they go to Rejects in stock, a Quality Reporting is written, and the reject waits
	for a supervisor to approve a replacement."""
	opl = _opl(order_pick_list)
	if opl.docstatus != 1:
		frappe.throw(_("{0} is not submitted.").format(order_pick_list))
	stems = cint(stems_rejected)
	if stems <= 0:
		frappe.throw(_("Enter how many stems are rejected."))
	if isinstance(issues, str):
		issues = json.loads(issues or "[]")
	issues = [
		{"parameter": (i.get("parameter") or "").strip(), "count": cint(i.get("count"))}
		for i in (issues or [])
		if (i.get("parameter") or "").strip()
	]
	if not issues:
		frappe.throw(_("Pick at least one quality issue."))
	if (
		reason
		and frappe.db.exists("DocType", "Packhouse Rejection Reason")
		and not frappe.db.exists("Packhouse Rejection Reason", reason)
	):
		frappe.throw(_("Unknown reason {0}.").format(reason))

	bunch_id = (bunch_id or "").strip() or None
	bucket = _bunch_bucket(bunch_id)
	line = _line(order_pick_list, variety, stem_length, bucket)
	bucket = bucket or line.bucket
	so_item = line.custom_sale_order_item or line.sales_order_item

	log = frappe.get_doc(
		{
			"doctype": "Packing Reject Log",
			"order_pick_list": order_pick_list,
			"box_id": str(box_id),
			"variety": variety,
			"stem_length": stem_length or line.stem_length or "",
			"stems_rejected": stems,
			"bunches": cint(bunches),
			"bunch_id": bunch_id,
			"bucket": bucket,
			"sale_order_item": so_item,
			"farm": opl.farm,
			"recorded_by": frappe.session.user,
			"reason": reason or "",
			"remarks": remarks or "",
			"status": "Pending Approval",
			"issues": issues,
		}
	)
	log.insert(ignore_permissions=True)

	# The rejected stems leave the packhouse for Rejects, under their bucket.
	row = stock_movement.mapping_row_for_farm(opl.farm, BUSINESS_UNIT)
	note = None
	if row and row.packhouse:
		hop = stock_movement.post_single_hop(
			item_code=variety,
			qty=stems,
			source=row.packhouse,
			target=REJECTS_WAREHOUSE,
			business_unit=BUSINESS_UNIT,
			entry_type=REJECTS_ENTRY_TYPE,
			bucket_id=bucket,
			farm=opl.farm,
			stem_length=log.stem_length,
			so_item=so_item,
			remarks=f"Rejected while packing box {box_id} of {order_pick_list}",
		)
		log.reject_stock_entry = hop.get("entry")
		note = hop.get("reason")
	else:
		note = f"no packhouse mapped for farm {opl.farm}"
	if note:
		frappe.log_error(title="Packing reject: nothing moved to Rejects", message=f"{log.name}: {note}")

	log.quality_reporting = _quality_reporting(log, opl, issues)
	log.save(ignore_permissions=True)
	frappe.get_doc("Order Pick List", order_pick_list).add_comment(
		"Info",
		_("{0} stems of {1} rejected while packing box {2} ({3}).").format(
			stems, variety, box_id, ", ".join(i["parameter"] for i in issues)
		),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the reject stands even if the reply is lost
	return {
		"success": True,
		"reject": _describe(log),
		"message": _("Reject recorded for box {0}. A supervisor approves the replacement.").format(box_id),
	}


@frappe.whitelist()
def replacement_options(reject: str, limit: int = 20):
	"""Buckets on the sales farm's shelf that can replace the rejected stems: same
	variety and length, unallocated, holding at least as many stems, oldest first."""
	log = frappe.get_doc("Packing Reject Log", reject)
	if log.status != "Pending Approval":
		return {"candidates": [], "message": _("This reject is already {0}.").format(log.status.lower())}
	found = _donors(log, log.farm, limit=max(1, min(cint(limit) or 20, 100)))
	return {
		"candidates": [
			{
				"bucket": c.bucket_id,
				"shelf": c.shelf,
				"stem_length": c.stem_length,
				"available_qty": flt(c.available_qty),
				"harvest_date": str(c.harvest_date)[:10] if c.harvest_date else None,
			}
			for c in found
		],
		"message": None
		if found
		else _("No unallocated {0} bucket of {1} at {2} holds {3} stems.").format(
			log.variety, log.stem_length or "", log.farm, cint(log.stems_rejected)
		),
		"can_approve": _can_approve(),
	}


@frappe.whitelist(methods=["POST"])
def approve_replacement(reject: str, donor_bucket: str):
	"""A supervisor approves `donor_bucket` as the replacement: its stems are sold to
	the order line and issued to the packhouse, and leave its shelf. The packer then
	packs them into the reject's box."""
	if not _can_approve():
		frappe.throw(_("Only a packing supervisor can approve a replacement."), frappe.PermissionError)
	frappe.db.sql("SELECT name FROM `tabPacking Reject Log` WHERE name = %s FOR UPDATE", reject)
	log = frappe.get_doc("Packing Reject Log", reject)
	if log.status != "Pending Approval":
		frappe.throw(_("This reject is already {0}.").format(log.status.lower()))
	opl = _opl(log.order_pick_list)
	stems = flt(log.stems_rejected)

	donor = next((c for c in _donors(log, log.farm, limit=500) if c.bucket_id == donor_bucket), None)
	if not donor:
		frappe.throw(_("Bucket {0} cannot replace these stems any more. Pick another.").format(donor_bucket))

	# ── Stock: shelf → Graded Sold (the sale, to this line) → Packhouse ──
	entries = post_donor_stock(
		donor_bucket=donor.bucket_id,
		variety=log.variety,
		stem_length=donor.stem_length,
		stems=stems,
		warehouse=donor.warehouse,
		farm=log.farm,
		sales_order=opl.sales_order,
		so_item=log.sale_order_item,
		opl=opl.name,
	)

	# ── Shelf: the stems leave the donor bucket; the row goes once it is empty ──
	left = flt(donor.stem_qty) - stems
	if left > stock_movement.QTY_TOLERANCE:
		frappe.db.set_value("Shelf Item", donor.shelf_item, "stem_qty", left)
		frappe.db.set_value("Shelf", donor.shelf, "modified", frappe.utils.now())
	else:
		sa._clear_shelf_item(donor.shelf_item, donor.shelf, "Issued to Sales Order")
		left = 0

	# ── Allocation: recorded against the line and already issued, so it never counts as outstanding ──
	bas_name = frappe.db.get_value(
		"Bucket Allocation Status",
		{"bucket_id": donor.bucket_id, "item_code": log.variety, "stem_length": donor.stem_length or ""},
		"name",
	)
	if bas_name:
		bas = frappe.get_doc("Bucket Allocation Status", bas_name, for_update=True)
		bas.append(
			"bucket_allocations",
			{
				"sales_order": opl.sales_order,
				"sales_order_item": log.sale_order_item,
				"quantity_allocated": stems,
				"cancelled": 0,
				"issued": 1,
			},
		)
		sa.recompute_bas_quantities(bas, shelf_qty=left)
		bas.flags.ignore_validate = True
		bas.save(ignore_permissions=True)

	log.status = "Replaced"
	log.donor_bucket = donor.bucket_id
	log.stems_replaced = cint(stems)
	log.approved_by = frappe.session.user
	log.approved_at = now_datetime()
	log.replacement_stock_entries = "\n".join(str(e) for e in entries)
	log.save(ignore_permissions=True)
	frappe.get_doc("Order Pick List", opl.name).add_comment(
		"Info",
		_("Box {0}: {1} stems of {2} replaced from bucket {3} (shelf {4}), approved by {5}.").format(
			log.box_id, cint(stems), log.variety, donor.bucket_id, donor.shelf, frappe.session.user
		),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {
		"success": True,
		"reject": _describe(log),
		"message": _(
			"Approved: take {0} stems from bucket {1} (shelf {2}) and pack them into box {3}."
		).format(cint(stems), donor.bucket_id, donor.shelf, log.box_id),
	}


@frappe.whitelist(methods=["POST"])
def close_without_replacement(reject: str):
	"""No bucket can replace the stems: the reject is closed and its box is packed
	short (the packer gives the under-pack reason as usual)."""
	frappe.db.sql("SELECT name FROM `tabPacking Reject Log` WHERE name = %s FOR UPDATE", reject)
	log = frappe.get_doc("Packing Reject Log", reject)
	if log.status != "Pending Approval":
		frappe.throw(_("This reject is already {0}.").format(log.status.lower()))
	log.status = "No Replacement"
	log.approved_by = frappe.session.user
	log.approved_at = now_datetime()
	log.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {
		"success": True,
		"reject": _describe(log),
		"message": _("No replacement for box {0}: close it short with an under-pack reason.").format(
			log.box_id
		),
	}
