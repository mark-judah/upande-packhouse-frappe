# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `packhouse-scheduler` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


def _own_packing_farms():
	"""Sales farms other than the transfer hub (e.g. Karen): they pack their own
	orders with no remote transfers, so the scheduler leaves those orders alone."""
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	hub = transfer_hub(required=False)
	if not hub:
		return []
	ps = frappe.get_cached_doc("Production Settings")
	return [
		r.farm for r in (ps.shelf_locations or []) if r.enabled and r.sales_shelf and r.farm and r.farm != hub
	]


def _tiers(opls):
	"""opl -> sort key for its team's schedule:
	  (0,)  fully issued
	  (1,)  every bucket already at the hub (nothing awaiting, loaded or in transit)
	  (2, …) still to come from remote farms, one block per farm in the order its truck
	        left the hub (on the way back, then dispatched to the farm by when it left,
	        then planned trips in run order, then no trip yet, by farm). An order
	        waiting at several farms goes with the one that arrives last.
	The schedule runs in that order, so packing follows the trucks."""
	from upande_packhouse.api.remote_transfer.transfer_scheduling import FARM_EXPR
	from upande_packhouse.mobile.api import _issue_progress

	opls = list(opls or [])
	if not opls:
		return {}
	waiting, on_truck = {}, set()
	for r in frappe.db.sql(
		"""SELECT pli.parent AS opl, """
		+ FARM_EXPR
		+ """ AS farm, GREATEST(IFNULL(pli.loaded_in_trolley, 0), IFNULL(pli.in_transit, 0)) AS moving
		FROM `tabPick List Item` pli
		WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(o)s
		  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)""",
		{"o": tuple(opls)},
		as_dict=True,
	):
		waiting.setdefault(r.opl, set()).add(r.farm or "")
		if r.moving:
			on_truck.add((r.opl, r.farm or ""))
	trip_key = {}
	if waiting:
		far = "9999-12-31 00:00:00"
		for r in frappe.db.sql(
			"""SELECT o.order_pick_list AS opl, o.farm, t.name, t.status, t.run,
			       t.released_at, t.dispatched_at, t.creation
			FROM `tabBucket Request Trip Order` o
			JOIN `tabBucket Request Trip` t ON t.name = o.parent
			WHERE o.order_pick_list IN %(o)s AND t.status IN ('Draft', 'Scheduled', 'Dispatched')
			  AND t.trip_date >= %(since)s""",
			{"o": tuple(waiting), "since": frappe.utils.add_days(frappe.utils.today(), -1)},
			as_dict=True,
		):
			left = str(r.released_at or r.dispatched_at or far)
			rank = {"Dispatched": 0, "Scheduled": 1}.get(r.status, 2)
			key = (rank, left if rank < 2 else far, int(r.run or 0), str(r.creation), r.name)
			k = (r.opl, r.farm or "")
			if k not in trip_key or key < trip_key[k]:
				trip_key[k] = key
	issued = {o for o, d in _issue_progress(opls).items() if d["total"] and d["issued"] >= d["total"]}
	out = {}
	for o in opls:
		if o in issued:
			out[o] = (0,)
		elif o not in waiting:
			out[o] = (1,)
		else:
			out[o] = (2,) + max(trip_key.get((o, f)) or _no_trip_key(o, f, on_truck) for f in waiting[o])
	return out


def _no_trip_key(opl, farm, on_truck):
	"""A farm portion on no trip record: already on a truck counts as on the road
	(after the trips with a time); still at the farm goes last, grouped by farm."""
	if (opl, farm) in on_truck:
		return (0, "9999-12-31 00:00:00", 0, "", farm)
	return (3, farm, 0, "", "")


def _keep_own_slots(others, own):
	"""One team's schedule in order: Karen-type rows (`own`, each with its current
	`sequence`) stay at their numbers; `others` fill the remaining slots in order."""
	total = len(others) + len(own)
	slots = [None] * total
	late = []
	for r in sorted(own, key=lambda r: r["sequence"]):
		i = r["sequence"] - 1
		if 0 <= i < total and slots[i] is None:
			slots[i] = r
		else:
			late.append(r)
	fill = iter(others + late)
	return [s if s is not None else next(fill) for s in slots]


