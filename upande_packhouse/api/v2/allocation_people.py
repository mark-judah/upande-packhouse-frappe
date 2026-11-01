# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation Planning v2: the Post Harvest people available on the planning date, per designation.

Post Harvest = every Active employee in a Department named "Post Harvest..." or under one, employed on the
date. Each falls in one bucket for the day, in this order: off day (weekly off or public holiday from their
holiday list), sick off (approved leave whose Leave Type name has "sick"), other leave; the rest are
expected at work. On today or an earlier date, present = any check-in that day (the readers mislabel IN and
OUT, so any scan counts) or Present / Half Day / Work From Home attendance.
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate

from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.upande_packhouse.doctype.packing_team_move.packing_team_move import moves_on

ROOT = "Post Harvest"


def _departments():
	roots = frappe.get_all(
		"Department", filters={"department_name": ("like", ROOT + "%")}, fields=["lft", "rgt"]
	)
	names = set()
	for r in roots:
		names.update(
			frappe.get_all("Department", filters={"lft": (">=", r.lft), "rgt": ("<=", r.rgt)}, pluck="name")
		)
	return sorted(names)


def _employees(day, ids=None):
	"""Active Post Harvest employees employed on the day, or these employees (`ids`) whatever their
	department."""
	if ids:
		cond, params = "e.name IN %(ids)s", {"ids": tuple(ids), "day": day}
	else:
		departments = _departments()
		if not departments:
			return []
		cond, params = "e.department IN %(departments)s", {"departments": tuple(departments), "day": day}
	# the f-string hole is one of two fixed conditions; values are bound
	return frappe.db.sql(  # nosemgrep
		f"""
		SELECT e.name AS employee, e.employee_name, e.company, IFNULL(e.branch, '') AS branch,
			IFNULL(e.designation, '') AS designation, e.department
		FROM `tabEmployee` e
		WHERE e.status = 'Active' AND {cond}
			AND (e.date_of_joining IS NULL OR e.date_of_joining <= %(day)s)
			AND (e.relieving_date IS NULL OR e.relieving_date >= %(day)s)
		ORDER BY e.designation, e.employee_name
		""",
		params,
		as_dict=True,
	)


def _off_days(employees, day):
	"""{employee: description} for those whose holiday list has the day off. Resolved through Holiday List
	Assignments (upande_ta's helper) when that app is installed, else the employee's or company's list."""
	if "upande_ta" in frappe.get_installed_apps():
		from upande_ta.upande_ta.api.canteen_analysis import _off_days as ta_off_days

		return {emp: days[day] for emp, days in ta_off_days(employees, day, day).items() if day in days}

	from hrms.hr.utils import get_holiday_list_for_employee

	lists, out = {}, {}
	for e in employees:
		hl = get_holiday_list_for_employee(e.employee, raise_exception=False)
		if hl:
			lists.setdefault(hl, []).append(e.employee)
	for h in frappe.get_all(
		"Holiday",
		filters={"parent": ("in", list(lists)), "holiday_date": day},
		fields=["parent", "weekly_off", "description"],
	):
		for emp in lists[h.parent]:
			out[emp] = "Weekly Off" if h.weekly_off else (h.description or "Public Holiday")
	return out


def _leave(employee_ids, day):
	"""{employee: leave type} of full-day approved leave on the day (Leave Applications, then On Leave
	attendance with no application behind it)."""
	if not employee_ids:
		return {}
	out = {}
	for la in frappe.db.sql(
		"""
		SELECT employee, leave_type, from_date, to_date, half_day, half_day_date
		FROM `tabLeave Application`
		WHERE docstatus = 1 AND status = 'Approved' AND from_date <= %(day)s AND to_date >= %(day)s
			AND employee IN %(emps)s
		""",
		{"day": day, "emps": tuple(employee_ids)},
		as_dict=True,
	):
		half = getdate(la.half_day_date) if la.half_day and la.half_day_date else None
		if la.half_day and not half and la.from_date == la.to_date:
			half = getdate(la.from_date)
		if half != day:
			out[la.employee] = la.leave_type
	for emp, leave_type in frappe.db.sql(
		"""
		SELECT employee, IFNULL(leave_type, 'Leave') FROM `tabAttendance`
		WHERE docstatus = 1 AND status = 'On Leave' AND attendance_date = %(day)s AND employee IN %(emps)s
		""",
		{"day": day, "emps": tuple(employee_ids)},
	):
		out.setdefault(emp, leave_type)
	return out


def _teams(employee_ids):
	"""{employee: "Team A, Print Room"}: the Packing Team each packs in, then any Post Harvest Teams
	(upande_quality) they are a member of."""
	if not employee_ids:
		return {}
	out = {}
	sources = (
		("Packing Team Member", "employee", "Packing Teams"),
		("Post Harvest Team Member Detail", "employee_id", "Post Harvest Team"),
	)
	for child, field, parent in sources:
		if not frappe.db.table_exists(child):
			continue
		for emp, team in frappe.get_all(
			child,
			filters={field: ("in", employee_ids), "parenttype": parent},
			fields=[field, "parent"],
			order_by="parent",
			as_list=True,
		):
			out.setdefault(emp, []).append(team)
	return {emp: ", ".join(dict.fromkeys(teams)) for emp, teams in out.items()}


