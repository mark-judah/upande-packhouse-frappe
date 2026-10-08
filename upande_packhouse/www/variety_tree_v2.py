# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Variety Tree v2 (www/variety-tree-v2.html). Rebuild of /variety-tree on the
# shared packhouse v2 design system. Same API as the original page
# (upande_packhouse.api.variety_tree.*).
#
# CSRF: the page POSTs (colour writes and, critically, the image upload to
# /api/method/upload_file, which sends the X-Frappe-CSRF-Token header itself).
# page_context() calls get_csrf_token(), which mints and persists a token on
# sessions that have none, so the render injects a valid `frappe.csrf_token`.
# See www/variety_tree.py for the full write-up.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "variety-tree", "Variety Tree")
