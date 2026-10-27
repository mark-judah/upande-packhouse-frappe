# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Label Print Manager v2 (www/label-print-manager-v2.html). QR labels for bunches,
# buckets, shelves, sea-freight shelves, trolleys and staging locations; data and
# actions from api/v2/label_manager.py.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "label-print-manager", "Label Print Manager")
