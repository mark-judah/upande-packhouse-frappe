# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Remote Transfers — Truck Routes tab (/remote-transfer/truck-routes): each truck's
# route for a day (Bucket Logistics Route), made fresh every day — a new one can start
# from the day before's or a saved route (Bucket Logistics Route Template) — and the
# road network routes are built on (Farm Distance). Split out of transfer_scheduling (formerly api/transfer_control);
# the old upande_packhouse.api.transfer_control.<name> paths still reach these.

import frappe
from frappe.utils import cint

from upande_packhouse.api.remote_transfer.transfer_scheduling import (
	_day_window,
	_dt,
	_on_road_message,
	_route_farms,
	_route_runs_by_name,
	_vehicle_on_road,
	route_runs,
	transfer_hub,
)


@frappe.whitelist(methods=["POST"])
def saveBucketLogisticsRoute():
	# Upsert a Bucket Logistics Route for (date, vehicle). legs = "|~|"-joined Farm
	# Distance record names, in travel order. Empty legs = clear the route (the
	# vehicle goes back to unusable for distribution until routed again).
	#
	# The legs must form one continuous round trip from the packhouse back to the
	# packhouse. Farm Distance records are undirected (FD-A-B serves A→B and B→A),
	# so each leg is stored in the direction it is actually driven.
	#
	# A truck can run several routes a day, each in its own From/To window: `name`
	# edits that route, `new=1` adds another one. With neither, the truck's only route
	# that day is replaced (or created). The route's date is the day of From.
	fd = frappe.form_dict
	from_datetime = fd.get("from_datetime")
	frappe.response["message"] = _save_route(
		date=str(frappe.utils.getdate(from_datetime))
		if from_datetime
		else (fd.get("date") or frappe.utils.today()),
		vehicle=fd.get("vehicle"),
		leg_names=(fd.get("legs") or "").split("|~|"),
		name=fd.get("name"),
		new=frappe.utils.cint(fd.get("new")),
		from_datetime=from_datetime,
		to_datetime=fd.get("to_datetime"),
	)


def _route_to_update(date, vehicle, name=None, new=0, auto_planned=0):
	"""The Bucket Logistics Route a save writes into, or None for a new one.
	Returns (doc_or_None, error_message)."""
	if name:
		if not frappe.db.exists("Bucket Logistics Route", name):
			return None, "Route {0} no longer exists. Refresh the page and try again.".format(name)
		doc = frappe.get_doc("Bucket Logistics Route", name)
		if doc.vehicle != vehicle:
			return None, "Route {0} belongs to {1}.".format(name, doc.vehicle)
		return doc, None
	if new:
		return None, None
	filters = {"route_date": date, "vehicle": vehicle}
	if auto_planned:
		filters["auto_planned"] = 1
	existing = frappe.get_all("Bucket Logistics Route", filters=filters, pluck="name")
	if len(existing) == 1:
		return frappe.get_doc("Bucket Logistics Route", existing[0]), None
	if len(existing) > 1:
		return None, "{0} has {1} routes on {2} — edit the one you mean under Truck Routes.".format(
			vehicle, len(existing), date
		)
	return None, None


def _route_path(leg_names):
	"""Farm Distance names in driving order -> (path, error). The path is one continuous
	round trip from the packhouse back to it, each leg stored in the direction driven
	(roads are undirected) and numbered with its run (trip)."""
	leg_names = [n for n in (leg_names or []) if n]
	leg_docs = []
	if leg_names:
		leg_docs = frappe.get_all(
			"Farm Distance",
			filters={"name": ["in", leg_names]},
			fields=["name", "from_farm", "to_farm", "distance_km"],
		)
	by_name = {d.name: d for d in leg_docs}

	unknown = [ln for ln in leg_names if ln not in by_name]
	if unknown:
		return None, "Unknown road leg(s): {0}. Refresh the page and try again.".format(", ".join(unknown))

	hub = transfer_hub()
	path, here = [], hub
	for ln in leg_names:
		d = by_name[ln]
		if d.from_farm == here:
			start, end = d.from_farm, d.to_farm
		elif d.to_farm == here:
			start, end = d.to_farm, d.from_farm
		else:
			return None, "Route is broken at {0}: leg {1} ({2}–{3}) doesn't start there.".format(
				here, ln, d.from_farm, d.to_farm
			)
		path.append({"leg": d.name, "from_farm": start, "to_farm": end, "distance_km": d.distance_km})
		here = end
	# Runs: each time the route is back at the packhouse a run ends; the next leg starts
	# the next run (Kapkolia → A → Kapkolia → B → Kapkolia = run 1 to A, run 2 to B).
	run = 1
	for leg in path:
		leg["run"] = run
		if leg["to_farm"] == hub:
			run += 1
	if path and here != hub:
		return None, "Route ends at {0} — it must return to {1}.".format(here, hub)
	return path, None


