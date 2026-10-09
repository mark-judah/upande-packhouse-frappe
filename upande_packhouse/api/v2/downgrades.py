# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Downgrades v2 (www/packhouse-downgrades-v2.html).

A downgrade is a Pick List Item on a submitted Order Pick List where the
allocator picked a bucket LONGER than the order line asked for and gave a
reason (sales_allocation only allows a longer bucket, `length_status =
"downgrade"`, and writes `downgrade_reason`):

    original length = Pick List Item.stem_length   (the bucket's graded length,
                      written at allocation from the shelf batch)
    sold length     = Sales Order Item.custom_length
    downgrade       = downgrade_reason set AND original cm > sold cm

This replaces the v1 rule (latest Harvesting entry of a reused bucket ID vs the
pick list length, audit WF-28): the v1 "picklist length" IS the bucket's
length, so v1 compared the bucket with an arbitrary older harvest.

Filters (all in SQL, before any limit, README rule 3):
  date    SO delivery date of the line's order (README rule 4: pick lists
          inherit their order's delivery date), falling back to
          OPL.date_created when the line has no order.
  region / farm   the ORDER's farm (README rule 6, amended): Sales Order.farm,
          then custom_farm, then Order Pick List.farm (region.order_farm_sql).
          The bucket's origin farm (Pick List Item.farm) is returned as
          `origin_farm` and shown as its own column, not filtered on.
  team    Order Pick List.team
  rose    item group subtree (core.rose)
  owner   Order Pick List.owner (the user who allocated / created the OPL)
  reason  exact downgrade reason
  q       free-text over order, OPL, customer, variety, reason, owner, team

