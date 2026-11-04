# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Standing orders: a Sales Order that repeats through Frappe's Auto Repeat,
created IN ADVANCE of its delivery.

Stock Auto Repeat copies a document on its schedule date and stamps every
required Date field -- a Sales Order's delivery_date included -- with that
same date. A repeated order therefore reaches the packhouse already due, and
nobody can process it. Here the Auto Repeat schedule is the day the order
APPEARS, and the delivery date is a number of days after it:

    appears   Auto Repeat schedule (start date, frequency, weekdays, end date)
    delivers  appears + Auto Repeat > Roses Packhouse > days after

Auto Repeat's own start_date is the day its FIRST copy is made for a standing
order (stock Auto Repeat treats it as the source document's date and makes the
first copy one interval later -- see first_appearance).

The copy is also cleaned of what belongs to the source order's own
fulfilment (confirmed stems, pick list links, truck, import link, PO), so it comes
up as a fresh draft. The reminder is Auto Repeat's own Notify by Email
(recipients, subject, message / Email Template), whose template reads the
figures through standing_summary(doc). Used by overrides/auto_repeat.py, the
Auto Repeat desk form and api/v2/standing_orders.py.
"""

from urllib.parse import quote

import frappe
from frappe import _
from frappe.automation.doctype.auto_repeat.auto_repeat import get_next_date, month_map, week_map
from frappe.utils import add_days, cint, getdate, nowdate, split_emails

DAYS_FIELD = "custom_delivery_days_after"
DEFAULT_TEMPLATE = "Standing Order Reminder"
MAX_DAYS_AFTER = 60
SCHEDULE_CAP = 1000  # iterations: a daily repeat run a long way


def is_standing(ar):
	"""Auto Repeat is global, so everything here applies to Sales Orders only."""
	return (ar.get("reference_doctype") or "") == "Sales Order"


def days_after(ar):
	return max(0, cint(ar.get(DAYS_FIELD)))


def delivery_for(ar, appears):
	return add_days(getdate(appears), days_after(ar))


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def first_appearance(ar):
	"""The first day an order appears: the start date itself, moved forward to
	the first repeat weekday / day of month when the start date is not one."""
	start = getdate(ar.start_date)
	freq = ar.frequency
	if freq == "Weekly" and ar.get("repeat_on_days"):
		names = [d.day for d in ar.get("repeat_on_days")]
		order = list(week_map.keys())
		for i in range(7):
			d = add_days(start, i)
			if order[d.weekday()] in names:
				return getdate(d)
	if month_map.get(freq):
		day = 31 if ar.get("repeat_on_last_day") else cint(ar.get("repeat_on_day")) or None
		if day:
			cand = get_next_date(start, 0, day)
			if cand < start:
				cand = get_next_date(start, month_map[freq], day)
			return getdate(cand)
	return start


def appearance_dates(ar, window_from=None, window_to=None, limit=None):
	"""Dates an order appears, in order, inside [window_from, window_to] (both
	optional) and never past the end date. Frappe's own get_next_schedule_date
	(full schedule) steps from one to the next, so weekday lists, fortnights and
	month rules are exactly the ones Auto Repeat applies. Pure: no database."""
	if not ar.get("start_date") or not ar.get("frequency"):
		return []
	end = getdate(ar.end_date) if ar.get("end_date") else None
	lo = getdate(window_from) if window_from else None
	hi = getdate(window_to) if window_to else None
	out, cur = [], first_appearance(ar)
	for _i in range(SCHEDULE_CAP):
		if end and cur > end:
			break
		if hi and cur > hi:
			break
		if not lo or cur >= lo:
			out.append(cur)
			if limit and len(out) >= limit:
				break
		nxt = getdate(ar.get_next_schedule_date(schedule_date=cur, for_full_schedule=True))
		if nxt <= cur:
			break
		cur = nxt
	return out


def next_appearance(ar, on_or_after=None, skip_today_if_made=False):
	"""The first appearance on/after a day (default today), or None."""
	lo = getdate(on_or_after or nowdate())
	if skip_today_if_made and ar.get("name") and made_on(ar, lo):
		lo = add_days(lo, 1)
	found = appearance_dates(ar, window_from=lo, limit=1)
	return found[0] if found else None


def made_on(ar, day):
	"""Whether this Auto Repeat already made an order on `day` (the source order does not count)."""
	return bool(
		frappe.db.exists(
			"Sales Order",
			{
				"auto_repeat": ar.name,
				"transaction_date": getdate(day),
				"docstatus": ["<", 2],
				"name": ["!=", ar.get("reference_document")],
			},
		)
	)


def schedule(ar, count=10, window_from=None):
	"""The upcoming schedule: [{appears, delivers, days_after}] for the next `count` appearances."""
	return [
		{"appears": d, "delivers": delivery_for(ar, d), "days_after": days_after(ar)}
		for d in appearance_dates(ar, window_from=window_from or nowdate(), limit=count)
	]


# ---------------------------------------------------------------------------
# The repeated order
# ---------------------------------------------------------------------------


def iso_week(d):
	return str(getdate(d).isocalendar()[1])


def apply_standing_dates(ar, new_doc, reference_doc=None):
	"""Stamp the copy: appears today, delivers `days after` later, and nothing
	of the source order's own fulfilment carried over."""
	appears = getdate(ar.next_schedule_date)
	delivers = delivery_for(ar, appears)
	# Order Name is required but never copied (no_copy); a standing order keeps the source's label.
	if reference_doc is not None and new_doc.meta.get_field("custom_order_name"):
		new_doc.custom_order_name = reference_doc.get("custom_order_name")
	new_doc.transaction_date = appears
	new_doc.delivery_date = delivers
	for it in new_doc.get("items") or []:
		it.delivery_date = delivers
		for f in ("custom_opl", "custom_truck"):
			if it.meta.get_field(f):
				it.set(f, None)
		for f in ("custom_fully_allocated", "custom_confirmed_box_quantity"):
			if it.meta.get_field(f):
				it.set(f, 0)
	meta = new_doc.meta
	if meta.get_field("custom_confirmed_stems_table"):
		new_doc.set("custom_confirmed_stems_table", [])
	# Each repeat is a new delivery with its own customer PO; ERPNext refuses a second order on the same PO.
	for f in (
		"po_no",
		"po_date",
		"custom_truck_details",
		"custom_order_import",
		"custom_allocation_sequence",
	):
		if meta.get_field(f):
			new_doc.set(f, None)
	if meta.get_field("custom_expected_delivery_date"):
		new_doc.custom_expected_delivery_date = delivers
	if meta.get_field("custom_week"):
		new_doc.custom_week = iso_week(delivers)


