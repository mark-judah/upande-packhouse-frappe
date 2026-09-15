# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `transfer-control` page API — ported from the kaitet-group v15 LIVE
# reference (never migrated to v16). Field names are translated throughout to
# their real v16 equivalents (the v15 scripts used a "custom_" prefix on several
# fields — Pick List Item's awaiting_transfer/loaded_in_trolley/in_transit/shelved/
# transit_truck/trolley_id and Order Pick List's team/schedule_number/item_group/
# mix_group/farm/order_name are all now BARE fieldnames in v16). Order Pick List's
# old custom_is_mixed_box_pick_list/custom_status/custom_consignee no longer exist
# at all — "mixed" is now derived from Sales Order Item.custom_mixed_box/
# custom_mixed_bunch (same derivation Packhouse Scheduler already uses), status/
# consignee are simply dropped.
#
# NEW v16 doctypes backing this page (none of these existed before this build):
#   Bucket Request Trip (+ child Bucket Request Trip Order)
#   Farm Distance
#   Bucket Logistics Route (+ child Bucket Logistics Route Leg)
# NEW Vehicle fields: custom_trolley_capacity, custom_buckets_per_trolley,
#   custom_is_internal_logistics_truck.
#
# Farm Distance is currently seeded with a PLACEHOLDER star topology (every farm
# 10.0km direct from Kapkolia, except Kaptumbo which sits behind Simotwo per the
# one real adjacency documented in the v15 source) — flagged for the ops team to
# correct with real road distances before this is trusted for real dispatch.

import frappe

PACKHOUSE = "Kapkolia"
FARM_EXPR = "SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''), pli.warehouse), ' ', 1)"


def _bucket_state(row):
	# home = shelved or sourced at the packhouse itself; else transit/loaded/farm/home.
	if int(row.get("shelved") or 0) or (row.get("farm_src") or "") == PACKHOUSE:
		return "home"
	if int(row.get("in_transit") or 0):
		return "transit"
	if int(row.get("loaded_in_trolley") or 0):
		return "loaded"
	if int(row.get("awaiting_transfer") or 0):
		return "farm"
	return "home"


def _mixed_map(opl_names):
	# Sales Order Item.custom_opl -> {box, bunch} — same derivation as getSchedulerFeed.
	mixed = {}
	if not opl_names:
		return mixed
	rows = frappe.get_all(
		"Sales Order Item",
		filters=[["custom_opl", "in", opl_names]],
		fields=["custom_opl", "custom_mixed_box", "custom_mixed_bunch"],
		limit_page_length=0,
	)
	for r in rows:
		op = r.get("custom_opl")
		m = mixed.setdefault(op, {"box": 0, "bunch": 0})
		if int(r.get("custom_mixed_box") or 0):
			m["box"] = 1
		if int(r.get("custom_mixed_bunch") or 0):
			m["bunch"] = 1
	return mixed


def _mixed_label(m):
	if not m:
		return "Straight box"
	if m.get("bunch"):
		return "Mixed bunch"
	if m.get("box"):
		return "Mixed box"
	return "Straight box"


def _schedule_map(lookback_days=14):
	# OPL -> {team, schedule} from the most recent Packhouse Schedule containing it
	# (schedule membership only — NOT tied to the delivery-date window; a schedule is
	# created the day before delivery, so gating on the delivery window misses it).
	from_date = frappe.utils.add_days(frappe.utils.today(), -lookback_days)
	names = frappe.get_all(
		"Packhouse Schedule",
		filters={"schedule_date": [">=", from_date]},
		fields=["name", "schedule_date", "team"],
		order_by="schedule_date desc",
	)
	out = {}
	for sc in names:
		rows = frappe.get_all(
			"Packhouse Schedule Order",
			filters={"parent": sc.name},
			fields=["order_pick_list", "sequence"],
		)
		for r in rows:
			op = r.get("order_pick_list")
			if op and op not in out:
				out[op] = {"team": sc.team, "schedule": int(r.get("sequence") or 0)}
	return out


