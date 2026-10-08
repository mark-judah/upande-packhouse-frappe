# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Sales Order v2 (www/sales-order-v2.html). Rebuild of /sales-order on the
# shared packhouse v2 design system. Same API as the original page
# (api/sales_order.py, api/order_import.py, spec_autofill.py).
#
# CSRF: page_context() mints the token at render time, same as the original
# controller -- saveSalesOrder / submitSalesOrder / etc are POSTs.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "sales-order", "Sales Order")