def rank_schedules(sdate=None, replan=True):
	"""Scheduler job (every 5 minutes): keep today's Packhouse Schedules in _tiers
	order as orders finish issuing and their buckets reach the hub. Karen-type orders
	(packed at their own sales farm) keep their places after the others."""
	sdate = sdate or frappe.utils.today()
	own_farms = _own_packing_farms()
	changed = []
	for name in frappe.get_all("Packhouse Schedule", filters={"schedule_date": sdate}, pluck="name"):
		doc = frappe.get_doc("Packhouse Schedule", name)
		rows = sorted(doc.orders or [], key=lambda r: int(r.sequence or 0))
		opls = [r.order_pick_list for r in rows if r.order_pick_list]
		own = set()
		if own_farms and opls:
			own = set(
				frappe.db.sql_list(
					"""SELECT o.name FROM `tabOrder Pick List` o
					JOIN `tabSales Order` so ON so.name = o.sales_order
					WHERE o.name IN %(o)s AND so.farm IN %(f)s""",
					{"o": tuple(opls), "f": tuple(own_farms)},
				)
			)
		tier = _tiers([o for o in opls if o not in own])
		others = sorted(
			[{"r": r} for r in rows if r.order_pick_list not in own],
			key=lambda x: tier.get(x["r"].order_pick_list, (2,)),
		)
		own_rows = [
			{"r": r, "sequence": pos} for pos, r in enumerate(rows, start=1) if r.order_pick_list in own
		]
		ranked = [x["r"] for x in _keep_own_slots(others, own_rows)]
		if [r.name for r in ranked] == [r.name for r in rows] and all(
			int(r.sequence or 0) == i for i, r in enumerate(rows, start=1)
		):
			continue
		# Only the numbers change: a row whose order was since deleted must not stop it.
		for i, r in enumerate(ranked, start=1):
			frappe.db.set_value(
				"Packhouse Schedule Order", r.name, {"sequence": i, "idx": i}, update_modified=False
			)
		changed.append(name)
	if changed and replan:
		from upande_packhouse.api.auto_transfer import replan_soon

		replan_soon()
	if changed:
		# A scheduler job does not commit by itself.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return changed


@frappe.whitelist()
def getScheduledOrders():
	# Frappe Server Script (API), api_method = getScheduledOrders
	# Flat map of every order on a Packhouse Schedule for a date -> {team, sequence}.
	# Powers the scheduler's scheduled/unscheduled marking (default = unscheduled).
	# Params: { date? } -> { status, date, scheduled: { <opl>: {team, sequence} } }
	fd = frappe.form_dict
	sdate = fd.get("date") or frappe.utils.today()
	scheduled = {}
	names = frappe.get_all("Packhouse Schedule", filters={"schedule_date": sdate}, pluck="name")
	i = 0
	while i < len(names):
		doc = frappe.get_doc("Packhouse Schedule", names[i])
		rows = doc.orders or []
		j = 0
		while j < len(rows):
			r = rows[j]
			if r.order_pick_list:
				scheduled[r.order_pick_list] = {"team": doc.team, "sequence": int(r.sequence or 0)}
			j = j + 1
		i = i + 1
	frappe.response["message"] = {"status": "success", "date": str(sdate), "scheduled": scheduled}


