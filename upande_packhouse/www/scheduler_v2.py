# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Scheduler v2 (www/scheduler-v2.html, /packhouse-dashboard-v2/scheduler-v2), in the sidebar: a copy of the Remote
# Transfers Scheduler (templates/remote_transfer/scheduler.html) showing every team's submitted orders, By team, with
# the team filter. The Scheduler tab of Remote Transfers stays as it is, with all its views.

from upande_packhouse.packhouse_v2 import page_context
from upande_packhouse.www.remote_transfer_v2 import fill

no_cache = 1


def get_context(context):
	page_context(context, "scheduler", "Scheduler", section="Workflow")
	context.rt_sched_teams = 1
	context.rt_title = "Scheduler"
	return fill(context, "scheduler")
