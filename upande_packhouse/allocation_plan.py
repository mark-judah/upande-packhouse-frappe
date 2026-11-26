# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Allocation plan: what is planned for an order line, and the stock held for it.

A planned order line is an Allocation Confirmation (docs/allocation-playground/spec.md):

  Real     stems confirmed from stock on the shelf (or received, waiting for a shelf). Stems only: no
           bucket is held, so Sales Allocation can take any free bucket, oldest first. Another line's
           Real stems still to allocate are counted off what the plan offers the next line
           (`_planned_elsewhere`), so the plan never promises the same stems twice.
  Virtual  expected from the farm. Stems only.

Allocation uses up what was confirmed: `consume` after it writes a bucket allocation, `release_line` once the
line is fully allocated. The other release paths live here too: order cancelled or changed, delivery date
passed, planner unplans.

Age and cooling rules are never written here: free stems come from core/stock.bucket_rows, which reads
Production Settings (amber time, discard age, max allocation age, cooling hours).
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, today

from upande_packhouse.api.v2.core import received, stock

LIVE = ("Planned", "Reserved", "Partly Fulfilled", "Broken")

# Production Settings > Allocation Planning > Planning (upande_quality's fields). The numbers are only the
# starting values a fresh site is seeded with; the business owns them from then on.
#: Days from cutting to delivery: what a farm supplies for a delivery day is cut this many days before it.
HARVEST_LEAD_DAYS = 1

