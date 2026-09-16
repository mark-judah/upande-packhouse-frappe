# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Single entry point for work that must happen after this app's resources land.

`install_app` and `bench migrate` both sync doctypes, customizations and
fixtures before calling these hooks, which makes this the first moment the app
can rely on its own fields existing. Anything here must be idempotent -- both
hooks run again on every deploy -- and must never raise: a failure here would
abort an otherwise good install/migrate.
"""

import frappe

from upande_packhouse.dashboard_links import ensure_dashboard_links


def after_install():
	_run()


def after_migrate():
	_run()


def _run():
	for step in (ensure_dashboard_links,):
		try:
			step()
		except Exception:
			frappe.log_error(
				title=f"upande_packhouse post-deploy step failed: {step.__name__}",
				message=frappe.get_traceback(),
			)
