# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Stock that exists but is not on a shelf yet: received (waiting for a shelf) and harvested (not received).

  received, not shelved   a bucket with a submitted Receiving / Late Receipt entry that has no shelf row, no
                          shelving record and no later onward entry (discard, issue, packing, dispatch): the same
                          rule the Cold Room page uses for "received, not shelved" (cold_room._not_shelved). The cold
                          room is the receiving entry's target warehouse. Only buckets within the discard age (and the
                          farm's max allocation age) count, read from Production Settings through core/stock; buckets
                          already allocated are left out.
  harvested, not received a bucket on a submitted Harvesting entry for the day with no Receiving entry since.
                          "Harvested" is core/harvest's definition (Stock Entry Detail transfer_qty).

Both are real stock, so the first can be held bucket by bucket (Bucket Reservation) exactly like shelf stock.
"""

import math
from collections import defaultdict

import frappe
from frappe.utils import add_days, getdate, today

from upande_packhouse.api.v2.cold_room import ONWARD_TYPES, RECEIPT_TYPES
from upande_packhouse.api.v2.core import stock

#: These are facts about the floor, not about the plan, so a minute-old answer is as good as a fresh one and the
#: page can reload after every confirmation without asking the huge Stock Entry table again.
CACHE_SECONDS = 60


def _cached(key, fn):
	val = frappe.cache.get_value(key)
	if val is None:
		val = fn()
		frappe.cache.set_value(key, val, expires_in_sec=CACHE_SECONDS)
	return val


def received_unshelved(*, farms=None, varieties=None, lengths=None):
	"""Buckets received and not yet shelved, within the discard age. One row per bucket with
	bucket_id, farm, variety, stem_length, stems, cold_room, received_on, harvest_date, age_days, stock_entry."""
	if farms is not None and not farms:
		return []
	cfg = stock._config()
	key = "allocation_received:%s:%s:%s" % (
		tuple(sorted(farms)) if farms is not None else "*",
		cfg["discard_age"],
		today(),
	)
	rows = _cached(key, lambda: _received_unshelved(tuple(farms) if farms is not None else None, cfg))
	vs, ls = set(varieties or ()), set(lengths or ())
	return [r for r in rows if (not vs or r.variety in vs) and (not ls or (r.stem_length or "") in ls)]


def _received_unshelved(farms, cfg):
	varieties = lengths = None
	discard_age = cfg["discard_age"]
	farm_cfg = cfg["farm_config"]
	since = add_days(
		today(), -min(int(math.ceil(discard_age)), 365)
	)  # a year is plenty: nothing waits that long
	params = {
		"rt": RECEIPT_TYPES,
		"cycle": RECEIPT_TYPES + ("Harvesting",),
		"onward": ONWARD_TYPES,
		"since": since,
	}
	cond = ""
	if farms is not None:
		cond += " AND se.farm IN %(farms)s"
		params["farms"] = tuple(farms)
	if varieties:
		cond += " AND sed.item_code IN %(varieties)s"
		params["varieties"] = tuple(varieties)
	if lengths:
		cond += " AND COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length) IN %(lengths)s"
		params["lengths"] = tuple(lengths)
	rows = frappe.db.sql(
		f"""
		SELECT se.custom_bucket_id AS bucket_id, se.farm, sed.item_code AS variety,
		       COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length) AS stem_length,
		       se.posting_date AS received_on, se.name AS stock_entry,
		       MIN(sed.t_warehouse) AS cold_room, SUM(sed.qty) AS stems,
		       (SELECT MAX(h.posting_date) FROM `tabStock Entry` h
		         WHERE h.custom_bucket_id = se.custom_bucket_id AND h.docstatus = 1
		           AND h.stock_entry_type = 'Harvesting' AND h.posting_date <= se.posting_date) AS harvest_date
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1 AND se.stock_entry_type IN %(rt)s AND se.posting_date >= %(since)s
		  AND IFNULL(se.custom_bucket_id, '') != ''{cond}
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabStock Entry` n
		      WHERE n.custom_bucket_id = se.custom_bucket_id AND n.docstatus = 1
		        AND n.stock_entry_type IN %(cycle)s AND n.name != se.name
		        AND (n.posting_date > se.posting_date
		             OR (n.posting_date = se.posting_date AND n.stock_entry_type != 'Harvesting'
		                 AND n.creation > se.creation)))
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabShelf Item` si
		      WHERE si.bucket_id = se.custom_bucket_id AND si.stem_qty > 0
		        AND DATE(si.date_added) >= se.posting_date)
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabShelving Log` sl
		      WHERE sl.bucket_id = se.custom_bucket_id AND DATE(sl.shelved_on) >= se.posting_date)
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabStock Entry` d
		      WHERE d.custom_bucket_id = se.custom_bucket_id AND d.docstatus = 1
		        AND d.stock_entry_type IN %(onward)s AND d.posting_date >= se.posting_date)
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabPick List Item` p
		      INNER JOIN `tabOrder Pick List` o ON o.name = p.parent
		      WHERE p.bucket = se.custom_bucket_id AND p.issued = 1 AND o.docstatus < 2
		        AND o.date_created >= se.posting_date)
		GROUP BY se.custom_bucket_id, se.farm, sed.item_code, stem_length, se.posting_date, se.name
		ORDER BY se.posting_date, se.custom_bucket_id
		""",
		params,
		as_dict=True,
	)  # nosemgrep: the f-string holes are fixed SQL, values are bound
	if not rows:
		return []
	allocated = {
		r[0]
		for r in frappe.db.sql(
			"""SELECT bucket_id FROM `tabBucket Allocation Status`
			   WHERE bucket_id IN %(b)s AND COALESCE(allocated_quantity, 0) > 0""",
			{"b": tuple({r.bucket_id for r in rows})},
		)
	}
	out, now = [], getdate(today())
	for r in rows:
		if r.bucket_id in allocated:
			continue
		anchor = r.harvest_date or r.received_on
		age = (now - getdate(anchor)).days if anchor else 0
		limit = farm_cfg.get(r.farm, {}).get("max_allocation_age", 5)
		if age > limit or age >= discard_age:
			continue
		r.stems = float(r.stems or 0)
		r.age_days = age
		out.append(r)
	return out


def unshelved_bucket_ids(bucket_ids):
	"""Which of these buckets are received and still waiting for a shelf (any farm, any age)."""
	if not bucket_ids:
		return set()
	rows = frappe.db.sql(
		"""SELECT DISTINCT se.custom_bucket_id FROM `tabStock Entry` se
		   WHERE se.docstatus = 1 AND se.stock_entry_type IN %(rt)s AND se.custom_bucket_id IN %(b)s
		     AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = se.custom_bucket_id AND si.stem_qty > 0)
		     AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` d WHERE d.custom_bucket_id = se.custom_bucket_id
		                     AND d.docstatus = 1 AND d.stock_entry_type IN %(onward)s AND d.posting_date >= se.posting_date)""",
		{"rt": RECEIPT_TYPES, "onward": ONWARD_TYPES, "b": tuple(bucket_ids)},
	)
	return {r[0] for r in rows}


def harvested_day(day, farms, varieties):
	"""{(variety, length): {"harvested": stems, "not_received": stems}} for one cutting day.

	`harvested` is every stem on that day's Harvesting entries at the farms; `not_received` the part whose
	bucket has no Receiving entry since."""
	if not farms or not varieties:
		return {}
	all_ = _cached(
		"allocation_harvest:%s:%s" % (day, tuple(sorted(farms))), lambda: _harvested_day(day, tuple(farms))
	)
	wanted = set(varieties)
	return {k: v for k, v in all_.items() if k[0] in wanted}


def _harvested_day(day, farms):
	rows = frappe.db.sql(
		"""
		SELECT STRAIGHT_JOIN sed.item_code AS variety,
		       COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length) AS stem_length,
		       SUM(sed.transfer_qty) AS stems,
		       SUM(CASE WHEN IFNULL(se.custom_bucket_id, '') != '' AND NOT EXISTS (
		               SELECT 1 FROM `tabStock Entry` r
		               WHERE r.custom_bucket_id = se.custom_bucket_id AND r.docstatus = 1
		                 AND r.stock_entry_type IN %(rt)s AND r.posting_date >= se.posting_date)
		           THEN sed.transfer_qty ELSE 0 END) AS not_received
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus = 1 AND se.posting_date = %(d)s
		  AND se.farm IN %(farms)s
		GROUP BY sed.item_code, stem_length
		""",
		{"rt": RECEIPT_TYPES, "d": day, "farms": tuple(farms)},
		as_dict=True,
	)
	out = defaultdict(lambda: {"harvested": 0.0, "not_received": 0.0})
	for r in rows:
		k = (r.variety, r.stem_length or "")
		out[k]["harvested"] += float(r.stems or 0)
		out[k]["not_received"] += float(r.not_received or 0)
	return dict(out)
