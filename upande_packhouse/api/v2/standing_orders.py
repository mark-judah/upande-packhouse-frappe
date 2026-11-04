# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Standing orders tab (www/sales-order-v2.html).

A standing order is an Auto Repeat whose reference document is a Sales Order,
set up so each copy APPEARS in advance of its delivery (standing_orders.py has
the rule). This module is the page's whole server side: list them with their
schedule against delivery, read one, preview a schedule before saving, save /
pause / delete, and preview or test-send the reminder email.

Reads follow the page's other lists: Roses orders only, the order's farm for the
region filter, boxes and stems from the shared core (standing_orders.order_figures).
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, nowdate

from upande_packhouse import standing_orders as so
from upande_packhouse.api.v2.core import region

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
FREQUENCIES = ["Daily", "Weekly", "Fortnightly", "Monthly", "Quarterly", "Half-yearly", "Yearly"]
DELIVERY_WINDOW_DAYS = 14
HISTORY_LIMIT = 8


def _arg(args, key):
	v = args.get(key)
	return str(v).strip() if v not in (None, "") else ""


def _status(ar, today):
	if cint(ar.disabled):
		return "Paused"
	if ar.end_date and getdate(ar.end_date) < today:
		return "Ended"
	return "Active"


def _repeats(customer="", farms=None):
	where = ["ar.reference_doctype = 'Sales Order'", "so.business_unit = 'Roses'", "so.docstatus < 2"]
	params = {}
	if customer:
		where.append("so.customer = %(customer)s")
		params["customer"] = customer
	if farms is not None:
		where.append("{0} IN %(farms)s".format(region.order_farm_sql("so", "opl")))
		params["farms"] = region.sql_tuple(farms)
	return frappe.db.sql(
		"""SELECT ar.name, ar.reference_document AS source, ar.frequency, ar.start_date, ar.end_date,
		          ar.next_schedule_date, ar.disabled,
		          ar.custom_delivery_days_after AS days_after, ar.notify_by_email AS send_reminder,
		          ar.recipients AS recipients,
		          so.customer, so.customer_name, so.po_no, so.docstatus AS source_docstatus,
		          {1} AS farm
		   FROM `tabAuto Repeat` ar
		   INNER JOIN `tabSales Order` so ON so.name = ar.reference_document
		   LEFT JOIN (SELECT sales_order, MIN(NULLIF(farm, '')) AS farm FROM `tabOrder Pick List`
		              WHERE docstatus < 2 GROUP BY sales_order) opl ON opl.sales_order = so.name
		   WHERE {0}
		   ORDER BY so.customer_name, ar.name""".format(
			" AND ".join(where), region.order_farm_sql("so", "opl")
		),  # nosemgrep: fixed SQL, values bound
		params,
		as_dict=True,
	)


def _weekdays_of(ar_name):
	return [
		r.day
		for r in frappe.get_all(
			"Auto Repeat Day",
			filters={"parent": ar_name, "parenttype": "Auto Repeat"},
			fields=["day"],
			order_by="idx",
		)
	]


def _describe(ar, weekdays):
	"""'Every week on Mon, Thu' -- the repeat rule in words."""
	f = ar.frequency
	if f == "Daily":
		return "Every day"
	if f == "Weekly":
		return "Every week on " + ", ".join(d[:3] for d in weekdays) if weekdays else "Every week"
	if f == "Fortnightly":
		return "Every 2 weeks"
	every = {"Monthly": "month", "Quarterly": "3 months", "Half-yearly": "6 months", "Yearly": "year"}[f]
	if cint(ar.get("repeat_on_last_day")):
		return "Every {0}, on the last day".format(every)
	if cint(ar.get("repeat_on_day")):
		return "Every {0}, on day {1}".format(every, cint(ar.get("repeat_on_day")))
	return "Every " + every