def _present(employee_ids, day):
	if not employee_ids:
		return set()
	scanned = frappe.db.sql_list(
		"""
		SELECT DISTINCT employee FROM `tabEmployee Checkin`
		WHERE time >= %(day)s AND time < %(next)s AND employee IN %(emps)s
		""",
		{"day": day, "next": add_days(day, 1), "emps": tuple(employee_ids)},
	)
	marked = frappe.db.sql_list(
		"""
		SELECT employee FROM `tabAttendance`
		WHERE docstatus = 1 AND status IN ('Present', 'Half Day', 'Work From Home')
			AND attendance_date = %(day)s AND employee IN %(emps)s
		""",
		{"day": day, "emps": tuple(employee_ids)},
	)
	return set(scanned) | set(marked)


@frappe.whitelist()
def get_people(delivery_date: str | None = None, branch: str | None = None):
	"""Post Harvest people per designation at one branch (the first when none is asked) on the planning
	date: headcount, off, sick, leave, expected, and present once the day has started."""
	day = getdate(pg._day(delivery_date))
	everyone = _employees(day)
	branches = sorted({e.branch for e in everyone if e.branch})
	if branch not in branches:
		branch = branches[0] if branches else ""
	staff = [e for e in everyone if not branch or e.branch == branch]
	ids = [e.employee for e in staff]
	off = _off_days(staff, day)
	leave = _leave(ids, day)
	started = day <= getdate(nowdate())
	present = _present(ids, day) if started else set()
	teams = _teams(ids)
	moves = moves_on(day, ids)
	packing = frappe.get_all("Packing Teams", pluck="name", order_by="name")

	rows = {}
	for e in staff:
		r = rows.setdefault(
			e.designation or "No designation",
			{
				"designation": e.designation or "No designation",
				"headcount": 0,
				"off": 0,
				"sick": 0,
				"leave": 0,
				"expected": 0,
				"present": 0,
				"employees": [],
			},
		)
		r["headcount"] += 1
		if e.employee in off:
			r["off"] += 1
			status, note = "off", off[e.employee]
		elif e.employee in leave:
			status = "sick" if "sick" in (leave[e.employee] or "").lower() else "leave"
			note = leave[e.employee]
			r[status] += 1
		else:
			r["expected"] += 1
			status, note = "expected", ""
		if e.employee in present:
			r["present"] += 1
		move = moves.get(e.employee)
		r["employees"].append(
			{
				"employee": e.employee,
				"name": e.employee_name,
				"branch": e.branch,
				# on a move the day's team comes first; the Post Harvest Teams they are in stay listed after it
				"team": ", ".join(
					t for t in dict.fromkeys([move.to_team] + teams.get(e.employee, "").split(", ")) if t
				)
				if move
				else teams.get(e.employee, ""),
				"day_team": move.to_team if move else _home(teams.get(e.employee, ""), packing),
				"move": {
					"name": move.name,
					"from_team": move.from_team or "",
					"from_date": str(move.from_date),
					"to_date": str(move.to_date),
				}
				if move
				else None,
				"status": status,
				"note": note,
				"present": e.employee in present,
			}
		)

	order = {"off": 0, "sick": 1, "leave": 2, "expected": 3}
	for r in rows.values():
		r["employees"].sort(key=lambda e: (order[e["status"]], e["name"] or ""))
	out = sorted(rows.values(), key=lambda r: (-r["headcount"], r["designation"]))
	totals = {k: sum(r[k] for r in out) for k in ("headcount", "off", "sick", "leave", "expected", "present")}
	return {
		"date": str(day),
		"branch": branch or "",
		"branches": branches,
		"packing_teams": packing,
		"started": started,
		"designations": out,
		"totals": totals,
	}


def _home(teams, packing):
	"""The Packing Team out of an employee's teams ("Team A, Print Room" -> "Team A")."""
	return next((t for t in teams.split(", ") if t in packing), "")


@frappe.whitelist(methods=["POST"])
def move_member(employee: str, to_team: str, from_date: str, to_date: str):
	"""Have a Packing Team member work in `to_team` from one date to another."""
	frappe.only_for(pg.PLANNERS)
	doc = frappe.get_doc(
		{
			"doctype": "Packing Team Move",
			"employee": employee,
			"to_team": to_team,
			"from_date": from_date,
			"to_date": to_date,
		}
	).insert(ignore_permissions=True)
	return {"success": True, "name": doc.name}


@frappe.whitelist(methods=["POST"])
def cancel_move(name: str):
	"""Send the member back to their own team: the move is deleted."""
	frappe.only_for(pg.PLANNERS)
	if not frappe.db.exists("Packing Team Move", name):
		frappe.throw(_("That move no longer exists."))
	frappe.delete_doc("Packing Team Move", name, ignore_permissions=True)
	return {"success": True}
