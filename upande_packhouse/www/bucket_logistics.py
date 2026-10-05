# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Logistics was merged into the Remote Transfers section
# (/remote-transfer/bucket-logistics, served by www/remote-transfer.html). Keep the old route working —
# sidebar/workspace links and deep links such as
# /bucket-logistics?date=…&q=…&variety=… from Order Fulfilment — by redirecting
# with the query string carried over.

from upande_packhouse.remote_transfer_routes import redirect_old_route

no_cache = 1


def get_context(context):
	redirect_old_route("logistics")
