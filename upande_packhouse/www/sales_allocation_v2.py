# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Sales Allocation in the packhouse v2 shell (www/sales-allocation-v2.html). The board is
# public/js/sales-allocation-v2.js; it calls the same whitelisted methods as the desk page.

import os

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public", "js", "sales-allocation-v2.js")


def get_context(context):
	page_context(context, "sales-allocation", "Sales Allocation")
	context.ph_wide = 1
	# the board's own cache-buster: it changes whenever the script does
	try:
		context.sa_v = str(int(os.path.getmtime(_SCRIPT)))
	except OSError:
		context.sa_v = context.ph_v
