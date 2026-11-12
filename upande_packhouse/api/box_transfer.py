# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Internal transfer of packed boxes from a sales farm to the main sales farm.

A box packed at a sales farm that is not the main one (Chepsito) is dispatched from the main sales farm
(Kapkolia, the first Remote Transfer Sales Farm). At its own farm it is staged as usual, then loaded onto
an internal transfer truck and sent; at the main sales farm it is scanned in on arrival, which moves its
stems from the farm's dispatch cold room to the main one's. From there it loads for dispatch like the main
farm's own boxes. No Loading Plan is involved.

Box Label: transfer_status (Loaded -> In Transit -> Received), transfer_vehicle, transfer_to,
transfer_sent_at, transfer_received_at, loaded_internal_transfer (on a transfer truck)."""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

LOADED, IN_TRANSIT, RECEIVED = "Loaded", "In Transit", "Received"


def main_sales_farm():
	"""Where the sales farms' boxes are dispatched from: the first Remote Transfer Sales Farm."""
	from upande_packhouse.api.remote_transfer.transfer_scheduling import transfer_hub

	return transfer_hub(required=False) or ""


def needs_transfer(box):
	"""The box was packed at a sales farm that sends its boxes to the main one for dispatch."""
	from upande_packhouse.api.remote_transfer.transfer_scheduling import sales_farms

	main = main_sales_farm()
	farm = box.get("farm") or ""
	return bool(main and farm and farm != main and farm in sales_farms())


def dispatch_farm(box):
	"""The farm a box is loaded for dispatch from: the main sales farm once it was received there."""
	if box.get("transfer_status") == RECEIVED and box.get("transfer_to"):
		return box.transfer_to
	return box.get("farm")


def load_refusal(box):
	"""Why the box may not be loaded for dispatch yet (it is still to come from its farm), or None."""
	if needs_transfer(box) and box.get("transfer_status") != RECEIVED:
		where = {LOADED: _("on {0} at {1}"), IN_TRANSIT: _("on its way from {1} on {0}")}.get(
			box.get("transfer_status"), _("at {1}")
		)
		return _("Box {0} is still {1} — receive it at {2} first.").format(
			box.name, where.format(box.get("transfer_vehicle") or "", box.farm), main_sales_farm()
		)
	return None


def transfer_trucks():
	"""The internal transfer trucks (Vehicles not marked as dispatch trucks)."""
	rows = frappe.get_all(
		"Vehicle",
		filters={"custom_dispatch_truck": ["!=", 1]},
		fields=["name", "model", "make"],
		order_by="name asc",
	)
	return [{"name": r.name, "label": " ".join(x for x in (r.name, r.make, r.model) if x)} for r in rows]


def _box(box_label):
	name = (box_label or "").strip()
	if not name or not frappe.db.exists("Box Label", name):
		frappe.throw(_("Box label {0} not found.").format(name or "—"))
	return frappe.get_doc("Box Label", name)


RECENT_DAYS = 7

_FIELDS = """b.name, b.box_number, b.customer, b.customer_name, b.order_name, b.order_pick_list, b.variety,
	b.length, b.farm, b.staged, b.loaded, b.transfer_status, b.transfer_vehicle, b.transfer_to,
	b.transfer_sent_at, b.transfer_received_at, b.transfer_loaded_at, b.arrived_at_main_farm,
	so.delivery_date"""

_JOINS = """FROM `tabBox Label` b
	LEFT JOIN `tabOrder Pick List` o ON o.name = b.order_pick_list
	LEFT JOIN `tabSales Order` so ON so.name = o.sales_order"""


def _boxes(where, values, order_by):
	return frappe.db.sql(f"SELECT {_FIELDS} {_JOINS} WHERE {where} ORDER BY {order_by}", values, as_dict=True)


def _row(b):
	return {
		"box_label": b.name,
		"box_number": b.box_number,
		"customer": b.customer_name or b.customer or "",
		"order_name": b.order_name or "",
		"opl": b.order_pick_list or "",
		"variety": b.variety or "",
		"length": b.length or "",
		"farm": b.farm or "",
		"delivery_date": str(b.get("delivery_date") or ""),
		"status": b.transfer_status or "",
		"staged": cint(b.get("staged")),
		"vehicle": b.transfer_vehicle or "",
		"to": b.transfer_to or "",
		"loaded_at": str(b.get("transfer_loaded_at") or ""),
		"sent_at": str(b.transfer_sent_at or ""),
		"received_at": str(b.transfer_received_at or ""),
	}


