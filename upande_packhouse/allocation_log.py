# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Log: every step an order goes through from planning to its pick lists, one row each.

Planned, revised and released plan entries; harvest held from the shelf; proposals; releases to pick lists and the
reasons one failed or was held back; substitutions and splits; replaced and unallocated buckets; Quality B pick
lists. `log()` never raises: a step is never stopped by its own log line.
"""

import json

import frappe
from frappe.utils import flt, now_datetime

LINE_FIELDS = ("parent", "item_code", "custom_length", "delivery_date")


def log(stage, message="", sales_order=None, sales_order_item=None, details=None, **fields):
	"""Write one Allocation Log row. `fields`: any of item_code, stem_length, team, farm, delivery_date,
	stems_before, stems_after, bucket_id, order_pick_list, reference_doctype, reference_name."""
	try:
		if not frappe.db.table_exists("Allocation Log"):
			return None
		line = {}
		if sales_order_item:
			line = (
				frappe.db.get_value("Sales Order Item", sales_order_item, list(LINE_FIELDS), as_dict=True)
				or {}
			)
		sales_order = sales_order or line.get("parent")
		# the split paths' messages open with the order and line, which the log has fields for
		prefix = "{0} {1}: ".format(sales_order, sales_order_item)
		if message and message.startswith(prefix):
			message = message[len(prefix) :]
		delivery = fields.pop("delivery_date", None) or line.get("delivery_date")
		if not delivery and sales_order:
			delivery = frappe.db.get_value("Sales Order", sales_order, "delivery_date")
		doc = frappe.new_doc("Allocation Log")
		doc.update(
			{
				"stage": stage,
				"sales_order": sales_order,
				"sales_order_item": sales_order_item,
				"item_code": fields.pop("item_code", None) or line.get("item_code"),
				"stem_length": fields.pop("stem_length", None) or line.get("custom_length"),
				"delivery_date": delivery,
				"message": (message or "")[:1000],
				"details": json.dumps(details, default=str) if details else None,
			}
		)
		for key, value in fields.items():
			if doc.meta.has_field(key) and value not in (None, ""):
				doc.set(key, flt(value, 3) if key.startswith("stems_") else value)
		doc.name = frappe.generate_hash(length=12)
		doc.owner = doc.modified_by = frappe.session.user
		doc.creation = doc.modified = now_datetime()
		doc.db_insert()
		return doc.name
	except Exception:
		return None


@frappe.whitelist()
def get_log(sales_order: str):
	"""The order's Allocation Log, oldest first, with who did each step."""
	if not sales_order:
		return []
	frappe.has_permission("Sales Order", "read", sales_order, throw=True)
	rows = frappe.get_all(
		"Allocation Log",
		filters={"sales_order": sales_order},
		fields=[
			"creation",
			"owner",
			"stage",
			"sales_order_item",
			"item_code",
			"stem_length",
			"team",
			"farm",
			"stems_before",
			"stems_after",
			"bucket_id",
			"order_pick_list",
			"message",
		],
		order_by="creation asc",
		limit_page_length=2000,
		ignore_permissions=True,
	)
	names = {}
	for r in rows:
		# a step that moved no stems shows none
		if not flt(r.stems_before) and not flt(r.stems_after):
			r.stems_before = r.stems_after = None
		if r.owner not in names:
			names[r.owner] = frappe.db.get_value("User", r.owner, "full_name") or r.owner
		r.user = names[r.owner]
	return rows
