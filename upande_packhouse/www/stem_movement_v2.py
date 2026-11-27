# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Stem Movement v2 (www/stem-movement-v2.html): the shelf ledger, served by
# api/v2/shelf_ledger.py (get_ledger). v1 api/stem_movement.py is untouched and
# still serves the v1 page.
# page_context() also mints the CSRF token the original controller did.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "stem-movement", "Stem Movement")