def transfer_data(station, delivery_date=None):
	"""What the Box Transfer screen shows for a station.

	send (a sales farm sending to the main one): its staged boxes still to load, the ones on each truck and
	the ones on their way; receive (the main sales farm): the boxes on their way to it and the ones received.
	Only orders delivering on `delivery_date` when given, else the ones still moving and the last days'."""
	from frappe.utils import add_days, getdate, today

	from upande_packhouse.api.remote_transfer.transfer_scheduling import sales_farms

	main = main_sales_farm()
	station = (station or "").strip()
	senders = [f for f in sales_farms() if f != main]
	role = "receive" if station and station == main else ("send" if station in senders else "none")
	values = {
		"station": station,
		"main": main,
		"loaded": LOADED,
		"in_transit": IN_TRANSIT,
		"received": RECEIVED,
		"since": add_days(today(), -RECENT_DAYS),
	}
	if delivery_date:
		values["date"] = getdate(delivery_date)
		when = "so.delivery_date = %(date)s"
	elif role == "send":
		# still here (recent orders), on a truck or on the way, or received in the last days
		when = """((IFNULL(b.transfer_status, '') = '' AND IFNULL(b.loaded, 0) = 0
				AND so.delivery_date >= %(since)s)
			OR b.transfer_status IN (%(loaded)s, %(in_transit)s)
			OR (b.transfer_status = %(received)s AND b.transfer_received_at >= %(since)s))"""
	else:
		when = """(b.transfer_status = %(in_transit)s
			OR (b.transfer_status = %(received)s AND b.transfer_received_at >= %(since)s))"""
	if role == "send":
		boxes = _boxes(
			f"b.farm = %(station)s AND {when}", values, "b.customer asc, b.order_name asc, b.box_number asc"
		)
	elif role == "receive":
		# a truck still loading at its farm has not left: nothing to receive yet
		boxes = _boxes(
			f"b.transfer_to = %(main)s AND b.transfer_status IN (%(in_transit)s, %(received)s) AND {when}",
			values,
			"b.transfer_sent_at desc, b.name asc",
		)
	else:
		boxes = []
	rows = [_row(b) for b in boxes]
	out = {
		"role": role,
		"main": main,
		"station": station,
		"trucks": transfer_trucks() if role == "send" else [],
	}
	if role == "send":
		on_dispatch = {b.name for b in boxes if cint(b.loaded)}
		waiting = [r for r in rows if not r["status"] and r["box_label"] not in on_dispatch]
		out["to_load"] = [r for r in waiting if r["staged"]]
		out["not_staged"] = [r for r in waiting if not r["staged"]]
		out["loaded"] = [r for r in rows if r["status"] == LOADED]
		out["in_transit"] = [r for r in rows if r["status"] == IN_TRANSIT]
	elif role == "receive":
		out["in_transit"] = [r for r in rows if r["status"] == IN_TRANSIT]
		out["received"] = [r for r in rows if r["status"] == RECEIVED]
	out["trips"] = _trips(_trip_rows([r for r in rows if r["status"]], station if role == "send" else None))
	return out


def _trip_rows(rows, farm):
	"""Every box of the trips `rows` belong to, whatever their delivery date: a trip's count is the truck's."""
	keys = {(r["vehicle"], r["sent_at"] if r["status"] != LOADED else "") for r in rows if r["vehicle"]}
	if not keys:
		return rows
	out = {r["box_label"]: r for r in rows}
	sent = {k for k in keys if k[1]}
	loading = {k[0] for k in keys if not k[1]}
	if sent:
		conds = " OR ".join(
			f"(b.transfer_vehicle = %(v{i})s AND b.transfer_sent_at = %(s{i})s)" for i in range(len(sent))
		)
		values = {"in_transit": IN_TRANSIT, "received": RECEIVED}
		for i, (v, s) in enumerate(sent):
			values[f"v{i}"], values[f"s{i}"] = v, s
		for b in _boxes(
			f"b.transfer_status IN (%(in_transit)s, %(received)s) AND ({conds})", values, "b.name asc"
		):
			out.setdefault(b.name, _row(b))
	if loading:
		for b in _boxes(
			"b.transfer_status = %(loaded)s AND b.transfer_vehicle IN %(vehicles)s"
			+ (" AND b.farm = %(farm)s" if farm else ""),
			{"loaded": LOADED, "vehicles": tuple(loading), "farm": farm},
			"b.name asc",
		):
			out.setdefault(b.name, _row(b))
	return list(out.values())