@frappe.whitelist()
def getSchedulerFeed():
	# Frappe Server Script (Type: API), api_method = getSchedulerFeed
	# Unified feed for the redesigned Packhouse Scheduler (2 tabs: Unscheduled + Schedule).
	# Returns every SCHEDULABLE Order Pick List for a date = a draft (docstatus 0) still
	# waiting on a remote transfer, with ZERO issued buckets — plus any order already on
	# a schedule, so saving never drops it. Once ANY bucket is issued the order is being
	# processed and drops off entirely (user rule).
	# Each row carries what both tabs need: team, stems, bucket count, farm count, the
	# transfer state, and the mixed-type (from the Sales Order Items).
	# Variations (for the Unscheduled tab's show/hide control):
	#   draft_plain    - docstatus 0, no transfer activity on any line
	#   draft_transfer - docstatus 0, at least one line awaiting/loaded/in-transit
	#   submitted      - docstatus 1 (not yet issued)
	# Mixed-type (from Sales Order Item of the OPL):
	#   Mixed bunch  - any SO item custom_mixed_bunch = 1
	#   Mixed box    - any SO item custom_mixed_box = 1 (or OPL custom_is_mixed_box_pick_list)
	#   Straight box - otherwise
	# Payload: { date: "YYYY-MM-DD" }
	# safe_exec: no import / no += / no def / no f-strings. for-loops + .append + .get ok.

	frappe.response["message"] = {"success": False, "error": "Script failed"}

	try:
		fd = frappe.form_dict
		dd = fd.get("date") or frappe.utils.today()

		takt = frappe.db.get_single_value("Production Settings", "takt_time") or 0

		# `dd` is the DELIVERY date. An OPL's shipping/delivery date lives on its Sales
		# Order (OPL has none of its own), so join it. The Schedule tab reads/writes the
		# Packhouse Schedule under the PROCESSING day (delivery - 1) — handled client-side.
		opls = frappe.db.sql(
			"""
            SELECT o.name AS name, o.customer AS customer, o.order_name AS custom_order_name,
                   o.team AS custom_team, o.custom_total_stems AS custom_total_stems,
                   o.docstatus AS docstatus, 0 AS custom_is_mixed_box_pick_list,
                   o.creation AS creation
            FROM `tabOrder Pick List` o
            JOIN `tabSales Order` so ON so.name = o.sales_order
            WHERE so.delivery_date = %(dd)s AND o.docstatus < 2
              AND IFNULL(so.farm, '') NOT IN %(own)s
            ORDER BY o.creation ASC
        """,
			{"dd": dd, "own": tuple(_own_packing_farms()) or ("",)},
			as_dict=True,
		)

		names = []
		oi = 0
		while oi < len(opls):
			names.append(opls[oi].name)
			oi = oi + 1

		# ---- per-OPL line stats (buckets, farms, transfer, issued) ----
		stats = {}
		if len(names) > 0:
			plis = frappe.get_all(
				"Pick List Item",
				filters=[["parent", "in", names]],
				fields=[
					"parent",
					"bucket",
					"source_warehouse",
					"farm",
					"item_code",
					"issued",
					"awaiting_transfer",
					"loaded_in_trolley",
					"in_transit",
				],
				limit_page_length=0,
			)
			pj = 0
			while pj < len(plis):
				r = plis[pj]
				op = r.get("parent")
				st = stats.get(op)
				if not st:
					st = {
						"buckets": {},
						"farms": {},
						"waiting": {},
						"varieties": {},
						"transfer": 0,
						"issued": 0,
						"lines": 0,
					}
					stats[op] = st
				b = r.get("bucket")
				if b:
					st["buckets"][b] = 1
				# The pick row's own farm (where the bucket's shelf is); the warehouse
				# name is only a fallback — remote farms may receive into the hub's store.
				farm = (r.get("farm") or "").strip()
				wh = (r.get("source_warehouse") or "").strip()
				if not farm and wh:
					farm = wh.split(" ")[0]
				if farm:
					st["farms"][farm] = 1
					# Buckets still waiting at the farm for a truck, per farm.
					if b and int(r.get("awaiting_transfer") or 0) == 1:
						st["waiting"].setdefault(farm, {})[b] = 1
				v = r.get("item_code")
				if v:
					st["varieties"][v] = 1
				st["lines"] = st["lines"] + 1
				if int(r.get("issued") or 0) == 1:
					st["issued"] = st["issued"] + 1
				xf = (
					int(r.get("awaiting_transfer") or 0)
					+ int(r.get("loaded_in_trolley") or 0)
					+ int(r.get("in_transit") or 0)
				)
				if xf > 0:
					st["transfer"] = 1
				pj = pj + 1

		# ---- mixed-type from Sales Order Items ----
		mixed = {}
		if len(names) > 0:
			soi = frappe.get_all(
				"Sales Order Item",
				filters=[["custom_opl", "in", names]],
				fields=["custom_opl", "custom_mixed_box", "custom_mixed_bunch"],
				limit_page_length=0,
			)
			sj = 0
			while sj < len(soi):
				s = soi[sj]
				op = s.get("custom_opl")
				m = mixed.get(op)
				if not m:
					m = {"box": 0, "bunch": 0}
					mixed[op] = m
				if int(s.get("custom_mixed_box") or 0) == 1:
					m["box"] = 1
				if int(s.get("custom_mixed_bunch") or 0) == 1:
					m["bunch"] = 1
				sj = sj + 1

		# Only orders still waiting on a remote transfer need scheduling: a draft OPL
		# with buckets awaiting / loaded / in transit. Submitted OPLs and drafts with
		# nothing to move are left out — unless already on a schedule: saving rebuilds
		# the whole day from this list, so dropping a scheduled order would unschedule it.
		on_schedule = {}
		if len(names) > 0:
			for sop in frappe.get_all(
				"Packhouse Schedule Order",
				filters=[["order_pick_list", "in", names]],
				pluck="order_pick_list",
				limit_page_length=0,
			):
				on_schedule[sop] = 1

		# ---- stems packed per OPL (Farm Pack List rows, as on the Order Summary) ----
		packed = {}
		if len(names) > 0:
			for pr in frappe.db.sql(
				"""
				SELECT fpl.order_pick_list AS opl, SUM(IFNULL(fpi.stock_qty, 0)) AS stems
				FROM `tabFarm Pack List` fpl
				JOIN `tabFarm Packlist Item` fpi ON fpi.parent = fpl.name
					AND fpi.parenttype = 'Farm Pack List' AND fpi.parentfield = 'pack_list_item'
				WHERE fpl.docstatus != 2 AND fpl.order_pick_list IN %(names)s
				GROUP BY fpl.order_pick_list
				""",
				{"names": names},
				as_dict=True,
			):
				packed[pr.opl] = float(pr.stems or 0)

		# ---- trips to each farm, for the schedule popup ----
		# Trips run the PROCESSING day (delivery - 1). A trip goes to every farm on its
		# collection order (falling back to its order rows' farms). Only Draft / Scheduled
		# trips are listed — they can still take buckets. on_trip = this order's buckets on it.
		hub = frappe.get_cached_doc("Production Settings").get("transfer_hub_farm") or ""
		trip_date = frappe.utils.add_days(dd, -1)
		trips = frappe.get_all(
			"Bucket Request Trip",
			filters={"trip_date": trip_date, "status": ["in", ["Draft", "Scheduled"]]},
			fields=["name", "vehicle", "status", "collection_order", "run"],
			order_by="creation asc",
			limit_page_length=0,
		)
		trip_rows = {}
		if trips:
			for tr in frappe.get_all(
				"Bucket Request Trip Order",
				filters=[["parent", "in", [t.name for t in trips]], ["unscheduled", "!=", 1]],
				fields=["parent", "order_pick_list", "farm", "buckets"],
				limit_page_length=0,
			):
				trip_rows.setdefault(tr.parent, []).append(tr)
		farm_trips = {}
		for t in trips:
			stops = [x.strip() for x in (t.collection_order or "").split("→") if x.strip()]
			for tr in trip_rows.get(t.name) or []:
				if tr.farm and tr.farm not in stops:
					stops.append(tr.farm)
			for fm in stops:
				if fm != hub:
					farm_trips.setdefault(fm, []).append(t)

		out = []
		ci = 0
		while ci < len(opls):
			o = opls[ci]
			op = o.name
			st = stats.get(op) or {
				"buckets": {},
				"farms": {},
				"varieties": {},
				"transfer": 0,
				"issued": 0,
				"lines": 0,
			}

			# Every order is scheduled -- drafts waiting on a transfer, drafts with nothing
			# to move, submitted orders issued straight from the hub -- and stays on the
			# schedule through issuing and packing (shown packed once it is). A fully issued
			# order that was never on a schedule has nothing left to sequence: it is left out.
			if st["lines"] and st["issued"] >= st["lines"] and not on_schedule.get(op):
				ci = ci + 1
				continue

			m = mixed.get(op) or {"box": 0, "bunch": 0}
			if m["bunch"] == 1:
				mtype = "Mixed bunch"
			elif m["box"] == 1 or int(o.get("custom_is_mixed_box_pick_list") or 0) == 1:
				mtype = "Mixed box"
			else:
				mtype = "Straight box"

			ds = int(o.get("docstatus") or 0)
			has_x = st["transfer"]
			if ds == 0 and has_x == 0:
				variation = "draft_plain"
			elif ds == 0 and has_x == 1:
				variation = "draft_transfer"
			else:
				variation = "submitted"

			stems = 0
			try:
				stems = int(float(o.get("custom_total_stems") or 0))
			except:
				stems = 0

			farm_list = sorted(st["farms"].keys())

			waiting = []
			for fm in sorted(st.get("waiting", {}).keys()):
				if fm == hub:
					continue
				ft = []
				for t in farm_trips.get(fm) or []:
					on_trip = 0
					for tr in trip_rows.get(t.name) or []:
						if tr.order_pick_list == op and (tr.farm or "") == fm:
							on_trip = on_trip + int(tr.buckets or 0)
					ft.append(
						{
							"trip": t.name,
							"vehicle": t.vehicle or "",
							"status": t.status,
							"run": int(t.run or 0),
							"route": t.collection_order or "",
							"on_trip": on_trip,
						}
					)
				waiting.append({"farm": fm, "buckets": len(st["waiting"][fm]), "trips": ft})

			row = {
				"opl": op,
				"order_name": o.get("custom_order_name") or op,
				"customer": o.get("customer") or "",
				"team": o.get("custom_team") or "",
				"total_stems": stems,
				"n_buckets": len(st["buckets"]),
				"n_farms": len(farm_list),
				"farms": farm_list,
				"farm_trips": waiting,
				"n_varieties": len(st["varieties"]),
				"has_transfer": has_x,
				"mixed_type": mtype,
				"docstatus": ds,
				"variation": variation,
				"issued_lines": st["issued"],
				"lines": st["lines"],
				"packed_stems": int(packed.get(op) or 0),
				"packed": 1 if stems > 0 and packed.get(op, 0) >= stems else 0,
			}
			out.append(row)
			ci = ci + 1

		frappe.response["message"] = {
			"success": True,
			"data": out,
			"date": str(dd),
			"count": len(out),
			"takt_minutes": takt,
		}

	except Exception as e:
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist(methods=["POST"])
def saveDaySchedule():
	# Frappe Server Script (API), api_method = saveDaySchedule
	# Reorder IS the schedule: takes the global ordered ready list and rebuilds each
	# team's Packhouse Schedule doc (orders in the global order, resequenced 1..N per team).
	# Params: { date?, order }  order = "|~|"-joined OPL names in global order.
	fd = frappe.form_dict
	sdate = fd.get("date") or frappe.utils.today()
	order_raw = fd.get("order") or ""
	opls = []
	parts = order_raw.split("|~|") if order_raw else []
	p = 0
	while p < len(parts):
		v = (parts[p] or "").strip()
		if v:
			opls.append(v)
		p = p + 1

	# Karen-type orders (packed at their own sales farm, no remote transfers) are not
	# the scheduler's: never taken from the page, and their existing places are kept.
	own_farms = _own_packing_farms()
	own = set()
	if own_farms:
		own = set(
			frappe.db.sql_list(
				"""SELECT DISTINCT pso.order_pick_list FROM `tabPackhouse Schedule Order` pso
				JOIN `tabPackhouse Schedule` ps ON ps.name = pso.parent
				JOIN `tabOrder Pick List` o ON o.name = pso.order_pick_list
				JOIN `tabSales Order` so ON so.name = o.sales_order
				WHERE ps.schedule_date = %(d)s AND so.farm IN %(f)s""",
				{"d": sdate, "f": tuple(own_farms)},
			)
		)
		if opls:
			own.update(
				frappe.db.sql_list(
					"""SELECT o.name FROM `tabOrder Pick List` o
					JOIN `tabSales Order` so ON so.name = o.sales_order
					WHERE o.name IN %(o)s AND so.farm IN %(f)s""",
					{"o": tuple(opls), "f": tuple(own_farms)},
				)
			)
		opls = [o for o in opls if o not in own]

	# "Distribute to teams": orders that had no team get the one the page gave them
	# ({opl: team}, a Packing Team) -- a team's schedule can only hold its own orders.
	teams = fd.get("teams")
	if isinstance(teams, str):
		teams = frappe.parse_json(teams) if teams.strip() else {}
	for opl_name, team in (teams or {}).items():
		if team and opl_name in opls and frappe.db.exists("Packing Teams", team):
			if not frappe.db.get_value("Order Pick List", opl_name, "team"):
				frappe.db.set_value("Order Pick List", opl_name, "team", team)

	info_map = {}
	if opls:
		recs = frappe.get_all(
			"Order Pick List",
			filters={"name": ["in", opls]},
			fields=["name", "team", "order_name", "customer"],
			limit_page_length=0,
		)
		r = 0
		while r < len(recs):
			info_map[recs[r]["name"]] = recs[r]
			r = r + 1

	by_team = {}
	team_order = []
	# An OPL with no team can't go on any team's schedule. It used to be dropped
	# silently, and because transfer planning only sees scheduled orders, its
	# remote buckets never got a truck. Report it so the scheduler can say so.
	no_team = []
	i = 0
	while i < len(opls):
		info = info_map.get(opls[i])
		if info:
			team = info.get("team") or ""
			if team:
				if team not in by_team:
					by_team[team] = []
					team_order.append(team)
				by_team[team].append(opls[i])
			else:
				no_team.append(
					{
						"opl": opls[i],
						"order_name": info.get("order_name") or "",
						"customer": info.get("customer") or "",
					}
				)
		i = i + 1

	# Every save re-ranks each team (_tiers); the page's order is kept within a tier.
	tier = _tiers(opls)
	for team in by_team:
		by_team[team].sort(key=lambda opl: tier.get(opl, (2,)))

	# Fully issued orders are off the page (the feed drops them) but stay on their
	# team's schedule, ahead of the list in their old order: packing and the
	# schedule-order checks still need their place. Rebuilding from the page alone
	# wiped the sequence of every order that was ready to pack.
	kept = {}
	own_kept = {}
	old_rows = frappe.db.sql(
		"""SELECT ps.team, pso.order_pick_list AS opl, pso.order_name, pso.customer, pso.sequence
		FROM `tabPackhouse Schedule Order` pso JOIN `tabPackhouse Schedule` ps ON ps.name = pso.parent
		JOIN `tabOrder Pick List` o ON o.name = pso.order_pick_list AND o.docstatus < 2
		WHERE ps.schedule_date = %s ORDER BY ps.team, pso.sequence""",
		sdate,
		as_dict=True,
	)
	from upande_packhouse.mobile.api import _issue_progress

	done = _issue_progress([r.opl for r in old_rows if r.opl not in opls])
	for r in old_rows:
		if r.opl in own:
			own_kept.setdefault(r.team, []).append(r)
			if r.team not in by_team:
				by_team[r.team] = []
				team_order.append(r.team)
			continue
		d = done.get(r.opl)
		if r.opl not in opls and d and d["issued"] >= d["total"]:
			kept.setdefault(r.team, []).append(r)
			if r.team not in by_team:
				by_team[r.team] = []
				team_order.append(r.team)

	# The ordered list IS the whole day's schedule, so a team that no longer has any
	# order in it must be emptied too. Only rebuilding the teams present used to
	# leave stale rows behind: an order moved from Team A to Team B (or unscheduled
	# entirely) stayed on PSCH-<date>-Team A and showed up under two teams.
	cleared = []
	for stale in frappe.get_all(
		"Packhouse Schedule",
		filters={"schedule_date": sdate, "team": ["not in", team_order or [""]]},
		pluck="name",
	):
		doc = frappe.get_doc("Packhouse Schedule", stale)
		if doc.orders:
			doc.set("orders", [])
			doc.save(ignore_permissions=True)
			cleared.append(doc.team)

	saved = {}
	k = 0
	while k < len(team_order):
		team = team_order[k]
		name = "PSCH-" + str(sdate) + "-" + str(team)
		if frappe.db.exists("Packhouse Schedule", name):
			doc = frappe.get_doc("Packhouse Schedule", name)
		else:
			doc = frappe.new_doc("Packhouse Schedule")
			doc.schedule_date = sdate
			doc.team = team
		rows = by_team[team]
		others = [
			{"order_pick_list": r.opl, "order_name": r.order_name or "", "customer": r.customer or ""}
			for r in kept.get(team, [])
		] + [
			{
				"order_pick_list": opl,
				"order_name": (info_map.get(opl) or {}).get("order_name") or "",
				"customer": (info_map.get(opl) or {}).get("customer") or "",
			}
			for opl in rows
		]
		own_rows = [
			{
				"order_pick_list": r.opl,
				"order_name": r.order_name or "",
				"customer": r.customer or "",
				"sequence": int(r.sequence or 0),
			}
			for r in own_kept.get(team, [])
		]
		doc.set("orders", [])
		for seq, r in enumerate(_keep_own_slots(others, own_rows), start=1):
			doc.append("orders", {**r, "sequence": seq})
		doc.save(ignore_permissions=True)
		saved[team] = len(rows) + len(kept.get(team, []))
		k = k + 1
	# Committed explicitly: this endpoint is whitelisted without `methods`, so it
	# is reachable over GET, and frappe rolls back writes made during a GET
	# request -- without this the caller gets a success response and no change.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "saved": saved, "no_team": no_team, "cleared": cleared}
