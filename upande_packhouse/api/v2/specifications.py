# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Specifications v2 (www/specifications-v2.html) — list + KPIs.

The editor (get / save / delete, pickers) keeps using the v1 endpoints in
api/specifications.py unchanged; only the list view reads from here, because
the v1 list is capped at 200 rows and the page used to count its KPIs in the
browser over that capped list (349 specs on kaitet.local, so every KPI was
silently wrong with no filter set).

Filters (all applied in SQL, before the row limit; KPIs over the full set):
    query          spec name or spec ID, LIKE
    customer       exact customer
    status         Active / Inactive ("" = any, including specs with no status)
    spec_type      Permanent / Temporary
    box_type       exact Box Type
    box_assortment Mono Box / Mixed Box
    from_date / to_date
                   "used on orders delivering in this range": keeps only specs
                   that a submitted Sales Order line (Sales Order Item.custom_line)
                   points at with Sales Order.delivery_date in the range
                   (README rule 4: the order pages' date meaning). Without a
                   range, usage is counted over all submitted orders.

There is no farm / region notion: a spec belongs to a customer, not a farm.
"""

import frappe
from frappe.utils import getdate, nowdate

ROW_LIMIT = 1000


def _args(kw):
	a = dict(frappe.form_dict)
	a.update({k: v for k, v in kw.items() if v is not None})
	return {k: (str(v).strip() if v is not None else "") for k, v in a.items()}


def _usage_sql(a, params):
	"""Per-spec usage on submitted Sales Order lines, optionally within the
	delivery-date range. Cancelled / draft orders never count (rule 5)."""
	cond = ["so.docstatus = 1", "IFNULL(soi.custom_line, '') != ''"]
	if a.get("from_date"):
		cond.append("so.delivery_date >= %(from_date)s")
		params["from_date"] = getdate(a["from_date"])
	if a.get("to_date"):
		cond.append("so.delivery_date <= %(to_date)s")
		params["to_date"] = getdate(a["to_date"])
	return f"""
		SELECT soi.custom_line AS spec,
			COUNT(*) AS order_lines,
			COUNT(DISTINCT so.name) AS orders,
			MAX(so.delivery_date) AS last_delivery
		FROM `tabSales Order Item` soi
		JOIN `tabSales Order` so ON so.name = soi.parent
		WHERE {" AND ".join(cond)}
		GROUP BY soi.custom_line
	"""


def _where(a, params):
	cond = ["1=1"]
	if a.get("query"):
		cond.append("(s.spec_name LIKE %(q)s OR s.name LIKE %(q)s)")
		params["q"] = "%" + a["query"] + "%"
	for key in ("customer", "status", "spec_type", "box_type", "box_assortment"):
		if a.get(key):
			cond.append(f"s.{key} = %({key})s")
			params[key] = a[key]
	if a.get("from_date") or a.get("to_date"):
		cond.append("u.spec IS NOT NULL")
	return " AND ".join(cond)


@frappe.whitelist()
def get_specifications(**kw):
	"""Filtered spec list + KPIs computed over the full filtered set."""
	try:
		a = _args(kw)
		params = {"today": getdate(nowdate())}
		usage = _usage_sql(a, params)
		where = _where(a, params)
		base = f"FROM `tabSpecifications` s LEFT JOIN ({usage}) u ON u.spec = s.name WHERE {where}"

		k = frappe.db.sql(
			f"""SELECT COUNT(*) AS total,
				SUM(s.status = 'Active') AS active,
				SUM(s.status = 'Inactive') AS inactive,
				SUM(s.spec_type = 'Temporary') AS temporary,
				SUM(s.spec_type = 'Temporary' AND s.expiry_date < %(today)s) AS temporary_expired,
				COUNT(DISTINCT NULLIF(s.customer, '')) AS customers,
				SUM(IFNULL(s.customer, '') = '') AS no_customer,
				SUM(u.spec IS NOT NULL) AS used,
				IFNULL(SUM(u.order_lines), 0) AS order_lines
			{base}""",
			params,
			as_dict=True,
		)[0]
		# Distinct orders across the filtered specs (one order can use many specs).
		orders = frappe.db.sql(
			f"""SELECT COUNT(DISTINCT so.name)
			FROM `tabSales Order Item` soi
			JOIN `tabSales Order` so ON so.name = soi.parent AND so.docstatus = 1
			WHERE soi.custom_line IN (SELECT s.name {base})
			{"AND so.delivery_date >= %(from_date)s" if a.get("from_date") else ""}
			{"AND so.delivery_date <= %(to_date)s" if a.get("to_date") else ""}""",
			params,
		)[0][0]

		rows = frappe.db.sql(
			f"""SELECT s.name, s.spec_name, s.customer, s.box_assortment, s.box_type,
				s.status, s.spec_type, s.valid_from, s.expiry_date, s.modified,
				IFNULL(u.order_lines, 0) AS order_lines, IFNULL(u.orders, 0) AS orders,
				u.last_delivery
			{base}
			ORDER BY s.modified DESC
			LIMIT {ROW_LIMIT}""",
			params,
			as_dict=True,
		)
		total = int(k.total or 0)
		kpis = {
			"total": total,
			"active": int(k.active or 0),
			"inactive": int(k.inactive or 0),
			"temporary": int(k.temporary or 0),
			"temporary_expired": int(k.temporary_expired or 0),
			"customers": int(k.customers or 0),
			"no_customer": int(k.no_customer or 0),
			"used": int(k.used or 0),
			"order_lines": int(k.order_lines or 0),
			"orders": int(orders or 0),
		}
		return {
			"success": True,
			"specifications": rows,
			"kpis": kpis,
			"total": total,
			"truncated": total > len(rows),
			"dated": bool(a.get("from_date") or a.get("to_date")),
		}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="v2 get_specifications error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}
