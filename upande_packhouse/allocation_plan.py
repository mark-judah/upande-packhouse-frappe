# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation plan: what is planned for an order line, and the stock held for it.

A planned order line is an Allocation Confirmation (docs/allocation-playground/spec.md):

  Real     stock on the shelf. The buckets and the stems taken from each are held in Bucket
           Reservation rows (status Active), oldest buckets first. Allocation to any OTHER order line
           cannot use them (availability.reserved_by_bucket, read by core/stock.py and the allocator).
  Virtual  expected from the farm. Stems only, no buckets.

Allocation releases what it uses: `consume` after it writes a bucket allocation, `release_line` once the
line is fully allocated. The other release paths live here too: order cancelled or changed, delivery date
passed, planner unplans, and a held bucket that is no longer on the shelf (Broken).

Age and cooling rules are never written here: free stems come from core/stock.bucket_rows, which reads
Production Settings (amber time, discard age, max allocation age, cooling hours).
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

from upande_packhouse import availability
from upande_packhouse.api.v2.core import received, stock

LIVE = ("Planned", "Reserved", "Partly Fulfilled", "Broken")

# Production Settings > Allocation Planning (fields added by setup_settings). The numbers are only the
# starting values a fresh site is seeded with; the business owns them from then on.
SETTING_DEFAULTS = {
	"allocation_harvest_lead_days": 1,
	"allocation_horizon_days": 3,
	"allocation_conversion_minutes": 15,
	"allocation_use_first_alert_stems": 500,
}


def _stored(name):
	"""The raw stored value, or None when it was never saved. (get_single_value cannot tell: a number
	field that was never saved reads as 0, which would silently turn the margin or the lead days off.)"""
	rows = frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype = 'Production Settings' AND field = %s", name
	)
	return rows[0][0] if rows else None


def setting(name):
	"""A Production Settings number; the seeded default only when the site has never saved it."""
	val = _stored(name)
	if val in (None, ""):
		return SETTING_DEFAULTS[name]
	return float(val) if "." in str(val) else int(val)


# ---------------------------------------------------------------- plans and confirmations


def get_plan(delivery_date, farm, create=True):
	name = frappe.db.get_value("Allocation Plan", {"delivery_date": delivery_date, "farm": farm})
	if name:
		return frappe.get_doc("Allocation Plan", name)
	if not create:
		return None
	doc = frappe.get_doc({"doctype": "Allocation Plan", "delivery_date": delivery_date, "farm": farm})
	doc.insert(ignore_permissions=True)
	return doc


def live_confirmations(soi=None, plan=None, source=None):
	filters = {"status": ("in", LIVE)}
	if soi:
		filters["sales_order_item"] = soi
	if plan:
		filters["plan"] = plan
	if source:
		filters["source"] = source
	return frappe.get_all("Allocation Confirmation", filters=filters, fields=["*"])


def held_buckets(conf_name):
	return frappe.get_all(
		"Bucket Reservation",
		filters={"confirmation": conf_name, "status": "Active"},
		fields=["name", "bucket_id", "stems"],
	)