@frappe.whitelist()
def get_standing_orders(**kwargs):
	"""All standing orders with the next order's appear and delivery dates, plus
	the next two weeks of deliveries. Args: customer, region / farm, state (all | active | paused)."""
	try:
		frappe.has_permission("Auto Repeat", "read", throw=True)
		args = frappe._dict(kwargs)
		today = getdate(nowdate())
		state = (_arg(args, "state") or "all").lower()
		farms = region.farms_for(region=_arg(args, "region"), farm=_arg(args, "farm"))
		rows = _repeats(_arg(args, "customer"), farms)
		for r in rows:
			r["status"] = _status(r, today)
		if state == "active":
			rows = [r for r in rows if r["status"] == "Active"]
		elif state == "paused":
			rows = [r for r in rows if r["status"] == "Paused"]
		names = [r.name for r in rows]

		# Orders this standing order has already made (the source itself is not one of them).
		made = defaultdict(list)
		if names:
			for o in frappe.db.sql(
				"""SELECT name, auto_repeat, transaction_date, delivery_date, docstatus
				   FROM `tabSales Order`
				   WHERE auto_repeat IN %(ars)s AND docstatus IN (0, 1) AND business_unit = 'Roses'
				   ORDER BY transaction_date DESC, name DESC""",
				{"ars": tuple(names)},
				as_dict=True,
			):
				made[o.auto_repeat].append(o)
		src_of = {r.name: r.source for r in rows}
		fig = so.order_figures(
			{r.source for r in rows}
			| {o.name for v in made.values() for o in v if o.name != src_of.get(o.auto_repeat)}
		)

		horizon = add_days(today, DELIVERY_WINDOW_DAYS - 1)
		by_day = defaultdict(lambda: {"orders": 0, "boxes": 0, "stems": 0, "customers": set(), "planned": 0})
		appear_week = 0
		out = []
		for r in rows:
			doc = frappe.get_doc("Auto Repeat", r.name)
			weekdays = _weekdays_of(r.name)
			src = fig.get(r.source, {"boxes": 0, "stems": 0, "lines": 0})
			# Auto Repeat's stored next date is the day the next order appears
			nxt = getdate(r.next_schedule_date) if r["status"] == "Active" and r.next_schedule_date else None
			copies = [o for o in made[r.name] if o.name != r.source]
			made_days = {getdate(o.transaction_date) for o in copies}
			if r["status"] == "Active":
				for d in so.appearance_dates(doc, window_from=today, window_to=add_days(today, 90)):
					if d in made_days:
						continue  # already created: counted below as a real order
					deliv = so.delivery_for(doc, d)
					if d <= add_days(today, 6):
						appear_week += 1
					if today <= deliv <= horizon:
						b = by_day[deliv]
						b["orders"] += 1
						b["planned"] += 1
						b["boxes"] += src["boxes"]
						b["stems"] += src["stems"]
						b["customers"].add(r.customer_name or r.customer)
					if d > horizon:
						break
			for o in copies:
				if today <= getdate(o.delivery_date) <= horizon:
					f = fig.get(o.name, {"boxes": 0, "stems": 0})
					b = by_day[getdate(o.delivery_date)]
					b["orders"] += 1
					b["boxes"] += f["boxes"]
					b["stems"] += f["stems"]
					b["customers"].add(r.customer_name or r.customer)
			out.append(
				{
					"name": r.name,
					"source": r.source,
					"customer": r.customer,
					"customer_name": r.customer_name,
					"po_no": r.po_no,
					"farm": r.farm or "",
					"status": r["status"],
					"frequency": r.frequency,
					"rule": _describe(doc, weekdays),
					"weekdays": weekdays,
					"start_date": r.start_date,
					"end_date": r.end_date,
					"days_after": cint(r.days_after),
					"next_appears": nxt,
					"next_delivers": so.delivery_for(doc, nxt) if nxt else None,
					"send_reminder": cint(r.send_reminder),
					"recipients": r.recipients or "",
					"made": len(copies),
					"last_made": copies[0].transaction_date if copies else None,
					"boxes": src["boxes"],
					"stems": src["stems"],
				}
			)
		deliveries = [
			{
				"date": d,
				"orders": v["orders"],
				"planned": v["planned"],
				"boxes": v["boxes"],
				"stems": v["stems"],
				"customers": sorted(c for c in v["customers"] if c),
			}
			for d, v in sorted(by_day.items())
		]
		kpis = {
			"active": sum(1 for o in out if o["status"] == "Active"),
			"paused": sum(1 for o in out if o["status"] == "Paused"),
			"total": len(out),
			"appear_week": appear_week,
			"deliver_week_boxes": sum(
				d["boxes"] for d in deliveries if getdate(d["date"]) <= add_days(today, 6)
			),
		}
		return {
			"success": True,
			"today": today,
			"standing": out,
			"deliveries": deliveries,
			"kpis": kpis,
			"user_email": frappe.db.get_value("User", frappe.session.user, "email") or "",
			"window_days": DELIVERY_WINDOW_DAYS,
		}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="v2 get_standing_orders error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}