@frappe.whitelist()
def getTransferControlData():
	# Read-only per-bucket transfer state for a delivery-date window.
	fd = frappe.form_dict
	from_date = fd.get("from_date") or frappe.utils.today()
	to_date = fd.get("to_date") or frappe.utils.add_days(frappe.utils.today(), 2)

	frappe.response["message"] = {"success": False, "error": "Script failed"}
	try:
		opl_rows = frappe.db.sql(
			"""
			SELECT DISTINCT opl.name AS opl, opl.order_name AS order_name, opl.sales_order AS so,
			       so.customer AS customer, so.delivery_date AS delivery_date, opl.creation AS created,
			       opl.schedule_number AS schedule, opl.team AS team, opl.item_group AS item_group,
			       opl.mix_group AS mix_group
			FROM `tabOrder Pick List` opl
			JOIN `tabSales Order` so ON so.name = opl.sales_order
			JOIN `tabPick List Item` pli ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
			WHERE opl.docstatus < 2 AND so.delivery_date BETWEEN %(f)s AND %(t)s
			  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
			""",
			{"f": from_date, "t": to_date},
			as_dict=True,
		)
		opl_names = [r["opl"] for r in opl_rows]
		mixed = _mixed_map(opl_names)

		buckets_by_opl = {}
		if opl_names:
			pli_rows = frappe.db.sql(
				"""
				SELECT pli.parent AS opl, pli.idx AS box, pli.bucket AS bucket, pli.item_code AS variety,
				       pli.stock_qty AS stems, """ + FARM_EXPR + """ AS farm_src, pli.shelf AS shelf,
				       pli.transit_truck AS transit_truck, pli.awaiting_transfer AS awaiting_transfer,
				       pli.loaded_in_trolley AS loaded_in_trolley, pli.in_transit AS in_transit,
				       pli.shelved AS shelved
				FROM `tabPick List Item` pli
				WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s
				""",
				{"opls": tuple(opl_names)},
				as_dict=True,
			)
			seen = set()
			for r in pli_rows:
				key = (r["opl"], (r.get("bucket") or "").upper())
				if r.get("bucket") and key in seen:
					continue
				seen.add(key)
				buckets_by_opl.setdefault(r["opl"], []).append(
					{
						"id": r.get("bucket"),
						"box": r.get("box"),
						"variety": r.get("variety"),
						"stems": r.get("stems"),
						"farm": r.get("farm_src"),
						"shelf": r.get("shelf"),
						"state": _bucket_state(r),
						"transit_truck": r.get("transit_truck"),
					}
				)

		farms = set()
		orders = []
		for r in opl_rows:
			bl = buckets_by_opl.get(r["opl"], [])
			for b in bl:
				if b.get("farm") and b["farm"] != PACKHOUSE:
					farms.add(b["farm"])
			orders.append(
				{
					"ref": r["opl"],
					"opl": r["opl"],
					"order_name": r.get("order_name") or r["opl"],
					"customer": r.get("customer"),
					"so": r.get("so"),
					"delivery_date": str(r.get("delivery_date") or ""),
					"truck": next((b.get("transit_truck") for b in bl if b.get("transit_truck")), None),
					"schedule": r.get("schedule"),
					"team": r.get("team"),
					"item_group": r.get("item_group"),
					"mixed": _mixed_label(mixed.get(r["opl"])),
					"mix_group": r.get("mix_group"),
					"created": str(r.get("created") or ""),
					"total": len(bl),
					"buckets": bl,
				}
			)

		frappe.response["message"] = {
			"success": True,
			"orders": orders,
			"farms": sorted(farms),
			"packhouse": PACKHOUSE,
			"window": {"from": str(from_date), "to": str(to_date)},
			"generated_at": str(frappe.utils.now()),
		}
	except Exception as e:
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def getTransferScheduleData():
	# Planning view: open (schedule-gated) orders aggregated by farm/variety, plus the
	# fleet, today's trips, truck-status snapshot, distance graph and today's routes.
	fd = frappe.form_dict
	from_date = fd.get("from_date") or frappe.utils.add_days(frappe.utils.today(), 1)
	to_date = fd.get("to_date") or from_date

	frappe.response["message"] = {"success": False, "error": "Script failed"}
	try:
		sched = _schedule_map()

		opl_rows = frappe.db.sql(
			"""
			SELECT DISTINCT opl.name AS opl, opl.order_name AS order_name, opl.sales_order AS so,
			       so.customer AS customer, so.delivery_date AS delivery_date
			FROM `tabOrder Pick List` opl
			JOIN `tabSales Order` so ON so.name = opl.sales_order
			WHERE opl.docstatus < 2 AND so.delivery_date BETWEEN %(f)s AND %(t)s
			  AND opl.name IN %(scheduled)s
			""",
			{"f": from_date, "t": to_date, "scheduled": tuple(sched.keys()) or ("",)},
			as_dict=True,
		) if sched else []
		opl_names = [r["opl"] for r in opl_rows]

		pli_rows = []
		if opl_names:
			pli_rows = frappe.db.sql(
				"""
				SELECT pli.parent AS opl, pli.bucket AS bucket, pli.item_code AS variety,
				       pli.stock_qty AS stems, """ + FARM_EXPR + """ AS farm
				FROM `tabPick List Item` pli
				WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(opls)s
				  AND COALESCE(pli.shelved, 0) = 0
				""",
				{"opls": tuple(opl_names)},
				as_dict=True,
			)

		agg = {}  # opl -> farm -> variety -> {buckets:set, stems}
		seen_bucket = set()
		for r in pli_rows:
			b = (r.get("bucket") or "").upper()
			key = (r["opl"], b)
			if b and key in seen_bucket:
				continue
			seen_bucket.add(key)
			farm = r.get("farm") or ""
			if not farm or farm == PACKHOUSE:
				continue
			fmap = agg.setdefault(r["opl"], {})
			vmap = fmap.setdefault(farm, {})
			vrow = vmap.setdefault(r.get("variety") or "", {"buckets": 0, "stems": 0})
			vrow["buckets"] += 1
			vrow["stems"] += float(r.get("stems") or 0)

		orders = []
		for r in opl_rows:
			fmap = agg.get(r["opl"]) or {}
			farms_out = []
			total_b, total_s = 0, 0.0
			for farm, vmap in fmap.items():
				fb = sum(v["buckets"] for v in vmap.values())
				fs = sum(v["stems"] for v in vmap.values())
				total_b += fb
				total_s += fs
				farms_out.append(
					{
						"farm": farm,
						"buckets": fb,
						"stems": fs,
						"varieties": [{"variety": k, "buckets": v["buckets"], "stems": v["stems"]} for k, v in vmap.items()],
					}
				)
			if not farms_out:
				continue
			sc = sched.get(r["opl"]) or {}
			orders.append(
				{
					"opl": r["opl"],
					"order_name": r.get("order_name") or r["opl"],
					"customer": r.get("customer"),
					"so": r.get("so"),
					"delivery_date": str(r.get("delivery_date") or ""),
					"truck": None,
					"mixed": None,
					"schedule": sc.get("schedule"),
					"team": sc.get("team"),
					"total_buckets": total_b,
					"total_stems": total_s,
					"farms": farms_out,
				}
			)

		# Fleet
		vehicles = frappe.get_all(
			"Vehicle",
			filters={"custom_is_internal_logistics_truck": 1},
			fields=["name", "custom_trolley_capacity", "custom_buckets_per_trolley"],
		)
		veh_out = []
		for v in vehicles:
			trolleys = int(v.get("custom_trolley_capacity") or 0)
			per = int(v.get("custom_buckets_per_trolley") or 0)
			veh_out.append(
				{
					"name": v.name,
					"trolleys": trolleys,
					"buckets_per_trolley": per,
					"capacity_buckets": trolleys * per,
				}
			)

		# Today's trips
		today = frappe.utils.today()
		trip_docs = frappe.get_all("Bucket Request Trip", filters={"trip_date": today}, pluck="name")
		trips_out = []
		for tn in trip_docs:
			doc = frappe.get_doc("Bucket Request Trip", tn)
			trips_out.append(
				{
					"name": doc.name,
					"vehicle": doc.vehicle,
					"trip_date": str(doc.trip_date),
					"status": doc.status,
					"notes": doc.notes,
					"collection_order": doc.collection_order,
					"farm": doc.farm,
					"total_buckets": doc.total_buckets,
					"total_stems": doc.total_stems,
					"capacity_buckets": doc.capacity_buckets,
					"dispatched_at": str(doc.dispatched_at) if doc.dispatched_at else None,
					"received_at": str(doc.received_at) if doc.received_at else None,
					"orders": [
						{
							"order_pick_list": o.order_pick_list,
							"order_name": o.order_name,
							"customer": o.customer,
							"farm": o.farm,
							"varieties": o.varieties,
							"buckets": o.buckets,
							"stems": o.stems,
							"full_farm_buckets": o.full_farm_buckets,
							"is_partial": o.is_partial,
						}
						for o in doc.orders
					],
				}
			)

		# Truck status — today's transit_truck activity only (an unbounded window pulls
		# in stale historical loads since the flag is set once and never cleared).
		truck_status = []
		ts_rows = frappe.db.sql(
			"""
			SELECT pli.transit_truck AS truck, """ + FARM_EXPR + """ AS farm,
			       pli.awaiting_transfer AS aw, pli.loaded_in_trolley AS ld,
			       pli.in_transit AS tr, pli.shelved AS sh, pli.modified AS modified
			FROM `tabPick List Item` pli
			WHERE pli.parenttype = 'Order Pick List' AND pli.transit_truck IS NOT NULL
			  AND pli.transit_truck != '' AND DATE(pli.modified) = %(today)s
			""",
			{"today": today},
			as_dict=True,
		)
		by_truck = {}
		for r in ts_rows:
			st = by_truck.setdefault(
				r["truck"], {"truck": r["truck"], "total": 0, "awaiting": 0, "loaded": 0, "in_transit": 0, "shelved": 0, "last": "", "farm": r.get("farm") or ""}
			)
			st["total"] += 1
			if int(r.get("tr") or 0):
				st["in_transit"] += 1
			elif int(r.get("sh") or 0):
				st["shelved"] += 1
			elif int(r.get("ld") or 0):
				st["loaded"] += 1
			elif int(r.get("aw") or 0):
				st["awaiting"] += 1
			mod = str(r.get("modified") or "")
			if mod > st["last"]:
				st["last"] = mod
		for st in by_truck.values():
			st["loading_pct"] = round((st["loaded"] + st["in_transit"] + st["shelved"]) / st["total"] * 100) if st["total"] else 0
			truck_status.append(st)

		# Distance graph
		dist_rows = frappe.get_all("Farm Distance", fields=["name", "from_farm", "to_farm", "distance_km", "is_road_leg"])
		distances = [{"name": d.name, "a": d.from_farm, "b": d.to_farm, "km": d.distance_km, "leg": int(d.is_road_leg or 0)} for d in dist_rows]

		# Today's routes
		route_docs = frappe.get_all("Bucket Logistics Route", filters={"route_date": today}, pluck="name")
		routes_out = []
		for rn in route_docs:
			doc = frappe.get_doc("Bucket Logistics Route", rn)
			legs = [{"leg": l.leg, "from_farm": l.from_farm, "to_farm": l.to_farm, "distance_km": l.distance_km} for l in doc.legs]
			farms_covered = sorted({l.from_farm for l in doc.legs} | {l.to_farm for l in doc.legs} - {PACKHOUSE})
			routes_out.append({"name": doc.name, "vehicle": doc.vehicle, "total_km": doc.total_km, "legs": legs, "farms": farms_covered})

		frappe.response["message"] = {
			"success": True,
			"orders": orders,
			"vehicles": veh_out,
			"trips": trips_out,
			"truck_status": truck_status,
			"distances": distances,
			"routes": routes_out,
			"packhouse": PACKHOUSE,
			"window": {"from": str(from_date), "to": str(to_date)},
			"generated_at": str(frappe.utils.now()),
		}
	except Exception as e:
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def saveBucketTrip():
	# Upsert a Bucket Request Trip. orders = rows joined by \x1e, fields within a row
	# by \x1f: [order_pick_list, order_name, customer, farm, varieties, buckets, stems,
	# full_farm_buckets(optional)]. Capacity is recomputed server-side and is authoritative
	# — over-capacity refuses the whole save, no partial write.
	fd = frappe.form_dict
	name = fd.get("name")
	vehicle = fd.get("vehicle")
	trip_date = fd.get("trip_date") or frappe.utils.today()
	status = fd.get("status") or "Draft"
	notes = fd.get("notes") or ""
	collection_order = fd.get("collection_order") or ""
	farm = fd.get("farm") or ""
	orders_raw = fd.get("orders") or ""

	if not vehicle:
		frappe.response["message"] = {"status": "error", "message": "A vehicle is required."}
		return

	rows = []
	for line in orders_raw.split("\x1e"):
		if not line:
			continue
		parts = line.split("\x1f")
		if len(parts) < 7:
			continue
		row = {
			"order_pick_list": parts[0],
			"order_name": parts[1],
			"customer": parts[2],
			"farm": parts[3],
			"varieties": parts[4],
			"buckets": int(float(parts[5] or 0)),
			"stems": int(float(parts[6] or 0)),
			"full_farm_buckets": int(float(parts[7])) if len(parts) > 7 and parts[7] else None,
		}
		if row["full_farm_buckets"] is None:
			row["full_farm_buckets"] = row["buckets"]
		row["is_partial"] = 1 if row["buckets"] < row["full_farm_buckets"] else 0
		rows.append(row)

	total_buckets = sum(r["buckets"] for r in rows)
	total_stems = sum(r["stems"] for r in rows)

	v = frappe.db.get_value("Vehicle", vehicle, ["custom_trolley_capacity", "custom_buckets_per_trolley"], as_dict=True)
	cap = int((v.custom_trolley_capacity or 0) * (v.custom_buckets_per_trolley or 0)) if v else 0
	if cap > 0 and total_buckets > cap:
		frappe.response["message"] = {
			"status": "error",
			"reason": "over_capacity",
			"message": "{0} buckets exceeds {1}'s capacity of {2}.".format(total_buckets, vehicle, cap),
			"total_buckets": total_buckets,
			"capacity_buckets": cap,
		}
		return

	if name and frappe.db.exists("Bucket Request Trip", name):
		doc = frappe.get_doc("Bucket Request Trip", name)
	else:
		doc = frappe.new_doc("Bucket Request Trip")

	doc.vehicle = vehicle
	doc.trip_date = trip_date
	doc.status = status
	doc.notes = notes
	doc.collection_order = collection_order
	doc.farm = farm
	doc.total_buckets = total_buckets
	doc.total_stems = total_stems
	doc.capacity_buckets = cap
	doc.set("orders", [])
	for r in rows:
		doc.append("orders", r)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.response["message"] = {
		"status": "success",
		"name": doc.name,
		"total_buckets": total_buckets,
		"total_stems": total_stems,
		"capacity_buckets": cap,
	}


