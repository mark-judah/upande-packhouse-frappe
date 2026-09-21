# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Page controller for the `sales-settings` web page. Same CSRF story as
# specifications.py / variety_tree.py: saveCustomerPriceListDefault /
# setCurrencyEnabled / createPriceList are POSTs.

import frappe
from frappe.sessions import get_csrf_token

no_cache = 1


def get_context(context):
	context.csrf_token = get_csrf_token()
	context.no_cache = 1
	return context