def _trips(rows):
	"""The transfer trips: one per truck run -- the boxes loaded on a truck and sent together (a truck still
	loading is its own trip). Latest first, each with every box and where it stands."""
	trips = {}
	for r in rows:
		key = (r["vehicle"], r["sent_at"] if r["status"] != LOADED else "")
		trip = trips.setdefault(
			key,
			{
				"vehicle": r["vehicle"],
				"sent_at": r["sent_at"] if r["status"] != LOADED else "",
				"from": r["farm"],
				"to": r["to"],
				"boxes": [],
			},
		)
		trip["boxes"].append(r)
	out = []
	for trip in trips.values():
		n = len(trip["boxes"])
		got = sum(1 for b in trip["boxes"] if b["status"] == RECEIVED)
		trip.update(
			total=n,
			received=got,
			received_at=max((b["received_at"] for b in trip["boxes"] if b["received_at"]), default=""),
			status=(
				"Loading"
				if not trip["sent_at"]
				else "Received"
				if got == n
				else "Partly received"
				if got
				else "On the way"
			),
		)
		out.append(trip)
	# loading first, then the latest sent
	return sorted(out, key=lambda x: (bool(x["sent_at"]), x["sent_at"] and -_ts(x["sent_at"])))


def _ts(v):
	from frappe.utils import get_datetime

	try:
		return get_datetime(v).timestamp()
	except Exception:
		return 0


def load_box(box_label, vehicle, station):
	"""Put a staged box on an internal transfer truck at its own farm."""
	box = _box(box_label)
	vehicle = (vehicle or "").strip()
	if not vehicle or not frappe.db.exists("Vehicle", vehicle):
		frappe.throw(_("Pick the transfer truck first."))
	if cint(frappe.db.get_value("Vehicle", vehicle, "custom_dispatch_truck")):
		frappe.throw(_("{0} is a dispatch truck, not a transfer truck.").format(vehicle))
	if station and box.farm != station:
		frappe.throw(_("Box {0} was packed at {1}, not at {2}.").format(box.name, box.farm, station))
	if not needs_transfer(box):
		frappe.throw(_("Box {0} is dispatched from {1}: it needs no transfer.").format(box.name, box.farm))
	if cint(box.loaded):
		frappe.throw(_("Box {0} is already loaded for dispatch.").format(box.name))
	if not cint(box.staged):
		frappe.throw(_("Box {0} must be staged before it can be loaded.").format(box.name))
	if box.transfer_status in (IN_TRANSIT, RECEIVED):
		frappe.throw(_("Box {0} is already {1}.").format(box.name, box.transfer_status.lower()))
	if box.transfer_status == LOADED and box.transfer_vehicle == vehicle:
		return {"status": "duplicate", "message": _("Box {0} is already on {1}.").format(box.name, vehicle)}
	frappe.db.set_value(
		"Box Label",
		box.name,
		{
			"transfer_status": LOADED,
			"transfer_vehicle": vehicle,
			"transfer_to": main_sales_farm(),
			"loaded_internal_transfer": 1,
			"transfer_loaded_at": now_datetime(),
		},
	)
	box.add_comment("Info", _("Loaded on {0} for {1}").format(vehicle, main_sales_farm()))
	return {"status": "success", "message": _("Box {0} on {1}.").format(box.name, vehicle)}


def send_truck(vehicle, station):
	"""The truck leaves: every box loaded on it at this farm is on its way to the main sales farm."""
	station = (station or "").strip()
	vehicle = (vehicle or "").strip()
	if not station:
		frappe.throw(_("Set this device's station (the farm the truck leaves from) first."))
	if not vehicle or not frappe.db.exists("Vehicle", vehicle):
		frappe.throw(_("Truck {0} not found.").format(vehicle or "—"))
	names = frappe.get_all(
		"Box Label",
		filters={"transfer_vehicle": vehicle, "transfer_status": LOADED, "farm": station},
		pluck="name",
	)
	if not names:
		frappe.throw(_("Nothing is loaded on {0}.").format(vehicle))
	at = now_datetime()
	for n in names:
		frappe.db.set_value("Box Label", n, {"transfer_status": IN_TRANSIT, "transfer_sent_at": at})
	return {
		"status": "success",
		"sent": len(names),
		"message": _("{0} left for {1} with {2} boxes.").format(vehicle, main_sales_farm(), len(names)),
	}


