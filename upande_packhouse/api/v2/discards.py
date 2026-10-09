# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Discards v2 (www/packhouse-discards-v2.html).

One data set, every figure on the page computed over it:

    discarded = Discard Request Bucket rows of Discard Requests whose
                workflow_state is 'Approved' (cancelled requests excluded),
                dated by the approval date (README rule 4:
                COALESCE(approval_date, requested_date)).

Filters (all applied in SQL to the KPIs, top lists and the bucket list alike):
    from_date / to_date   discard date range (any past range)
    region / farm         the discarded stock's farm (Discard Request Bucket.farm,
                          which equals the bucket's Shelf.farm on every row here)
    rose                  Spray / Standard through the Item Group tree of the
                          bucket's variety (rose.py). The request header's
                          item_group is empty on every request, so v1's flat
                          `dr.item_group = 'Spray Roses'` matched nothing.
    variety               exact variety
    q                     text search over request, bucket, variety, farm, shelf,
                          length

Value ("foregone"): stems x the selling rate on the valuation price list for the
bucket's variety and stem length that was valid ON THE DISCARD DATE (latest
valid_from <= date, valid_upto empty or >= date). No valid price -> the stems
are reported as unpriced, never valued with an out-of-date price. KES uses the
latest selling exchange rate on or before the discard date.

