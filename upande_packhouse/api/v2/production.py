# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Production v2 (www/packhouse-production-v2.html).

Numbers (all in stems, Stock Entry Detail transfer_qty = stock UOM):

    harvested = core/harvest.py (submitted "Harvesting" Stock Entries)
    received  = submitted "Receiving" Stock Entries
    graded    = submitted "Grading" Stock Entries

Date: each entry's own posting_date (README rule 4: harvest pages filter on
posting_date). Farm: the Stock Entry's `farm` (the harvesting / receiving /
grading farm); region narrows that same column via core/region.farms_for().
Rose: Item Group subtree (core/rose.py). Search (`q`) narrows the variety set
(item code / item name) on the server, so every KPI follows it.

Every roll-up (totals, length mix, farm list, top variety) is computed here
over the full filtered set (README rules 3, 7); the varieties list is never
truncated, so KPI totals equal the column sums of the table.
"""

import frappe

from upande_packhouse.api.v2.core import region as region_mod
from upande_packhouse.api.v2.core.harvest import harvest
from upande_packhouse.api.v2.core.rose import normalize as rose_normalize
from upande_packhouse.api.v2.core.rose import rose_sql

NO_LENGTH = "No Length"
NO_FARM = "Unknown"


def _entry_stems(entry_type, date_from, date_to, farms=None, rose=None, item_codes=None):
	"""Stems per (item_code, farm) for one Stock Entry type, same query shape as core/harvest."""
	params = {"et": entry_type, "f": date_from, "t": date_to}
	where = [
		"se.stock_entry_type = %(et)s",
		"se.docstatus = 1",
		"se.posting_date BETWEEN %(f)s AND %(t)s",
	]
	if farms:
		where.append("se.farm IN %(farms)s")
		params["farms"] = tuple(farms)
	if item_codes:
		where.append("sed.item_code IN %(items)s")
		params["items"] = tuple(item_codes)
	rose_cond = ""
	if rose and rose != "all":
		frag = rose_sql("item_group", rose, params, key="ph2_prose")
		rose_cond = " AND sed.item_code IN (SELECT name FROM `tabItem` WHERE 1=1{0})".format(frag)
	sql = """
		SELECT STRAIGHT_JOIN sed.item_code AS item_code, se.farm AS farm,
			SUM(sed.transfer_qty) AS stems
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE {0} {1}
		GROUP BY sed.item_code, se.farm
	""".format(" AND ".join(where), rose_cond)
	return frappe.db.sql(sql, params, as_dict=True)  # nosemgrep: fixed SQL, values bound


def _len_key(s):
	digits = "".join(ch for ch in str(s) if ch.isdigit())
	return int(digits) if digits else 99999


def _search_items(q):
	like = "%{0}%".format(q)
	return frappe.db.sql_list(
		"SELECT name FROM `tabItem` WHERE name LIKE %(q)s OR item_name LIKE %(q)s",
		{"q": like},
	)


def _empty(base):
	base.update(
		total_stems=0,
		received_stems=0,
		graded_stems=0,
		harvest_farm_count=0,
		variety_count=0,
		harvested_variety_count=0,
		varieties=[],
		lengths=[],
		farms=[],
		top_variety=None,
		main_length=None,
	)
	return base


@frappe.whitelist()
def get_production(from_date=None, to_date=None, region=None, farm=None, rose_type=None, q=None):
	"""Harvested / received / graded stems per variety, with server roll-ups."""
	date_from = from_date or frappe.utils.today()
	date_to = to_date or date_from
	if date_from > date_to:
		date_from, date_to = date_to, date_from
	rose = rose_normalize(rose_type)
	q = (q or "").strip()
	farms = region_mod.farms_for(region=region, farm=farm)
	base = {
		"success": True,
		"from_date": date_from,
		"to_date": date_to,
		"rose_type": rose,
		"region": region_mod.normalize(region),
		"farm": (farm or "").strip() or None,
		"farm_scope": farms,
		"q": q,
	}
	# Region + farm that do not intersect (Karen + Torongo): nothing, never everything.
	if farms is not None and not farms:
		return _empty(base)
	item_codes = None
	if q:
		item_codes = _search_items(q)
		if not item_codes:
			return _empty(base)

	harvested = harvest(
		date_from=date_from,
		date_to=date_to,
		group_by=("item_code", "stem_length", "farm"),
		farms=farms,
		rose=rose,
		item_codes=item_codes,
	)
	received = _entry_stems("Receiving", date_from, date_to, farms, rose, item_codes)
	graded = _entry_stems("Grading", date_from, date_to, farms, rose, item_codes)

	vmap = {}
	farm_map = {}
	len_map = {}

	def variety(ic):
		ic = ic or "Unknown"
		if ic not in vmap:
			vmap[ic] = {
				"item_code": ic,
				"total_stems": 0,
				"received_stems": 0,
				"graded_stems": 0,
				"lengths": {},
				"farms": {},
			}
		return vmap[ic]

	def farm_row(f):
		f = f or NO_FARM
		if f not in farm_map:
			farm_map[f] = {"farm": f, "stems": 0, "received_stems": 0, "graded_stems": 0}
		return farm_map[f]

	for r in harvested:
		stems = float(r.stems or 0)
		v = variety(r.item_code)
		v["total_stems"] += stems
		sl = r.stem_length or NO_LENGTH
		fm = r.farm or NO_FARM
		v["lengths"][sl] = v["lengths"].get(sl, 0) + stems
		v["farms"][fm] = v["farms"].get(fm, 0) + stems
		len_map[sl] = len_map.get(sl, 0) + stems
		farm_row(fm)["stems"] += stems
	for rows, key in ((received, "received_stems"), (graded, "graded_stems")):
		for r in rows:
			stems = float(r.stems or 0)
			variety(r.item_code)[key] += stems
			farm_row(r.farm)[key] += stems

	names = {}
	if vmap:
		names = dict(
			frappe.db.sql(
				"SELECT name, item_name FROM `tabItem` WHERE name IN %(n)s",
				{"n": tuple(vmap)},
			)
		)

	varieties = []
	for v in vmap.values():
		v["item_name"] = names.get(v["item_code"]) or v["item_code"]
		v["lengths"] = sorted(
			({"stem_length": k, "stems": s} for k, s in v["lengths"].items()),
			key=lambda x: _len_key(x["stem_length"]),
		)
		v["farms"] = sorted(
			({"farm": k, "stems": s} for k, s in v["farms"].items()),
			key=lambda x: -x["stems"],
		)
		varieties.append(v)
	varieties.sort(
		key=lambda x: (-x["total_stems"], -x["received_stems"], -x["graded_stems"], x["item_name"])
	)
	for i, v in enumerate(varieties):
		v["rank"] = i + 1

	total = sum(v["total_stems"] for v in varieties)
	lengths = sorted(
		({"stem_length": k, "stems": s} for k, s in len_map.items()),
		key=lambda x: _len_key(x["stem_length"]),
	)
	farms_out = sorted(
		farm_map.values(),
		key=lambda x: (-x["stems"], -x["received_stems"], -x["graded_stems"]),
	)
	top = varieties[0] if varieties and varieties[0]["total_stems"] > 0 else None
	main_len = max(lengths, key=lambda x: x["stems"]) if lengths else None

	base.update(
		total_stems=total,
		received_stems=sum(v["received_stems"] for v in varieties),
		graded_stems=sum(v["graded_stems"] for v in varieties),
		harvest_farm_count=sum(1 for f in farms_out if f["stems"] > 0),
		variety_count=len(varieties),
		harvested_variety_count=sum(1 for v in varieties if v["total_stems"] > 0),
		varieties=varieties,
		lengths=lengths,
		farms=farms_out,
		top_variety={
			"item_code": top["item_code"],
			"item_name": top["item_name"],
			"stems": top["total_stems"],
		}
		if top
		else None,
		main_length=main_len,
	)
	return base


@frappe.whitelist()
def get_production_farms():
	"""Farm names for the farm <select> (narrowed client-side by PH.regionFarms)."""
	return {"success": True, "farms": frappe.get_all("Farm", pluck="name", order_by="name asc")}
