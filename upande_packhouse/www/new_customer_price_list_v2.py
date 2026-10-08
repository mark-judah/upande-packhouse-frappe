# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# New Customer Price List v2 (www/new-customer-price-list-v2.html). Rebuild of
# /new-customer-price-list on the shared packhouse v2 design system. Same API
# as the original page.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "new-customer-price-list", "New Customer Price List")
