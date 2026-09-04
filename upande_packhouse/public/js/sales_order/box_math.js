// Live qty from packrate x boxes for STRAIGHT (non-mixed) rows, UOM autofill
// from the item's Sales UOM, and the Total Boxes / Total Stems order summary.
// The server engine (sales_order_engine.py) recomputes qty and the order
// summary authoritatively on save; this is just immediate feedback in the form.
// Mixed-box rows are owned by the mixed-box wizard.
frappe.ui.form.on('Sales Order Item', {
    custom_packrate(frm, cdt, cdn) { straight_calc(frm, cdt, cdn); recompute_order_summary(frm); },
    custom_number_of_boxes(frm, cdt, cdn) { straight_calc(frm, cdt, cdn); recompute_order_summary(frm); },
    custom_packrate_mixed_box(frm) { recompute_order_summary(frm); },
    uom(frm, cdt, cdn) { straight_calc(frm, cdt, cdn); },
    item_code(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row.item_code) return;
        // A directly-added line takes its UOM from the item's Sales UOM. If the item
        // has none, prompt so the user sets one (spec-filled lines get their UOM from
        // the specification and never trigger this handler).
        //
        // frappe.db.get_value's callback receives the fetched values directly —
        // NOT wrapped in {message: ...} the way a raw frappe.call() response is.
        // Checking `r.message` here always failed (r.message is undefined), so
        // this whole handler was a silent no-op: the "no Sales UOM" flag below
        // never fired, and the branch that looked like it worked was actually
        // just ERPNext's own native item_code handler filling uom from the
        // item's sales_uom in parallel — real, but not this handler's doing.
        frappe.db.get_value('Item', row.item_code, ['sales_uom', 'item_name'], r => {
            if (!r) return;
            if (r.item_name) frappe.model.set_value(cdt, cdn, 'item_name', r.item_name);
            if (r.sales_uom) {
                frappe.model.set_value(cdt, cdn, 'uom', r.sales_uom);
            } else {
                frappe.msgprint(__('Item {0} has no Sales UOM. Set a Sales UOM on the item so the order line has a unit of measure.', [row.item_code]));
            }
        });
    }
});

function so_uom_factor(uom) {
    const m = uom && uom.match(/\((\d+)\)/);
    return m ? cint(m[1]) : 1;
}

function straight_calc(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (row.custom_mixed_box || row.custom_mixed_bunch) return;  // mixed box/bunch rows are owned by the wizard/spec-autofill + server
    if (!row.custom_packrate || !row.custom_number_of_boxes || !row.uom) return;
    const stems = flt(row.custom_packrate) * flt(row.custom_number_of_boxes);   // Packrate name = stems/box
    const qty = stems / so_uom_factor(row.uom);
    frappe.model.set_value(cdt, cdn, 'stock_qty', stems);
    frappe.model.set_value(cdt, cdn, 'qty', qty);
    frappe.show_alert({
        message: __('{0} {1} = {2} stems', [qty.toFixed(2), row.uom, stems]),
        indicator: 'green'
    }, 3);
}

// Stems-per-box for ANY row shape — mirrors sales_order_engine._line_packrate.
function row_stems_per_box(row) {
    if (row.custom_mixed_box || row.custom_mixed_bunch) return cint(row.custom_packrate_mixed_box);
    return cint(row.custom_packrate);   // custom_packrate is a Link to Packrate whose name IS the stems-per-box number
}

// Live mirror of sales_order_engine._set_order_summary — the server recomputes
// this authoritatively on save regardless, so this is only for on-screen feedback
// before the user saves. Skips the set_value calls when nothing actually
// changed so merely opening/refreshing an already-correct order never marks
// the form dirty on its own.
function recompute_order_summary(frm) {
    let boxes = 0, stems = 0;
    (frm.doc.items || []).forEach(it => {
        if (!it.item_code) return;
        const b = cint(it.custom_number_of_boxes);
        boxes += b;
        stems += row_stems_per_box(it) * b;
    });
    if (cint(frm.doc.custom_total_boxes) !== boxes) frm.set_value('custom_total_boxes', boxes);
    if (cint(frm.doc.custom_total_stems) !== stems) frm.set_value('custom_total_stems', stems);
}

frappe.ui.form.on('Sales Order', {
    refresh(frm) {
        so_toggle_derived_readonly(frm);
        recompute_order_summary(frm);
    },
    business_unit(frm) { so_toggle_derived_readonly(frm); },
    // Row add/remove fire on the PARENT doctype, named "<table fieldname>_add"/"_remove" —
    // not on the child doctype's own registration above.
    items_add(frm) { recompute_order_summary(frm); },
    items_remove(frm) { recompute_order_summary(frm); }
});

// Qty & stock_qty are DERIVED (packrate x boxes) — keep them read-only on Roses
// orders so the user only enters Packrate + Number of Boxes, never the qty itself.
// Reads the real accounting-dimension field (business_unit) directly, not the
// legacy custom_business_unit mirror — that mirror is only synced server-side on
// save (see roses_invoice.sync_sales_order_accounting_dimensions), so it lags
// behind on an unsaved/just-edited form and this toggle would silently miss it.
function so_toggle_derived_readonly(frm) {
    const roses = (frm.doc.business_unit === 'Roses') ? 1 : 0;
    const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
    if (!grid) return;
    ['qty', 'stock_qty'].forEach(f => grid.update_docfield_property(f, 'read_only', roses));
    grid.refresh();
}
