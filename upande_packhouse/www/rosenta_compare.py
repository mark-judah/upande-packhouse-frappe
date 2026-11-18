# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Rosenta comparison (www/rosenta-compare.html). TEMPORARY: remove with api/v2/rosenta_compare.py when
# the parallel run on Rosenta stops. A stand-alone page, deliberately not in the Packhouse sidebar.
# System Manager only. Data: api/v2/rosenta_compare.get_rosenta_comparison.

from urllib.parse import urlencode

import frappe
from frappe.sessions import get_csrf_token

from upande_packhouse.packhouse_v2 import _asset_version

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?" + urlencode({"redirect-to": "/rosenta-compare"})
		raise frappe.Redirect(302)
	frappe.only_for("System Manager")
	context.no_cache = 1
	context.csrf_token = get_csrf_token()  # without it every POST from the page fails (see packhouse_v2.py)
	context.ph_v = _asset_version()
	return context
