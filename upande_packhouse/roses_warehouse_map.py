"""Sales Order routing off the SO Warehouse Mapping (Roses-MAP): the warehouse
a Roses line is sold FROM, plus the order's truck.

The mapping itself is owned by stock_movement.py -- its STAGES spell out the
whole pipeline a bucket walks:

    source_warehouse --Arrival--> transfer_to --Sold--> delivery_warehouse
      --Packing--> packhouse --Dispatch--> dispatch_cold_store
      --Loading--> delivery_truck

and stock_movement.resolve_route walks it (including the legacy shape where an
outlying farm's row has no `transfer_to` and chains through another source
row). This module reads that same route rather than re-deriving the chain, so
the two can't drift: everything here is the `from` side of the **Sold** leg --
the consolidated packhouse cold store an outlying farm's stems have already
been trucked into, which is what a Sales Order Item's own `warehouse` has to
point at for issuing and the Delivery Note to resolve.

Nothing is hardcoded to a farm name or a "<Farm> X - KR" naming pattern; a farm
is resolved through Warehouse.custom_farm on the mapping's own source rows.
"""

import frappe

from upande_packhouse import stock_movement

BUSINESS_UNIT = "Roses"


def source_warehouse_for_farm(farm, business_unit=BUSINESS_UNIT):
	"""The mapping's own source warehouse (Receiving Cold Store) for a FARM.

	A Sales Order names a farm and never a warehouse, so the farm is matched
	against Warehouse.custom_farm on each mapped source row -- which is exactly
	what stock_movement.mapping_row_for_farm already does, unmapped sites
	included (it answers None rather than throwing, and a missing mapping must
	never fail a Sales Order save: everything here only PREFILLS a form).
	"""
	row = stock_movement.mapping_row_for_farm(farm, business_unit) if farm else None
	return row.source_warehouse if row else None


def pre_graded_warehouse(source_warehouse, business_unit=BUSINESS_UNIT):
	"""The warehouse stock sits in immediately BEFORE the Sold leg moves it on
	-- i.e. that leg's `from` side, which is what the Sales Order line sells
	from: `transfer_to` on a row that has one (the outlying farm's stems are
	trucked into the packhouse cold store before anything is sold from them),
	else the source cold store itself.
	"""
	if not source_warehouse:
		return None
	try:
		route = stock_movement.resolve_route(source_warehouse, business_unit, upto=stock_movement.SALE_STAGE)
	except Exception:
		# Unmapped business unit: resolve_route throws, which is right for a
		# stock move (the leg cannot be posted) but never worth a failed save
		# here -- fall back to the coldstore itself.
		#
		# resolve_route also throws on a CYCLIC map ("Warehouse mapping loops
		# at ..."), and that is not a benign missing-mapping: it silently routes
		# every Roses line to the receiving cold store instead of the packhouse,
		# with nothing anywhere to say so. Still not worth failing a Sales Order
		# save over -- but it must leave a trace, or a broken Roses-MAP is
		# invisible until someone reconciles stock.
		frappe.log_error(
			title="roses_warehouse_map: could not resolve route",
			message=(
				f"source_warehouse={source_warehouse} business_unit={business_unit}\n"
				f"Falling back to the source warehouse itself.\n\n{frappe.get_traceback()}"
			),
		)
		return source_warehouse
	for hop in route:
		if hop["stage"] == stock_movement.SALE_STAGE:
			return hop["from"]
	# No Sold leg mapped -- the stems never leave the cold store they arrived in.
	return source_warehouse


def pre_graded_warehouse_for_farm(farm, business_unit=BUSINESS_UNIT):
	"""pre_graded_warehouse keyed by a FARM -- what a Sales Order has to go on."""
	source = source_warehouse_for_farm(farm, business_unit)
	return pre_graded_warehouse(source, business_unit) if source else None


@frappe.whitelist()
def routing_defaults(farm: str | None = None, source_warehouse: str | None = None):
	"""What the Sales Order form should prefill, for one farm (or coldstore).
	Client counterpart of sales_order_apply_routing -- warehouse_routing.js
	calls this so the operator sees the warehouse the moment the farm is set,
	rather than only after a save round-trip."""
	warehouse = pre_graded_warehouse_for_farm(farm) if farm else pre_graded_warehouse(source_warehouse)
	return {"warehouse": warehouse}


def sales_order_apply_routing(doc, method=None):
	"""Roses Sales Order: fill each line's `warehouse` (and the header's own
	`set_warehouse`) from the mapping, and settle the truck via sync_truck.

	Both were already meant to be on every line -- ERPNext's own set_warehouse
	cascade and misc_autopopulate.js's items_add handler fill them when a row is
	added through the grid -- but neither fires for a row built in code:
	spec_autofill's Add to Order Object.assigns straight onto add_child
	(deliberately, so the spec's own values aren't re-fired over), the mixed-box
	wizard does the same, and an order created over the API never runs form JS
	at all. Those lines then reach allocation with a blank warehouse, which is
	what leaves issuing with nothing to route from and a Delivery Note unable to
	resolve where to deduct.

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

	if (doc.get("business_unit") or doc.get("custom_business_unit") or "") != BUSINESS_UNIT:
		return

	farm = doc.get("farm") or doc.get("custom_farm")
	warehouse = pre_graded_warehouse_for_farm(farm) if farm else None
	if not warehouse:
		return

	# The header's own Source Warehouse, for the next row the operator adds
	# through the grid (ERPNext cascades set_warehouse to children itself).
	if not doc.get("set_warehouse"):
		doc.set_warehouse = warehouse

	for it in doc.items:
		if not it.get("warehouse"):
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
