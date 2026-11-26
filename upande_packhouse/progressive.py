# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Progressive issuing: a pick list still waiting for buckets from other farms is issued and packed box by box.

Production Settings > Progressive Issuing. A draft Order Pick List stays a draft until every bucket is on the sales
farm's shelf (opl_submit_blockers, unchanged), but once enough of its boxes are complete there (Start Issuing From
(Boxes), or every box of a smaller order) its complete boxes can be issued and packed. The other boxes are greyed
out in the apps until their buckets are shelved. When the last bucket is shelved the pick list submits as before and
the packing simply carries on under the same name.

A box is complete when every pick row with its box number is shelved at the sales farm -- not awaiting transfer,
not on a trip, not loaded without being shelved, not marked not found. A bucket can be issued when one of its boxes
is complete (a bucket can fill the end of one box and the start of the next): all its stems go to the packhouse and
the part for the other box waits there. Only complete boxes are packed.
"""

import frappe
from frappe import _
from frappe.utils import cint

ROW_FIELDS = (
	"name",
	"bucket",
	"custom_box_id",
	"awaiting_transfer",
	"in_transit",
	"loaded_in_trolley",
	"shelved",
	"not_found",
	"issued",
)


def settings():
	ps = frappe.get_cached_doc("Production Settings")
	return cint(ps.get("progressive_issuing")), max(1, cint(ps.get("progressive_issuing_from_boxes")))


def enabled():
	return bool(settings()[0])


def waiting_reason(row):
	"""Why a pick row's bucket is not on the sales farm's shelf yet, or "" when it is."""
	if cint(row.get("not_found")):
		return _("not found")
	if cint(row.get("in_transit")):
		return _("on a truck")
	if cint(row.get("loaded_in_trolley")) and not cint(row.get("shelved")):
		return _("loaded, not shelved")
	if cint(row.get("awaiting_transfer")):
		return _("awaiting transfer")
	return ""


def _box(row):
	return cint(row.get("custom_box_id")) or row.get("name")


def readiness(opl_name, rows=None):
	"""{boxes: {box: reason ("" = complete)}, total, ready, open, start_from} for a pick list. `open`: its complete
	boxes can be issued and packed now -- Progressive Issuing on, the pick list still a draft, and at least Start
	Issuing From boxes complete (every box when it has fewer)."""
	on, start_from = settings()
	if rows is None:
		rows = frappe.get_all(
			"Pick List Item",
			filters={"parent": opl_name, "parenttype": "Order Pick List"},
			fields=list(ROW_FIELDS),
		)
	boxes = {}
	for r in rows:
		key = _box(r)
		why = waiting_reason(r)
		if why and not boxes.get(key):
			boxes[key] = why
		else:
			boxes.setdefault(key, "")
	total = len(boxes)
	ready = sum(1 for why in boxes.values() if not why)
	draft = frappe.db.get_value("Order Pick List", opl_name, "docstatus") == 0 if opl_name else False
	is_open = bool(on and draft and total and ready and ready >= min(start_from, total))
	short = bool(on and draft and total) and _short(opl_name)
	return {
		"boxes": boxes,
		"total": total,
		"ready": ready,
		"start_from": min(start_from, total) if total else start_from,
		"short": bool(short),
		"open": is_open and not short,
	}


def _short(opl_name):
	"""Whether a draft pick list's lines still lack stems: its boxes are not all on it yet, so a box whose rows
	are shelved can still be short. Progressive issuing waits only for transfers, never for allocation."""
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import opl_short_lines

	opl = frappe.get_doc("Order Pick List", opl_name)
	return not opl.get("short_accepted") and bool(opl_short_lines(opl))


def readiness_many(opl_names):
	"""readiness() for several pick lists in one read."""
	opl_names = [o for o in opl_names or [] if o]
	if not opl_names:
		return {}
	by_opl = {}
	for r in frappe.get_all(
		"Pick List Item",
		filters={"parent": ("in", opl_names), "parenttype": "Order Pick List"},
		fields=["parent", *ROW_FIELDS],
	):
		by_opl.setdefault(r.parent, []).append(r)
	return {o: readiness(o, by_opl.get(o, [])) for o in opl_names}


def waiting_transfer(st):
	"""A draft pick list still waiting for buckets from other farms: listed in the apps with its boxes, issued
	box by box once it is open."""
	return any(st["boxes"].values())


def closed_reason(st):
	"""Why a waiting pick list cannot be issued yet, or "" when it is open."""
	if st["open"]:
		return ""
	if st.get("short"):
		return _("lines partially allocated")
	if st["start_from"] == 1:
		return _("issuing starts at 1 box")
	return _("issuing starts at {0} boxes").format(st["start_from"])


def bucket_issuable(opl_name, bucket):
	"""(ok, message): whether a progressive pick list's bucket can be issued now."""
	st = readiness(opl_name)
	if st.get("short"):
		return False, _("Pick list {0} is not submitted yet: its lines are partially allocated.").format(
			opl_name
		)
	if not st["open"]:
		return False, _(
			"Pick list {0} is not submitted yet: {1} of {2} boxes are complete on the shelf, issuing starts at {3}."
		).format(opl_name, st["ready"], st["total"], st["start_from"])
	rows = frappe.get_all(
		"Pick List Item",
		filters={"parent": opl_name, "parenttype": "Order Pick List", "bucket": bucket},
		fields=list(ROW_FIELDS),
	)
	if not rows:
		return True, ""  # not on this pick list: the issue scan's own checks answer
	if any(not st["boxes"].get(_box(r)) for r in rows):
		return True, ""
	box = _box(rows[0])
	return False, _(
		"Box {0} of {1} is not complete yet ({2}): issue it once its buckets are shelved."
	).format(box, opl_name, st["boxes"].get(box))


def throw_if_box_not_ready(opl_name, box_ids):
	"""Packing a draft pick list: only its complete boxes, and only while Progressive Issuing has it open."""
	if not opl_name or frappe.db.get_value("Order Pick List", opl_name, "docstatus") != 0:
		return
	st = readiness(opl_name)
	if not st["open"]:
		frappe.throw(
			_("Pick list {0} is not submitted yet: {1} of {2} boxes are complete on the shelf.").format(
				opl_name, st["ready"], st["total"]
			),
			title=_("Not ready to pack"),
		)
	for box in {cint(b) for b in box_ids or [] if cint(b)}:
		why = st["boxes"].get(box)
		if why:
			frappe.throw(
				_("Box {0} is not complete yet ({1}): pack it once its buckets are shelved.").format(
					box, why
				),
				title=_("Box not ready"),
			)
