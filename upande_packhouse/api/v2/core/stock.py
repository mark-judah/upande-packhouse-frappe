# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Live shelf stock, one row per (bucket, variety, stem length).

Every v2 page that shows stock on a shelf -- Stock Visibility, Avails, Cold
Room, Allocation Planning, Stem Movement's "on shelf", Bucket Journey -- reads
it from here. Each row's stems are split into exactly one state each, so the
parts always add up to what is on the shelf (audit ST-2):

    shelved = held + allocated + free

  held       the whole bucket is on an open discard request. The hold is per
             BUCKET, not per variety row (ST-1): a discard request keeps one
             row per bucket, but the allocator (availability.reserved_bucket_ids)
             refuses every stem in that bucket, and so does v2.
  allocated  outstanding allocation for this (bucket, variety, length)
             (Bucket Allocation Status.allocated_quantity, which equals the
             uncancelled, unissued Bucket Allocations exactly -- ST-13), capped
             at the stems present. Matched on bucket AND variety AND length, so a
             reused bucket's stale allocation never hits a different variety (ST-6).
  free       the rest.

Two more flags mirror the allocator (sales_allocation.get_sales_order_items_with_buckets)
so "available" here is what allocation will actually accept (OR-A5):
  too_old    age (days since harvest, else since shelving) over the shelf farm's
             max allocation age, or at/over the discard age
  cooling    fewer hours on the shelf than the farm's cooling hours
`allocatable` = free stems that are neither too old nor cooling.

