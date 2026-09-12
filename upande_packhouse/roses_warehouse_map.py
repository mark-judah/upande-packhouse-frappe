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
