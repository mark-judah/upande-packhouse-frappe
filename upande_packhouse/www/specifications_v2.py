# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Specifications v2 (www/specifications-v2.html). Rebuild of /specifications
# on the shared packhouse v2 design system. Same API as the original page
# (upande_packhouse.api.specifications.*). page_context mints the CSRF token,
# which saveSpecification / deleteSpecification (POSTs) depend on.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "specifications", "Specifications")
