# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Remote Transfers v2 (www/remote-transfer-v2.html?tab=<tab>). Rebuild of
# /remote-transfer/<tab> on the packhouse v2 design system. One page; the tab
# is picked by ?tab= and each tab is a self-contained template in
# templates/remote_transfer_v2/<tab>.html. Switching tabs is a normal page
# load (no iframes); the shared delivery date travels in ?date=.

import frappe

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1

#: tab key -> (label, hint, template). Order = order of the tab strip.
TABS = {
	"routes": ("Truck Routes", "Set today's farms for each transfer truck", "truck_routes"),
	"scheduler": ("Scheduler", "Sequence each team's orders", "scheduler"),
	"transfer": (
		"Transfer Scheduling",
		"Distribute across teams, plan trucks, dispatch trips",
		"transfer_scheduling",
	),
	"logistics": ("Bucket Logistics", "Track buckets to the sales farm", "bucket_logistics"),
	"journey": ("Bucket Journey", "Trace a bucket end to end", "bucket_journey"),
}

#: Old /remote-transfer/<slug> paths, for the eventual swap-over.
SLUGS = {
	"routes": "truck-routes",
	"scheduler": "scheduler",
	"transfer": "transfer-scheduling",
	"logistics": "bucket-logistics",
	"journey": "bucket-journey",
}


def get_context(context):
	tab = frappe.form_dict.get("tab") or "transfer"
	if tab not in TABS:
		tab = "transfer"
	page_context(context, "remote-transfer", "Remote Transfers", section="Workflow")
	context.rt_tab = tab
	context.rt_tabs = [(k, v[0], v[1]) for k, v in TABS.items()]
	context.rt_template = "upande_packhouse/templates/remote_transfer_v2/{0}.html".format(TABS[tab][2])
	context.ph_wide = 1
	# The Bucket Journey's "Transfer" stage names where remote buckets are trucked to.
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	context.transfer_hub = transfer_hub(required=False) or ""
	return context
