# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Allocation Planning v2 (www/sales-allocation-planning-v2.html). Rebuild of
# /sales-allocation-planning on the shared packhouse v2 design system. Same API
# as the original page for confirmations; reads come from api/v2/allocation_planning.

from upande_packhouse.api.v2.allocation_playground import _day, _default_farm, _farms
from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	page_context(context, "sales-allocation-planning", "Allocation Planning")
	context.ph_wide = 1
	# The day after tomorrow by the server's clock (EAT): tomorrow is already being allocated.
	context.default_delivery_date = _day(None)
	# the sales farms are on the page from the start: the board that refreshes them takes a few seconds
	context.sales_farms = [r.farm for r in _farms()]
	context.default_farm = _default_farm()
	return context
