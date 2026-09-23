// Roses warehouse routing: restrict `warehouse` (Sales Order Item's own
// native field) to the set of coldstores actually registered as a SOURCE in
// SO Warehouse Mapping "Roses-MAP" -- dynamic, not a hardcoded name-pattern
// filter, so adding a new farm's coldstore to Roses-MAP is the only step
// needed to make it choosable here too.
//
// `warehouse` is left exactly as picked (the coldstore the stems are sold
// from). It used to be swapped for Roses-MAP's mapped delivery (Graded Sold)
// warehouse right on selection -- that skipped the real stock moves stems must
// physically make on their way to a customer (the Sold leg when a bucket is
// issued, then Packing/Dispatch/Loading -- see stock_movement.STAGES). Those
// moves now happen at their own real, physical trigger points instead of being
// pretended-done the moment a Sales Order line is edited, so this file no
// longer touches `warehouse` after the operator picks it.
//
// The link_filters neutralise trick is retained: ERPNext's setup_queries applies
// an ARRAY-form Warehouse query to every Warehouse link field, and the link
// control object-spread-merges the field's `link_filters` into that array. If
// link_filters is non-empty the merge corrupts the array indices into operator
// slots -> "Operator must be one of ..." 417. NULL it, then supply a clean
// array-form query via set_query.
frappe.ui.form.on("Sales Order", {
	onload(frm) {
		frm.events.set_source_warehouse_query(frm);
	},
	refresh(frm) {
		frm.events.set_source_warehouse_query(frm);
		apply_roses_routing(frm);
	},
	// An explicit farm change re-points the WHOLE order: the rows already on
	// it were routed through the previous farm's packhouse and would now be
	// wrong, so this is the one case that overwrites rather than fills blanks.
	farm(frm) {
		apply_roses_routing(frm, { overwrite: true });
	},
	custom_farm(frm) {
		apply_roses_routing(frm, { overwrite: true });
	},
	business_unit(frm) {
		frm.events.set_source_warehouse_query(frm);
		apply_roses_routing(frm);
	},
	async set_source_warehouse_query(frm) {
		try {
			var grid = frm.fields_dict.items && frm.fields_dict.items.grid;
			var gf = grid && grid.get_docfield && grid.get_docfield("warehouse");
			if (gf) {
				gf.link_filters = null;
			} // neutralise the corrupting merge
		} catch (e) {
			/* non-fatal */
		}

		let source_warehouses = [];
		try {
			let map_doc = await frappe.db.get_doc("SO Warehouse Mapping", "Roses-MAP");
			source_warehouses = (map_doc.items || [])
				.map((item) => item.source_warehouse)
				.filter(Boolean);
		} catch (e) {
			/* Roses-MAP not present yet -- fall through to an empty list */
		}

		// Decided per CALL, not per registration: the business unit is often
		// still blank when the form first loads and is filled in a moment
		// later, and every other Roses-only rule on this doctype is gated the
		// same way (see the mandatory_depends_on on custom_length /
		// custom_number_of_boxes / custom_truck).
		//
		// Roses-MAP describes the Roses pipeline and nothing else, so anyone
		// else's order -- Coffee, Dairy, whatever the group adds next -- must
		// keep an ordinary warehouse list. Restricting it to Roses coldstores
		// for every business unit would leave those orders unable to pick any
		// valid warehouse at all.
		frm.set_query("warehouse", "items", function () {
			if (!is_roses(frm)) {
				return { filters: { company: frm.doc.company, is_group: 0 } };
			}
			return {
				filters: [["Warehouse", "name", "in", source_warehouses]],
			};
		});
	},
});

// Rows added by hand in the grid. Registered on the CHILD doctype, not on
// "Sales Order": "<table fieldname>_add" is named after the parent's table
// field, but grid.add_new_row() triggers it with the new CHILD row's doctype
// ("Sales Order Item"), and script_manager only runs handlers registered under
// the doctype it was passed -- so on "Sales Order" this silently never fires.
// ERPNext's own items_add (which is what fills a new row's warehouse from
// set_warehouse) sits on "Sales Order Item" for the same reason.
frappe.ui.form.on("Sales Order Item", {
	items_add(frm) {
		apply_roses_routing(frm);
	},
});

// The order's business unit, from whichever of the two fields carries it --
// custom_business_unit is what a Floriday-origin order and the visible form
// field use, business_unit is the real accounting dimension
// (accounting_dimension_sync.js mirrors one onto the other).
function is_roses(frm) {
	return (frm.doc.business_unit || frm.doc.custom_business_unit) === "Roses";
}

