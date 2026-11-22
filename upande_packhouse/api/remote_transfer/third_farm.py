# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""A bucket on a truck that is shelved at a third farm -- neither the farm it left nor the sales farm it is
going to (the truck dropped it on the way) -- is waiting at that farm now: its pick rows say so, off the truck
and off the trips that carried it, so a trip from that farm takes it on to its sales farm.

Before this the rows stayed "in transit" from the farm it left, on a trip that no longer had it: no trip could
plan it, and the order could not be packed (2026-11-22: 7 SANDY-05285 buckets shelved at Kapkolia for Chepsito).
"""

import frappe

from upande_packhouse.api.remote_transfer import transfer_scheduling as ts


def wait_at_third_farm(bucket_id, farm, shelf_id=None):
	"""Shelved at `farm` while in transit to another sales farm from another farm: its open pick rows wait at
	`farm` (on `shelf_id`), off the truck and off the trips that recorded it. Returns the pick lists touched."""
	if not bucket_id or not farm:
		return []
	rows = frappe.db.sql(
		"""SELECT pli.name, pli.parent,
		       COALESCE(NULLIF(pli.farm, ''), SUBSTRING_INDEX(pli.source_warehouse, ' ', 1)) AS row_farm
		FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %(b)s
		  AND IFNULL(pli.in_transit, 0) = 1
		  AND IFNULL(pli.shelved, 0) = 0 AND IFNULL(pli.issued, 0) = 0""",
		{"b": bucket_id},
		as_dict=True,
	)
	here = farm.lower()
	rows = [r for r in rows if (r.row_farm or "").lower() != here]
	if not rows:
		return []
	# shelved at the sales farm it was going to: that is its arrival, handled by the shelving itself
	hubs = ts.opl_hubs(sorted({r.parent for r in rows}))
	rows = [r for r in rows if (hubs.get(r.parent) or "").lower() != here]
	if not rows:
		return []

	values = {
		"farm": farm,
		"in_transit": 0,
		"loaded_in_trolley": 0,
		"transit_truck": None,
		"awaiting_transfer": 1,
	}
	if shelf_id:
		values["shelf"] = shelf_id
	for col, val in (("loaded_on_truck", 0), ("trolley_id", None)):
		if frappe.db.has_column("Pick List Item", col):
			values[col] = val
	for r in rows:
		frappe.db.set_value("Pick List Item", r.name, values)

	opls = sorted({r.parent for r in rows})
	key = bucket_id.upper()
	where = [w for w in ts._bucket_trip_rows([key]).get(key, []) if w.opl in set(opls)]
	if where:
		ts._drop_bucket_from_trips(key, where, "shelved at {0} on the way".format(farm))
	for opl in opls:
		frappe.get_doc("Order Pick List", opl).add_comment(
			"Info",
			"Bucket {0} was shelved at {1}{2} on its way to {3}: it waits there for a trip to {3}.".format(
				bucket_id,
				farm,
				" ({0})".format(shelf_id) if shelf_id else "",
				hubs.get(opl) or "its sales farm",
			),
		)
	ts.log_transfer_event(
		bucket_id,
		"Waiting at another farm",
		farm=farm,
		shelf=shelf_id,
		opl=opls[0],
		details="Shelved at {0} on the way to {1}".format(farm, hubs.get(opls[0]) or "its sales farm"),
	)
	return opls