def validate_standing(ar):
	"""Rules for the Roses Packhouse tab (Sales Order repeats only)."""
	n = cint(ar.get(DAYS_FIELD))
	if n < 0 or n > MAX_DAYS_AFTER:
		frappe.throw(
			_("Delivery must be between 0 and {0} days after the order appears.").format(MAX_DAYS_AFTER)
		)


def recipients(ar):
	raw = (ar.get("recipients") or "").replace("\n", ",").replace(";", ",")
	return [e.strip() for e in split_emails(raw) if e and e.strip()]


# ---------------------------------------------------------------------------
# Figures and the reminder email
# ---------------------------------------------------------------------------


def order_figures(names):
	"""{order: {boxes, stems, lines}} over each order's Roses lines (the page's own definitions)."""
	from collections import defaultdict

	from upande_packhouse.api.v2.core import boxes as bx
	from upande_packhouse.api.v2.core import pipeline

	names = [n for n in names if n]
	if not names:
		return {}
	by_so = defaultdict(list)
	for ln in pipeline.fetch_lines(sales_orders=names, docstatus=(0, 1)):
		by_so[ln.parent].append(ln)
	return {
		n: {
			"boxes": bx.order_boxes(by_so.get(n, [])),
			"stems": sum(ln.ordered_stems for ln in by_so.get(n, [])),
			"lines": len(by_so.get(n, [])),
		}
		for n in names
	}


def long_date(d):
	return getdate(d).strftime("%a %-d %b %Y")


def order_url(name):
	return frappe.utils.get_url("/sales-order-v2?view=edit&name={0}".format(quote(name)))


def summary(doc):
	"""What the standing-order email prints, from a Sales Order that belongs to a
	standing order (the copy just made, or the source order when previewing).
	Reached from the email template as standing_summary(doc) -- see jinja_methods.py."""
	name = doc.get("auto_repeat")
	if not name or not frappe.db.exists("Auto Repeat", name):
		return {}
	ar = frappe.get_doc("Auto Repeat", name)
	if doc.name == ar.get("reference_document"):  # previewing: the next order's dates
		appears = next_appearance(ar) or getdate(nowdate())
	else:
		appears = getdate(doc.get("transaction_date") or nowdate())
	delivers = delivery_for(ar, appears)
	fig = order_figures([doc.name]).get(doc.name, {"boxes": 0, "stems": 0, "lines": 0})
	following = next_appearance(ar, on_or_after=add_days(appears, 1))
	return {
		"appears": long_date(appears),
		"delivers": long_date(delivers),
		"days_after": days_after(ar),
		"boxes": fig["boxes"],
		"stems": fig["stems"],
		"lines": fig["lines"],
		"frequency": ar.frequency,
		"next_appears": long_date(following) if following else "",
		"next_delivers": long_date(delivery_for(ar, following)) if following else "",
		"auto_repeat": ar.name,
		"order_url": order_url(doc.name),
	}
