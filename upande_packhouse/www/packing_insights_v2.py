# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packing Insights v2 (www/packing-insights-v2.html): the live packing picture for
# management. Data comes from api/v2/packing_insights.py.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "packing-insights", "Packing Insights")
