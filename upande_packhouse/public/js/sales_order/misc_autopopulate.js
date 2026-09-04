// Small, independent Sales Order autopopulates carried over from v15/v16-local.

// Keep the Order Name's trailing number in sync with the submitted SO's own
// number, so a customer-facing order name still reads correctly after Frappe
// assigns the real document name on submit.
frappe.ui.form.on('Sales Order', {
    on_submit(frm) {
        if (!frm.doc.name || !frm.doc.custom_order_name) return;

        let so_number = frm.doc.name.split('-').pop();
        let current_order_name = frm.doc.custom_order_name;
        let has_number_suffix = /\d+$/.test(current_order_name);
        let new_order_name = has_number_suffix
            ? current_order_name.replace(/\d+$/, so_number)
            : current_order_name + '-' + so_number;

        if (frm.doc.custom_order_name === new_order_name) return;

        frappe.call({
            method: 'frappe.client.set_value',
            args: {
                doctype: 'Sales Order',
                name: frm.doc.name,
                fieldname: 'custom_order_name',
                value: new_order_name
            },
            callback(r) {
                if (!r.exc) {
                    frm.reload_doc();
                    frappe.show_alert({ message: __('Order name updated to {0}', [new_order_name]), indicator: 'green' }, 5);
                }
            }
        });
    },

    custom_truck_details(frm) {
        if (!frm.doc.custom_truck_details) return;
        frm.doc.items.forEach(row => { row.custom_truck = frm.doc.custom_truck_details; });
        frm.refresh_field('items');
    },

    delivery_date(frm) {
        if (!frm.doc.delivery_date) return;
        frm.set_value('custom_week', so_week_number(new Date(frm.doc.delivery_date)));
    },

    // New rows should carry the order's truck details too. "items_add" fires on
    // the PARENT doctype (named "<table fieldname>_add"), not on a registration
    // for the child doctype itself — the earlier version of this script
    // registered it under 'Sales Order Item', where it silently never fired.
    items_add(frm, cdt, cdn) {
        if (!frm.doc.custom_truck_details) return;
        let row = frappe.get_doc(cdt, cdn);
        row.custom_truck = frm.doc.custom_truck_details;
        frm.refresh_field('items');
    }
});

// ISO-8601 week number.
function so_week_number(date) {
    let d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
    d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
    let yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
    return Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
}
