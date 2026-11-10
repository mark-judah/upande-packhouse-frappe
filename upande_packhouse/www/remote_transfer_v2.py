# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Remote Transfers in the packhouse v2 shell (www/remote-transfer-v2.html,
# /packhouse-dashboard-v2/remote-transfer-v2?tab=<tab>). The screens are the Remote
# Transfers pages (templates/remote_transfer/, the same ones /remote-transfer/<tab>
# shows), rendered straight into the v2 page: no frames. The tab is picked by ?tab=.

import frappe

from upande_packhouse.packhouse_v2 import page_context
from upande_packhouse.remote_transfer_routes import GROUPS, template_paths

no_cache = 1

BASE = "/packhouse-dashboard-v2/remote-transfer-v2"
#: tab key -> its address on the v2 page (the tabs the section shows).
PATHS = {k: BASE + "?tab=" + k for k in ("routes", "scheduler", "logistics")}
#: Bucket Journey is its own page now; its old tab address lands there.
JOURNEY = "/packhouse-dashboard-v2/bucket-journey-v2"


def get_context(context):
	tab = frappe.form_dict.get("tab") or "logistics"
	if tab == "journey":
		frappe.local.flags.redirect_location = JOURNEY
		raise frappe.Redirect(302)  # not 301: browsers cache permanent redirects for good
	# No Transfer Scheduling tab any more: its address opens Bucket Logistics.
	if tab not in PATHS:
		tab = "logistics"
	page_context(context, "remote-transfer", "Remote Transfers", section="Workflow")
	context.rt_tab = tab
	context.rt_group = GROUPS[tab]
	context.rt_templates = template_paths(tab)
	context.rt_v2_paths = PATHS
	context.ph_wide = 1
	# The Bucket Journey's "Transfer" stage names where remote buckets are trucked to.
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	context.transfer_hub = transfer_hub(required=False) or ""
	return context
