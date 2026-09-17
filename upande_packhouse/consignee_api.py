"""Lookup helpers for the Sales Order consignee and delivery point pickers.

Consignee filtering is keyed off `Consignee.customers` (Table MultiSelect over
child doctype Consignee Customer) -- v16 already had this exact field built
(predates this session), it just had no real data and nothing queried it for
the picker. v15's real data (a Customer -> curated-consignee-list mapping,
confirmed against the live v15 system) has been inverted into this
Consignee -> customer-list shape on import, so no new schema was needed --
an earlier pass this session tried adding a parallel `Customer.custom_consignees`
field instead and that was undone; this native field is the only source now.

Delivery Point carries a single `customer` Link instead (matches v15's own schema,
confirmed live -- see delivery_point.json's field description): most Delivery Points
are generic freight forwarders shared by every customer and leave it blank; only a
handful are exclusive to one customer. So unlike Consignee, this is a "null-or-match"
filter, not a "must have a mapping" one -- a Delivery Point with no customer set is
never hidden from anyone.
"""

import json

import frappe
from frappe.utils import cint


@frappe.whitelist()
def consignees_for_customer(customer: str | None = None):
	"""Consignees whose own `customers` curated list (Table MultiSelect) includes
	this Customer.

	Falls back to every consignee when the customer has no curated list yet
	(or no customer is selected at all), so the picker is never empty and the
	user is not blocked.
	"""
	names = []
	if customer:
		names = frappe.get_all(
			"Consignee Customer",
			filters={"customer": customer, "parenttype": "Consignee"},
			pluck="parent",
		)
		names = list(dict.fromkeys(n for n in names if n))  # dedupe, keep order, drop blanks
	if not names:
		names = frappe.get_all("Consignee", pluck="name")
	return names


def _filter_value(filters, key):
	"""Read one key out of `filters`, whichever shape it arrived in.

	This endpoint is reached three ways and each hands `filters` over
	differently:
	  * frappe's own link-search (`set_query`) calls it server-side with a real
	    dict;
	  * `frappe.call` from JS form-encodes the POST body, so an object argument
	    arrives as a JSON **string** -- this is what the Delivery Point picker
	    sends, and treating it as a dict raised
	    "'str' object has no attribute 'get'";
	  * some frappe paths pass the list-of-conditions form,
	    [[doctype, fieldname, operator, value], ...].
	Anything unrecognised yields None rather than raising -- a filter we cannot
	read should widen the result set, never break the picker.
	"""
	if isinstance(filters, str):
		try:
			filters = json.loads(filters)
		except (ValueError, TypeError):
			return None

	if isinstance(filters, dict):
		return filters.get(key)

	if isinstance(filters, list | tuple):
		for cond in filters:
			# [fieldname, operator, value] or [doctype, fieldname, operator, value]
			if isinstance(cond, list | tuple) and len(cond) >= 3 and cond[-3] == key:
				return cond[-1]

	return None


@frappe.whitelist()
def delivery_points_for_customer(
	doctype: str | None = None,
	txt: str | None = None,
	searchfield: str | None = None,
	# Link-query contract, NOT a plain HTTP signature: frappe's search_widget
	# calls this with `filters` as the dict set_query passed
	# ({"customer": frm.doc.customer}) and start/page_len as ints. Annotating
	# `filters: str | None` made pydantic reject every lookup with
	# "should be of type 'str | None' but got 'dict'". The body does
	# `(filters or {}).get("customer")`, so dict is the shape it wants; list and
	# str are allowed because frappe hands those over in other call paths.
	start: int | str | None = 0,
	page_len: int | str | None = 20,
	filters: dict | list | str | None = None,
):
	"""frappe.set_query "query" callback for Sales Order's custom_delivery_point.

	Returns Delivery Points that are either customer-agnostic (customer not set --
	the common case, e.g. a shared freight forwarder) or explicitly reserved for
	THIS order's customer. A Delivery Point exclusive to a different customer is
	excluded outright, matching v15's own (rare, opt-in) exclusivity behaviour.
	Always additionally scoped to business_unit = "Roses" (unconditional, same as
	before this customer filter existed).

	Signature matches what frappe.set_query's "query" (dotted method path) expects:
	called with the standard link-search args, plus whatever `filters` the client
	passed via set_query's own filters dict (here: {"customer": frm.doc.customer}).
	"""
	customer = _filter_value(filters, "customer")
	txt = txt or ""

	conditions = ["business_unit = %(business_unit)s"]
	values = {"business_unit": "Roses", "txt": f"%{txt}%"}
	if customer:
		conditions.append("(customer IS NULL OR customer = '' OR customer = %(customer)s)")
		values["customer"] = customer
	else:
		conditions.append("(customer IS NULL OR customer = '')")
	conditions.append("name LIKE %(txt)s")

	# nosemgrep: frappe-sql-format-injection -- the f-string carries no request-derived value
	return frappe.db.sql(
		f"""
		SELECT name, description
		FROM `tabDelivery Point`
		WHERE {" AND ".join(conditions)}
		ORDER BY name
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		# cint: LIMIT/OFFSET cannot take a quoted value, and these arrive as
		# strings whenever the search comes in over HTTP -- "LIMIT '20'" is a
		# MariaDB syntax error.
		{**values, "page_len": cint(page_len) or 20, "start": cint(start)},
	)