Location is the SHELF's farm (Shelf.farm) everywhere (ST-22). Age has one
anchor: Shelf Item.harvest_date, else date_added -- the allocator's own (ST-8);
never the latest harvest of a reused bucket id (ST-19).
"""

from collections import defaultdict

import frappe
from frappe.utils import now_datetime

from upande_packhouse.api.v2.core.rose import rose_sql, rose_type
from upande_packhouse.availability import reserved_bucket_ids


def _config():
	from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import (
		_get_production_config,
	)

	return _get_production_config()


def bucket_rows(*, farms=None, varieties=None, lengths=None, rose=None, company=None):
	"""Live stock rows with their state split. Filters are applied in SQL."""
	params = {"now": now_datetime()}
	where = ["si.stem_qty > 0", "IFNULL(si.bucket_id, '') != ''", "IFNULL(si.variety, '') != ''"]
	# None = no farm filter; an empty list means "no such farm" and matches nothing.
	if farms is not None:
		where.append("s.farm IN %(farms)s")
		params["farms"] = tuple(farms) or ("",)
	if varieties:
		where.append("si.variety IN %(varieties)s")
		params["varieties"] = tuple(varieties)
	if lengths:
		where.append("si.stem_length IN %(lengths)s")
		params["lengths"] = tuple(lengths)
	if company:
		where.append("s.farm IN (SELECT name FROM `tabFarm` WHERE company = %(company)s)")
		params["company"] = company
	rose_cond = rose_sql("i.item_group", rose, params)
	rows = frappe.db.sql(
		f"""
		SELECT si.bucket_id, si.variety, si.stem_length,
		       MIN(s.farm) AS shelf_farm, MIN(s.name) AS shelf, COUNT(DISTINCT s.name) AS shelves,
		       SUM(si.stem_qty) AS stems,
		       MIN(si.date_added) AS date_added,
		       MIN(COALESCE(si.harvest_date, DATE(si.date_added))) AS age_anchor,
		       DATEDIFF(CURDATE(), MIN(COALESCE(si.harvest_date, DATE(si.date_added)))) AS age_days,
		       TIMESTAMPDIFF(HOUR, MIN(si.date_added), %(now)s) AS hours_on_shelf,
		       MIN(si.cut_stage) AS cut_stage, MIN(si.greenhouse) AS greenhouse,
		       i.item_name, i.item_group
		FROM `tabShelf Item` si
		INNER JOIN `tabShelf` s ON s.name = si.parent
		LEFT JOIN `tabItem` i ON i.name = si.variety
		WHERE {" AND ".join(where)} {rose_cond}
		GROUP BY si.bucket_id, si.variety, si.stem_length
		""",
		params,
		as_dict=True,
	)  # nosemgrep: the f-string holes are fixed SQL, values are bound
	if not rows:
		return []

	alloc = {}
	for r in frappe.db.sql(
		"""SELECT bucket_id, item_code, IFNULL(stem_length, '') AS stem_length,
		          SUM(allocated_quantity) AS allocated
		   FROM `tabBucket Allocation Status`
		   WHERE bucket_id IN %(b)s AND allocated_quantity > 0
		   GROUP BY bucket_id, item_code, IFNULL(stem_length, '')""",
		{"b": tuple({r.bucket_id for r in rows})},
		as_dict=True,
	):
		alloc[(r.bucket_id, r.item_code, r.stem_length)] = float(r.allocated or 0)

	held_ids = reserved_bucket_ids()
	cfg = _config()
	discard_age = cfg["discard_age"]
	fc = cfg["farm_config"]
	for r in rows:
		stems = float(r.stems or 0)
		a = alloc.get((r.bucket_id, r.variety, r.stem_length or ""), 0.0)
		r.stems = stems
		r.rose_type = rose_type(r.item_group)
		r.in_discard_request = r.bucket_id in held_ids
		if r.in_discard_request:
			r.held, r.allocated, r.free = stems, 0.0, 0.0
			r.allocated_in_held = min(a, stems)  # alert: an order is promised stock queued for discard
			r.over_allocated = 0.0
		else:
			r.held = 0.0
			r.allocated = min(a, stems)
			r.free = stems - r.allocated
			r.allocated_in_held = 0.0
			r.over_allocated = max(0.0, a - stems)
		farm_cfg = fc.get(r.shelf_farm, {})
		age = r.age_days if r.age_days is not None else 0
		r.too_old = age > farm_cfg.get("max_allocation_age", 5) or age >= discard_age
		r.cooling = (r.hours_on_shelf or 0) < farm_cfg.get("cooling_hours", 0)
		r.allocatable = 0.0 if (r.too_old or r.cooling) else r.free
	return rows


SUM_FIELDS = ("stems", "held", "allocated", "free", "allocatable", "allocated_in_held", "over_allocated")


def summarize(rows, by=("variety", "stem_length", "shelf_farm")):
	"""Group bucket rows. Buckets are counted once per group (distinct ids),
	oldest age and a stem-weighted average age are kept."""
	groups = {}
	for r in rows:
		key = tuple(r.get(k) for k in by)
		g = groups.get(key)
		if g is None:
			g = groups[key] = frappe._dict({k: r.get(k) for k in by})
			for f in SUM_FIELDS:
				g[f] = 0.0
			g.bucket_ids = set()
			g.oldest_days = None
			g._age_x_stems = 0.0
		for f in SUM_FIELDS:
			g[f] += r[f]
		g.bucket_ids.add(r.bucket_id)
		age = r.age_days or 0
		g.oldest_days = age if g.oldest_days is None else max(g.oldest_days, age)
		g._age_x_stems += age * r.stems
	out = []
	for g in groups.values():
		g.buckets = len(g.bucket_ids)
		g.avg_age_days = round(g._age_x_stems / g.stems, 1) if g.stems else None
		del g["bucket_ids"], g["_age_x_stems"]
		out.append(g)
	return out


def totals(rows):
	"""Whole-scope totals: stems by state, distinct buckets and shelves, ages."""
	t = frappe._dict({f: sum(r[f] for r in rows) for f in SUM_FIELDS})
	t.buckets = len({r.bucket_id for r in rows})
	t.shelves = len({r.shelf for r in rows})
	t.varieties = len({r.variety for r in rows})
	t.oldest_days = max((r.age_days or 0 for r in rows), default=None)
	t.avg_age_days = round(sum((r.age_days or 0) * r.stems for r in rows) / t.stems, 1) if t.stems else None
	return t


def age_bands(rows, edges=(1, 2, 3, 5, 7)):
	"""Stems per age band in days: [0,1), [1,2) ... [7, inf)."""
	labels = []
	lo = 0
	for e in edges:
		labels.append((lo, e))
		lo = e
	labels.append((lo, None))
	out = defaultdict(float)
	for r in rows:
		age = r.age_days or 0
		for lo_, hi in labels:
			if age >= lo_ and (hi is None or age < hi):
				out[(lo_, hi)] += r.stems
				break
	return [{"from_days": lo_, "to_days": hi, "stems": out.get((lo_, hi), 0.0)} for lo_, hi in labels]
