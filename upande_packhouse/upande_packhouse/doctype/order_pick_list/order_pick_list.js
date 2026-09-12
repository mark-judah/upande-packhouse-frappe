// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

frappe.ui.form.on("Order Pick List", {
	refresh(frm) {
		if (!frm.doc.sales_order) return;

		frm.add_custom_button(__('Sales Allocation'), () => {
			// This OPL's own date_created can differ from the Sales Order's
			// transaction_date by a day or more (OPL is often generated
			// after the order is placed) -- the allocation page's deep-link
			// widens its date window to an EXACT match on transaction_date,
			// so using date_created here silently excludes the order.
			// Fetch the SO's own value instead.
			frappe.db.get_value('Sales Order', frm.doc.sales_order, 'transaction_date').then((r) => {
				frappe.route_options = {
					sales_order: frm.doc.sales_order,
					farm: frm.doc.farm,
					transaction_date: r.message && r.message.transaction_date
				};
				frappe.set_route('sales-allocation');
			});
		}, __('Actions'));
	},
});
