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

import frappe


@frappe.whitelist()
def consignees_for_customer(customer=None):
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


@frappe.whitelist()
def delivery_points_for_customer(doctype=None, txt=None, searchfield=None, start=0, page_len=20, filters=None):
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
	customer = (filters or {}).get("customer")
	txt = txt or ""

	conditions = ["business_unit = %(business_unit)s"]
	values = {"business_unit": "Roses", "txt": f"%{txt}%"}
	if customer:
		conditions.append("(customer IS NULL OR customer = '' OR customer = %(customer)s)")
		values["customer"] = customer
	else:
		conditions.append("(customer IS NULL OR customer = '')")
	conditions.append("name LIKE %(txt)s")

	return frappe.db.sql(
		f"""
		SELECT name, description
		FROM `tabDelivery Point`
		WHERE {' AND '.join(conditions)}
		ORDER BY name
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		{**values, "page_len": page_len or 20, "start": start or 0},
	)
