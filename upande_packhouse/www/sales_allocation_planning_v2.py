# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Allocation Planning v2 (www/sales-allocation-planning-v2.html). Rebuild of
# /sales-allocation-planning on the shared packhouse v2 design system. Same API
# as the original page for confirmations; reads come from api/v2/allocation_planning.

from upande_packhouse.packhouse_v2 import page_context
from upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation import default_delivery_date

no_cache = 1


def get_context(context):
	page_context(context, "sales-allocation-planning", "Allocation Planning")
	context.ph_wide = 1
	# Tomorrow by the server's clock (EAT): the page opens on it.
	context.default_delivery_date = default_delivery_date()
	return context
