# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Biflorica Orders v2 (www/biflorica-orders-v2.html). Rebuild of /biflorica-orders on the
# shared packhouse v2 design system. The page body is shared with the other
# sales channel -- see templates/includes/packhouse_v2/channel_orders_v2.html.
# Same ecommerce_integration API as the original page.

import frappe

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	page_context(context, "biflorica-orders", "Biflorica Orders")
	context.co_ecommerce_ready = "ecommerce_integration" in frappe.get_installed_apps()
	return context
