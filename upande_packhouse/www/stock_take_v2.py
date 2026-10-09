# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Stock Take v2 (www/stock-take-v2.html). Cold Store Stock Take scans vs what the
# system expected in the cold store; data from api/v2/stock_take.py.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "stock-take", "Stock Take")