/* =====================================================
   ROUTING AUTOFILL — source warehouse + truck per line
   =====================================================

   The warehouse a Roses line is sold FROM is not a free choice: Roses-MAP
   already spells out, per farm, the warehouse the stems sit in immediately
   before the Sold leg moves them on (the packhouse coldstore an outlying
   farm's stems have already been trucked into -- see
   roses_warehouse_map.pre_graded_warehouse, which reads stock_movement's own
   route rather than re-deriving the chain). So it is prefilled instead of
   typed, and the link query above still lets the operator override it.

   The header's own Set Source Warehouse (`set_warehouse`) is filled from the
   same answer. It is not decoration: ERPNext seeds a grid-added row's
   warehouse from it, so leaving it blank is what makes the NEXT row the
   operator adds by hand come up empty even on an order whose existing lines
   are all routed. On a farm change it is set through frm.set_value, which
   fires ERPNext's own cascade and re-points every line (correct -- the old
   lines were routed through the previous farm's packhouse). Otherwise it is
   assigned directly, WITHOUT the cascade, so a warehouse someone deliberately
   picked on one line survives a plain refresh.

   The truck is the order's own Remote Truck Details copied down per line
   (what pick lists read as `transit_truck`). misc_autopopulate.js already
   does that when the header field CHANGES; this also covers rows that appear
   afterwards, including the ones spec_autofill appends straight onto
   add_child, which fire no grid event at all.

   Server-side, roses_warehouse_map.sales_order_apply_routing does the same on
   every save, so an order built over the API (or by an integration that never
   runs this file) still lands correctly. This is the live-feedback half:
   the operator sees the warehouse the moment the farm is set. */

/* Wipe the previous farm's routing off the header and every line. Only ever
   called on an overwrite (a farm change) that found no mapping for the new
   farm -- see apply_roses_routing. */
function clear_roses_routing(frm) {
	if (frm.doc.set_warehouse) {
		frm.set_value("set_warehouse", "");
	}
	(frm.doc.items || []).forEach((row) => {
		if (row.warehouse) {
			frappe.model.set_value(row.doctype, row.name, "warehouse", "");
		}
	});
}

async function apply_roses_routing(frm, opts) {
	const overwrite = !!(opts && opts.overwrite);
	if (frm.doc.docstatus !== 0) return; // submitted/cancelled items are frozen
	if (!is_roses(frm)) return;

	const warehouse = await roses_pre_graded_warehouse(frm);
	const truck = frm.doc.custom_truck_details;
	if (!warehouse && !truck) {
		// An overwrite means the FARM changed, and every existing line is now
		// routed through the previous farm's packhouse. If the new farm has no
		// Roses-MAP row we have nothing to re-point them to -- but leaving them
		// is the one outcome that is certainly wrong, because the order would
		// then ship sourced from a farm it is no longer for, silently. Clear
		// them instead and say why: a blank warehouse stops the save, which is
		// the behaviour a missing mapping should have.
		if (overwrite && !warehouse) {
			clear_roses_routing(frm);
			frappe.show_alert(
				{
					message: __(
						"No warehouse mapping for this farm -- source warehouse cleared on every line. Add the farm to Roses-MAP, or set the warehouse by hand."
					),
					indicator: "orange",
				},
				10
			);
		}
		return;
	}

	if (warehouse && frm.doc.set_warehouse !== warehouse) {
		if (overwrite) {
			// Farm changed -- let ERPNext's set_warehouse handler re-point every
			// existing line, which is exactly what a new farm means.
			frm.set_value("set_warehouse", warehouse);
		} else if (!frm.doc.set_warehouse) {
			// Fill the blank header field only. A direct assign skips ERPNext's
			// cascade (which overwrites EVERY row, hand-picked ones included);
			// the per-row loop below fills blanks and nothing else.
			frm.doc.set_warehouse = warehouse;
			frm.refresh_field("set_warehouse");
		}
	}

	(frm.doc.items || []).forEach((row) => {
		// set_value only marks the form dirty when the value actually changes,
		// so merely opening an already-routed order never dirties it.
		if (warehouse && (overwrite || !row.warehouse)) {
			frappe.model.set_value(row.doctype, row.name, "warehouse", warehouse);
		}
		if (truck && !row.custom_truck) {
			frappe.model.set_value(row.doctype, row.name, "custom_truck", truck);
		}
	});
}

// Cached per farm: the farm changes rarely, the item loop runs on every
// refresh and every added row.
async function roses_pre_graded_warehouse(frm) {
	const farm = frm.doc.farm || frm.doc.custom_farm;
	if (!farm) return null;
	if (frm.__roses_routing && frm.__roses_routing.farm === farm) {
		return frm.__roses_routing.warehouse;
	}
	let warehouse = null;
	try {
		const r = await frappe.call({
			method: "upande_packhouse.roses_warehouse_map.routing_defaults",
			args: { farm },
		});
		warehouse = (r.message || {}).warehouse || null;
	} catch (e) {
		/* no Roses-MAP row for this farm -- leave the line for the operator */
	}
	frm.__roses_routing = { farm, warehouse };
	return warehouse;
}
