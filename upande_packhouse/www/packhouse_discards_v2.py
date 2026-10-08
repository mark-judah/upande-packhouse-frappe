# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Discards v2 (www/packhouse-discards-v2.html). Rebuild of /packhouse-discards
# on the shared packhouse v2 design system. Data: api/v2/discards.get_discards
# (region / farm / variety / rose / date range / search all scope every KPI).

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "packhouse-discards", "Discards")
