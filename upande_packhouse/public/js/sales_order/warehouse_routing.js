// Roses warehouse routing: restrict `warehouse` (Sales Order Item's own
// native field) to the set of coldstores actually registered as a SOURCE in
// SO Warehouse Mapping "Roses-MAP" -- dynamic, not a hardcoded name-pattern
// filter, so adding a new farm's coldstore to Roses-MAP is the only step
// needed to make it choosable here too.
//
// `warehouse` is left exactly as picked (the farm's Receiving Cold Store).
// It used to be swapped for Roses-MAP's mapped delivery (Graded Sold)
// warehouse right on selection -- that skipped the two real stock moves
// stems must physically make on their way to a customer (coldstore ->
// Ungraded Sold when a bucket is issued, Ungraded Sold -> Graded Sold when
// the Farm Pack List submits -- see roses_warehouse_map.py). Those moves
// now happen at their own real, physical trigger points instead of being
// pretended-done the moment a Sales Order line is edited, so this file no
// longer touches `warehouse` after the operator picks it.
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
    async set_source_warehouse_query(frm) {
        try {
            var grid = frm.fields_dict.items && frm.fields_dict.items.grid;
            var gf = grid && grid.get_docfield && grid.get_docfield('warehouse');
            if (gf) { gf.link_filters = null; }   // neutralise the corrupting merge
        } catch (e) { /* non-fatal */ }

        let source_warehouses = [];
        try {
            let map_doc = await frappe.db.get_doc('SO Warehouse Mapping', 'Roses-MAP');
            source_warehouses = (map_doc.items || [])
                .map(item => item.source_warehouse)
                .filter(Boolean);
        } catch (e) { /* Roses-MAP not present yet -- fall through to an empty list */ }

        frm.set_query('warehouse', 'items', function () {
            return {
                filters: [
                    ['Warehouse', 'name', 'in', source_warehouses],
                ]
            };
        });
    }
});
