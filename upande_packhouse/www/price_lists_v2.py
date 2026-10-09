# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Price Lists v2 (www/price-lists-v2.html). Rebuild of /price-lists on the
# shared packhouse v2 design system. Grid + KPIs come from api/v2/price_lists.py
# (server-side, every filter applied); price lists and setItemPrice writes stay
# on the v1 api/price_list_management.py, unchanged.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "price-lists", "Price Lists")