def receive_box(box_label, station):
	"""A transferred box scanned in at the main sales farm: its stems move from its farm's dispatch cold room
	to the main one's, and it loads for dispatch from there."""
	from upande_packhouse import stock_movement
	from upande_packhouse.sales_order_engine import _uom_factor

	box = _box(box_label)
	main = main_sales_farm()
	if station and station != main:
		frappe.throw(_("Boxes are received at {0}.").format(main))
	if box.transfer_status == RECEIVED:
		return {"status": "duplicate", "message": _("Box {0} was already received.").format(box.name)}
	if box.transfer_status == LOADED:
		frappe.throw(
			_("Box {0} is still on {1} at {2} — send the truck first.").format(
				box.name, box.transfer_vehicle or "", box.farm
			)
		)
	if box.transfer_status != IN_TRANSIT:
		frappe.throw(
			_("Box {0} was not sent here: load it on a transfer truck at {1}.").format(box.name, box.farm)
		)
	src = stock_movement.mapping_row_for_farm(box.farm, "Roses")
	dst = stock_movement.mapping_row_for_farm(main, "Roses")
	if not (src and src.dispatch_cold_store and dst and dst.dispatch_cold_store):
		frappe.throw(_("No dispatch cold store is mapped for {0} or {1}.").format(box.farm, main))
	stems = {}
	for row in box.box_item or []:
		stems[row.variety] = stems.get(row.variety, 0) + (row.qty or 0) * (_uom_factor(row.uom) or 1)
	moved = []
	for variety, qty in stems.items():
		hop = stock_movement.post_single_hop(
			item_code=variety,
			qty=qty,
			source=src.dispatch_cold_store,
			target=dst.dispatch_cold_store,
			business_unit="Roses",
			entry_type=stock_movement.TYPE_REMOTE_TRANSFER,
			box_label=box.name,
			farm=main,
			remarks=_("Box {0} received at {1} from {2}").format(box.name, main, box.farm),
		)
		if hop.get("entry"):
			moved.append(hop["entry"])
		elif hop.get("reason"):
			frappe.log_error(title="Box transfer: nothing moved", message=f"box={box.name} {hop['reason']}")
	frappe.db.set_value(
		"Box Label",
		box.name,
		{
			"transfer_status": RECEIVED,
			"transfer_received_at": now_datetime(),
			"arrived_at_main_farm": 1,
			"loaded_internal_transfer": 0,
			"transfer_sent_at": box.transfer_sent_at or now_datetime(),
			# staged again at the main sales farm before it loads for dispatch
			"staged": 0,
			"staging_location": None,
		},
	)
	box.add_comment("Info", _("Received at {0} from {1}").format(main, box.farm))
	return {
		"status": "success",
		"message": _("Box {0} received from {1}.").format(box.name, box.farm),
		"stock_entries": moved,
	}


def receive_trip(vehicle, sent_at, count, station):
	"""A whole transfer trip received at the main sales farm on a count: when the boxes counted off the truck
	are the boxes on their way on it, every one is received; otherwise each is scanned in."""
	main = main_sales_farm()
	if station and station != main:
		frappe.throw(_("Boxes are received at {0}.").format(main))
	if not vehicle or not sent_at:
		frappe.throw(_("Pick a trip that has left its farm."))
	# every box of the trip, whatever its delivery date: the count is the truck's
	trip = frappe.get_all(
		"Box Label",
		filters={
			"transfer_vehicle": vehicle,
			"transfer_to": main,
			"transfer_sent_at": sent_at,
			"transfer_status": ["in", [IN_TRANSIT, RECEIVED]],
		},
		fields=["name", "transfer_status"],
		order_by="name asc",
	)
	names = [b.name for b in trip if b.transfer_status == IN_TRANSIT]
	if not names:
		frappe.throw(_("Nothing on {0} is waiting to be received.").format(vehicle))
	counted = cint(count)
	if counted != len(names):
		frappe.throw(
			_(
				"You counted {0} boxes but {1} are on {2} ({3} of the trip's {4} already received) — scan each box instead."
			).format(counted, len(names), vehicle, len(trip) - len(names), len(trip))
		)
	for n in names:
		receive_box(n, station)
	return {
		"status": "success",
		"received": len(names),
		"message": _("All {0} boxes on {1} received.").format(len(names), vehicle),
	}