def _save_route(
	date,
	vehicle,
	leg_names,
	auto_planned=0,
	name=None,
	new=0,
	from_datetime=None,
	to_datetime=None,
	template=None,
	check_on_road=True,
):
	"""Validate and save a truck's Bucket Logistics Route; returns the response message.
	A route saved by hand (auto_planned=0) is never re-routed by automatic scheduling.
	`template` marks a day copy of a saved route (made even while the truck is out)."""
	if not vehicle:
		return {"status": "error", "message": "A vehicle is required."}
	busy = _vehicle_on_road(vehicle) if check_on_road else None
	if busy and str(date) == str(frappe.utils.today()):
		return {
			"status": "error",
			"reason": "on_road",
			"message": _on_road_message(vehicle, busy, "re-routed"),
		}
	path, err = _route_path(leg_names)
	if err:
		return {"status": "error", "message": err}
	hub = transfer_hub()

	doc, err = _route_to_update(date, vehicle, name=name, new=new, auto_planned=auto_planned)
	if err:
		return {"status": "error", "message": err}
	if doc is not None:
		# A run that already has a trip keeps its farms: its trip (and the farm app)
		# drive it. Runs can be added after it, or changed once the trip is gone.
		new_runs = {r["run"]: r["stops"] for r in route_runs(path, hub)}
		old_runs = {r["run"]: r["stops"] for r in _route_runs_by_name(doc.name)}
		for t in frappe.get_all(
			"Bucket Request Trip",
			filters={"route": doc.name, "status": ["!=", "Received"]},
			fields=["name", "run"],
		):
			if new_runs.get(t.run) != old_runs.get(t.run):
				return {
					"status": "error",
					"message": "Trip {0} of {1} is planned as {2} — keep that trip's farms ({3}) or delete the trip first.".format(
						t.run, doc.name, t.name, ", ".join(old_runs.get(t.run) or [])
					),
				}
	if doc is None:
		doc = frappe.new_doc("Bucket Logistics Route")
		doc.route_date = date
		doc.vehicle = vehicle
	if from_datetime or to_datetime:
		if not (from_datetime and to_datetime):
			return {"status": "error", "message": "Set both From and To."}
		doc.from_datetime, doc.to_datetime = from_datetime, to_datetime
	elif not (doc.from_datetime and doc.to_datetime):
		doc.from_datetime, doc.to_datetime = _day_window(date)

	doc.set("legs", [])
	total_km = 0.0
	for leg in path:
		doc.append("legs", leg)
		total_km += float(leg["distance_km"] or 0)
	doc.total_km = total_km
	doc.auto_planned = 1 if auto_planned else 0
	if template:
		doc.template = template
	# A link to a saved route that is gone (deleted, or never copied to this site) would
	# fail the save: the day route stands on its own, so drop the link instead.
	if doc.get("template") and not frappe.db.exists(TEMPLATE, doc.template):
		doc.template = None
	try:
		doc.save(ignore_permissions=True)
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		return {"status": "error", "reason": "overlap", "message": str(e)}
	frappe.db.commit()  # nosemgrep: frappe-manual-commit

	return {
		"status": "success",
		"name": doc.name,
		"legs": len(doc.legs),
		"total_km": total_km,
	}


@frappe.whitelist()
def getBucketLogisticsRoutes():
	# Every truck's Bucket Logistics Route for one date (default today), legs in
	# driving order — for the Truck Routes list / route builder.
	date = frappe.form_dict.get("date") or frappe.utils.today()
	out = []
	for name in frappe.get_all(
		"Bucket Logistics Route",
		filters={"route_date": date},
		pluck="name",
		order_by="vehicle asc, from_datetime asc",
	):
		doc = frappe.get_doc("Bucket Logistics Route", name)
		legs = [
			{
				"leg": l.leg,
				"from_farm": l.from_farm,
				"to_farm": l.to_farm,
				"distance_km": l.distance_km,
				"run": l.get("run"),
			}
			for l in doc.legs
		]
		stops = [legs[0]["from_farm"]] + [l["to_farm"] for l in legs] if legs else []
		out.append(
			{
				"name": doc.name,
				"vehicle": doc.vehicle,
				"route_date": str(doc.route_date),
				"from_datetime": _dt(doc.from_datetime),
				"to_datetime": _dt(doc.to_datetime),
				"total_km": doc.total_km,
				"auto_planned": int(doc.get("auto_planned") or 0),
				"legs": legs,
				"stops": stops,
				"runs": [r["stops"] for r in route_runs(legs)],
				"farms": _route_farms(doc.legs),
				"modified": str(doc.modified),
			}
		)
	frappe.response["message"] = {"status": "success", "date": str(date), "routes": out}


