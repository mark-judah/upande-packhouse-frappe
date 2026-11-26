# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Quality B buckets from remote farms received at the sales farm in one go: no shelving, no scanning.

The QC app's Bucket Transfers page lists a Quality B pick list's buckets on the truck; one "Received" takes them
all in. Each bucket arrives like a shelved one (its truck's trip records the arrival, its transfer flags clear) and
is issued to its order at once, so it is packed without being scanned:

  * a Grade B bucket's stems come over from its farm's Grade B store to the sales farm's (Remote Transfers) and
    are sold to the line there (Move To Graded Sold) -- where a Quality B bucket of the sales farm itself stands
    once it is released;
  * any other aged bucket is brought over and sold by the issue itself (stock_movement.ensure_arrival /
    post_sale_on_arrival), as for an export bucket issued at the sales farm;
  * the issue (Graded Sold -> Packhouse Store) is the issuing scan's own (offline_issue._issue).
"""

from collections import OrderedDict

import frappe
from frappe import _
from frappe.utils import now_datetime

from upande_packhouse import quality_b, stock_movement

BUSINESS_UNIT = "Roses"

#: The pick rows on their way: on a trolley or a truck, not yet at the sales farm.
_ON_THE_WAY = """pli.parenttype = 'Order Pick List' AND pli.parent = %(opl)s AND IFNULL(pli.bucket, '') != ''
	AND IFNULL(pli.issued, 0) = 0 AND IFNULL(pli.not_found, 0) = 0 AND IFNULL(pli.shelved, 0) = 0
	AND (IFNULL(pli.in_transit, 0) = 1 OR IFNULL(pli.loaded_in_trolley, 0) = 1)"""


def _rows(opl_name):
	return frappe.db.sql(
		"""SELECT pli.name, pli.bucket, pli.item_code, pli.stem_length, pli.stock_qty, pli.farm,
		          pli.source_warehouse, pli.custom_sale_order_item, pli.sales_order_item
		FROM `tabPick List Item` pli WHERE """
		+ _ON_THE_WAY
		+ " ORDER BY pli.idx",
		{"opl": opl_name},
		as_dict=True,
	)


def _is_grade_b(warehouse):
	return bool(warehouse) and " Grade B - " in warehouse


def _bring_grade_b(row, hub, hub_grade_b, sold_to):
	"""A remote Grade B bucket's stems: its farm's Grade B store -> the sales farm's -> sold to the line."""
	so_item = row.custom_sale_order_item or row.sales_order_item
	src = row.source_warehouse
	if src != hub_grade_b:
		stock_movement.post_single_hop(
			item_code=row.item_code,
			qty=row.stock_qty,
			source=src,
			target=hub_grade_b,
			business_unit=BUSINESS_UNIT,
			entry_type=stock_movement.TYPE_REMOTE_TRANSFER,
			bucket_id=row.bucket,
			farm=hub,
			stem_length=row.stem_length,
			remarks=_("Quality B arrived {0}").format(hub),
		)
	stock_movement.post_single_hop(
		item_code=row.item_code,
		qty=row.stock_qty,
		source=hub_grade_b,
		target=sold_to,
		business_unit=BUSINESS_UNIT,
		entry_type=stock_movement.TYPE_TO_SOLD,
		bucket_id=row.bucket,
		farm=hub,
		stem_length=row.stem_length,
		so_item=so_item,
		remarks=_("Quality B sold to {0}").format(so_item),
	)


@frappe.whitelist()
def on_the_way(opl_names: str | list | None = None):
	"""{opl: buckets on the truck} for these Quality B pick lists (the Received button's count)."""
	names = frappe.parse_json(opl_names) if isinstance(opl_names, str) else opl_names
	out = {}
	for opl in quality_b.quality_b_opls(names or []):
		n = len({(r.bucket or "").upper() for r in _rows(opl)})
		if n:
			out[opl] = n
	return out


@frappe.whitelist(methods=["POST"])
def receive(opl_name: str):
	"""Every bucket of a Quality B pick list on the truck received at its sales farm and issued to the order.
	Returns {received, issued, failed: [{bucket, message}]}."""
	from upande_quality.grade_b import grade_b_warehouse

	from upande_packhouse.api import transfer_control as tc
	from upande_packhouse.api.offline_issue import _issue
	from upande_packhouse.api.remote_transfer.transfer_scheduling import opl_hub

	if not frappe.db.get_value("Order Pick List", opl_name, "quality_b"):
		frappe.throw(_("{0} is not a Quality B pick list.").format(opl_name))
	hub = opl_hub(opl_name)
	if not hub:
		frappe.throw(_("{0} has no sales farm.").format(opl_name))
	mapping = stock_movement.mapping_row_for_farm(hub, BUSINESS_UNIT)
	if not mapping or not mapping.delivery_warehouse:
		frappe.throw(_("No Graded Sold store is mapped for {0}.").format(hub))
	company = frappe.db.get_value("Warehouse", mapping.delivery_warehouse, "company")
	hub_grade_b = grade_b_warehouse(hub, company)

	buckets = OrderedDict()
	for r in _rows(opl_name):
		buckets.setdefault((r.bucket or "").upper(), []).append(r)
	received, issued, failed = 0, 0, []
	now = now_datetime()
	for bucket, rows in buckets.items():
		bucket_id = rows[0].bucket
		try:
			stock_movement.lock_bucket(bucket_id)
			try:
				tc.record_planned_load_on_arrival(bucket_id, hub)
			except Exception:
				frappe.log_error(
					"Quality B receive: recording the load on arrival failed", frappe.get_traceback()
				)
			for r in rows:
				if _is_grade_b(r.source_warehouse):
					_bring_grade_b(r, hub, hub_grade_b, mapping.delivery_warehouse)
				# arrived: off the transfer, at the sales farm -- as shelving it there would leave it
				flags = {
					"in_transit": 0,
					"awaiting_transfer": 0,
					"loaded_in_trolley": 0,
					"shelved": 1,
					"origin_warehouse": r.source_warehouse,
					"source_warehouse": hub_grade_b
					if _is_grade_b(r.source_warehouse)
					else r.source_warehouse,
				}
				flags["shelved_at"] = now
				meta = frappe.get_meta("Pick List Item")
				flags = {k: v for k, v in flags.items() if meta.has_field(k)}
				frappe.db.set_value("Pick List Item", r.name, flags, update_modified=False)
			frappe.db.sql(
				"""UPDATE `tabBucket Allocation Status` SET in_transit = 0, shelf_farm = %(hub)s
				WHERE bucket_id = %(b)s AND IFNULL(in_transit, 0) = 1""",
				{"hub": hub, "b": bucket_id},
			)
			received += 1
			try:
				tc.auto_receive_trucks_for_bucket(bucket_id)
			except Exception:
				frappe.log_error("Quality B receive: closing the truck's trip failed", frappe.get_traceback())
			# issued at once: Quality B is packed without scanning
			frappe.flags.quality_b_receive = True
			try:
				results = _issue(bucket_id, opl_name)
			finally:
				frappe.flags.quality_b_receive = False
			bad = [x for x in results if not x["ok"]]
			if bad:
				failed.append({"bucket": bucket_id, "message": "; ".join(str(x["message"]) for x in bad)})
			else:
				issued += 1
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- each bucket stands on its own
		except Exception as e:
			frappe.db.rollback()
			frappe.log_error("Quality B receive failed for " + bucket_id, frappe.get_traceback())
			failed.append({"bucket": bucket_id, "message": str(e)})
	from upande_packhouse.allocation_log import log

	so = frappe.db.get_value("Order Pick List", opl_name, "sales_order")
	log(
		"Quality B",
		_("{0}: {1} bucket(s) received at {2}, {3} issued.").format(opl_name, received, hub, issued)
		+ ("" if not failed else " " + "; ".join(f["bucket"] + ": " + f["message"] for f in failed)),
		sales_order=so,
		farm=hub,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"received": received, "issued": issued, "failed": failed}


def quality_b_issue_bypass(kind):
	"""The issue a Quality B receive makes follows no team schedule: its order is packed as it arrives."""
	return kind == "issue" and bool(frappe.flags.get("quality_b_receive"))
