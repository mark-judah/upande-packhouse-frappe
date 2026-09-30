// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt
/* global upande_packhouse */

frappe.ui.form.on("Box Label", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("QR Codes"), () =>
			frappe.require("/assets/upande_packhouse/js/box_label_qr.js", () =>
				upande_packhouse.box_label_qr.show([frm.doc.name])
			)
		);
	},
});