@frappe.whitelist()
def get_standing_order(name):
	"""One standing order: every setting, its upcoming schedule, and the orders it has made."""
	try:
		frappe.has_permission("Auto Repeat", "read", throw=True)
		doc = frappe.get_doc("Auto Repeat", name)
		if not so.is_standing(doc):
			frappe.throw(_("{0} is not a Sales Order repeat.").format(name))
		src = frappe.get_doc("Sales Order", doc.reference_document)
		weekdays = _weekdays_of(name)
		made = frappe.get_all(
			"Sales Order",
			filters={
				"auto_repeat": name,
				"docstatus": ["in", [0, 1]],
				"name": ["!=", doc.reference_document],
			},
			fields=["name", "transaction_date", "delivery_date", "docstatus", "status"],
			order_by="transaction_date desc, name desc",
			limit=HISTORY_LIMIT,
		)
		fig = so.order_figures([src.name] + [m.name for m in made])
		sched = so.schedule(doc, count=12) if not cint(doc.disabled) else []
		return {
			"success": True,
			"name": name,
			"source": src.name,
			"customer": src.customer,
			"customer_name": src.customer_name,
			"po_no": src.po_no,
			"source_delivery": src.delivery_date,
			"frequency": doc.frequency,
			"rule": _describe(doc, weekdays),
			"weekdays": weekdays,
			"repeat_on_day": cint(doc.repeat_on_day),
			"repeat_on_last_day": cint(doc.repeat_on_last_day),
			"start_date": doc.start_date,
			"end_date": doc.end_date,
			"disabled": cint(doc.disabled),
			"status": _status(doc, getdate(nowdate())),
			"days_after": so.days_after(doc),
			"send_reminder": cint(doc.notify_by_email),
			"recipients": doc.recipients or "",
			"template": doc.template or "",
			"boxes": fig.get(src.name, {}).get("boxes", 0),
			"stems": fig.get(src.name, {}).get("stems", 0),
			"schedule": [
				dict(
					s,
					boxes=fig.get(src.name, {}).get("boxes", 0),
					stems=fig.get(src.name, {}).get("stems", 0),
				)
				for s in sched
			],
			"made": [
				{
					"name": m.name,
					"appeared": m.transaction_date,
					"delivers": m.delivery_date,
					"docstatus": m.docstatus,
					"status": m.status,
					"boxes": fig.get(m.name, {}).get("boxes", 0),
					"stems": fig.get(m.name, {}).get("stems", 0),
				}
				for m in made
			],
		}
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="v2 get_standing_order error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}


def _draft_repeat(f):
	"""An unsaved Auto Repeat built from the form's values (schedule maths needs no database)."""
	freq = _arg(f, "frequency") or "Weekly"
	if freq not in FREQUENCIES:
		frappe.throw(_("Choose how often the order repeats."))
	days = f.get("weekdays") or []
	if isinstance(days, str):
		days = frappe.parse_json(days) if days.startswith("[") else [d for d in days.split(",") if d]
	days = [d for d in days if d in WEEKDAYS] if freq == "Weekly" else []
	return frappe.get_doc(
		{
			"doctype": "Auto Repeat",
			"reference_doctype": "Sales Order",
			"reference_document": _arg(f, "source") or "-",
			"frequency": freq,
			"start_date": _arg(f, "start_date") or nowdate(),
			"end_date": _arg(f, "end_date") or None,
			"repeat_on_day": cint(f.get("repeat_on_day")) or None,
			"repeat_on_last_day": cint(f.get("repeat_on_last_day")),
			"repeat_on_days": [{"day": d} for d in days],
			so.DAYS_FIELD: cint(f.get("days_after")),
		}
	)


@frappe.whitelist()
def preview_schedule(**kwargs):
	"""The schedule a form would produce, before anything is saved: appears and delivers, side by side."""
	try:
		frappe.has_permission("Auto Repeat", "read", throw=True)
		doc = _draft_repeat(frappe._dict(kwargs))
		return {
			"success": True,
			"schedule": so.schedule(doc, count=cint(kwargs.get("count")) or 8, window_from=doc.start_date),
		}
	except Exception as e:
		frappe.clear_messages()
		return {"success": False, "error": str(e)}


@frappe.whitelist()
def find_orders(q="", current=""):
	"""Roses Sales Orders that can become a standing order (not already repeating)."""
	frappe.has_permission("Sales Order", "read", throw=True)
	q = (q or "").strip()
	like = "%{0}%".format(q)
	rows = frappe.db.sql(
		"""SELECT name, customer_name, delivery_date, po_no, docstatus
		   FROM `tabSales Order`
		   WHERE business_unit = 'Roses' AND docstatus < 2
		     AND (IFNULL(auto_repeat, '') = '' OR name = %(cur)s)
		     AND (%(q)s = '' OR name LIKE %(like)s OR customer_name LIKE %(like)s OR po_no LIKE %(like)s)
		   ORDER BY modified DESC LIMIT 15""",
		{"q": q, "like": like, "cur": current or ""},
		as_dict=True,
	)
	fig = so.order_figures([r.name for r in rows])
	return [
		{
			"value": r.name,
			"label": "{0} · {1}".format(r.name, r.customer_name or ""),
			"description": "Delivery {0} · {1} boxes{2}{3}".format(
				so.long_date(r.delivery_date),
				fig.get(r.name, {}).get("boxes", 0),
				" · PO " + r.po_no if r.po_no else "",
				" · Draft" if r.docstatus == 0 else "",
			),
			"customer_name": r.customer_name,
		}
		for r in rows
	]


