// Box Label QR codes (own id + delivery point) -- shared by the form and the list view.
/* global upande_packhouse */
frappe.provide("upande_packhouse.box_label_qr");

upande_packhouse.box_label_qr.show = function (names) {
	frappe.call({
		method: "upande_packhouse.upande_packhouse.doctype.box_label.box_label.qr_codes",
		args: { names },
		freeze: true,
		callback({ message }) {
			if (!message || !message.labels.length) return;
			const labels = message.labels.map(codes_of);

			const d = new frappe.ui.Dialog({
				title: __("QR Codes"),
				size: labels.length > 1 ? "extra-large" : "large",
				fields: [{ fieldtype: "HTML", fieldname: "qr" }],
				primary_action_label: __("Print"),
				primary_action: () => print_labels(labels),
				secondary_action_label: __("Download"),
				secondary_action: () => labels.forEach((l) => l.codes.forEach(download)),
			});
			d.fields_dict.qr.$wrapper.html(
				labels
					.map(
						(
							l
						) => `<div style="display:flex;flex-wrap:wrap;justify-content:center;gap:24px;
							padding:12px 0;border-bottom:1px solid var(--border-color)">
							${l.codes.map(code_card).join("")}
						</div>`
					)
					.join("")
			);
			d.show();
		},
	});
};

function codes_of(label) {
	const codes = [
		{
			kind: __("Box Label"),
			text: frappe.utils.escape_html(label.name),
			image: label.box_qr,
			file: `${label.name}.png`,
		},
	];
	if (label.delivery_point_qr) {
		codes.push({
			kind: __("Delivery Point"),
			text: frappe.utils.escape_html(label.delivery_point),
			image: label.delivery_point_qr,
			file: `${label.name}-delivery-point.png`,
		});
	}
	return { name: label.name, codes };
}

function code_card(code) {
	return `<div style="text-align:center;width:220px">
		<div class="text-muted small">${code.kind}</div>
		<img src="${code.image}" style="width:200px;height:200px">
		<div style="font-size:15px;font-weight:600;margin-top:6px;word-break:break-all">${code.text}</div>
	</div>`;
}

function download(code) {
	const a = document.createElement("a");
	a.href = code.image;
	a.download = code.file;
	a.click();
}

function print_labels(labels) {
	const w = window.open("", "_blank");
	if (!w) return;
	const pages = labels
		.map(
			(l) =>
				`<section>${l.codes
					.map(
						(c) =>
							`<figure><small>${c.kind}</small><img src="${c.image}"><div>${c.text}</div></figure>`
					)
					.join("")}</section>`
		)
		.join("");
	w.document.write(`<!doctype html><html><head><title>${__("QR Codes")}</title>
		<style>@page{margin:10mm}body{margin:0;font-family:Arial,sans-serif}
		section{text-align:center;page-break-after:always}section:last-child{page-break-after:auto}
		figure{margin:8mm 0 0}small{font-size:10pt;color:#555}
		img{display:block;width:55mm;height:55mm;margin:2mm auto 0}
		div{font-size:15pt;font-weight:bold;margin-top:3mm;word-break:break-all}</style>
		</head><body>${pages}</body></html>`);
	w.document.close();
	w.onload = () => {
		w.focus();
		w.print();
	};
}
