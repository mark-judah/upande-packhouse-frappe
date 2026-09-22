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

#: Every column these helpers read off a mapping row. `transfer_to` is the
#: Arrival hop an outlying farm's coldstore takes into the packhouse coldstore
#: before anything is sold from it -- see stock_movement.STAGES for the full
#: pipeline this is the front half of.
ROW_FIELDS = [
	"source_warehouse",
	"transfer_to",
	"ungraded_sold_warehouse",
	"delivery_warehouse",
]


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
		fields=ROW_FIELDS,
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
		fields=ROW_FIELDS,
		limit_page_length=1,
	)
	return rows[0] if rows else None


def ungraded_sold_warehouse(source_warehouse):
	row = get_mapping_row(source_warehouse)
	return row.ungraded_sold_warehouse if row else None


def graded_sold_warehouse(source_warehouse):
	row = get_mapping_row(source_warehouse)
	return row.delivery_warehouse if row else None


def pre_graded_warehouse_from_row(row):
	"""The warehouse stock sits in immediately BEFORE the Sold leg moves it
	into Graded Sold -- i.e. the `from` side of that leg, for one mapping row.

	Reading stock_movement.STAGES, the pipeline is

	    source_warehouse --Arrival--> transfer_to --Sold--> delivery_warehouse

	so the warehouse right before Graded Sold is the consolidated packhouse
	coldstore an outlying farm's stems have already been trucked into, not the
	outlying farm's own receiving store. `ungraded_sold_warehouse` is exactly
	that warehouse (it is what the Sold leg issues FROM when the stems aren't
	graded yet -- see resolve_route's SALE_STAGE branch and
	farm_pack_list._move_to_graded_sold, which moves ungraded -> graded), so it
	wins; `transfer_to` is the same warehouse on a row that predates the
	ungraded column, and a row with neither never leaves its own source.

	Whatever comes back is itself a mapped SOURCE warehouse on this data
	(Kapkolia/Karen Receiving are both Roses-MAP rows in their own right), which
	is what keeps `graded_sold_warehouse(soi.warehouse)` resolving for the
	Delivery Note -- see mobile/api.py's own note on that.
	"""
	if not row:
		return None
	return row.get("ungraded_sold_warehouse") or row.get("transfer_to") or row.get("source_warehouse")


def pre_graded_warehouse(source_warehouse):
	"""pre_graded_warehouse_from_row keyed by a specific coldstore."""
	return pre_graded_warehouse_from_row(get_mapping_row(source_warehouse))


def pre_graded_warehouse_for_farm(farm):
	"""pre_graded_warehouse_from_row keyed by a FARM -- what a Sales Order has
	to go on, since an order names a farm and never a warehouse."""
	return pre_graded_warehouse_from_row(mapping_row_for_farm(farm))


@frappe.whitelist()
def routing_defaults(farm=None, source_warehouse=None):
	"""What the Sales Order form should prefill, for one farm (or coldstore).
	Client counterpart of sales_order_apply_routing -- warehouse_routing.js
	calls this so the operator sees the warehouse the moment the farm is set,
	rather than only after a save round-trip."""
	warehouse = pre_graded_warehouse_for_farm(farm) if farm else pre_graded_warehouse(source_warehouse)
	return {"warehouse": warehouse}


def sales_order_apply_routing(doc, method=None):
	"""Roses Sales Order: fill each line's `warehouse` (and the header's own
	`set_warehouse`) from Roses-MAP, and settle the truck via sync_truck.

	Both were already meant to be on every line -- ERPNext's own set_warehouse
	cascade and misc_autopopulate.js's custom_truck_details handler fill them
	when a row is added through the grid -- but neither fires for a row built
	in code: spec_autofill's Add to Order Object.assigns straight onto
	add_child (deliberately, so the spec's own values aren't re-fired over),
	the mixed-box wizard does the same, and an order created over the API
	never runs form JS at all. Those lines then reach allocation with a blank
	warehouse, which is what makes `graded_sold_warehouse(soi.warehouse)`
	return nothing and a Delivery Note refuse to submit.

	Doing it here, on validate, makes it true for EVERY route into the doctype
	rather than for the one that happens to go through the grid. Only blanks
	are filled -- a warehouse the operator picked by hand (say, to sell
	directly off an outlying farm's own coldstore) is never overwritten.

	Hooked after roses_invoice.sync_sales_order_accounting_dimensions so
	`farm` / `business_unit` are already bridged from the legacy
	custom_farm / custom_business_unit fields a Floriday-origin order carries.
	"""
	# The truck is not Roses-specific and travels in BOTH directions, so it is
	# settled first, for every business unit.
	sync_truck(doc)

	if (doc.get("business_unit") or doc.get("custom_business_unit") or "") != "Roses":
		return

	farm = doc.get("farm") or doc.get("custom_farm")
	warehouse = pre_graded_warehouse_for_farm(farm) if farm else None
	if not warehouse:
		return

	# The header's own Source Warehouse, for the next row the operator adds
	# through the grid (ERPNext cascades set_warehouse to children itself).
	if warehouse and not doc.get("set_warehouse"):
		doc.set_warehouse = warehouse

	for it in doc.items:
		if warehouse and not it.get("warehouse"):
			it.warehouse = warehouse


def sync_truck(doc):
	"""Settle the order's truck between header and lines, and return it.

	`custom_truck_details` (header) and `custom_truck` (line) are the same fact
	recorded in two places -- pick lists read the line's copy as
	`transit_truck`, while the header is what the operator usually fills in --
	so whichever one is filled has to reach the other:

	  * header -> lines, for every line that hasn't got one. This is the
	    original direction, and it is what a line built in code needs:
	    spec_autofill's Add to Order and the mixed-box wizard both go straight
	    onto add_child and fire no grid event at all.
	  * lines -> header, when the header is blank. The truck is as often typed
	    onto the first line as into the header, and a blank header is what then
	    leaves every LATER line with nothing to inherit.

	Only blanks are filled in either direction: a line carrying a different
	truck from the header is a deliberate split load, not a mistake to correct.
	Mirrors misc_autopopulate.js, which does the same live on the form.
	"""
	truck = (doc.get("custom_truck_details") or "").strip()
	if not truck:
		for it in doc.items:
			line_truck = (it.get("custom_truck") or "").strip()
			if line_truck:
				truck = line_truck
				doc.custom_truck_details = truck
				break
	if not truck:
		return ""

	for it in doc.items:
		if not (it.get("custom_truck") or "").strip():
			it.custom_truck = truck
	return truck
