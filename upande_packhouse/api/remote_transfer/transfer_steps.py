# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# When (and by whom) each step of a remote transfer was done, on the Order Pick
# List's own rows (Pick List Item): trolley -> truck -> left the farm -> arrived
# at the packhouse -> shelved -> issued.
#
# The step flags are set from many places (the farm app, trips, shelving,
# issuing), so the stamps are written in two ways:
#   * on_event: the moment a step is logged (log_transfer_event);
#   * reconcile: every 5 minutes, any row whose flag is set but has no stamp yet
#     (the step's logged event when there is one, else the row's last change).
# A stamp is written once and never moved.

import frappe
from frappe.utils import add_days, get_datetime, now_datetime

# step -> (check it sets, time field, user field)
STEPS = {
	"trolley": (None, "loaded_in_trolley_at", "loaded_in_trolley_by"),
	"truck": ("loaded_on_truck", "loaded_on_truck_at", "loaded_on_truck_by"),
	"left_farm": (None, "left_farm_at", "left_farm_by"),
	"arrived": (None, "arrived_at", "arrived_by"),
	"shelved": (None, "shelved_at", "shelved_by"),
	"issued": (None, "issued_at", "issued_by"),
}

# Bucket Transfer Event stage -> step
STAGE_STEP = {
	"Loaded in trolley": "trolley",
	"Loaded on truck": "truck",
	"Left the farm": "left_farm",
	"Arrived at packhouse": "arrived",
	"Shelved at sales farm": "shelved",
}

# The row flag that says a step is done (reconcile), and the events that date it.
DONE_WHEN = {
	"trolley": ("pli.loaded_in_trolley = 1", ("Loaded in trolley", "Loaded on truck")),
	# transit_truck alone is no proof: allocation pre-fills it with the order's delivery
	# truck label ("SIM Truck"). Only an internal transfer Vehicle on a moving row counts.
	"truck": (
		"(pli.loaded_in_trolley = 1 OR pli.in_transit = 1) AND EXISTS (SELECT 1 FROM `tabVehicle` v"
		" WHERE v.name = pli.transit_truck AND IFNULL(v.custom_dispatch_truck, 0) = 0)",
		("Loaded on truck",),
	),
	"left_farm": ("pli.in_transit = 1", ("Left the farm",)),
	"arrived": ("pli.shelved = 1", ("Arrived at packhouse", "Shelved at sales farm")),
	"shelved": ("pli.shelved = 1", ("Shelved at sales farm",)),
	"issued": ("pli.issued = 1", ()),
}

# A bucket id is reused every harvest: only rows of the current cycle are stamped.
CYCLE_DAYS = 21


def _ready():
	return frappe.db.has_column("Pick List Item", "issued_at")


def stamp(names, step, at=None, by=None):
	"""Mark `step` done on these Pick List Item rows. The time and user are written
	only where the step has none yet."""
	names = [n for n in (names or []) if n]
	if not names or step not in STEPS or not _ready():
		return
	check, at_field, by_field = STEPS[step]
	# The user first: it reads the time field before that is filled in.
	sets = [
		f"`{by_field}` = IF(`{at_field}` IS NULL, %(by)s, `{by_field}`)",
		f"`{at_field}` = IFNULL(`{at_field}`, %(at)s)",
	]
	if check:
		sets.append(f"`{check}` = 1")
	frappe.db.sql(
		"UPDATE `tabPick List Item` SET " + ", ".join(sets) + " WHERE name IN %(names)s",
		{"at": at or now_datetime(), "by": by or frappe.session.user, "names": tuple(names)},
	)


def on_event(bucket, stage, opl=None, at=None, by=None):
	"""A transfer step was just logged for `bucket`: stamp its rows -- on `opl`, and on
	any other open order the same physical bucket is being transferred for."""
	step = STAGE_STEP.get(stage)
	if not step or not bucket or not _ready():
		return
	at_field = STEPS[step][1]
	at = get_datetime(at) if at else now_datetime()
	names = frappe.db.sql_list(
		f"""SELECT pli.name FROM `tabPick List Item` pli
		JOIN `tabOrder Pick List` o ON o.name = pli.parent AND o.docstatus < 2
		WHERE pli.parenttype = 'Order Pick List' AND UPPER(pli.bucket) = UPPER(%(b)s)
		  AND IFNULL(pli.not_found, 0) = 0 AND pli.`{at_field}` IS NULL
		  AND pli.creation >= %(since)s
		  AND (pli.parent = %(opl)s OR (IFNULL(pli.issued, 0) = 0 AND (pli.awaiting_transfer = 1
		       OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1 OR pli.shelved = 1)))""",
		{"b": str(bucket).strip(), "opl": opl or "", "since": add_days(at, -CYCLE_DAYS)},
	)
	stamp(names, step, at, by)


def reconcile(since_days=1, limit=5000):
	"""Stamp every recently changed row whose step flag is set but carries no stamp:
	at the step's logged event when there is one, else at the row's last change."""
	if not _ready():
		return 0
	since = add_days(now_datetime(), -since_days)
	done = 0
	for step, (flag, stages) in DONE_WHEN.items():
		at_field = STEPS[step][1]
		# nosemgrep: frappe-sql-format-injection -- the holes are this module's fixed field names and conditions
		rows = frappe.db.sql(
			f"""SELECT pli.name, pli.bucket, pli.parent, pli.creation, pli.modified, pli.modified_by
			FROM `tabPick List Item` pli
			WHERE pli.parenttype = 'Order Pick List' AND pli.modified >= %(since)s
			  AND pli.`{at_field}` IS NULL AND {flag}
			LIMIT %(limit)s""",
			{"since": since, "limit": limit},
			as_dict=True,
		)
		for r in rows:
			event = None
			if stages and r.bucket:
				event = frappe.db.sql(
					"""SELECT event_time, user FROM `tabBucket Transfer Event`
					WHERE bucket = %(b)s AND stage IN %(stages)s AND outcome = 'Done'
					  AND event_time >= %(from)s
					  AND (order_pick_list = %(opl)s OR IFNULL(order_pick_list, '') = '')
					ORDER BY (order_pick_list = %(opl)s) DESC, event_time ASC LIMIT 1""",
					{"b": r.bucket, "stages": stages, "from": r.creation, "opl": r.parent},
					as_dict=True,
				)
			if event:
				stamp([r.name], step, event[0].event_time, event[0].user)
			else:
				stamp([r.name], step, r.modified, r.modified_by)
			done += 1
	return done


def run():
	"""Scheduler (every 5 minutes)."""
	reconcile(since_days=1)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
