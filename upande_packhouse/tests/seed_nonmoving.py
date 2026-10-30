# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Local test data for the Non-moving tab: re-dates a few shelf buckets so they are at the amber age.

Local shelf stock is all older than the farms' maximum allocation age, so nothing shows as non-moving. This picks a few
buckets for each case and moves their harvest and shelving dates to `age` days ago (default: the amber time), after
saving the old dates so `undo` puts everything back:

  exact       buckets of a variety and length that an order line for the day needs
  longer      buckets of a longer length of a variety a line ordered shorter
  substitute  buckets of a variety a line's specification approves instead of the one ordered
  no order    buckets of a variety nobody ordered that day

  bench --site kaitet.local execute upande_packhouse.tests.seed_nonmoving.seed
  bench --site kaitet.local execute upande_packhouse.tests.seed_nonmoving.undo
"""

import json
import os

import frappe
from frappe.utils import add_days, today

from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.api.v2.core import stock

UNDO = lambda: frappe.get_site_path("private", "files", "nonmoving_seed_undo.json")  # noqa: E731


def _buckets(farms, variety, length=None, limit=3, skip=()):
	cond = " AND stem_length = %(l)s" if length else ""
	rows = frappe.db.sql(
		f"""SELECT bucket_id, SUM(stem_qty) q FROM `tabShelf Item`
		    WHERE stem_qty > 0 AND variety = %(v)s AND farm IN %(f)s AND IFNULL(bucket_id, '') != ''{cond}
		    GROUP BY bucket_id ORDER BY MAX(date_added) DESC LIMIT 40""",
		{"v": variety, "f": tuple(farms), "l": length},
		as_dict=True,
	)
	return [r.bucket_id for r in rows if r.bucket_id not in skip][:limit]


def seed(delivery_date=None, farm=None, age=None):
	day = pg._day(delivery_date)
	farm = farm or pg._default_farm()
	age = int(age if age is not None else stock._config()["amber_time"])
	farms = pg._stock_farms(farm)
	lines = [ln for ln in pg._lines(day) if ln.item_code and ln.custom_length]
	specs = {}
	for spec, variety in frappe.db.sql(
		"SELECT parent, variety FROM `tabSpec Approved Variety` WHERE parenttype = 'Specifications'"
	):
		specs.setdefault(spec, set()).add(variety)
	ordered = {ln.item_code for ln in lines}
	picks, used = [], set()

	def take(label, variety, length=None):
		got = _buckets(farms, variety, length, skip=used)
		if got:
			used.update(got)
			picks.append((label, variety, length, got))

	cm = lambda x: int("".join(c for c in x if c.isdigit()) or 0)  # noqa: E731
	for ln in lines:  # exact
		if not any(p[0] == "exact" for p in picks):
			take("exact", ln.item_code, ln.custom_length)
	for ln in lines:  # longer: a longer length of a variety that has a shorter line
		if any(p[0] == "longer" for p in picks):
			break
		for (length,) in frappe.db.sql(
			"SELECT DISTINCT stem_length FROM `tabShelf Item` WHERE variety = %s AND stem_length != '' AND stem_qty > 0",
			ln.item_code,
		):
			if cm(length) > cm(ln.custom_length) and not any(
				x.item_code == ln.item_code and x.custom_length == length for x in lines
			):
				take("longer", ln.item_code, length)
				break
	for ln in lines:  # substitute: approved by the spec, not ordered by anyone that day
		if any(p[0] == "substitute" for p in picks):
			break
		for alt in sorted(specs.get(ln.custom_line, set()) - {ln.item_code} - ordered):
			take("substitute", alt, ln.custom_length)
			if picks and picks[-1][0] == "substitute":
				break
	for (variety,) in frappe.db.sql(
		"SELECT DISTINCT variety FROM `tabShelf Item` WHERE stem_qty > 0 AND farm IN %s LIMIT 200",
		(tuple(farms),),
	):
		if (
			variety not in ordered
			and not any(p[0] == "no order" for p in picks)
			and not any(variety in s for s in specs.values())
		):
			take("no order", variety)
	if not picks:
		print("No shelf stock found to re-date.")
		return
	when = add_days(today(), -age)
	saved = []
	for _label, _v, _l, ids in picks:
		for r in frappe.db.sql(
			"SELECT name, date_added, harvest_date FROM `tabShelf Item` WHERE bucket_id IN %s",
			(tuple(ids),),
			as_dict=True,
		):
			saved.append(
				{
					"name": r.name,
					"date_added": str(r.date_added),
					"harvest_date": str(r.harvest_date) if r.harvest_date else None,
				}
			)
	prev = json.load(open(UNDO())) if os.path.exists(UNDO()) else []
	json.dump(prev + saved, open(UNDO(), "w"))
	for _label, _v, _l, ids in picks:
		frappe.db.sql(
			"UPDATE `tabShelf Item` SET date_added = %s, harvest_date = %s WHERE bucket_id IN %s",
			(f"{when} 08:00:00", when, tuple(ids)),
		)
	frappe.db.commit()
	frappe.clear_cache()
	for label, v, length, ids in picks:
		print(f"{label:11} {v} {length or '(any length)'}: {len(ids)} buckets now {age} days old")
	print("Undo with: bench --site kaitet.local execute upande_packhouse.tests.seed_nonmoving.undo")


def undo():
	if not os.path.exists(UNDO()):
		print("Nothing to undo.")
		return
	for r in json.load(open(UNDO())):
		frappe.db.sql(
			"UPDATE `tabShelf Item` SET date_added = %s, harvest_date = %s WHERE name = %s",
			(r["date_added"], r["harvest_date"], r["name"]),
		)
	frappe.db.commit()
	os.remove(UNDO())
	frappe.clear_cache()
	print("Dates put back.")
