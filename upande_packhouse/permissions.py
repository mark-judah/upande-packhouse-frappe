# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Who may call a whitelisted write directly.

The checks apply when a method is the request's own target (called over the API, from a page or the app), never
when another of the app's own flows calls it inside its work -- that flow already decided who may do it."""

import functools

import frappe
from frappe import _


def _called_directly(fn):
	path = f"{fn.__module__}.{fn.__name__}"
	cmd = frappe.form_dict.get("cmd") if getattr(frappe.local, "form_dict", None) else None
	if cmd:
		return cmd == path
	request = getattr(frappe.local, "request", None)
	return bool(request) and (request.path or "").rstrip("/").rsplit("/", 1)[-1] == path


def guard(roles=None, doctype=None, ptype="write"):
	"""Refuse a direct call by anyone without one of `roles`, or without `ptype` permission on `doctype`."""

	def wrap(fn):
		@functools.wraps(fn)
		def inner(*args, **kwargs):
			if frappe.session.user != "Administrator" and _called_directly(fn):
				if roles and not set(roles) & set(frappe.get_roles()):
					frappe.throw(_("You are not permitted to do this."), frappe.PermissionError)
				if doctype and not frappe.has_permission(doctype, ptype):
					frappe.throw(_("You are not permitted to do this."), frappe.PermissionError)
			return fn(*args, **kwargs)

		return inner

	return wrap


#: Sales Allocation's own roles (its page's).
ALLOCATORS = (
	"System Manager",
	"Sales Manager",
	"Sales Master Manager",
	"Sales Representative",
	"Sales User",
	"Assistant Packhouse Manager",
)

#: Everyone who works the farms, the cold rooms and the packhouse (the scanners included); not portal users.
STAFF = (
	"System Manager",
	"Stock User",
	"Stock Manager",
	"Packing Supervisor",
	"Coldstore Attendant",
	"Assistant Packhouse Manager",
	"Sales Manager",
	"Sales Master Manager",
	"Sales Representative",
	"Sales User",
)
