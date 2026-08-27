# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Page controller for the `variety-tree` web page.
#
# The page talks to the server over POST — the variety colour reads/writes and,
# critically, the image upload (`/api/method/upload_file`) — all of which Frappe
# guards with a CSRF token. Frappe injects `frappe.csrf_token` into the page from
# `frappe.local.session.data.csrf_token`, but a session that was started without
# one (e.g. via API login) carries no token, so the framework injects an empty
# string and every POST comes back "CSRFTokenError: Invalid Request".
#
# get_csrf_token() generates and persists a token on the session when it is
# missing, so the render that follows injects a valid `frappe.csrf_token`.

import frappe
from frappe.sessions import get_csrf_token

no_cache = 1


def get_context(context):
    context.csrf_token = get_csrf_token()
    context.no_cache = 1
    return context
