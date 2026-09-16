import json

import frappe


def run():
	# All territories
	territories = frappe.get_all("Territory", pluck="name", order_by="name")
	print("TERRITORIES (%d):" % len(territories))
	for t in territories:
		print("  ", t)

	print()
	# Customer counts by territory + currently-assigned default_price_list + its currency
	rows = frappe.db.sql(
		"""
        SELECT c.territory, c.default_price_list, pl.currency, COUNT(*) as cnt
        FROM `tabCustomer` c
        LEFT JOIN `tabPrice List` pl ON pl.name = c.default_price_list
        GROUP BY c.territory, c.default_price_list, pl.currency
        ORDER BY c.territory, cnt DESC
    """,
		as_dict=True,
	)
	print("CUSTOMER default_price_list DISTRIBUTION BY TERRITORY:")
	for r in rows:
		print(
			"  territory=%r  default_price_list=%r  currency=%r  count=%d"
			% (r.territory, r.default_price_list, r.currency, r.cnt)
		)

	print()
	# currency field directly on customer (default_currency), by territory
	rows2 = frappe.db.sql(
		"""
        SELECT territory, default_currency, COUNT(*) as cnt
        FROM `tabCustomer`
        GROUP BY territory, default_currency
        ORDER BY territory, cnt DESC
    """,
		as_dict=True,
	)
	print("CUSTOMER default_currency DISTRIBUTION BY TERRITORY:")
	for r in rows2:
		print("  territory=%r  default_currency=%r  count=%d" % (r.territory, r.default_currency, r.cnt))


run()