@frappe.whitelist(methods=["POST"])
def saveTransferTruck():
	# Add a truck from the Remote Transfers page. `truck_type` says what it is used for:
	# "internal" (remote transfers — Internal Logistics Truck?), "dispatch" (Dispatch
	# Truck?) or "both"; left out (capacity edit) it stays an internal truck and its
	# dispatch flag is untouched. An existing registration is updated.
	fd = frappe.form_dict
	plate = (fd.get("license_plate") or "").strip().upper()
	if not plate:
		frappe.response["message"] = {"status": "error", "message": "Registration is required."}
		return
	truck_type = (fd.get("truck_type") or "").strip().lower()
	if truck_type and truck_type not in ("internal", "dispatch", "both"):
		frappe.response["message"] = {"status": "error", "message": "Unknown truck type."}
		return
	internal = truck_type in ("", "internal", "both")
	trolleys = frappe.utils.cint(fd.get("trolley_capacity"))
	per = frappe.utils.cint(fd.get("buckets_per_trolley"))
	if internal and (trolleys <= 0 or per <= 0):
		frappe.response["message"] = {
			"status": "error",
			"message": "Trolleys and buckets per trolley must be more than 0.",
		}
		return
	exists = frappe.db.exists("Vehicle", plate)
	if not frappe.has_permission("Vehicle", "write" if exists else "create"):
		frappe.response["message"] = {"status": "error", "message": "You can't add vehicles."}
		return
	if exists:
		doc = frappe.get_doc("Vehicle", plate)
	else:
		doc = frappe.new_doc("Vehicle")
		doc.license_plate = plate
		doc.make = (fd.get("make") or "").strip() or "Unknown"
		doc.model = (fd.get("model") or "").strip() or "Unknown"
		doc.fuel_type = fd.get("fuel_type") or "Diesel"
		doc.uom = "Litre" if frappe.db.exists("UOM", "Litre") else "Nos"
		doc.last_odometer = 0
	doc.custom_is_internal_logistics_truck = 1 if internal else 0
	if truck_type:
		doc.custom_dispatch_truck = 1 if truck_type in ("dispatch", "both") else 0
	if trolleys > 0 and per > 0:
		doc.custom_trolley_capacity = trolleys
		doc.custom_buckets_per_trolley = per
	try:
		doc.save()
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		frappe.response["message"] = {"status": "error", "message": str(e)}
		return
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": doc.name,
		"updated": 1 if exists else 0,
		"capacity_buckets": trolleys * per,
		"internal": 1 if internal else 0,
	}


@frappe.whitelist(methods=["POST"])
def deleteBucketLogisticsRoute():
	# Remove a truck's route for a day. The truck is then not used by ⚡ Distribute
	# for that day until it is routed again; trips already planned are untouched.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Bucket Logistics Route", name):
		frappe.response["message"] = {"status": "error", "message": "Route not found."}
		return
	live = frappe.get_all(
		"Bucket Request Trip",
		filters={"route": name, "status": ["!=", "Received"]},
		fields=["name", "run", "status"],
		order_by="run asc",
	)
	if live:
		frappe.response["message"] = {
			"status": "error",
			"message": "Route {0} still has trips: {1} — delete or finish them first.".format(
				name, ", ".join("trip {0} {1} ({2})".format(t.run, t.name, t.status) for t in live)
			),
		}
		return
	# Finished trips keep their record; only the link check is skipped.
	frappe.delete_doc("Bucket Logistics Route", name, ignore_permissions=True, force=1)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


