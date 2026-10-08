# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Stock Visibility v2 (www/stock-visibility-v2.html). Rebuild of
# /stock-visibility on the shared packhouse v2 design system. Same API as the
# original page (upande_packhouse.api.v2.stock_visibility.get_stock_visibility).

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "stock-visibility", "Stock Visibility")
