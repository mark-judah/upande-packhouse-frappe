// Business Unit / Farm are filled manually on the Sales Order, but two fields
// exist for each: the plain, visible custom_business_unit / custom_farm (what
// the sales rep actually types into) and the real accounting-dimension fields
// business_unit / farm (what pricing, box math, warehouse routing and every
// other script in this app reads). roses_invoice.sync_sales_order_accounting_dimensions
// already keeps these in sync SERVER-SIDE on save — this mirrors the same
// direction live on the CLIENT the moment the rep fills the visible field, so
// business-unit-gated automation (box_math.js, warehouse_routing.js) reacts
// immediately instead of only after a save round-trip.
frappe.ui.form.on('Sales Order', {
    custom_business_unit(frm) {
        if (frm.doc.custom_business_unit && frm.doc.business_unit !== frm.doc.custom_business_unit) {
            frm.set_value('business_unit', frm.doc.custom_business_unit);
        }
    },
    custom_farm(frm) {
        if (frm.doc.custom_farm && frm.doc.farm !== frm.doc.custom_farm) {
            frm.set_value('farm', frm.doc.custom_farm);
        }
    }
});
