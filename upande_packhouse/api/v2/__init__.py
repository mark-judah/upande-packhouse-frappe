# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Packhouse v2 backend.

Every v2 page endpoint (api/v2/<page>.py) computes its numbers through the
shared services in api/v2/core, so two pages showing the same quantity can
never disagree: one definition per number, one query per source.

See docs/backend-v2-audit/ for the findings this layer fixes (IDs such as
WF-1, OR-S3 are referenced in comments).
"""