@frappe.whitelist()
def deleteBucketTrip():
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	frappe.delete_doc("Bucket Request Trip", name, ignore_permissions=True, force=1)
	frappe.db.commit()
	frappe.response["message"] = {"status": "success"}


@frappe.whitelist()
def saveBucketLogisticsRoute():
	# Upsert a Bucket Logistics Route for (date, vehicle). legs = "|~|"-joined Farm
	# Distance record names, in travel order. Empty legs = clear the route (the
	# vehicle goes back to unrestricted for the day — a deliberate degrade, not an error).
	fd = frappe.form_dict
	date = fd.get("date") or frappe.utils.today()
	vehicle = fd.get("vehicle")
	legs_raw = fd.get("legs") or ""

	if not vehicle:
		frappe.response["message"] = {"status": "error", "message": "A vehicle is required."}
		return

	leg_names = [n for n in legs_raw.split("|~|") if n]
	leg_docs = []
	if leg_names:
		leg_docs = frappe.get_all(
			"Farm Distance",
			filters={"name": ["in", leg_names]},
			fields=["name", "from_farm", "to_farm", "distance_km"],
		)
	by_name = {d.name: d for d in leg_docs}

	name = "BLR-" + str(date) + "-" + vehicle
	if frappe.db.exists("Bucket Logistics Route", name):
		doc = frappe.get_doc("Bucket Logistics Route", name)
	else:
		doc = frappe.new_doc("Bucket Logistics Route")
		doc.route_date = date
		doc.vehicle = vehicle

	doc.set("legs", [])
	total_km = 0.0
	for ln in leg_names:
		d = by_name.get(ln)
		if not d:
			continue
		doc.append("legs", {"leg": d.name, "from_farm": d.from_farm, "to_farm": d.to_farm, "distance_km": d.distance_km})
		total_km += float(d.distance_km or 0)
	doc.total_km = total_km
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.response["message"] = {"status": "success", "name": doc.name, "legs": len(doc.legs), "total_km": total_km}


@frappe.whitelist()
def dispatchBucketTrip():
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	current = frappe.db.get_value("Bucket Request Trip", name, "status")
	if current not in ("Draft", "Scheduled"):
		frappe.response["message"] = {"status": "error", "message": "Trip is already {0} — cannot dispatch.".format(current)}
		return
	frappe.db.set_value("Bucket Request Trip", name, {"status": "Dispatched", "dispatched_at": frappe.utils.now()})
	frappe.db.commit()
	frappe.response["message"] = {"status": "success", "name": name, "trip_status": "Dispatched"}


@frappe.whitelist()
def receiveBucketTrip():
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists("Bucket Request Trip", name):
		frappe.response["message"] = {"status": "error", "message": "Trip not found."}
		return
	current = frappe.db.get_value("Bucket Request Trip", name, "status")
	if current != "Dispatched":
		frappe.response["message"] = {"status": "error", "message": "Trip is {0} — must be Dispatched before it can be received.".format(current)}
		return
	frappe.db.set_value("Bucket Request Trip", name, {"status": "Received", "received_at": frappe.utils.now()})
	frappe.db.commit()
	frappe.response["message"] = {"status": "success", "name": name, "trip_status": "Received"}
