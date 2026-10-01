# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# The Remote Transfers section lives under /remote-transfers/<tab>. Each tab is
# still served by its own www page (hooks.website_route_rules, same map as PATHS); the old addresses
# redirect here with their query string, so bookmarks and deep links keep working.

from urllib.parse import parse_qsl, urlencode

import frappe

BASE = "/remote-transfers"
#: tab key (remote_transfers_tabs.html data-rt) -> path
PATHS = {
	"routes": BASE + "/truck-routes",
	"scheduler": BASE + "/scheduler",
	"transfer": BASE + "/transfer-scheduling",
	"logistics": BASE + "/bucket-logistics",
	"journey": BASE + "/bucket-journey",
}


def tab_for_path(path):
	"""The tab key a /remote-transfers/<tab> path opens, else None."""
	path = (path or "").rstrip("/")
	return next((k for k, v in PATHS.items() if v == path), None)


def redirect_old_route(key, tab_from_query=False):
	"""Send a request for the page's old address (/transfer-control, /packhouse-scheduler,
	/bucket-tracker, /bucket-logistics) to its /remote-transfers/<tab> path.
	transfer-control picked its view with ?tab=; routes/logistics become their own
	path and the planning sub-tabs (build, triplist) stay as ?tab=."""
	path = (frappe.request.path if frappe.request else "") or ""
	if path.startswith(BASE):
		return
	query = parse_qsl(frappe.request.query_string.decode()) if frappe.request.query_string else []
	if tab_from_query:
		tab = next((v for k, v in query if k == "tab"), "")
		if tab in ("routes", "logistics"):
			key = tab
		if key != "transfer" or tab not in ("build", "triplist"):
			query = [(k, v) for k, v in query if k != "tab"]
	frappe.local.flags.redirect_location = PATHS[key] + ("?" + urlencode(query) if query else "")
	raise frappe.Redirect(302)  # not 301: browsers cache permanent redirects for good
