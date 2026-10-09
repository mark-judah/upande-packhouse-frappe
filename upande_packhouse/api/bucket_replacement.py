# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Replacement — the record of a requested bucket swapped for another one
# (sales_allocation._replace_requested_bucket). It says which bucket was replaced,
# why, by whom, where it should have been, the order and truck it was meant for and
# the stock entries that traded the two. The replaced bucket stays "Open" (missing)
# until it is shelved again (Found), or someone marks it Discarded / Written Off.

import frappe
from frappe import _

REASONS = ("Missing", "Damaged", "Wrong variety", "Issued offline", "Quality issue", "Other")
RESOLUTIONS = ("Found", "Discarded", "Written Off")


def record(
	old_bucket,
	new_bucket,
	anchor,
	rows,
	farm,
	opl_name,
	new_shelf,
	stems,
	stock_entries,
	reason=None,
	notes=None,
	extra=None,
):
	"""Create the replacement record (inside the swap's transaction). `extra` sets
	further fields -- the quality-issue section (packing_quality)."""
	opl = frappe.db.get_value("Order Pick List", opl_name, ["order_name", "sales_order"], as_dict=True) or {}
	trip = frappe.db.sql(
		"""SELECT t.name, t.vehicle FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name
		WHERE o.order_pick_list = %s AND IFNULL(o.farm, '') = %s
		  AND t.status IN ('Draft', 'Requested', 'Scheduled', 'Dispatched')
		ORDER BY t.creation DESC LIMIT 1""",
		(opl_name, farm or ""),
		as_dict=True,
	)
	doc = frappe.get_doc(
		{
			"doctype": "Bucket Replacement",
			"old_bucket": old_bucket,
			"new_bucket": new_bucket,
			"status": "Open",
			"reason": reason if reason in REASONS else "Missing",
			"notes": notes or "",
			"order_pick_list": opl_name,
			"order_name": opl.get("order_name") or opl_name,
			"sales_order": opl.get("sales_order"),
			"farm": farm if farm and frappe.db.exists("Farm", farm) else None,
			"item_code": anchor.item_code,
			"stem_length": anchor.get("stem_length") or "",
			"stems": stems,
			"expected_shelf": anchor.get("shelf") or "",
			"new_shelf": new_shelf or "",
			"trip": trip[0].name if trip else None,
			"vehicle": trip[0].vehicle if trip and trip[0].vehicle else None,
			"reported_by": frappe.session.user,
			"reported_at": frappe.utils.now(),
			"stock_entries": ", ".join(e for e in stock_entries if e),
			**(extra or {}),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def mark_found(bucket_id, shelf=None, farm=None):
	"""The replaced bucket was shelved again: close its open records as Found."""
	found = []
	for name in frappe.get_all(
		"Bucket Replacement", filters={"old_bucket": bucket_id, "status": "Open"}, pluck="name"
	):
		frappe.db.set_value(
			"Bucket Replacement",
			name,
			{
				"status": "Found",
				"resolved_by": frappe.session.user,
				"resolved_at": frappe.utils.now(),
				"resolution": _("Shelved on {0}{1}").format(shelf or "?", f" ({farm})" if farm else ""),
			},
		)
		found.append(name)
	return found


@frappe.whitelist(methods=["POST"])
def resolveBucketReplacement(
	name: str | None = None, status: str | None = None, resolution: str | None = None
):
	"""Close an open replacement by hand: Found, Discarded or Written Off."""
	if not name or not frappe.db.exists("Bucket Replacement", name):
		frappe.throw(_("Replacement not found."))
	if status not in RESOLUTIONS:
		frappe.throw(_("Status must be one of {0}.").format(", ".join(RESOLUTIONS)))
	frappe.db.set_value(
		"Bucket Replacement",
		name,
		{
			"status": status,
			"resolved_by": frappe.session.user,
			"resolved_at": frappe.utils.now(),
			"resolution": resolution or status,
		},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"name": name, "status": status}


def for_bucket(bucket_id):
	"""Every replacement the bucket took part in (as the replaced or the replacement one)."""
	return frappe.get_all(
		"Bucket Replacement",
		or_filters=[["old_bucket", "=", bucket_id], ["new_bucket", "=", bucket_id]],
		fields=[
			"name",
			"old_bucket",
			"new_bucket",
			"status",
			"reason",
			"notes",
			"order_pick_list",
			"order_name",
			"farm",
			"item_code",
			"stem_length",
			"stems",
			"expected_shelf",
			"new_shelf",
			"trip",
			"vehicle",
			"reported_by",
			"reported_at",
			"resolved_at",
			"resolution",
		],
		order_by="reported_at desc",
	)


def open_replacements():
	"""Replaced buckets still unaccounted for (status Open), newest first."""
	return frappe.get_all(
		"Bucket Replacement",
		filters={"status": "Open"},
		fields=[
			"name",
			"old_bucket",
			"new_bucket",
			"reason",
			"order_name",
			"order_pick_list",
			"farm",
			"item_code",
			"stem_length",
			"stems",
			"expected_shelf",
			"reported_by",
			"reported_at",
			"vehicle",
		],
		order_by="reported_at desc",
		limit_page_length=500,
	)
