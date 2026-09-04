// Roses warehouse routing: restrict custom_source_warehouse to the v16
// sale-source warehouses, and derive the delivery `warehouse` from it via
// SO Warehouse Mapping "Roses-MAP".

// v16 dropped the "Available for Sale" / "Ungraded for Sale" warehouse types;
// sale-ready graded stock now sits in each farm's "<Farm> Receiving Cold
// Store", so we filter custom_source_warehouse on that instead.
//
// The link_filters neutralise trick is retained: ERPNext's setup_queries applies
// an ARRAY-form Warehouse query to every Warehouse link field, and the link
// control object-spread-merges the field's `link_filters` into that array. If
// link_filters is non-empty the merge corrupts the array indices into operator
// slots -> "Operator must be one of ..." 417. NULL it, then supply a clean
// array-form query via set_query.
frappe.ui.form.on('Sales Order', {
    onload(frm)  { frm.events.set_source_warehouse_query(frm); },
    refresh(frm) { frm.events.set_source_warehouse_query(frm); },
    set_source_warehouse_query(frm) {
        try {
            var grid = frm.fields_dict.items && frm.fields_dict.items.grid;
            var gf = grid && grid.get_docfield && grid.get_docfield('custom_source_warehouse');
            if (gf) { gf.link_filters = null; }   // neutralise the corrupting merge
        } catch (e) { /* non-fatal */ }
        frm.set_query('custom_source_warehouse', 'items', function () {
            return {
                filters: [
                    ['Warehouse', 'name', 'like', '%Receiving Cold Store%'],
                    ['Warehouse', 'is_group', '=', 0]
                ]
            };
        });
    }
});

// Delivery (target) warehouse from Roses-MAP, keyed by the row's own source
// warehouse. Reads the real accounting-dimension field (business_unit)
// directly — not the legacy custom_business_unit mirror, which is only synced
// server-side on save (see roses_invoice.sync_sales_order_accounting_dimensions)
// and so lags behind on an unsaved/just-edited form.
async function apply_target_warehouse(frm, cdt, cdn) {
    if (frm.doc.business_unit !== 'Roses') return;
    let row = locals[cdt][cdn];
    if (!row.custom_source_warehouse) return;

    let source_warehouse = row.custom_source_warehouse;
    try {
        let map_doc = await frappe.db.get_doc('SO Warehouse Mapping', 'Roses-MAP');
        let map_array = map_doc.items || [];
        if (!map_array.length) {
            frappe.throw(__('No warehouse mapping items found in Roses-MAP'));
            return;
        }
        let map_object = map_array.find(item => item.source_warehouse === source_warehouse);
        if (!map_object) {
            frappe.throw(__('No delivery warehouse mapping found for source warehouse: {0}', [source_warehouse]));
            return;
        }
        if (!map_object.delivery_warehouse) {
            frappe.throw(__('Delivery warehouse not set for source warehouse: {0}', [source_warehouse]));
            return;
        }
        row.warehouse = map_object.delivery_warehouse;
    } catch (e) {
        frappe.throw(e.message);
    }
}

frappe.ui.form.on('Sales Order Item', {
    custom_source_warehouse(frm, cdt, cdn) { apply_target_warehouse(frm, cdt, cdn); },
    item_code(frm, cdt, cdn) { apply_target_warehouse(frm, cdt, cdn); }   // for confirmation in case it was missed
});