v1 (api/discards.getDiscardData) is unchanged and still serves the v1 page.
"""

import bisect

import frappe
from frappe.utils import getdate, today

from upande_packhouse.api.v2.core import region as region_core
from upande_packhouse.api.v2.core import rose as rose_core

ROW_LIMIT = 1000
TOP_VARIETIES = 8
DATE_COL = "COALESCE(dr.approval_date, dr.requested_date)"
DEFAULT_PRICE_LIST = "EUR Price List"


def _where(a, params, skip=()):
	"""WHERE clause for the discarded set; `skip` drops named filters (for option lists)."""
	cond = [
		"COALESCE(dr.workflow_state, '') = 'Approved'",
		"dr.docstatus < 2",
		DATE_COL + " BETWEEN %(from_date)s AND %(to_date)s",
	]
	params["from_date"] = a["from_date"]
	params["to_date"] = a["to_date"]
	if "farm" not in skip:
		farms = region_core.farms_for(region=a["region"], farm=a["farm"])
		if farms is not None:
			cond.append("drb.farm IN %(farms)s")
			params["farms"] = region_core.sql_tuple(farms)
	sql = " AND ".join(cond)
	if "rose" not in skip:
		sql += rose_core.rose_sql("i.item_group", a["rose"], params)
	if "variety" not in skip and a["variety"]:
		sql += " AND drb.variety = %(variety)s"
		params["variety"] = a["variety"]
	if "q" not in skip and a["q"]:
		sql += (
			" AND CONCAT_WS(' ', dr.name, drb.bucket_id, drb.variety, drb.farm, drb.shelf, drb.stem_length)"
			" LIKE %(q)s"
		)
		params["q"] = "%" + a["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
	return sql


FROM = (
	" FROM `tabDiscard Request Bucket` drb"
	" INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent AND drb.parenttype = 'Discard Request'"
	" LEFT JOIN `tabItem` i ON i.name = drb.variety"
)


def _args(kw):
	t = today()
	f = kw.get("from_date") or t
	to = kw.get("to_date") or f
	f, to = str(getdate(f)), str(getdate(to))
	if f > to:
		f, to = to, f
	return {
		"from_date": f,
		"to_date": to,
		"region": (kw.get("region") or "").strip(),
		"farm": (kw.get("farm") or "").strip(),
		"rose": rose_core.normalize(kw.get("rose") or kw.get("rose_type")),
		"variety": (kw.get("variety") or "").strip(),
		"q": (kw.get("q") or "").strip(),
	}


def _valuation_list():
	pl = None
	if frappe.get_meta("Production Settings").has_field("custom_discard_valuation_price_list"):
		pl = frappe.db.get_single_value("Production Settings", "custom_discard_valuation_price_list")
	pl = pl or DEFAULT_PRICE_LIST
	return pl, frappe.db.get_value("Price List", pl, "currency")


class _Pricer:
	"""Item Price on the valuation list, valid on a given date."""

	def __init__(self, price_list, items):
		self.by_key = {}
		self.cache = {}
		if not items:
			return
		for ip in frappe.db.sql(
			"SELECT item_code, custom_length, price_list_rate, valid_from, valid_upto, modified"
			" FROM `tabItem Price` WHERE selling = 1 AND price_list = %(pl)s AND item_code IN %(items)s",
			{"pl": price_list, "items": tuple(items)},
			as_dict=True,
		):
			self.by_key.setdefault((ip.item_code, ip.custom_length or ""), []).append(ip)

	def rate(self, item, length, date):
		key = (item, length or "", str(date))
		if key in self.cache:
			return self.cache[key]
		d = getdate(date)
		best = None
		for ip in self.by_key.get(key[:2], []):
			if ip.valid_from and getdate(ip.valid_from) > d:
				continue
			if ip.valid_upto and getdate(ip.valid_upto) < d:
				continue
			rank = (getdate(ip.valid_from) if ip.valid_from else getdate("1900-01-01"), ip.modified)
			if best is None or rank > best[0]:
				best = (rank, float(ip.price_list_rate or 0))
		self.cache[key] = best[1] if best else None
		return self.cache[key]


class _Fx:
	"""Latest selling exchange rate currency -> KES on or before a date."""

	def __init__(self, currency):
		self.dates, self.rates = [], []
		self.fixed = 1.0 if currency == "KES" else None
		if currency and currency != "KES":
			for r in frappe.db.sql(
				"SELECT date, exchange_rate FROM `tabCurrency Exchange`"
				" WHERE from_currency = %(c)s AND to_currency = 'KES' AND for_selling = 1"
				" ORDER BY date, modified",
				{"c": currency},
				as_dict=True,
			):
				self.dates.append(getdate(r.date))
				self.rates.append(float(r.exchange_rate))

	def rate(self, date):
		if self.fixed is not None:
			return self.fixed
		i = bisect.bisect_right(self.dates, getdate(date)) - 1
		return self.rates[i] if i >= 0 else None


def _int(v):
	return int(v or 0)


@frappe.whitelist()
def get_discards(**kw):
	"""Approved discards for the page's filters: KPIs, top lists, bucket rows, options."""
	a = _args(kw)
	params = {}
	where = _where(a, params)
	base = FROM + " WHERE " + where

	agg = frappe.db.sql(
		"SELECT COUNT(*) AS buckets, COALESCE(SUM(drb.stem_qty), 0) AS stems,"
		" AVG(drb.age_days) AS avg_age, COUNT(drb.age_days) AS aged,"
		" COALESCE(SUM(drb.discarded = 1), 0) AS removed_buckets,"
		" COALESCE(SUM(CASE WHEN drb.discarded = 1 THEN drb.stem_qty END), 0) AS removed_stems,"
		" COUNT(DISTINCT dr.name) AS requests" + base,
		params,
		as_dict=True,
	)[0]

	by_variety = frappe.db.sql(
		"SELECT drb.variety AS variety, COUNT(*) AS buckets, COALESCE(SUM(drb.stem_qty), 0) AS stems"
		+ base
		+ " GROUP BY drb.variety ORDER BY stems DESC, buckets DESC, variety LIMIT "
		+ str(TOP_VARIETIES),
		params,
		as_dict=True,
	)
	by_farm = frappe.db.sql(
		"SELECT drb.farm AS farm, COUNT(*) AS buckets, COALESCE(SUM(drb.stem_qty), 0) AS stems"
		+ base
		+ " GROUP BY drb.farm ORDER BY stems DESC, buckets DESC, farm",
		params,
		as_dict=True,
	)

	# Valuation over the FULL filtered set, grouped by what the price depends on.
	val_pl, currency = _valuation_list()
	groups = frappe.db.sql(
		"SELECT " + DATE_COL + " AS date, drb.variety AS variety, drb.stem_length AS length,"
		" COALESCE(SUM(drb.stem_qty), 0) AS stems" + base + " GROUP BY 1, 2, 3",
		params,
		as_dict=True,
	)
	rows = frappe.db.sql(
		"SELECT dr.name AS request, " + DATE_COL + " AS date, drb.bucket_id AS bucket,"
		" drb.farm AS farm, drb.shelf AS shelf, drb.stem_length AS length, drb.variety AS variety,"
		" i.item_group AS item_group, COALESCE(drb.stem_qty, 0) AS stems, drb.age_days AS age_days,"
		" drb.discarded AS removed"
		+ base
		+ " ORDER BY "
		+ DATE_COL
		+ " DESC, dr.name DESC, drb.idx LIMIT "
		+ str(ROW_LIMIT),
		params,
		as_dict=True,
	)

	pricer = _Pricer(val_pl, {g.variety for g in groups if g.variety} if currency else set())
	fx = _Fx(currency)
	total_value = 0.0
	total_kes = 0.0
	kes_ok = bool(currency)
	priced_stems = unpriced_stems = 0
	for g in groups:
		stems = _int(g.stems)
		rt = pricer.rate(g.variety, g.length, g.date) if currency else None
		if rt is None:
			unpriced_stems += stems
			continue
		priced_stems += stems
		v = rt * stems
		total_value += v
		r = fx.rate(g.date)
		if r is None:
			kes_ok = False
		else:
			total_kes += v * r

	out_rows = []
	for r in rows:
		r.stems = _int(r.stems)
		r.age_days = round(float(r.age_days), 1) if r.age_days is not None else None
		r.rose = rose_core.rose_type(r.item_group)
		r.removed = bool(r.removed)
		rt = pricer.rate(r.variety, r.length, r.date) if currency else None
		r.rate = rt
		r.foregone = round(rt * r.stems, 2) if rt is not None else None
		out_rows.append(r)

	total_buckets = _int(agg.buckets)
	for r in by_variety + by_farm:
		r.stems = _int(r.stems)
		r.buckets = _int(r.buckets)

	# Filter options: farms that ever had an approved discard; varieties in the
	# current range/region/farm/rose (ignoring the variety pick and search).
	farms = frappe.db.sql_list(
		"SELECT DISTINCT drb.farm FROM `tabDiscard Request Bucket` drb"
		" INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent"
		" WHERE COALESCE(dr.workflow_state, '') = 'Approved' AND dr.docstatus < 2"
		" AND IFNULL(drb.farm, '') != '' ORDER BY drb.farm"
	)
	vparams = {}
	varieties = frappe.db.sql_list(
		"SELECT DISTINCT drb.variety"
		+ FROM
		+ " WHERE "
		+ _where(a, vparams, skip=("variety", "q"))
		+ " AND IFNULL(drb.variety, '') != '' ORDER BY drb.variety",
		vparams,
	)
	if a["variety"] and a["variety"] not in varieties:
		varieties.append(a["variety"])

	return {
		"success": True,
		"filters": a,
		"total_buckets": total_buckets,
		"total_stems": _int(agg.stems),
		"requests": _int(agg.requests),
		"removed_buckets": _int(agg.removed_buckets),
		"removed_stems": _int(agg.removed_stems),
		"avg_age_days": round(float(agg.avg_age), 1) if agg.avg_age is not None else None,
		"aged_buckets": _int(agg.aged),
		"valuation_price_list": val_pl,
		"valuation_currency": currency,
		"total_foregone": round(total_value, 2) if currency else None,
		"total_foregone_kes": round(total_kes, 2) if kes_ok and currency != "KES" else None,
		"priced_stems": priced_stems,
		"unpriced_stems": unpriced_stems,
		"truncated": total_buckets > len(out_rows),
		"row_limit": ROW_LIMIT,
		"rows": out_rows,
		"by_variety": by_variety,
		"by_farm": by_farm,
		"farms": farms,
		"varieties": varieties,
	}
