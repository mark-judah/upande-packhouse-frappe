// Auto Repeat > Roses Packhouse tab (Sales Order repeats only; the tab itself is
// hidden for every other document type by its depends_on). Shows the full
// schedule against the delivery date. The email is the Notification tab's.
// Server side: standing_orders.py, api/v2/standing_orders.py.

const SO_API = "upande_packhouse.api.v2.standing_orders.";

function so_day(iso) {
	if (!iso) return "";
	return frappe.datetime.str_to_user(iso) + " (" + moment(iso).format("ddd") + ")";
}

function is_so_repeat(frm) {
	return frm.doc.reference_doctype === "Sales Order";
}

function render_schedule(frm) {
	const wrap = frm.fields_dict.custom_schedule_preview;
	if (!wrap) return;
	if (!is_so_repeat(frm) || !frm.doc.start_date || !frm.doc.frequency) {
		wrap.$wrapper.html("");
		return;
	}
	frappe
		.call({
			method: SO_API + "preview_schedule",
			args: {
				source: frm.doc.reference_document,
				frequency: frm.doc.frequency,
				start_date: frm.doc.start_date,
				end_date: frm.doc.end_date,
				weekdays: JSON.stringify((frm.doc.repeat_on_days || []).map((d) => d.day)),
				repeat_on_day: frm.doc.repeat_on_day,
				repeat_on_last_day: frm.doc.repeat_on_last_day,
				days_after: frm.doc.custom_delivery_days_after || 0,
				count: 8,
			},
			type: "POST",
		})
		.then((r) => {
			const m = r.message || {};
			if (!m.success) {
				wrap.$wrapper.html(`<p class="text-muted">${frappe.utils.escape_html(m.error || "")}</p>`);
				return;
			}
			const rows = (m.schedule || [])
				.map(
					(s) =>
						`<tr><td>${so_day(s.appears)}</td><td>${so_day(s.delivers)}</td></tr>`
				)
				.join("");
			wrap.$wrapper.html(
				`<table class="table table-bordered" style="max-width:520px">
					<thead><tr><th>${__("Order appears")}</th><th>${__("Delivered")}</th></tr></thead>
					<tbody>${rows || `<tr><td colspan="2" class="text-muted">${__("No dates yet")}</td></tr>`}</tbody>
				</table>`
			);
		});
}

frappe.ui.form.on("Auto Repeat", {
	refresh(frm) {
		if (!is_so_repeat(frm)) return;
		frm.set_df_property("start_date", "label", __("First order appears"));
		frm.set_df_property("next_schedule_date", "label", __("Next order appears"));
		render_schedule(frm);
	},
});

// anything that moves the schedule redraws it
["start_date", "end_date", "frequency", "repeat_on_day", "repeat_on_last_day", "custom_delivery_days_after"].forEach((f) => {
	frappe.ui.form.on("Auto Repeat", f, (frm) => render_schedule(frm));
});
frappe.ui.form.on("Auto Repeat Day", {
	day(frm) {
		render_schedule(frm);
	},
});
frappe.ui.form.on("Auto Repeat", {
	repeat_on_days_remove(frm) {
		render_schedule(frm);
	},
});
