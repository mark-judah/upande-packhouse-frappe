# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Automatic Allocation (Production Settings > Allocation Planning > Automatic Allocation).

At each Run Time, for each enabled Sales Farm, the orders delivering the next day are allocated the way the
Allocate button allocates them -- a fresh allocation: the proposals waiting release (hand picks included) are
planned again from what is on the shelves, in Allocation Planning's sequence with each line's planned team,
in complete boxes only (a mix as far as its scarcest variety goes). Released pick lists are never touched.
A Run Time with Release ticked then releases the complete boxes (one pick list per team), puts the new pick
lists on the teams' Packhouse Schedules and plans the remote transfer trips. Every run is an Auto Allocation
Run record.

`tick` runs every 5 minutes: a Run Time runs once a day per Sales Farm, at the first tick at or after it; one
missed by more than CATCH_UP_SECONDS (the server was down) is recorded as Missed instead.
"""

import json

import frappe
from frappe.utils import add_days, cint, get_datetime, now_datetime, nowdate

RUN = "Auto Allocation Run"
#: a Run Time still runs this long after it was due; later it is recorded as Missed
CATCH_UP_SECONDS = 3600
#: a run held up by someone allocating the same Sales Farm is tried again at the next tick, this long
DONE = ("Done", "Failed", "Missed", "Running")


def _settings():
	return frappe.get_single("Production Settings")


def _times(ps):
	return [t for t in ps.get("auto_allocation_times") or [] if cint(t.enabled) and t.run_time]


def _farms(ps):
	return [f for f in ps.get("auto_allocation_farms") or [] if cint(f.enabled) and f.sales_farm]


def _stock_farms(row):
	"""The farms the Sales Farm takes stock from: the row's list, else every farm of its location (None)."""
	farms = [f.strip() for f in (row.stock_farms or "").replace("\n", ",").split(",") if f.strip()]
	return farms or None


def _time_key(t):
	"""A Run Time as HH:MM:SS (a Time field reads back as a timedelta or a string)."""
	s = str(t)
	if len(s.split(":")[0]) == 1:
		s = "0" + s
	return s.split(".")[0]


def tick():
	"""Scheduler (every 5 minutes): start the Run Times that are due."""
	ps = _settings()
	if not cint(ps.get("enable_auto_allocation")):
		return []
	today, now = nowdate(), now_datetime()
	started = []
	for t in _times(ps):
		at = _time_key(t.run_time)
		due = get_datetime(f"{today} {at}")
		if now < due:
			continue
		late = (now - due).total_seconds()
		for f in _farms(ps):
			existing = frappe.db.get_value(
				RUN,
				{"run_date": today, "run_time": at, "sales_farm": f.sales_farm},
				["name", "status"],
				as_dict=True,
			)
			if existing and existing.status in DONE:
				continue
			if late > CATCH_UP_SECONDS:
				if not existing:
					_new_run(today, at, f.sales_farm, status="Missed")
				else:
					frappe.db.set_value(RUN, existing.name, "status", "Missed")
				continue
			name = existing.name if existing else _new_run(today, at, f.sales_farm, release=cint(t.release))
			frappe.db.set_value(RUN, name, "status", "Running")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the next tick must see it running
			frappe.enqueue(
				"upande_packhouse.api.auto_allocation.run",
				queue="long",
				timeout=3600,
				job_id=f"auto_allocation:{name}",
				deduplicate=True,
				run_name=name,
				stock_farms=_stock_farms(f),
				include_unplanned=cint(ps.get("auto_allocation_include_unplanned")),
			)
			started.append(name)
	return started


