# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Order Summary v2 was merged into "Orders & Workflow" (/packhouse-dashboard-v2).
# Old links and bookmarks are redirected there with their query string intact.

import frappe

no_cache = 1


def get_context(context):
	qs = frappe.request.query_string.decode() if frappe.request and frappe.request.query_string else ""
	frappe.local.flags.redirect_location = "/packhouse-dashboard-v2" + ("?" + qs if qs else "")
	raise frappe.Redirect(302)
