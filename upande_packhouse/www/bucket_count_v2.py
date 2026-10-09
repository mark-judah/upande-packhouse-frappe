# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Count v2 (www/bucket-count-v2.html). The empty-bucket count by farm and
# location; data from api/v2/bucket_count.py.

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "bucket-count", "Bucket Count")
