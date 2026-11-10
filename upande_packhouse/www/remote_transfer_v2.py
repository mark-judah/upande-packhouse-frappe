# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Remote Transfers in the packhouse v2 shell (www/remote-transfer-v2.html,
# /packhouse-dashboard-v2/remote-transfer-v2?tab=<tab>). The screens are the Remote
# Transfers pages (templates/remote_transfer/, the same ones /remote-transfer/<tab>
# shows), rendered straight into the v2 page: no frames. The tab is picked by ?tab=.

import os

import frappe

from upande_packhouse.packhouse_v2 import page_context
from upande_packhouse.remote_transfer_routes import GROUPS, template_paths

no_cache = 1

BASE = "/packhouse-dashboard-v2/remote-transfer-v2"
#: tab key -> its address on the v2 page (the tabs the section shows).
PATHS = {k: BASE + "?tab=" + k for k in ("routes", "scheduler", "logistics", "journey")}


def _tab_version():
	"""Cache-buster for the Bucket Journey script and styles: changes whenever any rt-* file does, so a browser is not kept
	on a stale journey script."""
	public = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public")
	newest = 0
	for kind, ext in (("js", ".js"), ("css", ".css")):
		try:
			for f in os.listdir(os.path.join(public, kind)):
				if f.startswith("rt-") and f.endswith(ext):
					newest = max(newest, os.path.getmtime(os.path.join(public, kind, f)))
		except OSError:
			pass
	return str(int(newest)) or "1"


def get_context(context):
	tab = frappe.form_dict.get("tab") or "logistics"
	# No Transfer Scheduling tab any more: its address opens Bucket Logistics.
	if tab not in PATHS:
		tab = "logistics"
	page_context(context, "remote-transfer", "Remote Transfers", section="Workflow")
	context.rt_tab = tab
	context.rt_group = GROUPS[tab]
	context.rt_templates = template_paths(tab)
	if tab == "journey":
		# The full Bucket Journey (search or scan, one timeline per journey, Watchlist and Inactive views) was replaced by the
		# older Remote Transfers screen on 5 Nov; it is back. It is built for this shell, so it gets its own script part.
		context.rt_templates = ["upande_packhouse/templates/remote_transfer_v2/bucket_journey.html"]
		context.rt_v = _tab_version()
	context.rt_v2_paths = PATHS
	context.ph_wide = 1
	# The Bucket Journey's "Transfer" stage names where remote buckets are trucked to.
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	context.transfer_hub = transfer_hub(required=False) or ""
	return context
