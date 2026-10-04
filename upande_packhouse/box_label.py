"""Box Label generation -- the step between a fully-packed Farm Pack List
and staging/loading. Nothing in this codebase ever created a Box Label
before this (confirmed: every reference elsewhere is get_doc/exists/
get_value/set_value against an ALREADY-EXISTING one) -- staging
(createStagingEntry), loading (createLoadingEntry) and dispatch
(createOrUpdateDispatch) all assume Box Labels already exist, but nothing
ever made that first one real.

One Box Label per physical box (box_number), sourced from the Farm Pack
List's OWN packed rows (ground truth of what was actually packed), not the
Packing Guide plan -- so a label always reflects reality even if a pack
deviated slightly from plan. Header fields (consignee/delivery point/
freight agent/truck details) are carried straight from the Sales Order,
mirroring exactly what createOrUpdateDispatch already does when it later
builds the Delivery Note from these same fields -- so a box label and the
delivery note it feeds never disagree.
"""

import frappe

from upande_packhouse.server_scripts.box_label_barcode_gen import generate_box_barcode


def label_filters(order_name, rows, mix_by_item):
	"""Order name, variety and mix name for a box, the filters labels are printed by.

	`rows`: (variety, stems) per packed line of the box. The variety is the one with
	the most stems (a mixed box carries several — its mix name says which mix);
	`mix_by_item`: variety -> the Sales Order line's mix name."""
	stems = {}
	for variety, qty in rows:
		if variety:
			stems[variety] = stems.get(variety, 0) + (qty or 0)
	variety = max(stems, key=stems.get) if stems else None
	mixes = []
	for v in stems:
		m = (mix_by_item.get(v) or "").strip()
		if m and m not in mixes:
			mixes.append(m)
	return {"order_name": order_name or "", "variety": variety, "mix_name": ", ".join(mixes)}


def mix_names(sales_order):
	"""variety -> mix name of its Sales Order line (the first line of that variety)."""
	out = {}
	if sales_order:
		for r in frappe.get_all(
			"Sales Order Item",
			filters={"parent": sales_order},
			fields=["item_code", "custom_mix_name"],
			order_by="idx asc",
		):
			out.setdefault(r.item_code, r.custom_mix_name or "")
	return out


def sync_box_labels_for_fpl(fpl_doc, opl_doc, so_doc):
	"""Create (or refresh, pre-staging) one Box Label per box_number packed
	on this Farm Pack List. Idempotent: an existing label for a box is
	updated in place rather than duplicated, but only while it's still
	untouched by the physical flow (not yet precooling/staged/loaded/delivered) --
	once a box has left packing, its label is left alone even if the FPL
	is amended.
	"""
	by_box = {}
	for row in fpl_doc.pack_list_item:
		box_no = int(row.box_id or 0) or 1
		by_box.setdefault(box_no, []).append(row)

	total_boxes = len(by_box)
	farm_code = frappe.db.get_value("Farm", fpl_doc.farm, "farm_code") if fpl_doc.farm else None

	created, updated, skipped = [], [], []
	mix_by_item = mix_names(so_doc.name)
	order_name = opl_doc.get("order_name") or so_doc.get("custom_order_name") or ""

	for box_no, rows in by_box.items():
		# Found by what it labels, not by name: labels are named with a short
		# random code (Box Label.autoname), and older ones as BOX-<OPL>-<box no>.
		existing = frappe.db.get_value(
			"Box Label", {"order_pick_list": opl_doc.name, "box_number": box_no}, "name"
		)
		name = existing
		if existing:
			box = frappe.get_doc("Box Label", name)
			if box.precooling or box.staged or box.loaded or box.delivered:
				# Already moving through the physical flow -- a re-pack/
				# amendment must not silently rewrite a label that may
				# already be printed and stuck on a real box.
				skipped.append(name)
				continue
		else:
			box = frappe.new_doc("Box Label")
			box.order_pick_list = opl_doc.name
			box.box_number = box_no

		# The barcode only encodes the box's own name, which never changes across
		# re-packs -- generate it once. A new label has no name until it is
		# inserted, so its barcode is made right after (below).
		if existing and not box.barcode:
			box.barcode = generate_box_barcode(name)

		total_stems = sum(int(r.stock_qty or 0) for r in rows)

		box.farm = fpl_doc.farm
		box.farm_code = farm_code
		box.customer = fpl_doc.customer
		box.length = rows[0].stem_length
		box.pack_rate = total_stems
		box.farm_pack_lis = fpl_doc.name
		box.date = frappe.utils.today()
		# customer_purchase_order is (despite its label) the field
		# createOrUpdateDispatch already reads as the SALES ORDER name when
		# grouping loaded boxes into a Delivery Note -- match that, don't
		# invent a second convention.
		box.customer_purchase_order = so_doc.name
		box.consignee = so_doc.get("custom_consignee")
		box.truck_details = so_doc.get("custom_truck_details")
		box.freight_agent = so_doc.get("custom_shipping_agent")
		box.delivery_point = so_doc.get("custom_delivery_point")
		box.box_total_count = total_boxes
		box.update(label_filters(order_name, [(r.item_code, int(r.stock_qty or 0)) for r in rows], mix_by_item))

		box.set("box_item", [])
		for r in rows:
			box.append(
				"box_item",
				{
					"variety": r.item_code,
					"qty": r.bunch_qty,
					"uom": r.bunch_uom,
					"length": r.stem_length,
					"source_farm": fpl_doc.farm,
				},
			)

		box.flags.ignore_permissions = True
		if existing:
			box.save(ignore_permissions=True)
			updated.append(box.name)
		else:
			box.insert(ignore_permissions=True)
			box.db_set("barcode", generate_box_barcode(box.name), update_modified=False)
			created.append(box.name)

	return {"created": created, "updated": updated, "skipped": skipped}
