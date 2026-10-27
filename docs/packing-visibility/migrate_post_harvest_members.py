"""Copy Post Harvest Team members from the old v15 site into this (v16) site.

Source data: post_harvest_members_v15.json (191 rows read from the v15 instance, team + employee_id +
role). Nothing is read from or written to any other site by this script.

What it does, per row:
  * employee_id (or payroll_id, or a single exact name match when both are blank) must exist in this
    site's Employee table and not be Left/Inactive, otherwise the row is
    skipped and listed in the report (people who left, or ids that were never migrated).
  * payroll_id is set to the Employee id; full_name is filled by v16 with the employee's name. full_name is
    a Link to Employee in v16 but holds a name, so links are not validated on save (see the note in the code).
  * role: v16 roles were renamed, and most v15 'roles' were really job titles (PACKER, GRADER, SLEEVER...).
    A value is kept only when a Role of that name exists (matched ignoring case, e.g. QUALITY CONTROLLER ->
    Quality Controller). Otherwise role stays empty; the job title is on Employee.designation anyway.
  * the team must exist (matched ignoring case); it is never created here.
  * running it twice adds nothing: a (team, employee) pair already present is skipped.

Run from the sites folder:
  ../env/bin/python ../apps/upande_packhouse/docs/packing-visibility/migrate_post_harvest_members.py           # dry run
  ../env/bin/python ../apps/upande_packhouse/docs/packing-visibility/migrate_post_harvest_members.py --apply   # write
"""

import json
import os
import sys
from collections import Counter

import frappe

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.environ.get("SITE", "kaitet.local")


def main(apply):
	frappe.init(site=SITE, sites_path=".")
	frappe.connect()
	rows = json.load(open(os.path.join(HERE, "post_harvest_members_v15.json")))
	teams = {t.lower(): t for t in frappe.get_all("Post Harvest Team", pluck="name")}
	roles = {r.lower(): r for r in frappe.get_all("Role", pluck="name")}
	emp = {
		e.name: e
		for e in frappe.get_all("Employee", fields=["name", "employee_name", "status", "designation"])
	}
	by_name = {}
	for e in emp.values():
		by_name.setdefault((e.employee_name or "").strip().lower(), []).append(e)
	existing = {
		(m.parent, m.employee_id)
		for m in frappe.get_all("Post Harvest Team Member Detail", fields=["parent", "employee_id"])
	}
	plan, report = {}, Counter()
	skipped = []
	for r in rows:
		team = teams.get((r["team"] or "").lower())
		e = emp.get(r["employee_id"]) or emp.get(r["payroll_id"])
		if not e:
			# a few v15 rows have no employee id: accept a single exact name match
			hits = by_name.get((r["full_name"] or "").strip().lower(), [])
			e = hits[0] if len(hits) == 1 else None
			if e:
				report["matched by name"] += 1
		if not team:
			report["team missing"] += 1
			skipped.append((r["team"], r["employee_id"], r["full_name"], "team does not exist in v16"))
		elif not e:
			report["employee not found"] += 1
			skipped.append((r["team"], r["employee_id"], r["full_name"], "no such Employee in v16"))
		elif e.status != "Active":
			report["employee " + e.status] += 1
			skipped.append((r["team"], r["employee_id"], r["full_name"], "employee is " + e.status))
		elif (team, e.name) in existing or any(x["employee_id"] == e.name for x in plan.get(team, [])):
			report["already present"] += 1
		else:
			role = roles.get((r["role"] or "").lower())
			report["role kept" if role else "role dropped"] += 1
			plan.setdefault(team, []).append(
				{"employee_id": e.name, "payroll_id": e.name, "full_name": e.name, "role": role}
			)
			report["to import"] += 1
	print("Source rows:", len(rows))
	for k, v in sorted(report.items()):
		print(f"  {k}: {v}")
	for t, v in sorted(plan.items()):
		print(f"  {t}: {len(v)} members")
	if skipped:
		print("Skipped:")
		for s in skipped:
			print("  ", *s)
	if not apply:
		print("\nDry run. Nothing written. Re-run with --apply to import.")
		return
	for team, members in plan.items():
		doc = frappe.get_doc("Post Harvest Team", team)
		for m in members:
			doc.append("members", m)
		doc.flags.ignore_permissions = True
		# v16 makes full_name a Link to Employee but fills it with the employee's NAME (fetch_from),
		# which then fails link validation on every save. v15 stored the name as plain text, so skip the check.
		doc.flags.ignore_links = True
		doc.save()
	frappe.db.commit()
	print("\nImported", report["to import"], "members.")


if __name__ == "__main__":
	main("--apply" in sys.argv)
