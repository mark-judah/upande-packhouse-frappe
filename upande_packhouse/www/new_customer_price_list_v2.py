# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# New Customer Price List v2 (www/new-customer-price-list-v2.html). Rebuild of
# /new-customer-price-list on the shared packhouse v2 design system. Same API
# as the original page.

import frappe

no_cache = 1


def get_context(context):
	# creating a customer list now lives on Price Lists ("New customer list")
	frappe.local.flags.redirect_location = "/packhouse-dashboard-v2/price-lists-v2"
	raise frappe.Redirect
