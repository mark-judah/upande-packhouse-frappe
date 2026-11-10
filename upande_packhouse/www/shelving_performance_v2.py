# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Shelving Performance v2 (www/shelving-performance-v2.html): how much each cold room
# shelves of what it receives, how fast, who shelved it and how often a called bucket
# is missing. Data comes from api/v2/shelving_performance.py.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "shelving-performance", "Shelving Performance")