@frappe.whitelist(methods=["POST"])
def addFarmTrip():
	# Send a truck to one farm: a new trip (packhouse → farm → packhouse) added to the end
	# of its latest route on `date`, or a new all-day route when it has none that day.
	# Only that day's route changes. Returns the route and the new trip's number, so buckets can be planned straight in.
	fd = frappe.form_dict
	vehicle, farm = fd.get("vehicle"), fd.get("farm")
	date = str(frappe.utils.getdate(fd.get("date") or frappe.utils.today()))
	if not vehicle or not farm:
		frappe.response["message"] = {"status": "error", "message": "A truck and a farm are required."}
		return
	hub = transfer_hub()
	road = _farm_distance_between(hub, farm)
	if not road:
		frappe.response["message"] = {
			"status": "error",
			"message": "No road between {0} and {1} — add it under Road network on the Truck routes tab.".format(
				hub, farm
			),
		}
		return
	latest = frappe.get_all(
		"Bucket Logistics Route",
		filters={"vehicle": vehicle, "route_date": date},
		pluck="name",
		order_by="from_datetime desc",
		limit=1,
	)
	if latest:
		legs = frappe.get_all(
			"Bucket Logistics Route Leg",
			filters={"parent": latest[0], "parenttype": "Bucket Logistics Route"},
			pluck="leg",
			order_by="idx asc",
		)
		res = _save_route(date=date, vehicle=vehicle, leg_names=legs + [road, road], name=latest[0])
	else:
		res = _save_route(date=date, vehicle=vehicle, leg_names=[road, road], new=1)
	if res.get("status") == "success":
		res["route"] = res["name"]
		res["run"] = len(_route_runs_by_name(res["name"]))
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = res


# ---- Saved routes (no date) ---------------------------------------------------------
# A route saved once. It is never put on a day by itself: on the Truck routes tab a new
# day route (Bucket Logistics Route, which trips link to) can start from it.

TEMPLATE = "Bucket Logistics Route Template"


