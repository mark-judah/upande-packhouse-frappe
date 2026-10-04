# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Remote Transfers page controller (www/remote-transfer.html, /remote-transfer/<tab>).
# Picks the tab from the URL and the templates it renders — the page, or with ?embed=1
# one tab in its frame; old addresses redirect here (remote_transfer_routes.py). Also mints/persists a session CSRF token at render
# time so Frappe injects a valid `frappe.csrf_token` into the page — without it a
# session that has no token yet gets `frappe.csrf_token = "None"` and every POST from
# the page fails with "CSRFTokenError: Invalid Request" (see www/variety_tree.py).

import frappe
from frappe.sessions import get_csrf_token

from upande_packhouse.remote_transfer_routes import (
	GROUPS,
	PATHS,
	redirect_old_route,
	tab_for_path,
	template_paths,
)

no_cache = 1


def get_context(context):
	tab = tab_for_path(frappe.request.path) or "transfer"
	redirect_old_route(tab)
	context.rt_tab = tab
	# ?embed=1: just this tab, in its frame on the page (www/remote-transfer.html).
	context.rt_embed = frappe.utils.cint(
		frappe.form_dict.get("embed") or (frappe.request.args.get("embed") if frappe.request else 0)
	)
	context.rt_group = GROUPS[tab]
	context.rt_templates = template_paths(tab)
	context.rt_paths = PATHS
	context.rt_groups = GROUPS
	context.csrf_token = get_csrf_token()
	# The Bucket Journey's "Transfer" stage names where remote buckets are trucked to.
	# Unset (and ambiguous) just leaves it blank instead of failing the whole page.
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	context.transfer_hub = transfer_hub(required=False) or ""
	context.no_cache = 1
	return context
