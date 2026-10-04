# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Bucket Distribution (one entry per ⚡ Distribute run) is new: trips planned before
# it have no entry, so Transfer Scheduling's Distributed list started empty. Rebuild
# the entries from the planned trips of the last two weeks — trips the same person
# (or the automatic scheduler) saved within a few minutes of each other were one run.
# Unplanned loads (trips made from a farm's load) were never distributed and are left
# out, as is any trip already on an entry, so running this again adds nothing.

from collections import Counter

import frappe

LOOKBACK_DAYS = 14
#: Trips saved this close together by the same person were one Distribute.
RUN_GAP_SECONDS = 180


def execute():
	if not frappe.db.table_exists("Bucket Distribution"):
		return
	from upande_packhouse.api.transfer_control import transfer_hub

	logged = set(frappe.get_all("Bucket Distribution Truck", pluck="trip"))
	trips = [
		t
		for t in frappe.get_all(
			"Bucket Request Trip",
			filters={
				"creation": [">=", frappe.utils.add_days(frappe.utils.today(), -LOOKBACK_DAYS)],
				"unscheduled": 0,
			},
			fields=[
				"name",
				"creation",
				"owner",
				"auto_planned",
				"vehicle",
				"run",
				"total_buckets",
				"total_stems",
			],
			order_by="creation asc",
		)
		# Planner trips only (BRT- naming series): hand-made test records are skipped.
		if t.name not in logged and t.name.startswith("BRT-")
	]
	hub = transfer_hub(required=False)

	runs, last = [], {}
	for t in trips:
		key = (t.owner, int(t.auto_planned or 0))
		prev = last.get(key)
		if prev and frappe.utils.time_diff_in_seconds(t.creation, prev[-1].creation) <= RUN_GAP_SECONDS:
			prev.append(t)
		else:
			prev = [t]
			runs.append(prev)
		last[key] = prev

	for run in runs:
		loads, dates = [], Counter()
		for t in run:
			orders = frappe.get_all(
				"Bucket Request Trip Order",
				filters={"parent": t.name, "parenttype": "Bucket Request Trip"},
				fields=["order_pick_list", "order_name", "farm"],
				order_by="idx",
			)
			if not orders:
				continue
			for o in orders:
				due = frappe.db.sql(
					"""SELECT so.delivery_date FROM `tabOrder Pick List` opl
					JOIN `tabSales Order` so ON so.name = opl.sales_order WHERE opl.name = %s""",
					o.order_pick_list,
				)
				if due and due[0][0]:
					dates[str(due[0][0])] += 1
			loads.append(
				{
					"vehicle": t.vehicle,
					"trip": t.name,
					"run": t.run,
					"farms": ", ".join(dict.fromkeys(o.farm for o in orders if o.farm and o.farm != hub)),
					"buckets": t.total_buckets,
					"stems": t.total_stems,
					"orders": list(dict.fromkeys(o.order_name or o.order_pick_list for o in orders)),
				}
			)
		if not loads or not dates:
			continue
		doc = frappe.new_doc("Bucket Distribution")
		doc.delivery_date = dates.most_common(1)[0][0]
		doc.source = "Automatic" if run[0].auto_planned else "Manual"
		for l in loads:
			doc.append("trucks", {**l, "orders": "\n".join(l["orders"])})
		doc.total_trucks = len(loads)
		doc.total_buckets = sum(int(l["buckets"] or 0) for l in loads)
		doc.total_stems = sum(int(l["stems"] or 0) for l in loads)
		doc.insert(ignore_permissions=True)
		# The entry is dated and owned like the run it records, not like this patch.
		frappe.db.set_value(
			"Bucket Distribution",
			doc.name,
			{"creation": run[0].creation, "owner": run[0].owner},
			update_modified=False,
		)
