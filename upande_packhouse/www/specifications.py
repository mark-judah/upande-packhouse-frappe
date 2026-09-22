# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Page controller for the `specifications` web page. See variety_tree.py for
# why the CSRF token needs to be forced onto the session here: same story --
# saveSpecification/deleteSpecification are POSTs, so a session started
# without a token (e.g. via API login) needs one injected before render.

import frappe
from frappe.sessions import get_csrf_token

no_cache = 1


def get_context(context):
	context.csrf_token = get_csrf_token()
	context.no_cache = 1
	return context