def free_for_line(farm, variety, length, soi):
	"""Buckets a line can be confirmed against now, oldest first, so ageing stock is used first:

	  on the shelf            free stems, not too old, not cooling, not held for another line
	  received, not shelved   buckets waiting for a shelf (within the discard age), with their cold room

	Stems already held for THIS line count as free for it. A line with a cut stage only takes shelf buckets
	at that stage: the ones Sales Allocation offers it."""
	rows = stock.bucket_rows(farms=[farm], varieties=[variety], lengths=[length] if length else None)
	stage = frappe.db.get_value("Sales Order Item", soi, "custom_cut_stage") if soi else None
	if stage:
		from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import _norm_cut_stage

		rows = [r for r in rows if _norm_cut_stage(r.cut_stage) == _norm_cut_stage(stage)]
	recv = [
		r
		for r in received.received_unshelved(farms=[farm], varieties=[variety], lengths=[length] if length else None)
		if (r.stem_length or "") == (length or "")
	]
	own = _own_held(soi, {r.bucket_id for r in rows} | {r.bucket_id for r in recv})
	out = []
	for r in rows:
		if (r.stem_length or "") != (length or ""):
			continue
		avail = r.allocatable + min(own.get(r.bucket_id, 0.0), r.reserved)
		if avail > 0:
			out.append(frappe._dict(bucket_id=r.bucket_id, free=avail, age_days=r.age_days or 0, stems=r.stems, source="shelf"))
	others = availability.reserved_by_bucket(exclude_soi=soi, bucket_ids={r.bucket_id for r in recv})
	for r in recv:
		avail = r.stems - others.get(r.bucket_id, 0.0)
		if avail > 0:
			out.append(
				frappe._dict(
					bucket_id=r.bucket_id, free=avail, age_days=r.age_days, stems=r.stems, source="received", cold_room=r.cold_room
				)
			)
	out.sort(key=lambda b: (-b.age_days, b.bucket_id))
	return out


def _own_held(soi, bucket_ids):
	if not bucket_ids:
		return {}
	rows = frappe.db.sql(
		"""SELECT bucket_id, SUM(stems) FROM `tabBucket Reservation`
		   WHERE status = 'Active' AND sales_order_item = %(soi)s AND bucket_id IN %(b)s GROUP BY bucket_id""",
		{"soi": soi, "b": tuple(bucket_ids)},
	)
	return {r[0]: flt(r[1]) for r in rows}


def _lock_buckets(bucket_ids):
	"""Two planners reserving the same bucket at once must not both win: serialise on the bucket rows."""
	if bucket_ids:
		frappe.db.sql(
			"SELECT name FROM `tabBucket QR Code` WHERE name IN %(b)s ORDER BY name FOR UPDATE",
			{"b": tuple(sorted(bucket_ids))},
		)


def reserve_real(*, plan, line, farm, variety, length, stems, user=None, team=None):
	"""Plan up to `stems` of a line from shelf stock and hold the buckets. Returns (confirmation or None, held_stems).

	`line` is a dict with sales_order, sales_order_item, customer, ordered_variety, ordered_length, mix_key.
	Replaces any earlier Real plan of the same line. Holds only what is free, so held_stems can be less
	than asked; the caller plans the rest as Virtual."""
	soi = line["sales_order_item"]
	existing = frappe.db.get_value(
		"Allocation Confirmation", {"sales_order_item": soi, "source": "Real", "status": ("in", LIVE)}, ["name", "farm"], as_dict=True
	)
	if existing:
		release_confirmation(existing.name, "Manual", user=user, keep_open=True)
		if existing.farm and existing.farm != farm:  # confirmed against another farm before: its mirror row must go
			refresh_mirror(line["sales_order"], soi, existing.farm)

	pool = free_for_line(farm, variety, length, soi)
	_lock_buckets([b.bucket_id for b in pool])
	pool = free_for_line(farm, variety, length, soi)  # re-read under the lock

	remaining, picks = flt(stems), []
	for b in pool:
		if remaining <= 0:
			break
		take = min(b.free, remaining)
		picks.append((b.bucket_id, take))
		remaining -= take
	held = flt(stems) - max(remaining, 0.0)
	if held <= 0:
		refresh_mirror(line["sales_order"], soi, farm)
		return None, 0.0

	conf = _confirmation(plan, line, "Real", held, farm, variety, length, team=team)
	for bucket_id, take in picks:
		frappe.get_doc(
			{
				"doctype": "Bucket Reservation",
				"bucket_id": bucket_id,
				"status": "Active",
				"sales_order_item": soi,
				"stems": take,
				"confirmation": conf.name,
				"plan": plan.name,
				"item_code": variety,
				"stem_length": length or "",
			}
		).insert(ignore_permissions=True)
	refresh_mirror(line["sales_order"], soi, farm)
	return conf, held


