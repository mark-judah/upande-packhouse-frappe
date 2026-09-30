# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Page controller for the `biflorica-orders` web page. Same CSRF story as
# packhouse_dashboard.py: polling, enabling stock and posting offers are POSTs.
# The page body is shared with the other sales channel -- see
# templates/includes/channel_orders.html.

import frappe
from frappe.sessions import get_csrf_token

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/biflorica-orders"
		raise frappe.Redirect
	context.csrf_token = get_csrf_token()
	context.no_cache = 1
	return context
