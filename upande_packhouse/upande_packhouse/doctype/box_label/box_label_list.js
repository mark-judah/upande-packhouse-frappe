// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt
/* global upande_packhouse */

const show_qr_codes = (names) =>
	frappe.require("/assets/upande_packhouse/js/box_label_qr.js", () =>
		upande_packhouse.box_label_qr.show(names)
	);

frappe.listview_settings["Box Label"] = {
	button: {
		show: () => true,
		get_label: () => __("QR Codes"),
		get_description: (doc) => doc.name,
		action: (doc) => show_qr_codes([doc.name]),
	},

	onload(listview) {
		listview.page.add_actions_menu_item(__("QR Codes"), () => {
			const names = listview.get_checked_items(true);
			if (names.length) show_qr_codes(names);
		});
	},
};
