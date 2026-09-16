// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

// Ported from a live-only Desk "Client Script" (never in the codebase) --
// the button itself was fine, it just called a broken Server Script; see
// loading_plan.py's fetch_loading_plan_orders for the actual fix.
frappe.ui.form.on("Loading Plan", {
	refresh: function (frm) {
		// Filter vehicle to dispatch trucks only
		frm.set_query("vehicle", function () {
			return {
				filters: {
					custom_dispatch_truck: 1,
				},
			};
		});

		if (frm.doc.delivery_date) {
			frm.add_custom_button(__("Fetch Orders"), function () {
				if (!frm.doc.location) {
					frappe.msgprint({
						title: __("Select a location"),
						message: __(
							"Choose a Location before fetching orders — the loading plan is built per location."
						),
						indicator: "orange",
					});
					return;
				}
				frappe.confirm(
					"This will replace all current items with fresh data from " +
						frappe.utils.escape_html(frm.doc.location) +
						" Sales Orders for " +
						frm.doc.delivery_date +
						". Continue?",
					function () {
						frappe.call({
							method: "upande_packhouse.upande_packhouse.doctype.loading_plan.loading_plan.fetch_loading_plan_orders",
							args: {
								delivery_date: frm.doc.delivery_date,
								location: frm.doc.location,
							},
							freeze: true,
							freeze_message: "Fetching orders...",
							callback: function (r) {
								if (r.message && r.message.status === "success") {
									frm.clear_table("loading_plan_items");
									var items = r.message.items || [];
									items.forEach(function (item) {
										var row = frm.add_child("loading_plan_items");
										row.customer = item.customer;
										row.delivery_point = item.delivery_point;
										row.loading_position = item.loading_position;
										row.box_type = item.box_type;
										row.number_of_boxes = item.number_of_boxes;
									});
									frm.refresh_field("loading_plan_items");
									frm.dirty();
									frappe.show_alert({
										message: r.message.message,
										indicator: "green",
									});
								} else {
									frappe.show_alert({
										message:
											(r.message && r.message.message) ||
											"Failed to fetch orders",
										indicator: "red",
									});
								}
							},
						});
					}
				);
			}).addClass("btn-primary");
		}
	},
});
