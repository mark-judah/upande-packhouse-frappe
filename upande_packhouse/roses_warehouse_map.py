"""Roses SO Warehouse Mapping (Roses-MAP) resolution -- the single place
that turns a Sales Order Item's own `warehouse` (the farm's Receiving Cold
Store -- see spec_autofill.build_spec_rows) into the two further warehouses
stock actually moves through on its way to a customer:

    Receiving Cold Store (source_warehouse)
      --[bucket issued from the coldstore, issueBucketToSaleOrderItem]-->
    Ungraded Sold (ungraded_sold_warehouse)
      --[Farm Pack List submits fully packed, farm_pack_list.py]-->
    Graded Sold (delivery_warehouse)
      --[Delivery Note deducts stock on submit]

Nothing here is hardcoded to a farm name or a "<Farm> X - KR" naming
pattern; every lookup goes through Roses-MAP's own rows, keyed by the real
Receiving Cold Store warehouse a Sales Order Item already carries.
"""

import frappe

MAPPING_DOC = "Roses-MAP"


def mapping_row_for_farm(farm):
	"""Roses-MAP row for a FARM directly (via Warehouse.custom_farm on each
	row's source_warehouse), not a specific warehouse name. Used once
	packing has happened: an Order Pick List / Farm Pack List already
	aggregates every bucket by then (individual bucket provenance no longer
	matters -- see farm_pack_list.py's _move_to_graded_sold), so resolving
	by the OPL's own `farm` field is both simpler and correct even for a
	location spanning more than one farm's coldstore.
	"""
	if not farm:
		return None
	rows = frappe.get_all(
		"SO Warehouse Mapping Item",
		filters={"parent": MAPPING_DOC},
		fields=["source_warehouse", "ungraded_sold_warehouse", "delivery_warehouse"],
	)
	for r in rows:
		if frappe.db.get_value("Warehouse", r.source_warehouse, "custom_farm") == farm:
			return r
	return None


def get_mapping_row(source_warehouse):
	"""Roses-MAP row for this coldstore, or None if unmapped."""
	if not source_warehouse:
		return None
	rows = frappe.get_all(
		"SO Warehouse Mapping Item",
		filters={"parent": MAPPING_DOC, "source_warehouse": source_warehouse},
		fields=["source_warehouse", "ungraded_sold_warehouse", "delivery_warehouse"],
		limit_page_length=1,
	)
	return rows[0] if rows else None


def ungraded_sold_warehouse(source_warehouse):
	row = get_mapping_row(source_warehouse)
	return row.ungraded_sold_warehouse if row else None


def graded_sold_warehouse(source_warehouse):
	row = get_mapping_row(source_warehouse)
	return row.delivery_warehouse if row else None


def transfer_to_farm_warehouse(source_warehouse, target_farm, item_rows, bucket_id=None):
	"""Move stock OUT of `source_warehouse` and INTO `target_farm`'s own
	Receiving Cold Store (its Roses-MAP row's source_warehouse), for the
	case where a bucket was received at one farm's coldstore but is being
	shelved at a DIFFERENT farm (carried there physically after receiving --
	e.g. a satellite farm's harvest consolidated onto Kapkolia's sales
	shelf). Without this, the bucket's Shelf Item keeps pointing at the
	farm it happened to be RECEIVED at, so allocation (which reads the
	Shelf Item's own `warehouse`) keeps offering stock from a warehouse the
	stems have already physically left.

	Returns the warehouse the bucket should now be considered "in":
	`target_farm`'s own coldstore after a real Stock Entry moves it there,
	or `source_warehouse` unchanged if nothing needs to move (already the
	same farm) or if `target_farm` has no Roses-MAP row yet (logged, never
	guessed at -- same "no fixture/patch mechanism, real data required"
	discipline every other lookup in this module follows).

	`item_rows`: iterable of (item_code, qty) tuples, all sharing
	`source_warehouse` -- one combined Stock Entry, one row per variety.
	"""
	if not source_warehouse:
		return source_warehouse

	origin_farm = frappe.db.get_value("Warehouse", source_warehouse, "custom_farm")
	if not origin_farm or origin_farm == target_farm:
		# Already this farm's own coldstore (the normal, same-farm case) --
		# or the warehouse carries no farm at all, in which case there's
		# nothing safe to compare against, so leave it as-is.
		return source_warehouse

	map_row = mapping_row_for_farm(target_farm)
	target_warehouse = map_row.source_warehouse if map_row else None
	if not target_warehouse or target_warehouse == source_warehouse:
		frappe.log_error(
			title="Shelving farm-transfer skipped -- no Roses-MAP row",
			message=(
				f"Bucket {bucket_id} shelved at farm {target_farm} but its stock is "
				f"still in {source_warehouse} (farm {origin_farm}); add/complete a "
				f"Roses-MAP row for {target_farm} so this transfers automatically "
				f"next time."
			),
		)
		return source_warehouse

	items = [
		{
			"item_code": item_code,
			"qty": qty,
			"uom": "Stems",
			"conversion_factor": 1,
			"s_warehouse": source_warehouse,
			"t_warehouse": target_warehouse,
			"allow_zero_valuation_rate": 1,
			"basic_rate": 0,
		}
		for item_code, qty in item_rows if item_code and qty
	]
	if not items:
		return source_warehouse

	transfer = frappe.new_doc("Stock Entry")
	transfer.stock_entry_type = "Farm Transfer"
	transfer.company = frappe.db.get_value("Warehouse", target_warehouse, "company")
	transfer.business_unit = "Roses"
	transfer.farm = target_farm
	transfer.custom_bucket_id = bucket_id
	transfer.remarks = (
		f"Bucket {bucket_id} shelved at {target_farm} -- stock moved from {source_warehouse}"
	)
	for item in items:
		transfer.append("items", item)
	transfer.insert(ignore_permissions=True)
	transfer.submit()
	return target_warehouse