def plan_virtual(*, plan, line, farm, variety, length, stems, forecast_basis="", team=None, harvest_stems=0.0):
	"""Plan stems from the expected harvest. Stems only, no buckets."""
	soi = line["sales_order_item"]
	existing = frappe.db.get_value(
		"Allocation Confirmation", {"sales_order_item": soi, "source": "Virtual", "status": ("in", LIVE)}, ["name", "farm"], as_dict=True
	)
	if existing:
		release_confirmation(existing.name, "Manual", keep_open=True)
		if existing.farm and existing.farm != farm:
			refresh_mirror(line["sales_order"], soi, existing.farm)
	if flt(stems) <= 0:
		refresh_mirror(line["sales_order"], soi, farm)
		return None
	conf = _confirmation(plan, line, "Virtual", flt(stems), farm, variety, length, forecast_basis=forecast_basis, team=team, harvest=min(flt(harvest_stems), flt(stems)))
	refresh_mirror(line["sales_order"], soi, farm)
	return conf


def _confirmation(plan, line, source, stems, farm, variety, length, forecast_basis="", team=None, harvest=0.0):
	doc = frappe.get_doc(
		{
			"doctype": "Allocation Confirmation",
			"plan": plan.name,
			"delivery_date": plan.delivery_date,
			"farm": farm,
			"source": source,
			"status": "Reserved" if source == "Real" else "Planned",
			"sales_order": line["sales_order"],
			"sales_order_item": line["sales_order_item"],
			"customer": line.get("customer"),
			"mix_key": line.get("mix_key") or "",
			"ordered_variety": line.get("ordered_variety"),
			"ordered_length": line.get("ordered_length"),
			"variety": variety,
			"length": length,
			"stems": stems,
			"forecast_basis": forecast_basis,
			"team": team or "",
			"from_harvest": flt(harvest),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


# ---------------------------------------------------------------- teams


def team_for_line(soi):
	"""The team an order line was confirmed for (None when it is not confirmed)."""
	return frappe.db.get_value(
		"Allocation Confirmation",
		{"sales_order_item": soi, "status": ("in", LIVE), "team": ("!=", "")},
		"team",
		order_by="modified desc",
	)


def farm_for_line(soi):
	"""The stock farm a line is confirmed against (None when it is not confirmed)."""
	return frappe.db.get_value(
		"Allocation Confirmation",
		{"sales_order_item": soi, "status": ("in", LIVE)},
		"farm",
		order_by="modified desc",
	)


def teams_for_lines(sois):
	"""{sales_order_item: team} for the lines the plan has a team for: the map allocation takes to stamp a team on a
	pick list (sales_allocation.allocate_stock_with_buckets `teams`)."""
	if not sois:
		return {}
	rows = frappe.get_all(
		"Allocation Confirmation",
		filters={"sales_order_item": ("in", list(sois)), "status": ("in", LIVE), "team": ("!=", "")},
		fields=["sales_order_item", "team"],
		order_by="modified asc",
	)
	return {r.sales_order_item: r.team for r in rows}


def planned_teams(sois):
	"""{sales_order_item: team} the plan gave each line, including lines it has since seen allocated: a line
	unallocated again keeps its team."""
	if not sois:
		return {}
	rows = frappe.db.sql(
		"""SELECT sales_order_item, team FROM `tabAllocation Confirmation`
		   WHERE sales_order_item IN %(s)s AND IFNULL(team, '') != ''
		     AND (status IN %(kept)s OR (status = 'Released' AND release_reason = 'Allocated'))
		   ORDER BY modified ASC""",
		{"s": tuple(sois), "kept": (*LIVE, "Fulfilled")},
		as_dict=True,
	)
	return {r.sales_order_item: r.team for r in rows}


def group_team(mix_key):
	"""The team already chosen for a mixed box (any of its lines), or None."""
	if not mix_key:
		return None
	return frappe.db.get_value(
		"Allocation Confirmation",
		{"mix_key": mix_key, "status": ("in", LIVE), "team": ("!=", "")},
		"team",
		order_by="modified desc",
	)


def apply_team_to_group(mix_key, team):
	"""A mixed box has one team: the team chosen for one variety is saved on every line of the box."""
	if mix_key and team:
		frappe.db.sql(
			"""UPDATE `tabAllocation Confirmation` SET team = %(t)s
			   WHERE mix_key = %(k)s AND status IN %(live)s""",
			{"t": team, "k": mix_key, "live": LIVE},
		)


def sequence_orders(plan, first=None):
	"""Number the plan's orders 1..n in Sales Order.custom_allocation_sequence: the order Sales Allocation lists them in.

	`first` (sales orders) goes first, in that order; the rest keep their place and an order new to the plan goes last.
	An order planned and since allocated keeps its place; an order taken out of the plan goes back to 0."""
	if not plan or not frappe.db.has_column("Sales Order", "custom_allocation_sequence"):
		return []
	rows = frappe.db.sql(
		"""SELECT ac.sales_order, MIN(ac.creation) AS since,
			MAX(ac.status IN %(kept)s OR (ac.status = 'Released' AND ac.release_reason = 'Allocated')) AS planned,
			MAX(so.custom_allocation_sequence) AS seq
		   FROM `tabAllocation Confirmation` ac
		   INNER JOIN `tabSales Order` so ON so.name = ac.sales_order
		   WHERE ac.plan = %(plan)s
		   GROUP BY ac.sales_order""",
		{"plan": plan, "kept": (*LIVE, "Fulfilled")},
		as_dict=True,
	)
	pick = {so: i for i, so in enumerate(first or [])}
	planned = sorted(
		(r for r in rows if r.planned),
		key=lambda r: (
			pick.get(r.sales_order, len(pick)),
			not cint(r.seq),
			cint(r.seq),
			r.since,
			r.sales_order,
		),
	)
	want = {r.sales_order: i for i, r in enumerate(planned, 1)}
	for r in rows:
		if cint(r.seq) != want.get(r.sales_order, 0):
			frappe.db.set_value(
				"Sales Order",
				r.sales_order,
				"custom_allocation_sequence",
				want.get(r.sales_order, 0),
				update_modified=False,
			)
	return [r.sales_order for r in planned]


# ---------------------------------------------------------------- release


def release_confirmation(name, reason, user=None, keep_open=False):
	"""Give the held buckets back and close the plan entry. `keep_open` is for callers about to replace it."""
	frappe.db.sql(
		"""UPDATE `tabBucket Reservation` SET status = 'Released', released_on = %(now)s
		   WHERE confirmation = %(c)s AND status = 'Active'""",
		{"now": now_datetime(), "c": name},
	)
	conf = frappe.db.get_value(
		"Allocation Confirmation", name, ["source", "farm", "sales_order", "sales_order_item"], as_dict=True
	)
	frappe.db.set_value(
		"Allocation Confirmation",
		name,
		{
			"status": "Released",
			"released_on": now_datetime(),
			"released_by": user or frappe.session.user,
			"release_reason": reason,
		},
		update_modified=True,
	)
	if conf and not keep_open:
		refresh_mirror(conf.sales_order, conf.sales_order_item, conf.farm)


def consume(bucket_id, soi, qty):
	"""Allocation took `qty` stems of `bucket_id` for line `soi`: use up what was held for it."""
	qty = flt(qty)
	rows = frappe.get_all(
		"Bucket Reservation",
		filters={"bucket_id": bucket_id, "sales_order_item": soi, "status": "Active"},
		fields=["name", "stems", "confirmation"],
		order_by="creation asc",
	)
	taken_by = defaultdict(float)
	for r in rows:
		if qty <= 0:
			break
		used = min(flt(r.stems), qty)
		left = flt(r.stems) - used
		frappe.db.set_value(
			"Bucket Reservation",
			r.name,
			{"stems": left, "status": "Active" if left > 0 else "Consumed", "released_on": None if left > 0 else now_datetime()},
		)
		taken_by[r.confirmation] += used
		qty -= used
	for conf, used in taken_by.items():
		done = flt(frappe.db.get_value("Allocation Confirmation", conf, "fulfilled_stems")) + used
		frappe.db.set_value("Allocation Confirmation", conf, "fulfilled_stems", done)
		_set_fulfilment_status(conf)


def _set_fulfilment_status(conf):
	stems, done = frappe.db.get_value("Allocation Confirmation", conf, ["stems", "fulfilled_stems"])
	frappe.db.set_value(
		"Allocation Confirmation", conf, "status", "Fulfilled" if flt(done) >= flt(stems) - 0.0001 else "Partly Fulfilled"
	)


def release_line(soi, reason="Allocated"):
	"""A line is fully allocated: whatever is still held or planned for it is no longer needed."""
	for c in live_confirmations(soi=soi):
		held = held_buckets(c.name)
		if c.status in ("Partly Fulfilled", "Fulfilled") and not held:
			frappe.db.set_value("Allocation Confirmation", c.name, "status", "Fulfilled")
			continue
		release_confirmation(c.name, reason)


def after_allocation(allocations):
	"""Called by the allocator once its bucket allocations are written.

	`allocations` are the dicts it was given (bucket_id, sales_order_item, qty). Uses up what was held for
	each line; a line that is now fully allocated gives back anything left."""
	lines = set()
	for a in allocations:
		consume(a["bucket_id"], a["sales_order_item"], a.get("qty") or 0)
		lines.add(a["sales_order_item"])
	for soi in lines:
		if cint(frappe.db.get_value("Sales Order Item", soi, "custom_fully_allocated")):
			release_line(soi, "Allocated")


def release_changed_lines(doc, method=None):
	"""Doc event on Sales Order (cancel, update after submit): drop plan entries whose line is gone or changed."""
	if not frappe.db.table_exists("Allocation Confirmation"):
		return
	cancelled = doc.docstatus == 2
	lines = {r.name: r for r in doc.get("items") or []}
	for c in live_confirmations():
		if c.sales_order != doc.name:
			continue
		row = lines.get(c.sales_order_item)
		if cancelled:
			release_confirmation(c.name, "Order cancelled")
		elif (
			row is None
			or row.item_code != c.ordered_variety
			or (row.get("custom_length") or "") != (c.ordered_length or "")
			or flt(row.stock_qty) < flt(c.stems)
		):
			release_confirmation(c.name, "Line changed")


def release_passed_days():
	"""Daily job: a plan for a delivery date that has passed no longer holds anything."""
	for c in frappe.get_all(
		"Allocation Confirmation",
		filters={"status": ("in", LIVE), "delivery_date": ("<", today())},
		pluck="name",
	):
		release_confirmation(c, "Delivery passed", user="Administrator")
	for p in frappe.get_all("Allocation Plan", filters={"status": ("!=", "Closed"), "delivery_date": ("<", today())}, pluck="name"):
		frappe.db.set_value("Allocation Plan", p, "status", "Closed")


def mark_missing_buckets():
	"""Hourly job: a held bucket that is no longer on a shelf (taken, not found, discarded) is Broken."""
	held = frappe.get_all(
		"Bucket Reservation", filters={"status": "Active"}, fields=["name", "bucket_id", "confirmation"]
	)
	if not held:
		return
	on_shelf = {
		r[0]
		for r in frappe.db.sql(
			"SELECT DISTINCT bucket_id FROM `tabShelf Item` WHERE stem_qty > 0 AND bucket_id IN %(b)s",
			{"b": tuple({h.bucket_id for h in held})},
		)
	}
	waiting = received.unshelved_bucket_ids({h.bucket_id for h in held} - on_shelf)  # received, not shelved yet
	discarding = availability.reserved_bucket_ids()
	for h in held:
		if (h.bucket_id not in on_shelf and h.bucket_id not in waiting) or h.bucket_id in discarding:
			frappe.db.set_value("Bucket Reservation", h.name, {"status": "Broken", "released_on": now_datetime()})
			frappe.db.set_value("Allocation Confirmation", h.confirmation, "status", "Broken")


# ---------------------------------------------------------------- expected stock reaches the shelf


def convert_virtual(delivery_date=None, farm=None):
	"""Expected stems become real as the flowers reach the shelf: for every confirmation planned from the
	forecast, hold buckets for as many stems as the shelf can now give that line (oldest plans first), and keep
	only the rest as expected. Returns the stems converted."""
	filters = {"source": "Virtual", "status": "Planned"}
	if delivery_date:
		filters["delivery_date"] = delivery_date
	if farm:  # the sales farm: its plan holds every confirmation, whichever farm's stock they are against
		filters["plan"] = ("in", frappe.get_all("Allocation Plan", filters={"farm": farm}, pluck="name") or ["-"])
	converted = 0.0
	for c in frappe.get_all("Allocation Confirmation", filters=filters, fields=["*"], order_by="creation asc"):
		soi = c.sales_order_item
		real = frappe.db.get_value(
			"Allocation Confirmation", {"sales_order_item": soi, "source": "Real", "status": ("in", LIVE)}, "stems"
		)
		real = flt(real)
		free = sum(b.free for b in free_for_line(c.farm, c.variety, c.length, soi))
		target = min(real + flt(c.stems), free)
		if target <= real + 0.0001:
			continue
		plan = frappe.get_doc("Allocation Plan", c.plan)
		line = {
			"sales_order": c.sales_order,
			"sales_order_item": soi,
			"customer": c.customer,
			"ordered_variety": c.ordered_variety,
			"ordered_length": c.ordered_length,
			"mix_key": c.mix_key,
		}
		_conf, held = reserve_real(
			plan=plan, line=line, farm=c.farm, variety=c.variety, length=c.length, stems=target, team=c.team
		)
		release_confirmation(c.name, "Now on the shelf", keep_open=True)
		rest = real + flt(c.stems) - held
		if rest > 0.0001:
			plan_virtual(
				plan=plan, line=line, farm=c.farm, variety=c.variety, length=c.length, stems=rest,
				forecast_basis=c.forecast_basis, team=c.team, harvest_stems=min(rest, flt(c.from_harvest)),
			)
		refresh_mirror(c.sales_order, soi, c.farm)
		converted += max(0.0, held - real)
	return converted


def convert_virtual_job():
	"""Scheduled every few minutes; does the work only when `allocation_conversion_minutes` have passed."""
	minutes = cint(setting("allocation_conversion_minutes")) or 15
	last = frappe.cache.get_value("allocation_convert_last")
	now = now_datetime()
	if last and (now - last).total_seconds() < minutes * 60:
		return
	frappe.cache.set_value("allocation_convert_last", now)
	convert_virtual()


# ---------------------------------------------------------------- Confirmed Stems compatibility


def refresh_mirror(sales_order, soi, farm):
	"""Set the line's Confirmed Stems row for `farm` to everything the plan has confirmed for it there: what is held
	from stock plus what is confirmed from the expected harvest. Allocation shows a farm only the lines confirmed
	there and caps what it offers at this number, so it has to include the stems still to arrive."""
	total = sum(
		flt(stems)
		for stems in frappe.get_all(
			"Allocation Confirmation",
			filters={"sales_order_item": soi, "farm": farm, "status": ("in", LIVE)},
			pluck="stems",
		)
	)
	_sync_confirmed_stems(sales_order, soi, farm, total)


def _sync_confirmed_stems(sales_order, soi, farm, stems):
	"""Keep the v1 `Confirmed Stems` row (farm, stems, order line) in step with the plan (Real and Virtual entries).

	The allocator reads it to show a farm only the lines confirmed there and to cap what is pending. Written
	straight to the child table, not through a Sales Order save: a submitted order's save runs the order
	engine hooks, and this is only a mirror of the plan."""
	filters = {
		"parent": sales_order,
		"parenttype": "Sales Order",
		"parentfield": "custom_confirmed_stems_table",
		"sales_order_item": soi,
		"farm": farm,
	}
	name = frappe.db.get_value("Confirmed Stems", filters)
	if flt(stems) <= 0:
		if name:
			frappe.db.delete("Confirmed Stems", {"name": name})
		return
	if name:
		frappe.db.set_value("Confirmed Stems", name, "stems", flt(stems))
		return
	idx = cint(
		frappe.db.sql(
			"""SELECT COALESCE(MAX(idx), 0) FROM `tabConfirmed Stems`
			   WHERE parent = %s AND parenttype = 'Sales Order' AND parentfield = 'custom_confirmed_stems_table'""",
			sales_order,
		)[0][0]
	)
	row = frappe.get_doc({"doctype": "Confirmed Stems", **filters, "stems": flt(stems), "idx": idx + 1})
	row.name = frappe.generate_hash(length=10)
	row.owner = row.modified_by = frappe.session.user
	row.creation = row.modified = now_datetime()
	row.db_insert()


# ---------------------------------------------------------------- setup


def setup_settings():
	"""Add the Production Settings > Allocation Planning fields (idempotent; Production Settings belongs to upande_quality)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	fields = [
		{"fieldname": "allocation_planning_section", "fieldtype": "Section Break", "label": "Planning"},
		{
			"fieldname": "allocation_harvest_lead_days",
			"fieldtype": "Int",
			"label": "Days from cutting to delivery",
			"description": "How many days before delivery the flowers are cut. Used to work out what the farm will supply.",
			"default": str(SETTING_DEFAULTS["allocation_harvest_lead_days"]),
		},
		{
			"fieldname": "allocation_horizon_days",
			"fieldtype": "Int",
			"label": "Days ahead people can plan",
			"description": "How many delivery days ahead the planning page lets you plan.",
			"default": str(SETTING_DEFAULTS["allocation_horizon_days"]),
		},
		{"fieldname": "allocation_planning_col", "fieldtype": "Column Break"},
		{
			"fieldname": "allocation_conversion_minutes",
			"fieldtype": "Int",
			"label": "Check for new stock every (minutes)",
			"description": "How often expected stock is matched to flowers that reach the shelf.",
			"default": str(SETTING_DEFAULTS["allocation_conversion_minutes"]),
		},
		{
			"fieldname": "allocation_use_first_alert_stems",
			"fieldtype": "Int",
			"label": "Tell me when old stock is at least (stems)",
			"description": 'Smallest amount of getting-old stock that is worth a "use first" suggestion.',
			"default": str(SETTING_DEFAULTS["allocation_use_first_alert_stems"]),
		},
	]
	# In Production Settings' own Allocation Planning tab, after its Sales Allocation v2 section (a custom
	# Tab Break used to stand in for the tab; it goes).
	if frappe.db.exists("Custom Field", "Production Settings-allocation_planning_tab"):
		frappe.delete_doc("Custom Field", "Production Settings-allocation_planning_tab", ignore_permissions=True, force=True)
	meta = frappe.get_meta("Production Settings")
	after = "hide_allocation_planning_team" if meta.has_field("hide_allocation_planning_team") else "release_trips_without_confirmation"
	for field in fields:
		field["insert_after"] = after
		after = field["fieldname"]
	create_custom_fields({"Production Settings": fields}, ignore_validate=True)
	# The forecast margin is no longer a setting: it is the forecast's own average error this year.
	if frappe.db.exists("Custom Field", "Production Settings-allocation_virtual_haircut_pct"):
		frappe.delete_doc("Custom Field", "Production Settings-allocation_virtual_haircut_pct", ignore_permissions=True, force=True)
	# Seed once. A value the business has already saved (even 0) is never touched.
	for name, default in SETTING_DEFAULTS.items():
		if _stored(name) is None:
			frappe.db.set_single_value("Production Settings", name, default)


def ensure_indexes():
	"""The allocator asks 'is this bucket held?' on every allocation; index it."""
	if frappe.db.table_exists("Bucket Reservation"):
		frappe.db.add_index("Bucket Reservation", ["status", "bucket_id"], "idx_status_bucket")
		frappe.db.add_index("Bucket Reservation", ["status", "sales_order_item"], "idx_status_soi")
