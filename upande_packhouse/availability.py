"""Shelf availability — the single source of truth.

Truly-available stems = shelf stock (`Shelf Item.stem_qty`)
  minus what is already allocated (`Bucket Allocation Status.allocated_quantity`)
  minus buckets reserved by an open Discard Request (workflow_state != 'Rejected')
  whose row is not yet discarded,
  minus stems held by the allocation plan for another order line (`reserved_by_bucket`).

Used by both the Sales Order spec-autofill popup and the allocation page, so the
number a salesperson sees when picking varieties matches what can actually be
allocated.
"""

import json

import frappe


def _as_list(v):
	if not v:
		return []
	if isinstance(v, str):
		try:
			v = json.loads(v)
		except Exception:
			v = [x.strip() for x in v.split(",") if x.strip()]
	return list(v) if isinstance(v, list | tuple) else [v]


def reserved_bucket_ids():
	"""bucket_ids locked by an open (non-Rejected) Discard Request.

	A row already marked `discarded` no longer holds the bucket: the old stems
	are gone, and a reused bucket's fresh harvest must not inherit the hold
	(upande_agriculture's reuse cleanup flags the old rows discarded)."""
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT drb.bucket_id
		FROM `tabDiscard Request Bucket` drb
		INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
		WHERE COALESCE(dr.workflow_state, '') != 'Rejected'
		  AND COALESCE(drb.discarded, 0) = 0
		  AND COALESCE(drb.bucket_id, '') != ''
		"""
	)
	return {r[0] for r in rows}


def length_cm(length):
	"""'62cm' -> 62; anything without a number -> 0."""
	import re

	m = re.match(r"\s*(\d+)", str(length or ""))
	return int(m.group(1)) if m else 0


def reserved_by_bucket(exclude_soi=None, bucket_ids=None):
	"""{bucket_id: stems} held by the allocation plan for OTHER order lines.

	A planned (Real) confirmation holds buckets and stems for one order line (Bucket Reservation,
	status Active). Allocation to any other line must not take them, so every availability read
	subtracts this. `exclude_soi` (one Sales Order Item name, or a collection) are the lines asking:
	what is held for them is theirs to use, so it is left out of the result."""
	if isinstance(exclude_soi, str):
		exclude_soi = {exclude_soi}
	exclude = tuple(exclude_soi or ()) or ("",)
	params = {"exclude": exclude}
	where = "status = 'Active' AND sales_order_item NOT IN %(exclude)s"
	if bucket_ids is not None:
		if not bucket_ids:
			return {}
		where += " AND bucket_id IN %(buckets)s"
		params["buckets"] = tuple(bucket_ids)
	try:
		rows = frappe.db.sql(
			f"SELECT bucket_id, SUM(stems) FROM `tabBucket Reservation` WHERE {where} GROUP BY bucket_id",
			params,
		)  # nosemgrep: the f-string hole is fixed SQL, values are bound
	except frappe.db.TableMissingError:  # before the doctype is migrated
		return {}
	return {r[0]: float(r[1] or 0) for r in rows}


def reservations_by_bucket_line(bucket_ids):
	"""{bucket_id: {sales_order_item: stems}} of active holds, for the allocator to tell a line's own hold
	from the holds of other lines."""
	if not bucket_ids:
		return {}
	try:
		rows = frappe.db.sql(
			"""SELECT bucket_id, sales_order_item, SUM(stems) FROM `tabBucket Reservation`
			   WHERE status = 'Active' AND bucket_id IN %(b)s GROUP BY bucket_id, sales_order_item""",
			{"b": tuple(bucket_ids)},
		)
	except frappe.db.TableMissingError:
		return {}
	out = {}
	for bucket_id, soi, stems in rows:
		out.setdefault(bucket_id, {})[soi] = float(stems or 0)
	return out


@frappe.whitelist()
def variety_availability(varieties: str | list | None, lengths: str | list | None = None):
	"""Return { variety: { farm: available_stems } } for the given varieties
	(optionally constrained to stem lengths), net of allocations and discards.
	"""
	varieties = _as_list(varieties)
	lengths = _as_list(lengths)
	if not varieties:
		return {}

	conds = ["si.variety IN %(varieties)s"]
	params = {"varieties": tuple(varieties)}
	if lengths:
		conds.append("si.stem_length IN %(lengths)s")
		params["lengths"] = tuple(lengths)

	rows = frappe.db.sql(
		"""
		SELECT si.variety AS variety, si.farm AS farm, si.bucket_id AS bucket_id,
		       COALESCE(si.stem_qty, 0) AS stem_qty,
		       COALESCE(bas.allocated_quantity, 0) AS allocated
		FROM (
			-- one row per (bucket, variety, length): duplicate Shelf Item rows for the
			-- same bucket would otherwise each have the full allocation subtracted
			SELECT bucket_id, variety, stem_length, farm, SUM(COALESCE(stem_qty, 0)) AS stem_qty
			FROM `tabShelf Item`
			GROUP BY bucket_id, variety, stem_length, farm
		) si
		LEFT JOIN `tabBucket Allocation Status` bas
			ON bas.bucket_id = si.bucket_id AND bas.item_code = si.variety
			AND COALESCE(bas.stem_length, '') = COALESCE(si.stem_length, '')
		WHERE """
		+ " AND ".join(conds),
		params,
		as_dict=True,
	)

	reserved = reserved_bucket_ids()
	held_for_others = reserved_by_bucket()
	agg = {}
	for r in rows:
		if r.bucket_id and r.bucket_id in reserved:
			continue
		avail = (r.stem_qty or 0) - (r.allocated or 0) - held_for_others.get(r.bucket_id, 0)
		if avail <= 0:
			continue
		farm = r.farm or "Unknown"
		agg.setdefault(r.variety, {})
		agg[r.variety][farm] = agg[r.variety].get(farm, 0) + avail
	return agg