def _fill_from_template(doc, name):
	"""Same as picking the template on the Notification tab: copy its subject and message in."""
	if not frappe.db.exists("Email Template", name):
		return
	tpl = frappe.get_doc("Email Template", name)
	doc.template = name
	doc.subject = tpl.subject
	doc.message = tpl.response_html if tpl.use_html else tpl.response


@frappe.whitelist()
def save_standing_order(**kwargs):
	"""Create or change a standing order. `name` present = change that one."""
	try:
		f = frappe._dict(kwargs)
		name = _arg(f, "name")
		frappe.has_permission("Auto Repeat", "write" if name else "create", throw=True)
		draft = _draft_repeat(f)
		if name:
			doc = frappe.get_doc("Auto Repeat", name)
		else:
			source = _arg(f, "source")
			if not source:
				frappe.throw(_("Choose the order to repeat."))
			status = frappe.db.get_value(
				"Sales Order", source, ["docstatus", "business_unit", "auto_repeat"], as_dict=True
			)
			if not status:
				frappe.throw(_("{0} does not exist.").format(source))
			if status.docstatus == 2:
				frappe.throw(_("{0} is cancelled and cannot be repeated.").format(source))
			if status.auto_repeat:
				frappe.throw(_("{0} is already a standing order ({1}).").format(source, status.auto_repeat))
			doc = frappe.new_doc("Auto Repeat")
			doc.update(
				{
					"reference_doctype": "Sales Order",
					"reference_document": source,
					"notify_by_email": 0,
					"submit_on_creation": 0,
				}
			)
		doc.frequency = draft.frequency
		doc.start_date = draft.start_date
		doc.end_date = draft.end_date
		doc.repeat_on_day = draft.repeat_on_day
		doc.repeat_on_last_day = draft.repeat_on_last_day
		doc.set("repeat_on_days", [{"day": d.day} for d in draft.repeat_on_days])
		doc.set(so.DAYS_FIELD, so.days_after(draft))
		# The reminder is Auto Repeat's own Notify by Email (Notification tab).
		doc.notify_by_email = cint(f.get("send_reminder"))
		doc.recipients = _arg(f, "recipients") if doc.notify_by_email else doc.recipients
		if doc.notify_by_email and not (doc.subject and doc.message):
			_fill_from_template(doc, doc.template or so.DEFAULT_TEMPLATE)
		if f.get("disabled") is not None:
			doc.disabled = cint(f.get("disabled"))
		doc.save()
		return {"success": True, "name": doc.name, "next_appears": doc.next_schedule_date}
	except Exception as e:
		frappe.clear_messages()
		return {"success": False, "error": str(e)}


@frappe.whitelist()
def set_standing_order_state(name, paused):
	"""Pause or resume. A paused standing order makes no orders and sends nothing."""
	try:
		frappe.has_permission("Auto Repeat", "write", throw=True)
		doc = frappe.get_doc("Auto Repeat", name)
		doc.disabled = cint(paused)
		doc.save()
		return {"success": True, "status": _status(doc, getdate(nowdate()))}
	except Exception as e:
		frappe.clear_messages()
		return {"success": False, "error": str(e)}


@frappe.whitelist()
def delete_standing_order(name):
	"""Stop repeating for good. Orders already made are kept."""
	try:
		frappe.has_permission("Auto Repeat", "delete", throw=True)
		frappe.delete_doc("Auto Repeat", name)
		return {"success": True}
	except Exception as e:
		frappe.clear_messages()
		return {"success": False, "error": str(e)}


@frappe.whitelist()
def preview_reminder(name):
	"""The email as it will arrive: the Notification tab's subject and message, with the order
	being repeated standing in for the next copy."""
	try:
		frappe.has_permission("Auto Repeat", "read", throw=True)
		doc = frappe.get_doc("Auto Repeat", name)
		if not so.is_standing(doc):
			frappe.throw(_("{0} is not a Sales Order repeat.").format(name))
		if not doc.message:
			frappe.throw(
				_("This standing order has no email message yet. Pick a template on its Notification tab.")
			)
		order = frappe.get_doc("Sales Order", doc.reference_document)
		ctx = {"doc": order}
		return {
			"success": True,
			"subject": frappe.render_template(doc.subject or "", ctx),
			"html": frappe.render_template(doc.message, ctx),
			"to": so.recipients(doc),
		}
	except Exception as e:
		frappe.clear_messages()
		return {"success": False, "error": str(e)}
