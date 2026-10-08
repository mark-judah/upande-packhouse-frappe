# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Stem Movement v2 (www/stem-movement-v2.html), served by api/v2/stem_movement.py.
# Reads api/v2/stem_movement.py (get_flow, search_box_labels, get_box_trace); v1
# api/stem_movement.py is untouched and still serves the v1 page.
# page_context() also mints the CSRF token the original controller did.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "stem-movement", "Stem Movement")