SETTING_DEFAULTS = {
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


def free_for_line(farm, variety, length, soi, harvested_from=None):
	"""Buckets a line can be confirmed against now, oldest first, so ageing stock is used first:

	  on the shelf            free stems, not too old, not cooling
	  received, not shelved   buckets waiting for a shelf (within the discard age), with their cold room

	A line with a cut stage only takes shelf buckets
	at that stage: the ones Sales Allocation offers it. `harvested_from` keeps only buckets harvested on or
	after that day."""
	rows = stock.bucket_rows(farms=[farm], varieties=[variety], lengths=[length] if length else None)
	if harvested_from:
		rows = [r for r in rows if r.age_anchor and getdate(r.age_anchor) >= getdate(harvested_from)]
	stage = frappe.db.get_value("Sales Order Item", soi, "custom_cut_stage") if soi else None
	if stage:
		from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import (
			_norm_cut_stage,
		)

		rows = [r for r in rows if _norm_cut_stage(r.cut_stage) == _norm_cut_stage(stage)]
	recv = [
		r
		for r in received.received_unshelved(
			farms=[farm], varieties=[variety], lengths=[length] if length else None
		)
		if (r.stem_length or "") == (length or "")
		and (not harvested_from or getdate(r.harvest_date or r.received_on) >= getdate(harvested_from))
	]
	out = []
	for r in rows:
		if (r.stem_length or "") != (length or ""):
			continue
		avail = r.allocatable
		if avail > 0:
			out.append(
				frappe._dict(
					bucket_id=r.bucket_id, free=avail, age_days=r.age_days or 0, stems=r.stems, source="shelf"
				)
			)
	for r in recv:
		avail = flt(r.stems)
		if avail > 0:
			out.append(
				frappe._dict(
					bucket_id=r.bucket_id,
					free=avail,
					age_days=r.age_days,
					stems=r.stems,
					source="received",
					cold_room=r.cold_room,
				)
			)
	out.sort(key=lambda b: (-b.age_days, b.bucket_id))
	return out


def _planned_elsewhere(farm, variety, length, soi):
	"""Stems other lines have confirmed from this farm's stock of `variety` at `length` and not allocated yet."""
	return flt(
		frappe.db.sql(
			"""SELECT SUM(GREATEST(stems - IFNULL(fulfilled_stems, 0), 0)) FROM `tabAllocation Confirmation`
			   WHERE source = 'Real' AND status IN %(live)s AND farm = %(farm)s AND variety = %(variety)s
			     AND IFNULL(length, '') = %(length)s AND sales_order_item != %(soi)s""",
			{"live": LIVE, "farm": farm, "variety": variety, "length": length or "", "soi": soi or ""},
		)[0][0]
	)


def real_outstanding(farms=None):
	"""{(farm, variety, length): stems} live Real confirmations still have to allocate."""
	cond = "AND farm IN %(farms)s" if farms else ""
	return {
		(r[0], r[1], r[2] or ""): flt(r[3])
		# nosemgrep: frappe-sql-format-injection -- the f-string hole is fixed SQL, values are bound
		for r in frappe.db.sql(
			f"""SELECT farm, variety, IFNULL(length, ''), SUM(GREATEST(stems - IFNULL(fulfilled_stems, 0), 0))
			    FROM `tabAllocation Confirmation`
			    WHERE source = 'Real' AND status IN %(live)s {cond}
			    GROUP BY farm, variety, IFNULL(length, '')""",
			{"live": LIVE, "farms": tuple(farms or ()) or ("",)},
		)
	}


def _lock_pool(variety):
	"""Two planners confirming the same stock at once must not both win: serialise on the variety."""
	frappe.db.sql("SELECT name FROM `tabItem` WHERE name = %s FOR UPDATE", variety)


def _live_of_line(soi, source):
	"""The line's live confirmations of `source`, read under a lock on the line: a second planner confirming the
	same line waits here for the first, then sees (and replaces) what the first made, never adding a second."""
	frappe.db.sql("SELECT name FROM `tabSales Order Item` WHERE name = %s FOR UPDATE", soi)
	return frappe.db.sql(
		"""SELECT name, farm FROM `tabAllocation Confirmation`
		WHERE sales_order_item = %s AND source = %s AND status IN %s FOR UPDATE""",
		(soi, source, tuple(LIVE)),
		as_dict=True,
	)


def reserve_real(
	*, plan, line, farm, variety, length, stems, user=None, team=None, harvested_from=None, forecast_basis=""
):
	"""Plan up to `stems` of a line from shelf stock. Returns (confirmation or None, confirmed stems).

	`line` is a dict with sales_order, sales_order_item, customer, ordered_variety, ordered_length, mix_key.
	Replaces any earlier Real plan of the same line. Confirms only what is free once other lines' Real stems
	are counted off, so the confirmed stems can be less than asked; the caller plans the rest as Virtual."""
	soi = line["sales_order_item"]
	for existing in _live_of_line(soi, "Real"):
		release_confirmation(existing.name, "Manual", user=user, keep_open=True)
		if (
			existing.farm and existing.farm != farm
		):  # confirmed against another farm before: its mirror row must go
			refresh_mirror(line["sales_order"], soi, existing.farm)

	_lock_pool(variety)
	free = sum(b.free for b in free_for_line(farm, variety, length, soi, harvested_from))
	free -= _planned_elsewhere(farm, variety, length, soi)
	held = max(0.0, min(flt(stems), free))
	if held <= 0:
		refresh_mirror(line["sales_order"], soi, farm)
		return None, 0.0

	conf = _confirmation(
		plan, line, "Real", held, farm, variety, length, forecast_basis=forecast_basis, team=team
	)
	refresh_mirror(line["sales_order"], soi, farm)
	return conf, held


def plan_virtual(
	*, plan, line, farm, variety, length, stems, forecast_basis="", team=None, harvest_stems=0.0
):
	"""Plan stems from the expected harvest. Stems only, no buckets."""
	soi = line["sales_order_item"]
	for existing in _live_of_line(soi, "Virtual"):
		release_confirmation(existing.name, "Manual", keep_open=True)
		if existing.farm and existing.farm != farm:
			refresh_mirror(line["sales_order"], soi, existing.farm)
	if flt(stems) <= 0:
		refresh_mirror(line["sales_order"], soi, farm)
		return None
	conf = _confirmation(
		plan,
		line,
		"Virtual",
		flt(stems),
		farm,
		variety,
		length,
		forecast_basis=forecast_basis,
		team=team,
		harvest=min(flt(harvest_stems), flt(stems)),
	)
	refresh_mirror(line["sales_order"], soi, farm)
	return conf


def _confirmation(
	plan, line, source, stems, farm, variety, length, forecast_basis="", team=None, harvest=0.0
):
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


def teams_for_mixes(mix_keys):
	"""{mix_key: team} already chosen for these mixed boxes (group_team for many at once)."""
	keys = [k for k in mix_keys or () if k]
	if not keys:
		return {}
	rows = frappe.get_all(
		"Allocation Confirmation",
		filters={"mix_key": ("in", keys), "status": ("in", LIVE), "team": ("!=", "")},
		fields=["mix_key", "team"],
		order_by="modified asc",
	)
	return {r.mix_key: r.team for r in rows}


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
	# an order taken out of this plan but planned on another (its team moved it to another sales farm) keeps the
	# number that plan gave it
	elsewhere = set(
		frappe.db.sql_list(
			"""SELECT DISTINCT sales_order FROM `tabAllocation Confirmation`
			   WHERE sales_order IN %(s)s AND plan != %(plan)s
			     AND (status IN %(kept)s OR (status = 'Released' AND release_reason = 'Allocated'))""",
			{
				"s": tuple(r.sales_order for r in rows if r.sales_order not in want) or ("",),
				"plan": plan,
				"kept": (*LIVE, "Fulfilled"),
			},
		)
	)
	for r in rows:
		if r.sales_order in elsewhere:
			continue
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
	"""Close the plan entry. `keep_open` is for callers about to replace it."""
	conf = frappe.db.get_value(
		"Allocation Confirmation",
		name,
		[
			"source",
			"farm",
			"sales_order",
			"sales_order_item",
			"variety",
			"length",
			"stems",
			"team",
			"delivery_date",
		],
		as_dict=True,
	)
	if conf:
		conf.name = name
	# replacements ("Manual", "Revised", a hold replacing its expected harvest) are logged by the step doing them
	if conf and not keep_open and reason not in ("Manual", "Revised"):
		_log(
			"Plan released",
			"{0} {1}: {2} {3} stems released -- {4}".format(
				conf.variety, conf.length or "", int(flt(conf.stems)), conf.source.lower(), reason
			),
			conf,
			stems_before=conf.stems,
			stems_after=0,
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
	"""Allocation took `qty` stems of `bucket_id` for line `soi`: use up what was confirmed for it, the stems
	confirmed from stock first, then the expected harvest."""
	qty = flt(qty)
	taken_by = defaultdict(float)
	confs = sorted(live_confirmations(soi=soi), key=lambda c: (c.source != "Real", str(c.creation), c.name))
	for c in confs:
		if qty <= 0.0001:
			break
		used = min(max(0.0, flt(c.stems) - flt(c.fulfilled_stems)), qty)
		if used > 0:
			taken_by[c.name] += used
			qty -= used
	for conf, used in taken_by.items():
		done = flt(frappe.db.get_value("Allocation Confirmation", conf, "fulfilled_stems")) + used
		frappe.db.set_value("Allocation Confirmation", conf, "fulfilled_stems", done)
		_set_fulfilment_status(conf)


def _set_fulfilment_status(conf):
	stems, done = frappe.db.get_value("Allocation Confirmation", conf, ["stems", "fulfilled_stems"])
	frappe.db.set_value(
		"Allocation Confirmation",
		conf,
		"status",
		"Fulfilled" if flt(done) >= flt(stems) - 0.0001 else "Partly Fulfilled",
	)


def release_line(soi, reason="Allocated"):
	"""A line is fully allocated: whatever is still held or planned for it is no longer needed."""
	for c in live_confirmations(soi=soi):
		if c.status in ("Partly Fulfilled", "Fulfilled"):
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


def _log(stage, message, conf, **fields):
	"""An Allocation Log line about a plan entry `conf` (its order, line, variety, length, team and farm)."""
	from upande_packhouse.allocation_log import log

	log(
		stage,
		message,
		sales_order=conf.get("sales_order"),
		sales_order_item=conf.get("sales_order_item"),
		item_code=conf.get("variety"),
		stem_length=conf.get("length"),
		team=conf.get("team"),
		farm=conf.get("farm"),
		reference_doctype="Allocation Confirmation" if conf.get("name") else None,
		reference_name=conf.get("name"),
		**fields,
	)


def replan_split(splits):
	"""Carry the plan along when Sales Allocation splits an order line (a variety split, a mix split by boxes, a
	variety spread beside a line): those write the lines straight to the table, so release_changed_lines never
	sees them, and the plan kept confirming the old line in full while the new lines had none.

	`splits` = [(source line, [new lines made from it])], read after the split. Each confirmation of a source is
	shared out in proportion to the stems: the source keeps its share; a new line of the same variety takes its
	share of the same confirmation; a new line of another variety (a
	substitute) is planned from the expected harvest for its share, and the old variety's share is given back.
	Same plan, farm and team. A line that was never planned stays unplanned."""
	from upande_packhouse.api.v2 import allocation_guards as guards

	fields = [
		"name",
		"parent",
		"item_code",
		"stock_qty",
		"custom_length",
		"custom_mixed_box",
		"custom_mixed_bunch",
		"custom_mix_group",
		"custom_bunch_group",
	]
	touched = set()
	before_of = {}
	for source, new_lines in splits:
		live = live_confirmations(soi=source)
		if not live:
			continue
		before_of[source] = (sum(flt(c.stems) for c in live), max(live, key=lambda c: flt(c.stems)).team)
		src = frappe.db.get_value("Sales Order Item", source, fields, as_dict=True)
		news = [frappe.db.get_value("Sales Order Item", n, fields, as_dict=True) for n in new_lines or []]
		news = [n for n in news if n]
		before = flt(src.stock_qty) + sum(flt(n.stock_qty) for n in news)
		if before <= 0:
			continue
		planned = sum(flt(c.stems) for c in live)
		main = max(live, key=lambda c: flt(c.stems))
		for c in live:
			touched.add((c.sales_order, source, c.farm))
			# whole stems: the source's share, then each new line of the same variety; what is left (the old
			# variety's share of a substituted line) is given back
			keep, parts = _shares(c, src, news, before)
			_share_out(c, keep, parts, guards._mix_key)
			touched.update((c.sales_order, n.name, c.farm) for n, _st in parts)
		# a line now holding another variety (a substitute, or the source itself replaced) is planned from the
		# expected harvest for its share of what was planned
		plan = frappe.get_doc("Allocation Plan", main.plan)
		for ln in [src, *news]:
			if any((c.variety or "") == (ln.item_code or "") for c in live):
				continue
			stems = flt(ln.stock_qty) * planned / before
			conf = _confirmation(
				plan,
				{
					"sales_order": ln.parent,
					"sales_order_item": ln.name,
					"customer": main.customer,
					"mix_key": guards._mix_key(ln),
					"ordered_variety": ln.item_code,
					"ordered_length": ln.custom_length,
				},
				"Virtual",
				stems,
				main.farm,
				ln.item_code,
				ln.custom_length,
				forecast_basis=main.forecast_basis or "",
				team=main.team,
			)
			frappe.db.set_value("Allocation Confirmation", conf.name, "flag", "Other variety")
			touched.add((ln.parent, ln.name, main.farm))
	for sales_order, soi, farm in touched:
		refresh_mirror(sales_order, soi, farm)
	for source, new_lines in splits:
		now = {
			soi: sum(flt(c.stems) for c in live_confirmations(soi=soi))
			for soi in [source, *(new_lines or [])]
		}
		if source in before_of:
			from upande_packhouse.allocation_log import log

			was, team = before_of[source]
			log(
				"Planned",
				"Plan moved with the line: "
				+ ", ".join(
					"{0} {1}".format(frappe.db.get_value("Sales Order Item", soi, "item_code"), int(stems))
					for soi, stems in now.items()
				),
				sales_order_item=source,
				team=team,
				stems_before=was,
				stems_after=sum(now.values()),
				details={"plan_after": now},
			)


def _shares(c, src, news, before):
	"""Whole-stem shares of confirmation `c` for the source and each new line of its variety, in proportion to
	their stems: rounded down, then the stems left go one by one to the largest remainders, so the shares never
	add up to more than `c` (rounding each share up could confirm a stem more than was planned)."""
	takers = ([src] if c.variety == src.item_code else []) + [n for n in news if n.item_code == c.variety]
	exact = [flt(c.stems) * flt(t.stock_qty) / before for t in takers]
	whole = [int(x) for x in exact]
	left = round(sum(exact)) - sum(whole)
	for i in sorted(range(len(exact)), key=lambda i: exact[i] - whole[i], reverse=True)[: max(0, left)]:
		whole[i] += 1
	shares = list(zip(takers, whole, strict=True))
	keep = shares.pop(0)[1] if takers and takers[0] is src else 0
	return keep, shares


def _share_out(conf, keep, parts, mix_key):
	"""Share a live confirmation out after a split: it keeps `keep` stems, each (new line, stems) in `parts` takes
	a confirmation of its own for its stems, and the rest is given back."""
	for line, stems in parts:
		if stems <= 0:
			continue
		doc = frappe.copy_doc(frappe.get_doc("Allocation Confirmation", conf.name))
		doc.update(
			{
				"sales_order_item": line.name,
				"mix_key": mix_key(line),
				"stems": flt(stems),
				"fulfilled_stems": 0,
				"ordered_variety": line.item_code,
				"ordered_length": line.custom_length,
			}
		)
		doc.insert(ignore_permissions=True)
	if keep <= 0:
		release_confirmation(conf.name, "Line split", keep_open=True)
	else:
		frappe.db.set_value("Allocation Confirmation", conf.name, "stems", flt(keep))


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
	for p in frappe.get_all(
		"Allocation Plan", filters={"status": ("!=", "Closed"), "delivery_date": ("<", today())}, pluck="name"
	):
		frappe.db.set_value("Allocation Plan", p, "status", "Closed")


# ---------------------------------------------------------------- expected stock reaches the shelf


def convert_virtual(delivery_date=None, farm=None):
	"""Expected stems become real as the flowers reach the shelf: every confirmation planned from the forecast is
	confirmed from stock for as many stems of its cutting day's harvest (or later) as the shelf can now give that
	line (oldest plans first), and keeps only the rest as expected. Older stock is never counted for a plan.
	Returns the stems converted."""
	filters = {"source": "Virtual", "status": "Planned"}
	if delivery_date:
		filters["delivery_date"] = delivery_date
	if farm:  # the sales farm: its plan holds every confirmation, whichever farm's stock they are against
		filters["plan"] = (
			"in",
			frappe.get_all("Allocation Plan", filters={"farm": farm}, pluck="name") or ["-"],
		)
	converted = 0.0
	for c in frappe.get_all(
		"Allocation Confirmation", filters=filters, fields=["*"], order_by="creation asc"
	):
		soi = c.sales_order_item
		# held now: a held confirmation's stems less what of them is already allocated (fulfilled) -- those are
		# the line's allocation, not stems still to hold, so they never make the line hold more than it needs
		held_now = frappe.db.get_value(
			"Allocation Confirmation",
			{"sales_order_item": soi, "source": "Real", "status": ("in", LIVE)},
			["stems", "fulfilled_stems"],
			as_dict=True,
		)
		real = max(0.0, flt(held_now.stems) - flt(held_now.fulfilled_stems)) if held_now else 0.0
		cut = add_days(c.delivery_date, -HARVEST_LEAD_DAYS)
		free = sum(b.free for b in free_for_line(c.farm, c.variety, c.length, soi, cut))
		free -= _planned_elsewhere(c.farm, c.variety, c.length, soi)
		# what this expected-harvest confirmation still has to cover: its stems already allocated (fulfilled)
		# are the line's allocation, never held a second time
		expected = max(0.0, flt(c.stems) - flt(c.fulfilled_stems))
		target = min(real + expected, free)
		if target <= real + 0.0001:
			continue
		# one line that cannot be held (a bucket record missing, a validation) must not stop the others, nor
		# the page that converts on load
		frappe.db.savepoint("convert_virtual")
		try:
			plan = frappe.get_doc("Allocation Plan", c.plan)
			line = {
				"sales_order": c.sales_order,
				"sales_order_item": soi,
				"customer": c.customer,
				"ordered_variety": c.ordered_variety,
				"ordered_length": c.ordered_length,
				"mix_key": c.mix_key,
			}
			# the hold replaces this expected-harvest confirmation (released right after): it is not counted
			# beside it against the line's order (Allocation Confirmation.validate_not_above_order)
			frappe.flags.ac_replacing = {c.name}
			try:
				_conf, held = reserve_real(
					plan=plan,
					line=line,
					farm=c.farm,
					variety=c.variety,
					length=c.length,
					stems=target,
					team=c.team,
					harvested_from=cut,
					forecast_basis=c.forecast_basis,
				)
			finally:
				frappe.flags.ac_replacing = None
			release_confirmation(c.name, "Now on the shelf", keep_open=True)
			rest = real + expected - held
			if rest > 0.0001:
				plan_virtual(
					plan=plan,
					line=line,
					farm=c.farm,
					variety=c.variety,
					length=c.length,
					stems=rest,
					forecast_basis=c.forecast_basis,
					team=c.team,
					harvest_stems=min(rest, flt(c.from_harvest)),
				)
			refresh_mirror(c.sales_order, soi, c.farm)
			if held - real > 0.0001:
				_log(
					"Held from shelf",
					"{0} {1}: {2} stems of the expected harvest reached the shelf and are held ({3} still expected)".format(
						c.variety, c.length or "", int(held - real), int(max(0.0, rest))
					),
					c,
					stems_before=real,
					stems_after=held,
				)
		except Exception:
			frappe.db.rollback(save_point="convert_virtual")
			frappe.clear_messages()
			frappe.log_error(f"Allocation plan: could not convert {soi}", frappe.get_traceback())
			continue
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


def _confirmed_row_details(soi):
	"""What a line's Confirmed Stems row shows beside its stems: the line's variety, the team it is confirmed for
	and the sales farm that packs it (the team's). Only the fields the table has."""
	from upande_packhouse.api.v2.allocation_teams import team_sales_farms

	meta = frappe.get_meta("Confirmed Stems")
	team = team_for_line(soi)
	details = {
		"item_code": frappe.db.get_value("Sales Order Item", soi, "item_code"),
		"team": team,
		"processing_sales_farm": team_sales_farms().get(team) if team else None,
	}
	return {k: v for k, v in details.items() if meta.has_field(k)}


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
	names = frappe.get_all("Confirmed Stems", filters=filters, pluck="name", order_by="creation asc")
	name = names[0] if names else None
	shown = _confirmed_row_details(soi)
	# one row per line and farm: copies left by a confirmation saved twice go
	for extra in names[1:]:
		frappe.db.delete("Confirmed Stems", {"name": extra})
	if flt(stems) <= 0:
		if name:
			frappe.db.delete("Confirmed Stems", {"name": name})
		return
	if name:
		frappe.db.set_value("Confirmed Stems", name, {"stems": flt(stems), **shown})
		return
	idx = cint(
		frappe.db.sql(
			"""SELECT COALESCE(MAX(idx), 0) FROM `tabConfirmed Stems`
			   WHERE parent = %s AND parenttype = 'Sales Order' AND parentfield = 'custom_confirmed_stems_table'""",
			sales_order,
		)[0][0]
	)
	row = frappe.get_doc(
		{"doctype": "Confirmed Stems", **filters, **shown, "stems": flt(stems), "idx": idx + 1}
	)
	row.name = frappe.generate_hash(length=10)
	row.owner = row.modified_by = frappe.session.user
	row.creation = row.modified = now_datetime()
	row.db_insert()


# ---------------------------------------------------------------- setup


def setup_settings():
	"""Seed Production Settings > Allocation Planning > Planning (upande_quality's fields) once. A value the
	business has already saved (even 0) is never touched."""
	if not frappe.db.exists("DocType", "Production Settings"):
		return
	for name, default in SETTING_DEFAULTS.items():
		if _stored(name) is None:
			frappe.db.set_single_value("Production Settings", name, default)
	frappe.db.delete(
		"Singles",
		{
			"doctype": "Production Settings",
			"field": ["in", ["allocation_forecast_source", "allocation_harvest_lead_days"]],
		},
	)


def ensure_indexes():
	"""Indexes the allocation plan reads by."""
	ensure_bucket_activity_index()


#: Stock Entry rows are looked up by bucket for the Watchlist and the Bucket Journey. The covering index answers that from
#: the index alone (about 20x faster on 3.8 million rows: 5.4 s to 0.3 s for the open watches); without it every hit is a
#: random read of the wide Stock Entry row.
BUCKET_ACTIVITY_INDEX = "idx_bucket_activity"
BUCKET_ACTIVITY_COLUMNS = [
	"custom_bucket_id",
	"posting_date",
	"docstatus",
	"stock_entry_type",
	"farm",
	"posting_time",
	"creation",
]


def ensure_bucket_activity_index():
	if not frappe.db.has_column("Stock Entry", "custom_bucket_id"):
		return
	existing = {r[2] for r in frappe.db.sql("SHOW INDEX FROM `tabStock Entry`")}
	if BUCKET_ACTIVITY_INDEX in existing:
		return
	cols = [c for c in BUCKET_ACTIVITY_COLUMNS if frappe.db.has_column("Stock Entry", c)]
	frappe.db.sql_ddl(
		"ALTER TABLE `tabStock Entry` ADD INDEX `%s` (%s)"
		% (BUCKET_ACTIVITY_INDEX, ", ".join("`%s`" % c for c in cols))
	)