def _new_run(run_date, run_time, sales_farm, status="Running", release=0):
	doc = frappe.get_doc(
		{
			"doctype": RUN,
			"run_date": run_date,
			"run_time": run_time,
			"sales_farm": sales_farm,
			"delivery_date": add_days(run_date, 1),
			"status": status,
			"released": cint(release),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def run(run_name, stock_farms=None, include_unplanned=0):
	"""One run: allocate the Sales Farm's orders delivering on the run's delivery date, then (when the Run Time
	releases) release their complete boxes, schedule them and plan the trips."""
	from upande_packhouse.api import auto_transfer
	from upande_packhouse.api.remote_transfer import scheduler
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as day

	doc = frappe.get_doc(RUN, run_name)
	location, delivery = doc.sales_farm, str(doc.delivery_date)
	doc.started_at = now_datetime()
	errors = []
	try:
		try:
			day._lock(location)
		except frappe.ValidationError:
			# someone is allocating this Sales Farm right now: the next tick tries again
			frappe.clear_messages()
			doc.status = "Busy"
			doc.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
			return doc.status
		res = day._run_day(
			location,
			delivery,
			delivery,
			stock_farms,
			frappe.session.user,
			only_planned=not cint(include_unplanned),
		)
		if res.get("error"):
			raise Exception(res["error"])
		doc.orders = cint(res.get("new_orders"))
		doc.boxes = cint(res.get("new_boxes"))
		doc.stems = res.get("new_stems") or 0
		doc.shortages = _shortage_text(res.get("shortages"))
		if cint(doc.released):
			opls_before = set(_pick_lists(delivery))
			if day._proposal(location, delivery, delivery):
				day._lock(location, "confirm")
				# this is a background job already: the whole release here, before scheduling and trips
				out = day._run_confirm(
					location=location,
					delivery_start=delivery,
					delivery_end=delivery,
					farms=stock_farms,
					sales_orders=None,
					user=frappe.session.user,
					item_group=None,
					budget=None,
				)
				doc.released_orders = cint((out or {}).get("saved"))
				errors += [str(e) for e in (out or {}).get("errors") or []]
				if (out or {}).get("error"):
					errors.append(str(out["error"]))
			made = [n for n in _pick_lists(delivery) if n not in opls_before]
			doc.pick_lists = ", ".join(made)
			doc.scheduled = sum((scheduler.auto_schedule([delivery]) or {}).values())
			if auto_transfer.enabled():
				# plan the trips now, not at the next re-plan
				frappe.cache().delete_value(auto_transfer.LAST_RUN_KEY)
				auto_transfer.run()
				doc.trips = ", ".join(_open_trips(delivery))
		doc.status = "Done"
	except Exception:
		frappe.db.rollback()
		errors.append(frappe.get_traceback())
		doc.status = "Failed"
	finally:
		frappe.cache.delete_value(f"sa2_day_allocation:{location}")
	doc.errors = "\n\n".join(errors)[:60000]
	doc.finished_at = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return doc.status


def _shortage_text(shortages):
	"""What is still short after the run, one line each."""
	if not shortages:
		return ""
	rows = shortages if isinstance(shortages, list) else shortages.get("lines") or shortages.get("rows") or []
	out = []
	for r in rows[:500]:
		if isinstance(r, dict):
			out.append(
				"{0} · {1} {2} · short {3} stems ({4} boxes){5}".format(
					r.get("order_name") or r.get("sales_order") or "",
					r.get("item_code") or "",
					r.get("length") or "",
					int(r.get("short") or 0),
					int(r.get("boxes_short") or 0),
					" · " + str(r["why"]) if r.get("why") else "",
				)
			)
		else:
			out.append(str(r))
	return "\n".join(out)[:60000] if out else json.dumps(shortages, default=str)[:60000]


def _pick_lists(delivery):
	return frappe.db.sql_list(
		"""SELECT o.name FROM `tabOrder Pick List` o JOIN `tabSales Order` so ON so.name = o.sales_order
		WHERE so.delivery_date = %s AND o.docstatus < 2""",
		delivery,
	)


def _open_trips(delivery):
	"""Today's trips carrying the delivery date's orders, not yet left."""
	return frappe.db.sql_list(
		"""SELECT DISTINCT t.name FROM `tabBucket Request Trip` t
		JOIN `tabBucket Request Trip Order` o ON o.parent = t.name
		JOIN `tabOrder Pick List` opl ON opl.name = o.order_pick_list
		JOIN `tabSales Order` so ON so.name = opl.sales_order
		WHERE so.delivery_date = %s AND t.status IN ('Draft', 'Requested', 'Scheduled')""",
		delivery,
	)


@frappe.whitelist(methods=["POST"])
def run_now(sales_farm: str, release: int = 0):
	"""Production Settings > Automatic Allocation > Run Now: a run for one Sales Farm now (testing, or a missed
	Run Time)."""
	frappe.only_for(("System Manager", "Sales Manager", "Sales Master Manager"))
	ps = _settings()
	row = next((f for f in ps.get("auto_allocation_farms") or [] if f.sales_farm == sales_farm), None)
	now = now_datetime()
	name = _new_run(nowdate(), now.strftime("%H:%M:%S"), sales_farm, release=cint(release))
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.enqueue(
		"upande_packhouse.api.auto_allocation.run",
		queue="long",
		timeout=3600,
		run_name=name,
		stock_farms=_stock_farms(row) if row else None,
		include_unplanned=cint(ps.get("auto_allocation_include_unplanned")),
	)
	return {"run": name}
