# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Production v2 (www/packhouse-production-v2.html). Rebuild of /packhouse-production
# on the shared packhouse v2 design system. Data: api/v2/production.py
# (harvested via api/v2/core/harvest.py, plus received / graded stems; region,
# farm, rose, variety search and a historical posting-date range).

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "packhouse-production", "Production")
