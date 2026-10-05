# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Old address of Remote Transfers → Scheduler: redirects to /remote-transfer/scheduler
# (the section is one page now, www/remote-transfer.html).

from upande_packhouse.remote_transfer_routes import redirect_old_route

no_cache = 1


def get_context(context):
	redirect_old_route("scheduler")
