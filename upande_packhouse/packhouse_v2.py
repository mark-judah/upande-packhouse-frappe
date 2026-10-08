# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Shared page context for the packhouse v2 www pages (www/*-v2.html).

Every v2 page controller calls `page_context(context, ...)` so the shell
(templates/includes/packhouse_v2/shell_start.html) gets the same data on
every page: CSRF token, company name, the signed-in user and which optional
sidebar entries apply on this site.

CSRF: minting the token at render time makes Frappe inject a valid
`frappe.csrf_token`. Without it a session with no token yet gets
`frappe.csrf_token = "None"` and every POST from the page fails with
"CSRFTokenError: Invalid Request" (see www/variety_tree.py).
"""

import os
from urllib.parse import urlencode

import frappe
from frappe.sessions import get_csrf_token

_PUBLIC = os.path.join(os.path.dirname(__file__), "public")

#: Sidebar, in order: (group, [(page key, label, href, icon), ...]).
#: Page key == the www page name without "-v2"; it marks the active entry.
NAV = [
	(
		"Workflow",
		[
			("packhouse-dashboard", "Orders & Workflow", "/packhouse-dashboard-v2", "layout"),
			("sales-allocation-planning", "Allocation Planning", "/sales-allocation-planning-v2", "grid"),
			("packhouse-production", "Production", "/packhouse-production-v2", "home"),
			("remote-transfer", "Remote Transfers", "/remote-transfer-v2?tab=transfer", "truck"),
		],
	),
	(
		"Tracking",
		[
			("cold-room", "Cold Room", "/cold-room-v2", "snow"),
			("packhouse-discards", "Discards", "/packhouse-discards-v2", "trash"),
			("packhouse-downgrades", "Downgrades", "/packhouse-downgrades-v2", "down"),
			("stock-take", "Stock Take", "/stock-take-v2", "clipboard"),
		],
	),
	(
		"Stock & Orders",
		[
			("stock-visibility", "Stock Visibility", "/stock-visibility-v2", "cube"),
			("avails", "Avails", "/avails-v2", "layers"),
			("stem-movement", "Stem Movement", "/stem-movement-v2", "chart"),
			("order-fulfilment", "Order Fulfilment", "/order-fulfilment-v2", "check"),
			("biflorica-orders", "Biflorica Orders", "/biflorica-orders-v2", "bag"),
			("floriday-orders", "Floriday Orders", "/floriday-orders-v2", "flower"),
			("sales-order", "Sales Order", "/sales-order-v2", "clipboard"),
			("sales-settings", "Sales Settings", "/sales-settings-v2", "gear"),
		],
	),
	(
		"Reference",
		[
			("variety-tree", "Variety Tree", "/variety-tree-v2", "tree"),
			("price-lists", "Price Lists", "/price-lists-v2", "dollar"),
			(
				"new-customer-price-list",
				"New Customer Price List",
				"/new-customer-price-list-v2",
				"user-plus",
			),
			("specifications", "Specifications", "/specifications-v2", "doc"),
		],
	),
]

#: Entries that only apply when another app is installed.
REQUIRES_APP = {
	"biflorica-orders": "ecommerce_integration",
	"floriday-orders": "ecommerce_integration",
}


def page_context(context, active_page, title, section=None):
	"""Fill the context every v2 page's shell needs. Returns context.

	Signed-out visitors are sent to the login page and brought back here
	afterwards: every page reads whitelisted methods a Guest can't call.
	"""
	if frappe.session.user == "Guest":
		path = frappe.request.full_path if frappe.request else "/" + active_page + "-v2"
		frappe.local.flags.redirect_location = "/login?" + urlencode({"redirect-to": path.rstrip("?")})
		raise frappe.Redirect(302)
	context.no_cache = 1
	context.csrf_token = get_csrf_token()
	context.ph_active = active_page
	context.ph_title = title
	context.ph_section = section or _section_of(active_page)
	user = frappe.session.user if frappe.session else "Guest"
	full = frappe.utils.get_fullname(user) if user and user != "Guest" else ""
	context.ph_user = user
	context.ph_user_name = full or (user or "").split("@")[0]
	context.ph_company = frappe.db.get_single_value("Global Defaults", "default_company") or "Upande"
	installed = set(frappe.get_installed_apps())
	context.ph_nav = [
		(group, [i for i in items if REQUIRES_APP.get(i[0]) in (None, *installed)]) for group, items in NAV
	]
	context.ph_v = _asset_version()
	return context


def _asset_version():
	"""Cache-buster for the shared css/js: changes whenever either file changes."""
	try:
		return str(
			int(
				max(
					os.path.getmtime(os.path.join(_PUBLIC, "css", "packhouse-v2.css")),
					os.path.getmtime(os.path.join(_PUBLIC, "js", "packhouse-v2.js")),
				)
			)
		)
	except OSError:
		return "1"


def _section_of(active_page):
	for group, items in NAV:
		if any(i[0] == active_page for i in items):
			return group
	return "Packhouse"
