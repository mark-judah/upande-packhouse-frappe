"""Shelf availability — the single source of truth.

Truly-available stems = shelf stock (`Shelf Item.stem_qty`)
  minus what is already allocated (`Bucket Allocation Status.allocated_quantity`)
  minus buckets reserved by an open Discard Request (workflow_state != 'Rejected').

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
	return list(v) if isinstance(v, (list, tuple)) else [v]


def reserved_bucket_ids():
	"""bucket_ids locked by an open (non-Rejected) Discard Request."""
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT drb.bucket_id
		FROM `tabDiscard Request Bucket` drb
		INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
		WHERE COALESCE(dr.workflow_state, '') != 'Rejected'
		  AND COALESCE(drb.bucket_id, '') != ''
		"""
	)
	return {r[0] for r in rows}


@frappe.whitelist()
def variety_availability(varieties, lengths=None):
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
		FROM `tabShelf Item` si
		LEFT JOIN `tabBucket Allocation Status` bas ON bas.bucket_id = si.bucket_id
		WHERE """
		+ " AND ".join(conds),
		params,
		as_dict=True,
	)

	reserved = reserved_bucket_ids()
	agg = {}
	for r in rows:
		if r.bucket_id and r.bucket_id in reserved:
			continue
		avail = (r.stem_qty or 0) - (r.allocated or 0)
		if avail <= 0:
			continue
		farm = r.farm or "Unknown"
		agg.setdefault(r.variety, {})
		agg[r.variety][farm] = agg[r.variety].get(farm, 0) + avail
	return agg
