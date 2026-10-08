# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Sales Settings v2 (www/sales-settings-v2.html) -- KPI read.

The page's lists and every save still go through the v1 endpoints in
api/sales_settings.py (unchanged). This module only computes the KPI strip on
the server (README rules 3, 7) over the same rows those list endpoints return,
narrowed by the page's two filters:

    currency  exact currency code -- Currency.name, Price List.currency,
              Customer Price List Default.currency
    q         case-insensitive substring, the same test as PH.match() in the
              browser, over the fields each list shows:
                currencies     name, symbol
                price lists    name, currency
                defaults       customer, price_list, currency

So every KPI equals the count of what the page lists for the same filters.
The default warehouse is one site-wide setting; no filter applies to it.

The page has no date dimension (reference records, not transactions) and no
farm notion, so it takes neither from_date/to_date nor region.
"""

import frappe


def _match(row, q, keys):
	"""Python twin of PH.match(row, q, keys)."""
	if not q:
		return True
	q = q.lower()
	return any(q in str(row.get(k) if row.get(k) is not None else "").lower() for k in keys)


@frappe.whitelist(methods=["GET"])
def get_summary(currency=None, q=None):
	try:
		currency = (currency or "").strip()
		q = (q or "").strip()

		currencies = frappe.get_all("Currency", fields=["name", "symbol", "enabled"])
		currencies = [
			c for c in currencies if (not currency or c.name == currency) and _match(c, q, ("name", "symbol"))
		]

		price_lists = frappe.get_all(
			"Price List", filters={"selling": 1}, fields=["name", "currency", "enabled"]
		)
		price_lists = [
			p
			for p in price_lists
			if (not currency or p.currency == currency) and _match(p, q, ("name", "currency"))
		]

		defaults = frappe.get_all(
			"Customer Price List Default", fields=["customer", "currency", "price_list", "disabled"]
		)
		defaults = [
			d
			for d in defaults
			if (not currency or d.currency == currency)
			and _match(d, q, ("customer", "price_list", "currency"))
		]
		defaults_active = sum(1 for d in defaults if not d.disabled)

		return {
			"success": True,
			"filters": {"currency": currency, "q": q},
			"currencies_total": len(currencies),
			"currencies_enabled": sum(1 for c in currencies if c.enabled),
			"price_lists_total": len(price_lists),
			"price_lists_enabled": sum(1 for p in price_lists if p.enabled),
			"defaults_total": len(defaults),
			"defaults_active": defaults_active,
			"defaults_disabled": len(defaults) - defaults_active,
			"default_warehouse": frappe.db.get_single_value("Sales Settings", "default_warehouse") or "",
		}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="sales_settings.get_summary error", message=str(e))
		return {"success": False, "error": str(e)}
