# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Biflorica Orders v2 / Floriday Orders v2 (www/biflorica-orders-v2.html,
www/floriday-orders-v2.html) — the orders list and its KPIs.

The rest of those pages (settings, schedules, polling, stock offered, live
offers) still runs on ecommerce_integration's own endpoints
(utils.channel_portal / utils.stock_picker); only the read of the imported
orders moved here so every KPI follows every filter on the server
(README rules 3, 6, 7).

Which orders belong to a channel: the same test as
ecommerce_integration.utils.channel_portal (_channel_filters /
_belongs_to_channel):
  Biflorica  po_no LIKE 'BIFLORICA-%'  (Predeal when 'BIFLORICA-PREDEAL-%')
  Floriday   po_no is a UUID, or custom_floriday_delivery_id /
             custom_floriday_fulfillment_order_id is set
             (Fulfilled when the fulfilment id is set)

Date meaning: the order date (Sales Order.transaction_date; for a Quotation
also transaction_date). This page is the channel's intake list: the importers
stamp transaction_date with the day the order came in (Biflorica: import day,
Floriday: the order's own date), so a just-pulled draft is always inside
"today" / "7 days", whatever its delivery date. Delivery date is shown per row.

Orders: drafts (docstatus 0) and submitted (1), labelled separately; cancelled
orders are never listed or counted (rule 5).

Numbers (all over the full filtered set, never the truncated list):
  orders        count of listed orders
  drafts        count of listed orders with docstatus 0
  stems         pipeline.fetch_lines -> ordered_stems (units.ordered_stems)
                for Sales Orders; Quotation Item.stock_qty for Quotations
  value         sum of the in-scope line amounts (Sales/Quotation Item.amount,
                i.e. before tax), per currency
A row's stems / value are its in-scope lines only, so KPI = sum of rows.

Farm / region (rule 6, amended): the ORDER's farm,
region.order_farm_sql() = COALESCE(so.farm, so.custom_farm, Order Pick List.farm).
With a farm or region set, only orders whose farm is in the selection count
(drafts and orders without a pick list included when they carry a farm).
Orders with no farm anywhere, and Quotations (no farm field), drop out while a
farm/region filter is on. The filter is applied in SQL on the order header, so
every line of an in-scope order counts.
"""

from collections import defaultdict

import frappe
from frappe.utils import cint, flt, getdate

from upande_packhouse.api.v2.core import pipeline
from upande_packhouse.api.v2.core import region as region_mod

CHANNELS = ("biflorica", "floriday")
UUID_LIKE = "________-____-____-____-____________"
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


def _check_channel(channel):
	"""Same gate as channel_portal._channel: known channel, settings exist, readable."""
	key = (channel or "").strip().lower()
	if key not in CHANNELS:
		return None, f"Unknown sales channel: {channel}"
	if "ecommerce_integration" not in frappe.get_installed_apps():
		return None, "The Ecommerce Integration app is not installed on this site."
	settings = "Biflorica Setting" if key == "biflorica" else "Floriday Settings"
	if not frappe.db.exists("DocType", settings):
		return None, f"{settings} is not set up on this site."
	frappe.has_permission(settings, "read", throw=True)
	return key, None


def _channel_sql(key, doctype, alias):
	"""SQL condition picking this channel's orders out of `doctype`."""
	if key == "biflorica":
		return f"{alias}.po_no LIKE 'BIFLORICA-%%'"
	ors = [f"{alias}.po_no LIKE '{UUID_LIKE}'"]
	for col in ("custom_floriday_delivery_id", "custom_floriday_fulfillment_order_id"):
		if frappe.db.has_column(doctype, col):
			ors.append(f"IFNULL({alias}.{col}, '') != ''")
	return "(" + " OR ".join(ors) + ")"


def _orders(key, doctype, params, status, q, farms=None):
	"""Header rows of this channel's orders in the date range (no limit)."""
	if not frappe.db.has_column(doctype, "po_no") or not frappe.has_permission(doctype, "read"):
		return [], set()
	sales = doctype == "Sales Order"
	cols = [
		"d.name",
		"d.customer" if sales else "d.party_name AS customer",
		"d.customer_name",
		"d.transaction_date",
		"d.delivery_date" if sales else "d.valid_till AS delivery_date",
		"d.po_no",
		"d.status",
		"d.docstatus",
		"d.grand_total",
		"d.currency",
	]
	optional = ("custom_consignee", "custom_floriday_fulfillment_order_id")
	for col in optional:
		cols.append(f"d.{col}" if frappe.db.has_column(doctype, col) else f"NULL AS {col}")

	joins = ""
	if sales:
		# one pick-list farm per order (fallback of the order-farm expression)
		joins = (
			" LEFT JOIN (SELECT sales_order, MIN(NULLIF(farm, '')) AS farm FROM `tabOrder Pick List`"
			" WHERE docstatus < 2 GROUP BY sales_order) opl ON opl.sales_order = d.name"
		)
		cols.append(region_mod.order_farm_sql(so_alias="d", opl_alias="opl") + " AS order_farm")
	else:
		cols.append("NULL AS order_farm")
	where = [
		_channel_sql(key, doctype, "d"),
		"d.docstatus < 2",
		"d.transaction_date BETWEEN %(from_date)s AND %(to_date)s",
	]
	if status == "draft":
		where.append("d.docstatus = 0")
	elif status == "submitted":
		where.append("d.docstatus = 1")
	if q:
		search = ["d.name", "d.customer_name", "d.po_no", "d.status"]
		search.append("d.customer" if sales else "d.party_name")
		if frappe.db.has_column(doctype, "custom_consignee"):
			search.append("d.custom_consignee")
		where.append("(" + " OR ".join(f"IFNULL({c}, '') LIKE %(q)s" for c in search) + ")")

	rows = frappe.db.sql(
		f"SELECT {', '.join(cols)} FROM `tab{doctype}` d{joins} WHERE {' AND '.join(where)}",  # nosemgrep: fixed SQL, values bound
		params,
		as_dict=True,
	)
	options = {r.order_farm for r in rows if r.order_farm}
	if farms is not None:
		fs = set(farms)
		rows = [r for r in rows if r.order_farm in fs]
	for r in rows:
		r.doctype = doctype
		po = r.po_no or ""
		if key == "biflorica":
			r.kind = "Predeal" if po.startswith("BIFLORICA-PREDEAL-") else "Deal"
			r.channel_ref = po.split("-")[-1]
		else:
			r.kind = "Fulfilled" if r.custom_floriday_fulfillment_order_id else "Order"
			r.channel_ref = po[-8:]
	return rows, options


def _sales_order_lines(names):
	"""Sales Order lines through the core definition (ordered_stems), drafts included."""
	if not names:
		return []
	return pipeline.fetch_lines(sales_orders=list(names), docstatus=(0, 1), business_unit=None)


def _quotation_lines(names):
	if not names:
		return []
	return frappe.db.sql(
		"""SELECT name, parent, stock_qty AS ordered_stems, amount
		   FROM `tabQuotation Item`
		   WHERE parent IN %(p)s AND parenttype = 'Quotation' AND IFNULL(item_code, '') != ''""",
		{"p": tuple(names)},
		as_dict=True,
	)


@frappe.whitelist()
def get_channel_orders(
	channel=None,
	from_date=None,
	to_date=None,
	status="all",
	q=None,
	region=None,
	farm=None,
	limit=DEFAULT_LIMIT,
):
	"""Orders imported from a sales channel, with KPIs over the full filtered set."""
	key, error = _check_channel(channel)
	if error:
		return {"success": False, "error": error}
	try:
		d_from = getdate(from_date) if from_date else None
		d_to = getdate(to_date) if to_date else None
	except Exception:
		return {"success": False, "error": "Dates must be YYYY-MM-DD."}
	if not d_from or not d_to:
		return {"success": False, "error": "Choose a from and a to date."}
	if d_from > d_to:
		d_from, d_to = d_to, d_from
	status = (status or "all").strip().lower()
	if status not in ("all", "draft", "submitted"):
		status = "all"
	q = (q or "").strip()
	limit = max(1, min(cint(limit) or DEFAULT_LIMIT, MAX_LIMIT))

	params = {"from_date": d_from, "to_date": d_to, "q": f"%{q}%"}
	farms = region_mod.farms_for(region=region, farm=farm)
	orders, farm_options = [], set()
	for doctype in ("Sales Order", "Quotation"):
		found, opts = _orders(key, doctype, params, status, q, farms)
		orders.extend(found)
		farm_options |= opts

	so_names = [o.name for o in orders if o.doctype == "Sales Order"]
	qt_names = [o.name for o in orders if o.doctype == "Quotation"]
	agg = {}  # (doctype, name) -> {stems, value, lines}
	for doctype, lines in (
		("Sales Order", _sales_order_lines(so_names)),
		("Quotation", _quotation_lines(qt_names)),
	):
		for ln in lines:
			a = agg.setdefault((doctype, ln.parent), {"stems": 0.0, "value": 0.0, "lines": 0})
			a["stems"] += flt(ln.ordered_stems)
			a["value"] += flt(ln.amount)
			a["lines"] += 1

	rows = []
	for o in orders:
		a = agg.get((o.doctype, o.name)) or {"stems": 0.0, "value": 0.0, "lines": 0}
		o.ordered_stems = a["stems"]
		o.value = round(a["value"], 2)
		o.grand_total = flt(o.grand_total)
		o.lines = a["lines"]
		o.farms = o.pop("order_farm") or ""
		o.pop("custom_floriday_fulfillment_order_id", None)
		rows.append(o)

	rows.sort(key=lambda r: (str(r.transaction_date or ""), r.name), reverse=True)

	value_by_currency = defaultdict(float)
	for r in rows:
		value_by_currency[r.currency or ""] += r.value
	kpis = {
		"orders": len(rows),
		"drafts": sum(1 for r in rows if r.docstatus == 0),
		"submitted": sum(1 for r in rows if r.docstatus == 1),
		"ordered_stems": float(sum(r.ordered_stems for r in rows)),
		"value_by_currency": [
			{"currency": c, "value": round(v, 2)}
			for c, v in sorted(value_by_currency.items(), key=lambda kv: -kv[1])
		],
	}

	return {
		"success": True,
		"channel": key,
		"from_date": str(d_from),
		"to_date": str(d_to),
		"date_field": "transaction_date",
		"farms_filter": farms,
		"kpis": kpis,
		"orders": rows[:limit],
		"total": len(rows),
		"truncated": len(rows) > limit,
		"farm_options": sorted(farm_options),
	}
