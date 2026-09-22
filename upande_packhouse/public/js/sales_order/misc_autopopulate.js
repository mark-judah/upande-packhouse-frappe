// Small, independent Sales Order autopopulates carried over from v15/v16-local.

// Keep the Order Name's trailing number in sync with the submitted SO's own
// number, so a customer-facing order name still reads correctly after Frappe
// assigns the real document name on submit.
frappe.ui.form.on("Sales Order", {
	on_submit(frm) {
		if (!frm.doc.name || !frm.doc.custom_order_name) return;

		let so_number = frm.doc.name.split("-").pop();
		let current_order_name = frm.doc.custom_order_name;
		// Must require the DASH too, not just trailing digits -- otherwise a
		// purely numeric order name (e.g. "4389") reads as "already has our
		// appended suffix" and gets its entire value replaced by so_number
		// instead of having it appended (so "4389" -> "000857" instead of
		// "4389-000857"). The dash is what actually marks a suffix as one
		// WE appended (on an earlier submit of this same order, e.g. after
		// an amendment changed the SO's own number) versus the order name
		// just happening to end in digits.
		let has_number_suffix = /-\d+$/.test(current_order_name);
		let new_order_name = has_number_suffix
			? current_order_name.replace(/-\d+$/, "-" + so_number)
			: current_order_name + "-" + so_number;

		if (frm.doc.custom_order_name === new_order_name) return;

		frappe.call({
			method: "frappe.client.set_value",
			args: {
				doctype: "Sales Order",
				name: frm.doc.name,
				fieldname: "custom_order_name",
				value: new_order_name,
			},
			callback(r) {
				if (!r.exc) {
					frm.reload_doc();
					frappe.show_alert(
						{
							message: __("Order name updated to {0}", [new_order_name]),
							indicator: "green",
						},
						5
					);
				}
			},
		});
	},

	custom_truck_details(frm) {
		if (!frm.doc.custom_truck_details) return;
		frm.doc.items.forEach((row) => {
			row.custom_truck = frm.doc.custom_truck_details;
		});
		frm.refresh_field("items");
	},

	delivery_date(frm) {
		if (!frm.doc.delivery_date) return;
		frm.set_value("custom_week", so_week_number(new Date(frm.doc.delivery_date)));
	},

	refresh(frm) {
		// Belt and braces. items_add covers the grid's own Add Row, but rows
		// also arrive by routes that fire no grid event at all — spec_autofill's
		// Add to Order and the mixed-box wizard both go straight onto add_child
		// — and a sweep on refresh catches every one of them without this file
		// having to know they exist.
		sync_truck_rows(frm);
	},
});

// ...and the other direction: a truck typed onto a LINE backfills the order's
// own Remote Truck Details. The grid is where the operator already is, so the
// truck lands there as often as in the header — and a blank header is what
// then leaves every LATER row with nothing to inherit.
frappe.ui.form.on("Sales Order Item", {
	custom_truck(frm) {
		sync_truck_rows(frm);
	},

	// New rows should carry the order's truck too. "<table fieldname>_add" is
	// named after the PARENT's table field but is triggered with the CHILD
	// doctype -- grid.add_new_row() calls
	// script_manager.trigger("items_add", d.doctype, d.name) with d being the
	// new Sales Order ITEM, and script_manager only looks up handlers
	// registered under the doctype it was passed. Registered on "Sales Order"
	// (where the name makes it look like it belongs) it silently never fires,
	// which is exactly how a hand-added row came up with the warehouse ERPNext
	// fills here -- its own items_add sits on "Sales Order Item" -- and no
	// truck. Same for items_remove (see grid_row.remove).
	items_add(frm) {
		sync_truck_rows(frm);
	},
});

// The order's truck: the header field when it has one, else whatever the lines
// already carry. The fallback is what keeps a truck alive across an added row
// on an order where it was only ever typed into the grid.
function order_truck(frm) {
	const header = (frm.doc.custom_truck_details || "").trim();
	if (header) return header;
	const row = (frm.doc.items || []).find((r) => (r.custom_truck || "").trim());
	return row ? row.custom_truck.trim() : "";
}

// Settle the truck across header and lines, filling BLANKS ONLY in both
// directions. Deliberately a sweep rather than a per-event copy: whichever
// event happens to fire (or not) for a given way of adding a row, the next one
// puts it right, so a new line can't end up as the only one without a truck.
// Mirrors roses_warehouse_map.sync_truck, which does the same on validate for
// the routes that never run form JS at all.
//
// Blanks only, and the header is assigned WITHOUT frm.set_value on purpose:
// set_value would fire the custom_truck_details handler below, which overwrites
// every line — so backfilling the header off line 1 would wipe a different
// truck deliberately set on line 2 (a split load). The handler stays an
// overwrite because typing into that field IS the "retruck the whole order"
// action; this sweep just isn't that.
function sync_truck_rows(frm) {
	if (frm.doc.docstatus !== 0 || frm.__truck_sweep) return;
	const truck = order_truck(frm);
	if (!truck) return;

	frm.__truck_sweep = true;
	try {
		if (!(frm.doc.custom_truck_details || "").trim()) {
			frm.doc.custom_truck_details = truck;
			frm.refresh_field("custom_truck_details");
		}
		let filled = 0;
		(frm.doc.items || []).forEach((row) => {
			if (!(row.custom_truck || "").trim()) {
				// Assigned straight onto the row rather than through
				// frappe.model.set_value: set_value resolves the row via
				// locals[doctype][name] and does nothing at all, silently, if
				// that lookup misses -- which is exactly the case for a row the
				// grid has only just created. Writing the row we already hold
				// cannot miss. frm.dirty() below is what set_value would have
				// given us, so the value still reaches the database.
				row.custom_truck = truck;
				filled++;
			}
		});
		if (filled) {
			frm.dirty();
			frm.refresh_field("items");
		}
	} finally {
		frm.__truck_sweep = false;
	}
}

// ISO-8601 week number.
function so_week_number(date) {
	let d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
	d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
	let yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
	return Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
}
