# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Page controller — mints/persists a session CSRF token at render time so Frappe
# injects a valid `frappe.csrf_token` into the page. Without it a session that has
# no token yet makes the framework inject `frappe.csrf_token = "None"`, and every
# POST from the page (frappe.call, uploads) then fails with
# "CSRFTokenError: Invalid Request". See www/variety_tree.py for the full write-up.

import frappe
from frappe.sessions import get_csrf_token

from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import default_delivery_date

no_cache = 1


def get_context(context):
	context.csrf_token = get_csrf_token()
	context.no_cache = 1
	# Tomorrow by the server's clock (EAT): the page opens on it.
	context.default_delivery_date = default_delivery_date()
	return context
