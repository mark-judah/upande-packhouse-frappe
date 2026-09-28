// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

frappe.ui.form.on("Order Pick List", {
	refresh(frm) {
		// Testing aid: print every QR this OPL needs (buckets, shelves, bunches)
		// so it can be walked through the mobile app without the label printers.
		// System Manager only — it can create Grading stock entries.
		if (!frm.is_new() && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(
				__("Generate Test QR Codes"),
				() => open_test_label_dialog(frm),
				__("Testing")
			);
		}

		if (!frm.doc.sales_order) return;

		frm.add_custom_button(
			__("Sales Allocation"),
			() => {
				// This OPL's own date_created can differ from the Sales Order's
				// transaction_date by a day or more (OPL is often generated
				// after the order is placed) -- the allocation page's deep-link
				// widens its date window to an EXACT match on transaction_date,
				// so using date_created here silently excludes the order.
				// Fetch the SO's own value instead.
				frappe.db
					.get_value("Sales Order", frm.doc.sales_order, "transaction_date")
					.then((r) => {
						frappe.route_options = {
							sales_order: frm.doc.sales_order,
							farm: frm.doc.farm,
							transaction_date: r.message && r.message.transaction_date,
						};
						frappe.set_route("sales-allocation");
					});
			},
			__("Actions")
		);
	},
});

function open_test_label_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Generate Test QR Codes"),
		fields: [
			{ fieldname: "buckets", fieldtype: "Check", label: __("Bucket QR codes"), default: 1 },
			{
				fieldname: "shelves",
				fieldtype: "Check",
				label: __("Shelf QR codes"),
				default: 1,
				description: __(
					"Current shelves, plus empty shelves at the sales farm for buckets still to be transferred."
				),
			},
			{ fieldname: "bunches", fieldtype: "Check", label: __("Bunch QR codes"), default: 1 },
			{
				fieldname: "grade_bunches",
				fieldtype: "Check",
				label: __("Create grading entries for new bunches"),
				default: 1,
				depends_on: "bunches",
				description: __(
					"Packing only accepts a bunch that was graded from its bucket. Existing graded bunches are reused; only the missing ones are created (one Grading stock entry each)."
				),
			},
		],
		primary_action_label: __("Generate"),
		primary_action(values) {
			d.hide();
			frappe.call({
				method: "upande_packhouse.api.test_labels.generate_opl_test_labels",
				args: { opl: frm.doc.name, ...values },
				freeze: true,
				freeze_message: __("Generating QR codes…"),
				callback(r) {
					if (!r.message) return;
					const s = r.message.summary;
					frappe.show_alert(
						{
							message: __(
								"{0} bucket, {1} shelf, {2} bunch labels ({3} newly graded)",
								[s.buckets, s.shelves, s.bunches, s.grading_created]
							),
							indicator: "green",
						},
						7
					);
					if ((r.message.warnings || []).length) {
						frappe.msgprint({
							title: __("Some labels need attention"),
							indicator: "orange",
							message: r.message.warnings
								.map((w) => frappe.utils.escape_html(w))
								.join("<br>"),
						});
					}
					print_test_labels(r.message);
				},
			});
		},
	});
	d.show();
}

function print_test_labels(data) {
	const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
	const sections = (data.sections || [])
		.filter((sec) => sec.labels && sec.labels.length)
		.map(
			(sec) => `
			<h2>${esc(sec.title)} <span>${sec.labels.length}</span></h2>
			${sec.hint ? `<p class="hint">${esc(sec.hint)}</p>` : ""}
			<div class="grid">${sec.labels
				.map(
					(l) => `
				<div class="lbl ${esc(l.kind)}">
					<img src="data:image/png;base64,${l.png}" alt="${esc(l.id)}">
					<div class="id">${esc(l.id)}</div>
					${(l.lines || []).map((t) => `<div class="ln">${esc(t)}</div>`).join("")}
				</div>`
				)
				.join("")}</div>`
		)
		.join("");
	const html = `<!doctype html><html><head><meta charset="utf-8">
		<title>${esc(data.opl)} test labels</title>
		<style>
			body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:16px;color:#111}
			header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:8px}
			header h1{font-size:18px;margin:0} header .meta{color:#555;font-size:12px}
			header button{margin-left:auto;padding:6px 14px;font-size:13px;cursor:pointer}
			h2{font-size:14px;margin:18px 0 6px;border-bottom:1px solid #ddd;padding-bottom:4px}
			h2 span{color:#777;font-weight:500} .hint{font-size:11px;color:#666;margin:0 0 8px}
			.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
			.lbl{border:1px dashed #999;border-radius:4px;padding:6px;text-align:center;break-inside:avoid;page-break-inside:avoid}
			.lbl img{width:120px;height:120px;image-rendering:pixelated}
			.lbl .id{font:700 12px ui-monospace,Menlo,monospace;margin-top:2px;word-break:break-all}
			.lbl .ln{font-size:10px;color:#444;line-height:1.3}
			.lbl.shelf{border-color:#1d4ed8} .lbl.bunch{border-color:#15803d}
			@media print{header button{display:none} body{margin:6mm}}
		</style></head><body>
		<header><h1>${esc(data.order_name || data.opl)} — test labels</h1>
			<span class="meta">${esc(data.opl)} · ${esc(data.sales_order || "")} · ${esc(
		data.customer || ""
	)} · sales farm ${esc(data.sales_farm || "")}</span>
			<button onclick="window.print()">Print</button></header>
		${sections || "<p>No labels generated.</p>"}
		</body></html>`;
	const w = window.open("", "_blank");
	if (!w) {
		frappe.msgprint(__("Allow pop-ups for this site to open the label sheet."));
		return;
	}
	w.document.open();
	w.document.write(html);
	w.document.close();
}
