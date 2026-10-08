# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Sales Settings v2 (www/sales-settings-v2.html). Rebuild of /sales-settings on
# the shared packhouse v2 design system. Same API as the original page
# (api/sales_settings.py + api/specifications.searchCustomers). page_context()
# mints the CSRF token the original controller did -- setCurrencyEnabled,
# setPriceListEnabled, createPriceList, saveCustomerPriceListDefault,
# deleteCustomerPriceListDefault and setDefaultWarehouse are POSTs.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "sales-settings", "Sales Settings")
