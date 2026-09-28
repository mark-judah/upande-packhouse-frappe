# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Logistics was merged into the Bucket Transfers dashboard
# (www/transfer-control.html, Logistics tab). Keep the old route working —
# sidebar/workspace links and deep links such as
# /bucket-logistics?date=…&q=…&variety=… from Order Fulfilment — by redirecting
# with the query string carried over.

import frappe

no_cache = 1


def get_context(context):
	qs = frappe.request.query_string.decode() if frappe.request and frappe.request.query_string else ""
	frappe.local.flags.redirect_location = "/transfer-control?tab=logistics" + ("&" + qs if qs else "")
	raise frappe.Redirect
