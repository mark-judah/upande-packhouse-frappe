# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Order Fulfilment v2 (www/order-fulfilment-v2.html): supplied vs ordered and
# supplied vs confirmed stems, grouped customer -> order -> line, with an
# account-manager ranking. Data: api/v2/order_fulfilment.get_order_fulfilment.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "order-fulfilment", "Order Fulfilment")
