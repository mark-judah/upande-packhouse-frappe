// Sales Order — Actions shortcuts to the two pages that pick up from here:
// the Order Pick List(s) generated for this order, and the Sales Allocation
// dashboard, pre-filtered to this order so the allocator doesn't have to hunt
// for it in the list. The allocation button's own label reflects how far the
// order is already allocated, so it reads as a next-step verb rather than a
// static page name.

frappe.ui.form.on('Sales Order', {
    refresh(frm) {
        frm.add_custom_button(__('Order Pick List'), () => {
            frappe.set_route('list', 'Order Pick List', { sales_order: frm.doc.name });
        }, __('Actions'));

        // Allocation only makes sense once the order is submitted — that's
        // also what the allocation page's own order list requires.
        if (frm.doc.docstatus === 1) {
            frappe.call({
                method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.get_order_allocation_status',
                args: { sales_order: frm.doc.name },
                callback: (r) => {
                    const status = (r.message && r.message.status) || 'none';
                    const label = status === 'full' ? __('View Allocation')
                        : status === 'partial' ? __('Continue Allocating')
                        : __('Allocate');
                    // The label depends on server-side state that can change
                    // between refreshes (someone allocates, or re-runs
                    // allocation) without a fresh add_custom_button knowing to
                    // replace an earlier, differently-labeled one -- so drop
                    // every label this button could have had before adding
                    // the current one. Scoped to just this button (not
                    // clear_custom_buttons, which would also wipe the Mixed
                    // Box wizard's own Actions buttons from another script).
                    [__('Allocate'), __('Continue Allocating'), __('View Allocation')].forEach(l => {
                        frm.remove_custom_button(l, __('Actions'));
                    });
                    frm.add_custom_button(label, () => {
                        frappe.route_options = {
                            sales_order: frm.doc.name,
                            farm: frm.doc.farm || frm.doc.custom_farm,
                            transaction_date: frm.doc.transaction_date
                        };
                        frappe.set_route('sales-allocation');
                    }, __('Actions'));
                }
            });
        }
    }
});
