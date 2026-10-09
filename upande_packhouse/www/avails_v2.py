# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Avails v2 (www/avails-v2.html). Rebuild of /avails on the shared packhouse
# v2 design system. Same API as the original page
# (upande_packhouse.api.avails.getAvailsData). page_context mints the CSRF
# token, which is all the original controller added.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	context.ph_wide = 1  # the farm × length pivot needs the full width
	return page_context(context, "avails", "Avails")