Foregone revenue per line = max(0, list price at original length − sold price
per stem) × stems, in the order's currency, at the price valid on the line
date. Price list: Production Settings "Currency Price List" for the currency,
else the order's own selling price list (when it is in the order currency),
else "<CUR> Price List". KES total = Σ line foregone × the selling FX rate on
the line date.
"""

import re

import frappe
from frappe.utils import getdate, today

from upande_packhouse.api.v2.core import region as region_mod
from upande_packhouse.api.v2.core import rose as rose_mod

ROW_LIMIT = 2000
SEP = "|~|"

_DATE = "COALESCE(so.delivery_date, opl.date_created)"
# Region / farm filter: the ORDER's farm (README rule 6, amended 2026-10-07).
_FARM = region_mod.order_farm_sql("so", "opl")
# Where the downgraded bucket came from (Pick List Item.farm): shown as a column.
_ORIGIN = "NULLIF(pli.farm, '')"
_CM = "CAST(NULLIF(REGEXP_REPLACE({0}, '[^0-9]', ''), '') AS UNSIGNED)"

_FROM = (
	" FROM `tabPick List Item` pli"
	" INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent"
	" LEFT JOIN `tabSales Order Item` soi ON soi.name = pli.sales_order_item"
	" LEFT JOIN `tabSales Order` so ON so.name = COALESCE(NULLIF(soi.parent, ''), opl.sales_order)"
	" LEFT JOIN `tabItem` it ON it.name = pli.item_code"
)


def _cm(v):
	m = re.search(r"\d+", str(v or ""))
	return int(m.group()) if m else None


def _split(v, sep):
	return [x.strip() for x in str(v or "").split(sep) if x.strip()]


def _in(params, key, values):
	params[key] = tuple(values) or ("",)
	return "%(" + key + ")s"


def _base_where(args, params):
	"""Population + date / region / farm / team / rose (the filters the option lists follow)."""
	w = [
		"pli.parenttype = 'Order Pick List'",
		"opl.docstatus = 1",
		"IFNULL(so.docstatus, 1) = 1",
		"IFNULL(pli.downgrade_reason, '') != ''",
		_CM.format("pli.stem_length") + " > " + _CM.format("soi.custom_length"),
		_DATE + " BETWEEN %(from_date)s AND %(to_date)s",
	]
	farms = region_mod.farms_for(region=args.get("region"), farm=args.get("farm"))
	if farms is not None:
		w.append(_FARM + " IN " + _in(params, "farms", region_mod.sql_tuple(farms)))
	teams = _split(args.get("team"), ",")
	if teams:
		w.append("opl.team IN " + _in(params, "teams", teams))
	sql = " AND ".join(w)
	sql += rose_mod.rose_sql("it.item_group", args.get("rose"), params)
	return sql


def _full_where(args, params):
	sql = _base_where(args, params)
	owners = _split(args.get("owner"), SEP)
	if owners:
		sql += " AND opl.owner IN " + _in(params, "owners", owners)
	reasons = _split(args.get("reason"), SEP)
	if reasons:
		sql += " AND TRIM(pli.downgrade_reason) IN " + _in(params, "reasons", reasons)
	q = (args.get("q") or "").strip()
	if q:
		params["q"] = "%" + q + "%"
		cols = [
			"opl.order_name",
			"opl.name",
			"opl.customer",
			"pli.item_code",
			"pli.downgrade_reason",
			"opl.owner",
			"opl.team",
			"so.name",
		]
		sql += " AND (" + " OR ".join("IFNULL({0}, '') LIKE %(q)s".format(c) for c in cols) + ")"
	return sql


def _args(kw):
	a = dict(frappe.form_dict)
	a.update({k: v for k, v in kw.items() if v is not None})
	return a


# ── pricing ──────────────────────────────────────────────────────────


def _pick_price_list(currency, so_price_list, cur_pl, pl_cur):
	if not currency:
		return None
	if cur_pl.get(currency):
		return cur_pl[currency]
	if so_price_list and pl_cur.get(so_price_list) == currency:
		return so_price_list
	default = currency + " Price List"
	return default if pl_cur.get(default) == currency else None


def _best_on(cands, on, f="valid_from", u="valid_upto"):
	"""The record valid on date `on` (latest start); else the first one starting after it."""
	valid = [c for c in cands if (not c[f] or getdate(c[f]) <= on) and (not c.get(u) or getdate(c[u]) >= on)]
	if valid:
		return max(valid, key=lambda c: getdate(c[f]) if c[f] else getdate("1900-01-01"))
	later = [c for c in cands if c[f] and getdate(c[f]) > on]
	return min(later, key=lambda c: getdate(c[f])) if later else None


def _value(rows):
	cur_pl = {}
	if frappe.db.exists("DocType", "Currency Price List"):
		for r in frappe.get_all(
			"Currency Price List",
			filters={"parenttype": "Production Settings"},
			fields=["currency", "price_list"],
		):
			cur_pl[r.currency] = r.price_list
	pl_cur = {p.name: p.currency for p in frappe.get_all("Price List", fields=["name", "currency"])}

	for r in rows:
		r["price_list"] = _pick_price_list(r.get("so_currency"), r.get("so_price_list"), cur_pl, pl_cur)
		r["currency"] = r.get("so_currency")

	keys = {
		(r["price_list"], r["variety"], r["original_length"])
		for r in rows
		if r["price_list"] and r["original_length"]
	}
	prices = {}
	if keys:
		p = {}
		for ip in frappe.db.sql(
			"SELECT price_list, item_code, custom_length, price_list_rate, valid_from, valid_upto"
			" FROM `tabItem Price` WHERE selling = 1"
			" AND price_list IN "
			+ _in(p, "pl", {k[0] for k in keys})
			+ " AND item_code IN "
			+ _in(p, "it", {k[1] for k in keys})
			+ " AND custom_length IN "
			+ _in(p, "ln", {k[2] for k in keys}),
			p,
			as_dict=True,
		):
			prices.setdefault((ip.price_list, ip.item_code, ip.custom_length), []).append(ip)

	fx_rows = {}
	curs = {r["currency"] for r in rows if r["currency"] and r["currency"] != "KES"}
	if curs:
		p = {}
		for x in frappe.db.sql(
			"SELECT from_currency, exchange_rate, date AS valid_from FROM `tabCurrency Exchange`"
			" WHERE to_currency = 'KES' AND for_selling = 1 AND from_currency IN " + _in(p, "c", curs),
			p,
			as_dict=True,
		):
			fx_rows.setdefault(x.from_currency, []).append(x)

	fx_used = {}
	for r in rows:
		on = getdate(r["date"]) if r.get("date") else getdate(today())
		sold = None
		if r.get("so_rate") is not None:
			conv = float(r.get("conv") or 0) or 1.0
			sold = round(float(r["so_rate"]) / conv, 4)
		r["rate_sold"] = sold
		best = _best_on(prices.get((r["price_list"], r["variety"], r["original_length"]), []), on)
		r["rate_original"] = float(best.price_list_rate) if best else None
		r["foregone"] = None
		r["foregone_kes"] = None
		if r["rate_original"] is not None and sold is not None:
			r["foregone"] = round(max(0.0, r["rate_original"] - sold) * r["dg_stems"], 2)
			if r["currency"] == "KES":
				rate = 1.0
			else:
				fx = _best_on(fx_rows.get(r["currency"], []), on, u="__none__")
				rate = float(fx.exchange_rate) if fx else None
			if rate is not None:
				r["fx_to_kes"] = rate
				r["foregone_kes"] = round(r["foregone"] * rate, 2)
				fx_used.setdefault(r["currency"], set()).add(rate)
	return fx_used


# ── endpoint ─────────────────────────────────────────────────────────


@frappe.whitelist()
def get_downgrades(**kw):
	"""Downgraded pick list lines, KPIs and roll-ups for the filters.

	Args (all optional): from_date, to_date, region, farm, team (comma list),
	rose ('all'|'standard'|'spray'), owner / reason ('|~|' lists), q.
	"""
	a = _args(kw)
	t = today()
	from_date = a.get("from_date") or t
	to_date = a.get("to_date") or t
	if str(from_date) > str(to_date):
		from_date, to_date = to_date, from_date

	params = {"from_date": from_date, "to_date": to_date}
	where = _full_where(a, params)
	rows = frappe.db.sql(
		"SELECT opl.name AS opl, opl.customer, opl.team, " + _DATE + " AS date, opl.order_name,"
		" so.name AS sales_order, opl.owner AS allocated_by, "
		+ _FARM
		+ " AS farm, "
		+ _ORIGIN
		+ " AS origin_farm,"
		" pli.item_code AS variety, pli.bucket, pli.stem_length AS original_length,"
		" soi.custom_length AS sold_length,"
		" COALESCE(NULLIF(pli.stock_qty, 0), pli.qty * COALESCE(NULLIF(pli.conversion_factor, 0), 1), 0) AS dg_stems,"
		" pli.available_stems_of_exact_length AS avail_raw, TRIM(pli.downgrade_reason) AS reason,"
		" it.item_group AS rose_group, soi.rate AS so_rate, soi.conversion_factor AS conv,"
		" so.currency AS so_currency, so.selling_price_list AS so_price_list"
		+ _FROM
		+ " WHERE "
		+ where
		+ " ORDER BY date DESC, opl.name, pli.idx",
		params,
		as_dict=True,
	)

	for r in rows:
		r["dg_stems"] = float(r["dg_stems"] or 0)
		try:
			r["avail_exact_stems"] = int(float(r.pop("avail_raw") or 0))
		except (TypeError, ValueError):
			r["avail_exact_stems"] = 0
		oc, sc = _cm(r["original_length"]), _cm(r["sold_length"])
		r["cm_lost"] = (oc - sc) if oc and sc else None
		kind = rose_mod.rose_type(r.get("rose_group"))
		r["rose"] = kind.title() if kind else ""

	fx_used = _value(rows)

	# KPIs and roll-ups over the FULL filtered set.
	by_cur = {}
	no_price = 0
	no_fx = set()
	stems = 0.0
	cm_weighted = 0.0
	reasons = {}
	for r in rows:
		stems += r["dg_stems"]
		if r["cm_lost"]:
			cm_weighted += r["cm_lost"] * r["dg_stems"]
		if r["foregone"] is None:
			no_price += 1
		else:
			by_cur[r["currency"]] = round(by_cur.get(r["currency"], 0.0) + r["foregone"], 2)
			if r["foregone_kes"] is None:
				no_fx.add(r["currency"])
		# Grouped case-insensitively, like the SQL reason filter (MariaDB collation).
		label = r["reason"] or "(no reason)"
		g = reasons.setdefault(label.casefold(), {"reason": label, "lines": 0, "stems": 0.0})
		g["lines"] += 1
		g["stems"] += r["dg_stems"]
		for k in ("so_rate", "conv", "rose_group", "so_price_list"):
			r.pop(k, None)

	total_kes = round(sum(r["foregone_kes"] or 0 for r in rows), 2)
	unpriced = sorted({r["so_currency"] for r in rows if r["so_currency"] and not r["price_list"]})

	# Option lists follow date / region / farm / team / rose, not owner / reason / q.
	op = {"from_date": from_date, "to_date": to_date}
	bw = _base_where(a, op)
	opts = frappe.db.sql(
		"SELECT DISTINCT opl.owner, TRIM(pli.downgrade_reason) AS reason" + _FROM + " WHERE " + bw,
		op,
		as_dict=True,
	)
	farms_seen = frappe.db.sql_list(
		"SELECT DISTINCT "
		+ _FARM
		+ " FROM `tabPick List Item` pli INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent"
		" LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order"
		" WHERE pli.parenttype = 'Order Pick List' AND IFNULL(pli.downgrade_reason, '') != ''"
	)
	all_farms = sorted({f for fs in region_mod.REGIONS.values() for f in fs} | {f for f in farms_seen if f})

	return {
		"success": True,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"kpis": {
			"lines": len(rows),
			"stems": stems,
			"orders": len({r["sales_order"] or r["opl"] for r in rows}),
			"avg_cm_lost": round(cm_weighted / stems, 1) if stems else None,
			"foregone_kes": total_kes,
			"priced_lines": len(rows) - no_price,
			"unpriced_lines": no_price,
		},
		"foregone_by_currency": by_cur,
		"fx_to_kes": {c: sorted(v) for c, v in fx_used.items()},
		"currencies_without_fx": sorted(no_fx),
		"currencies_without_price_list": unpriced,
		"by_reason": sorted(reasons.values(), key=lambda g: -g["stems"]),
		"owners": sorted({o.owner for o in opts if o.owner}),
		"reasons": sorted({o.reason.casefold(): o.reason for o in opts if o.reason}.values()),
		"farms": all_farms,
		"total_rows": len(rows),
		"truncated": len(rows) > ROW_LIMIT,
		"rows": rows[:ROW_LIMIT],
	}
