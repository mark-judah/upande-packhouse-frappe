# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Downgrades v2 (www/packhouse-downgrades-v2.html). Rebuild of
# /packhouse-downgrades on the shared packhouse v2 design system. Data comes
# from upande_packhouse.api.v2.downgrades.get_downgrades (the v1
# api/downgrades.getDowngradeData is left untouched for the v1 page).

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "packhouse-downgrades", "Downgrades")
