# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Orders & Workflow v2 (www/packhouse-dashboard-v2.html): the Packhouse
# Workflow and Order Summary pages merged into one. Data comes from
# api/v2/orders_workflow.py. /order-summary-v2 redirects here.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "packhouse-dashboard", "Orders & Workflow")
