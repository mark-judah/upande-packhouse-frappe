# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# The Remote Transfers section is ONE www page (www/remote-transfer.html) at
# /remote-transfer/<tab>; each tab's screen is its own template under
# templates/remote_transfer/ and its own API module under api/remote_transfer/.
# Old addresses — /remote-transfers/<tab>, /transfer-control, /packhouse-scheduler,
# /bucket-tracker, /bucket-logistics — redirect here with their query string, so
# bookmarks and deep links keep working.

from urllib.parse import parse_qsl, urlencode

import frappe

BASE = "/remote-transfer"
#: Addresses the section used before it was one page.
OLD_BASE = "/remote-transfers"
#: tab key (remote_transfers_tabs.html data-rt) -> path
PATHS = {
	"routes": BASE + "/truck-routes",
	"scheduler": BASE + "/scheduler",
	"transfer": BASE + "/transfer-scheduling",
	"logistics": BASE + "/bucket-logistics",
	"journey": BASE + "/bucket-journey",
}
#: tab key -> the templates the page renders for it (templates/remote_transfer/).
#: Truck Routes, Transfer Scheduling and Bucket Logistics share one planner script
#: that switches between their views in place, so each of them brings all three.
_PLANNER = ("transfer_scheduling", "truck_routes", "bucket_logistics")
TEMPLATES = {
	"routes": _PLANNER,
	"transfer": _PLANNER,
	"logistics": _PLANNER,
	"scheduler": ("scheduler",),
	"journey": ("bucket_journey",),
}


#: tab key -> the frame it opens in on the page (tabs sharing templates share a frame).
GROUPS = {k: ("planner" if v == _PLANNER else k) for k, v in TEMPLATES.items()}


def tab_for_path(path):
	"""The tab key a /remote-transfer/<tab> (or old /remote-transfers/<tab>) path opens,
	else None."""
	path = (path or "").rstrip("/")
	if path.startswith(OLD_BASE):
		path = BASE + path[len(OLD_BASE) :]
	return next((k for k, v in PATHS.items() if v == path), None)


def template_paths(tab):
	return ["upande_packhouse/templates/remote_transfer/{0}.html".format(t) for t in TEMPLATES[tab]]


def redirect_old_route(key, tab_from_query=False):
	"""Send a request for an old address to /remote-transfer/<tab>, keeping its query.
	transfer-control picked its view with ?tab=; routes/logistics became their own
	paths and the planning sub-tabs (build, triplist) stay as ?tab=. A request already
	under /remote-transfer is left alone."""
	path = (frappe.request.path if frappe.request else "") or ""
	if path == BASE or path.startswith(BASE + "/"):
		return
	query = parse_qsl(frappe.request.query_string.decode()) if frappe.request.query_string else []
	if path.startswith(OLD_BASE):
		key = tab_for_path(path) or key
	if tab_from_query:
		tab = next((v for k, v in query if k == "tab"), "")
		if tab in ("routes", "logistics"):
			key = tab
		if key != "transfer" or tab not in ("build", "triplist"):
			query = [(k, v) for k, v in query if k != "tab"]
	frappe.local.flags.redirect_location = PATHS[key] + ("?" + urlencode(query) if query else "")
	raise frappe.Redirect(302)  # not 301: browsers cache permanent redirects for good
