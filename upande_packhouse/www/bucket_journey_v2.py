# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Journey v2 (www/bucket-journey-v2.html, /packhouse-dashboard-v2/bucket-journey-v2): search or scan a bucket and
# read one timeline per journey, plus the Watchlist and Inactive views. It used to be a tab of Remote Transfers; it is its
# own page now. The screen is templates/remote_transfer_v2/bucket_journey.html, data from api/v2/rt_journey.py.

import os

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def _version():
	"""Cache-buster for the journey scripts and styles: changes whenever any rt-* file does, so a browser is not kept on a
	stale journey script."""
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
	page_context(context, "bucket-journey", "Bucket Journey", section="Tracking")
	context.rt_v = _version()
	context.ph_wide = 1
	# The journey's "Transfer" stage names where remote buckets are trucked to.
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	context.transfer_hub = transfer_hub(required=False) or ""
	return context
