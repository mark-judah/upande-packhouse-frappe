// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

// Bucket QR Code list: "Regenerate QR Codes" redraws the QR image of the selected
// buckets (api/bucket_qr_codes.py) — same payload as the printed labels.
frappe.listview_settings["Bucket QR Code"] = {
	onload(listview) {
		const run = () => {
			const names = listview.get_checked_items(true);
			if (!names.length) {
				frappe.msgprint(__("Select the buckets whose QR codes should be regenerated."));
				return;
			}
			frappe.confirm(
				__(
					"Regenerate the QR code of {0} bucket(s)? Their current QR images are replaced.",
					[names.length]
				),
				() =>
					frappe
						.call({
							method: "upande_packhouse.api.bucket_qr_codes.regenerate",
							args: { names },
							freeze: true,
							freeze_message: __("Regenerating QR codes…"),
						})
						.then((r) => {
							const m = r.message || {};
							if (m.queued) {
								frappe.show_alert(
									{
										message: __(
											"Regenerating {0} QR codes in the background — you'll be told when it's done.",
											[m.queued]
										),
										indicator: "blue",
									},
									7
								);
								return;
							}
							const failed = (m.failed || []).length;
							frappe.show_alert(
								{
									message: failed
										? __(
												"Regenerated {0} QR code(s), {1} failed (see Error Log).",
												[m.regenerated, failed]
										  )
										: __("Regenerated {0} QR code(s).", [m.regenerated]),
									indicator: failed ? "orange" : "green",
								},
								6
							);
							listview.refresh();
						})
			);
		};
		listview.page.add_inner_button(__("Regenerate QR Codes"), run);
		listview.page.add_actions_menu_item(__("Regenerate QR Codes"), run, false);
	},
};