def _hhmm(value):
	"""A Time value as "HH:MM" ("6:00:00" and timedelta both)."""
	if value is None or value == "":
		return ""
	if hasattr(value, "total_seconds"):
		mins = int(value.total_seconds() // 60)
		return "{0:02d}:{1:02d}".format(mins // 60 % 24, mins % 60)
	parts = str(value).split(":")
	return "{0:02d}:{1:02d}".format(int(parts[0]), int(parts[1] if len(parts) > 1 else 0))


def _minutes_window(start, end):
	a = int(start[:2]) * 60 + int(start[3:5])
	b = int(end[:2]) * 60 + int(end[3:5])
	return a, (b if b > a else b + 1440)


def _windows_clash(s1, e1, s2, e2):
	"""Two times-of-day windows overlap on some day (a window may run past midnight)."""
	a1, b1 = _minutes_window(s1, e1)
	a2, b2 = _minutes_window(s2, e2)
	return any(a1 < b2 + k and a2 + k < b1 for k in (-1440, 0, 1440))


def _template_dict(doc):
	legs = [
		{
			"leg": l.leg,
			"from_farm": l.from_farm,
			"to_farm": l.to_farm,
			"distance_km": l.distance_km,
			"run": l.get("run"),
		}
		for l in doc.legs
	]
	return {
		"name": doc.name,
		"vehicle": doc.vehicle,
		"active": int(doc.active or 0),
		"from_time": _hhmm(doc.from_time),
		"to_time": _hhmm(doc.to_time),
		"total_km": doc.total_km,
		"legs": legs,
		"stops": ([legs[0]["from_farm"]] + [l["to_farm"] for l in legs]) if legs else [],
		"runs": [r["stops"] for r in route_runs(legs)],
		"farms": _route_farms(doc.legs),
		"modified": str(doc.modified),
	}


@frappe.whitelist()
def getRouteTemplates():
	# Every saved route, by truck then start time — the Truck routes list.
	out = [
		_template_dict(frappe.get_doc(TEMPLATE, n))
		for n in frappe.get_all(TEMPLATE, pluck="name", order_by="vehicle asc, from_time asc")
	]
	frappe.response["message"] = {"status": "success", "routes": out}


@frappe.whitelist(methods=["POST"])
def saveRouteTemplate():
	# Create or edit a saved route: vehicle, legs ("|~|"-joined Farm Distance names in
	# driving order), from_time / to_time ("HH:MM"), active. `name` edits that route.
	fd = frappe.form_dict
	vehicle = fd.get("vehicle")
	start, end = (fd.get("from_time") or "")[:5], (fd.get("to_time") or "")[:5]
	if not vehicle:
		frappe.response["message"] = {"status": "error", "message": "A truck is required."}
		return
	if len(start) != 5 or len(end) != 5 or start == end:
		frappe.response["message"] = {"status": "error", "message": "Set a From and a To time."}
		return
	path, err = _route_path((fd.get("legs") or "").split("|~|"))
	if err or not path:
		frappe.response["message"] = {"status": "error", "message": err or "Add at least one farm."}
		return
	name = fd.get("name")
	for o in frappe.get_all(
		TEMPLATE,
		filters={"vehicle": vehicle, "active": 1, "name": ["!=", name or ""]},
		fields=["name", "from_time", "to_time"],
	):
		if _windows_clash(start, end, _hhmm(o.from_time), _hhmm(o.to_time)):
			frappe.response["message"] = {
				"status": "error",
				"message": "{0} already has route {1} from {2} to {3} — pick a time that doesn’t overlap.".format(
					vehicle, o.name, _hhmm(o.from_time), _hhmm(o.to_time)
				),
			}
			return
	doc = (
		frappe.get_doc(TEMPLATE, name)
		if name and frappe.db.exists(TEMPLATE, name)
		else frappe.new_doc(TEMPLATE)
	)
	if doc.name and doc.vehicle and doc.vehicle != vehicle:
		frappe.response["message"] = {
			"status": "error",
			"message": "Route {0} belongs to {1}.".format(doc.name, doc.vehicle),
		}
		return
	doc.vehicle = vehicle
	doc.from_time, doc.to_time = start + ":00", end + ":00"
	if fd.get("active") is not None:
		doc.active = cint(fd.get("active"))
	doc.set("legs", [])
	for leg in path:
		doc.append("legs", leg)
	doc.total_km = sum(float(l["distance_km"] or 0) for l in path)
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": doc.name, "total_km": doc.total_km}


@frappe.whitelist(methods=["POST"])
def setRouteTemplateActive():
	# Turn a saved route on or off (off = not offered for copying onto a day).
	fd = frappe.form_dict
	name = fd.get("name")
	if not name or not frappe.db.exists(TEMPLATE, name):
		frappe.response["message"] = {"status": "error", "message": "Route not found."}
		return
	frappe.db.set_value(TEMPLATE, name, "active", 1 if cint(fd.get("active")) else 0)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


@frappe.whitelist(methods=["POST"])
def deleteRouteTemplate():
	# Delete a saved route. Day routes copied from it stay; they no longer point at it.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists(TEMPLATE, name):
		frappe.response["message"] = {"status": "error", "message": "Route not found."}
		return
	frappe.db.sql("UPDATE `tabBucket Logistics Route` SET template = NULL WHERE template = %s", name)
	frappe.delete_doc(TEMPLATE, name, ignore_permissions=True, force=1)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}


def _farm_distance_between(a, b):
	"""The Farm Distance record for the pair in either direction (roads are two-way)."""
	return frappe.db.get_value("Farm Distance", {"from_farm": a, "to_farm": b}) or frappe.db.get_value(
		"Farm Distance", {"from_farm": b, "to_farm": a}
	)


@frappe.whitelist(methods=["POST"])
def saveFarmDistance():
	# Add a road to the network the truck routes are built from — or update the km /
	# road-leg flag of an existing pair (either direction). is_road_leg=1 is a real
	# road a truck drives; 0 is a direct-distance figure used for totals only.
	fd = frappe.form_dict
	a = (fd.get("from_farm") or "").strip()
	b = (fd.get("to_farm") or "").strip()
	km = frappe.utils.flt(fd.get("distance_km"))
	road = 1 if frappe.utils.cint(fd.get("is_road_leg")) else 0

	if not a or not b:
		frappe.response["message"] = {"status": "error", "message": "Pick both farms."}
		return
	if a == b:
		frappe.response["message"] = {"status": "error", "message": "A road needs two different farms."}
		return
	missing = [f for f in (a, b) if not frappe.db.exists("Farm", f)]
	if missing:
		frappe.response["message"] = {"status": "error", "message": "Unknown farm: " + ", ".join(missing)}
		return
	if km <= 0:
		frappe.response["message"] = {"status": "error", "message": "Distance must be more than 0 km."}
		return

	name = _farm_distance_between(a, b)
	doc = frappe.get_doc("Farm Distance", name) if name else frappe.new_doc("Farm Distance")
	if not name:
		doc.from_farm, doc.to_farm = a, b
	doc.distance_km = km
	doc.is_road_leg = road
	doc.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {
		"status": "success",
		"name": doc.name,
		"updated": bool(name),
		"via_farms": doc.get("via_farms") or "",
	}


@frappe.whitelist(methods=["POST"])
def deleteFarmDistance():
	# Remove a road. Refused while a saved truck route still drives it — deleting it
	# would silently break that route's legs.
	name = frappe.form_dict.get("name")
	if not name or not frappe.db.exists("Farm Distance", name):
		frappe.response["message"] = {"status": "error", "message": "Road not found."}
		return
	used = frappe.get_all(
		"Bucket Logistics Route Leg", filters={"leg": name}, pluck="parent", distinct=True, limit=5
	)
	if used:
		frappe.response["message"] = {
			"status": "error",
			"message": "{0} is used by route(s) {1} — change those routes first.".format(
				name, ", ".join(used)
			),
		}
		return
	frappe.delete_doc("Farm Distance", name, ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	frappe.response["message"] = {"status": "success", "name": name}
