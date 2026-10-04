// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

// "Generate QR Codes" (Actions): bucket QR codes for the standard rows, bunch
// QR codes for the spray rows. Which is which is decided server-side per row
// (upande_packhouse.api.opl_qr_codes), so this never has to know what kind of
// order it is on. The codes are drawn and stored server-side; records that
// already carry an image are reused untouched -- their label is already
// printed and stuck on something physical.
//
// The result dialog is a slideshow: one label at a time, big enough to read
// and scan off the screen, stepped through with the arrow keys (Home/End jump
// to the first/last). A filmstrip underneath jumps straight to any label, and
// when an OPL has both kinds, a Buckets/Bunches switch jumps between groups.
// Printing is one label per page.

const OPL_QR_API = "upande_packhouse.api.opl_qr_codes";
// Namespace for the document-level keydown handler, so it can be removed
// cleanly when the dialog closes and never stacks up across re-opens.
const KEY_NS = ".upande_qr_slideshow";

frappe.ui.form.on("Order Pick List", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Generate QR Codes"), () => open_plan(frm), __("Actions"));

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

function open_plan(frm) {
	frappe
		.call({
			method: `${OPL_QR_API}.plan`,
			args: { opl: frm.doc.name },
			freeze: true,
			freeze_message: __("Reading allocated buckets and bunches..."),
		})
		.then((r) => {
			const plan = r && r.message;
			if (!plan) return;

			const total = plan.bucket_labels.length + plan.bunch_labels.length;
			if (!total) {
				frappe.msgprint({
					title: __("Nothing to Generate"),
					indicator: "orange",
					message:
						warnings(plan).join("<br>") ||
						__("No bucket is allocated on this pick list yet."),
				});
				return;
			}

			const d = new frappe.ui.Dialog({
				title: __("Generate QR Codes"),
				size: "small",
				fields: [{ fieldtype: "HTML", fieldname: "summary" }],
				primary_action_label: __("Generate"),
				primary_action() {
					d.hide();
					generate(frm, plan);
				},
			});
			d.fields_dict.summary.$wrapper.html(plan_html(plan));
			d.show();
		});
}

function generate(frm, plan) {
	const missing = missing_count(plan.bucket_labels) + missing_count(plan.bunch_labels);
	frappe
		.call({
			method: `${OPL_QR_API}.generate`,
			args: { opl: frm.doc.name },
			freeze: true,
			freeze_message: __("Generating {0} QR code(s)...", [missing]),
		})
		.then((r) => {
			const result = r && r.message;
			if (!result) return;
			show_labels(frm, result);
		});
}

function show_labels(frm, result) {
	const labels = result.bucket_labels.concat(result.bunch_labels);
	// The print sheet keeps its one-label-per-page layout; only the on-screen
	// preview is a slideshow.
	const sheet = labels.map((label) => label_html(label, label.image)).join("");
	const n_buckets = result.bucket_labels.length;

	const d = new frappe.ui.Dialog({
		title: __("QR Codes for {0}", [result.opl]),
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "sheet" }],
		primary_action_label: __("Print"),
		primary_action() {
			print_sheet(result, sheet);
		},
	});

	const notes = [
		__("{0} bucket code(s) for standard rows, {1} bunch code(s) for spray rows.", [
			result.bucket_labels.length,
			result.bunch_labels.length,
		]),
		__("{0} newly stored on their records, {1} already had one.", [
			result.saved,
			labels.filter((label) => label.has_image).length,
		]),
	].concat(warnings(result));

	if ((result.failed || []).length) {
		notes.push(__("Could not store: {0}", [result.failed.join(", ")]));
	}

	const $wrapper = d.fields_dict.sheet.$wrapper;
	$wrapper.html(
		`<div class="text-muted small" style="margin-bottom:10px">${notes.join("<br>")}</div>` +
			slideshow_html(labels, n_buckets) +
			preview_css()
	);

	// Arrow keys are only listened for while this dialog is open.
	d.onhide = () => $(document).off(KEY_NS);
	d.show();
	slideshow(d, $wrapper, labels.length, n_buckets);
	frm.reload_doc();
}

// ---- Slideshow ---------------------------------------------------------

function slideshow_html(labels, n_buckets) {
	const n_bunches = labels.length - n_buckets;

	const slides = labels
		.map((label, i) => {
			const kind = i < n_buckets ? "bucket" : "bunch";
			return `<div class="upande-qr-slide" data-index="${i}" data-kind="${kind}">${slide_body(
				label,
				kind
			)}</div>`;
		})
		.join("");

	const thumbs = labels
		.map((label, i) => {
			const src = label.image;
			const kind = i < n_buckets ? "bucket" : "bunch";
			const title = frappe.utils.escape_html(String(first_line(label) || i + 1));
			const inner = src ? `<img src="${src}" alt="">` : `<span>?</span>`;
			return `<button type="button" class="upande-qr-thumb" data-index="${i}" data-kind="${kind}" title="${title}">${inner}</button>`;
		})
		.join("");

	// The group switch only earns its place when there is something to
	// switch between.
	const groups =
		n_buckets && n_bunches
			? `<div class="upande-qr-groups btn-group btn-group-sm" role="group">
					<button type="button" class="btn btn-default upande-qr-group" data-kind="bucket" data-start="0">${__(
						"Buckets ({0})",
						[n_buckets]
					)}</button>
					<button type="button" class="btn btn-default upande-qr-group" data-kind="bunch" data-start="${n_buckets}">${__(
						"Bunches ({0})",
						[n_bunches]
					)}</button>
				</div>`
			: "";

	return `<div class="upande-qr-show" tabindex="-1">
		<div class="upande-qr-toolbar">
			${groups}
			<div class="upande-qr-counter"></div>
			<div class="upande-qr-hint text-muted">${__("Use ← and → to browse")}</div>
		</div>
		<div class="upande-qr-stage">
			<button type="button" class="upande-qr-nav upande-qr-prev" aria-label="${__(
				"Previous"
			)}">&#8249;</button>
			<div class="upande-qr-slides">${slides}</div>
			<button type="button" class="upande-qr-nav upande-qr-next" aria-label="${__(
				"Next"
			)}">&#8250;</button>
		</div>
		<div class="upande-qr-strip">${thumbs}</div>
	</div>`;
}

function slide_body(label, kind) {
	const src = label.image;
	const lines = (label.lines || []).filter((line) => line);
	const text = lines
		.map(
			(line, i) =>
				`<div class="${i === 0 ? "upande-qr-id" : "upande-qr-line"}">${frappe.utils.escape_html(
					String(line)
				)}</div>`
		)
		.join("");
	const code = src
		? `<img class="upande-qr-big" src="${src}" alt="">`
		: `<div class="upande-qr-big upande-qr-missing">${__("No image")}</div>`;
	const badge = kind === "bucket" ? __("Bucket") : __("Bunch");
	const state = label.has_image
		? `<span class="indicator-pill gray">${__("Already on record")}</span>`
		: `<span class="indicator-pill green">${__("New")}</span>`;
	return `${code}
		<div class="upande-qr-details">
			<div class="upande-qr-badges">
				<span class="indicator-pill ${kind === "bucket" ? "blue" : "purple"}">${badge}</span>
				${state}
			</div>
			${text}
		</div>`;
}

function first_line(label) {
	return (label.lines || []).filter((line) => line)[0];
}

function slideshow(d, $root, total, n_buckets) {
	const n_bunches = total - n_buckets;
	const $slides = $root.find(".upande-qr-slide");
	const $thumbs = $root.find(".upande-qr-thumb");
	const $groups = $root.find(".upande-qr-group");
	const $counter = $root.find(".upande-qr-counter");
	const $prev = $root.find(".upande-qr-prev");
	const $next = $root.find(".upande-qr-next");
	let current = -1;

	function go(i) {
		i = Math.max(0, Math.min(total - 1, i));
		if (i === current) return;
		current = i;

		$slides.filter(".is-active").removeClass("is-active");
		$slides.eq(i).addClass("is-active");
		$thumbs.filter(".is-active").removeClass("is-active");
		const thumb = $thumbs.eq(i).addClass("is-active")[0];
		if (thumb) thumb.scrollIntoView({ block: "nearest", inline: "center" });

		const is_bucket = i < n_buckets;
		$groups.removeClass("btn-primary").addClass("btn-default");
		$groups
			.filter(`[data-kind="${is_bucket ? "bucket" : "bunch"}"]`)
			.removeClass("btn-default")
			.addClass("btn-primary");

		const in_group = is_bucket
			? __("Bucket {0} of {1}", [i + 1, n_buckets])
			: __("Bunch {0} of {1}", [i - n_buckets + 1, n_bunches]);
		$counter.html(
			`<strong>${in_group}</strong>` +
				(n_buckets && n_bunches
					? ` <span class="text-muted">(${i + 1} / ${total})</span>`
					: "")
		);

		$prev.prop("disabled", i === 0);
		$next.prop("disabled", i === total - 1);
	}

	$root.on("click", ".upande-qr-prev", () => go(current - 1));
	$root.on("click", ".upande-qr-next", () => go(current + 1));
	$root.on("click", ".upande-qr-thumb", (e) => go(+$(e.currentTarget).data("index")));
	$root.on("click", ".upande-qr-group", (e) => go(+$(e.currentTarget).data("start")));

	$(document)
		.off(KEY_NS)
		.on("keydown" + KEY_NS, (e) => {
			if (!d.$wrapper.is(":visible")) return;
			if (e.ctrlKey || e.metaKey || e.altKey) return;
			if ($(e.target).is("input, textarea, select, [contenteditable]")) return;
			const moves = {
				ArrowLeft: current - 1,
				ArrowRight: current + 1,
				Home: 0,
				End: total - 1,
			};
			if (!(e.key in moves)) return;
			e.preventDefault();
			go(moves[e.key]);
		});

	go(0);
}

// ---- Print -------------------------------------------------------------

function label_html(label, src) {
	const lines = (label.lines || [])
		.filter((line) => line)
		.map((line) => `<div>${frappe.utils.escape_html(String(line))}</div>`)
		.join("");
	if (!src) {
		return `<div class="upande-qr-label"><div class="upande-qr-missing">${__(
			"No image"
		)}</div><div class="upande-qr-text">${lines}</div></div>`;
	}
	return `<div class="upande-qr-label"><img src="${src}"><div class="upande-qr-text">${lines}</div></div>`;
}

function print_sheet(plan, sheet) {
	// Opened straight off the Print click so the browser still counts it
	// as a user gesture and does not block the window.
	const w = window.open("", "_blank");
	if (!w) {
		frappe.msgprint(
			__("Allow pop-ups for this site to print the labels, then click Print again.")
		);
		return;
	}
	// The images are /files URLs: <base> resolves them against this site,
	// and printing waits until every one has loaded (or failed).
	w.document.write(
		`<html><head><base href="${window.location.origin}/">` +
			`<title>${plan.opl} QR Codes</title>${print_css()}</head>` +
			`<body>${sheet}</body></html>`
	);
	w.document.close();
	w.focus();
	const pending = Array.from(w.document.images).map((img) =>
		img.complete
			? Promise.resolve()
			: new Promise((resolve) => {
					img.onload = img.onerror = resolve;
			  })
	);
	Promise.all(pending).then(() => setTimeout(() => w.print(), 200));
}

// One label per A4-landscape page with a 40mm QR and 6mm bold text --
// the same geometry gen_label_id's PDF prints, so these come off the
// same label printer setup as the pre-printed batches.
function print_css() {
	return `<style>
		@page { size: A4 landscape; margin: 0; }
		body { margin: 0; font-family: Helvetica, Arial, sans-serif; }
		.upande-qr-label { display: flex; align-items: flex-start; height: 210mm;
			page-break-after: always; }
		.upande-qr-label:last-child { page-break-after: auto; }
		.upande-qr-label img { width: 40mm; height: 40mm; }
		.upande-qr-text { font-size: 6mm; font-weight: bold; line-height: 8mm;
			padding: 4mm 0 0 2mm; }
		.upande-qr-missing { width: 40mm; height: 40mm; border: 1px dashed #999; }
	</style>`;
}

// Uses Frappe's own theme variables so it follows light/dark desk themes.
function preview_css() {
	return `<style>
		.upande-qr-show { outline: none; }
		.upande-qr-toolbar { display: flex; align-items: center; gap: 12px;
			flex-wrap: wrap; margin-bottom: 10px; }
		.upande-qr-counter { font-size: 13px; }
		.upande-qr-hint { margin-left: auto; font-size: 12px; }

		.upande-qr-stage { display: flex; align-items: stretch; gap: 8px; }
		.upande-qr-slides { flex: 1; min-width: 0;
			border: 1px solid var(--border-color, #d1d8dd); border-radius: 8px;
			background: var(--fg-color, #fff); }
		.upande-qr-slide { display: none; align-items: center; gap: 24px;
			padding: 20px 24px; min-height: 280px; }
		.upande-qr-slide.is-active { display: flex; }

		/* The QR sits on white whatever the theme -- dark-mode padding
		   around a code is what stops phones reading it off a screen. */
		.upande-qr-big { width: 240px; height: 240px; flex: none;
			background: #fff; padding: 8px; border-radius: 4px;
			image-rendering: pixelated; }
		.upande-qr-missing { display: flex; align-items: center; justify-content: center;
			border: 1px dashed var(--gray-500, #999); color: var(--text-muted, #888);
			font-size: 12px; background: transparent; }

		.upande-qr-details { min-width: 0; }
		.upande-qr-badges { display: flex; gap: 6px; margin-bottom: 12px; }
		.upande-qr-id { font-size: 22px; font-weight: 700; line-height: 1.25;
			word-break: break-all; margin-bottom: 6px; color: var(--heading-color, inherit); }
		.upande-qr-line { font-size: 15px; font-weight: 500; line-height: 1.5;
			word-break: break-word; }

		.upande-qr-nav { flex: none; width: 40px; border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 8px; background: var(--control-bg, #f4f5f6);
			font-size: 28px; line-height: 1; color: var(--text-color, #333); }
		.upande-qr-nav:hover:not(:disabled) { background: var(--control-bg-on-gray, #e9ecef); }
		.upande-qr-nav:disabled { opacity: .35; cursor: default; }

		.upande-qr-strip { display: flex; gap: 6px; overflow-x: auto;
			padding: 10px 2px 6px; margin-top: 8px; scroll-behavior: smooth; }
		.upande-qr-thumb { flex: none; width: 48px; height: 48px; padding: 3px;
			border: 2px solid transparent; border-radius: 6px; background: #fff;
			opacity: .6; }
		.upande-qr-thumb img { width: 100%; height: 100%; display: block; }
		.upande-qr-thumb span { color: #999; font-size: 14px; }
		.upande-qr-thumb:hover { opacity: 1; }
		.upande-qr-thumb.is-active { border-color: var(--primary, #2490ef); opacity: 1; }
		/* Where the buckets stop and the bunches start in the strip. */
		.upande-qr-thumb[data-kind="bucket"] + .upande-qr-thumb[data-kind="bunch"] {
			margin-left: 14px; }

		@media (max-width: 576px) {
			.upande-qr-slide { flex-direction: column; text-align: center; gap: 14px; }
			.upande-qr-badges { justify-content: center; }
			.upande-qr-big { width: 200px; height: 200px; }
			.upande-qr-hint { display: none; }
		}
		@media (prefers-reduced-motion: reduce) {
			.upande-qr-strip { scroll-behavior: auto; }
		}
	</style>`;
}

// ---- Plan summary --------------------------------------------------------

function plan_html(plan) {
	const rows = [];
	if (plan.bucket_labels.length) {
		rows.push([
			__("Bucket QR codes (standard rows)"),
			plan.bucket_labels.length,
			missing_count(plan.bucket_labels),
		]);
	}
	if (plan.bunch_labels.length) {
		rows.push([
			__("Bunch QR codes (spray rows)"),
			plan.bunch_labels.length,
			missing_count(plan.bunch_labels),
		]);
	}

	const body = rows
		.map(
			(row) =>
				`<tr><td>${row[0]}</td><td class="text-right">${row[1]}</td>` +
				`<td class="text-muted">${__("{0} without a code yet", [row[2]])}</td></tr>`
		)
		.join("");

	const notes = warnings(plan);
	return (
		`<table class="table table-bordered"><tbody>${body}</tbody></table>` +
		(notes.length ? `<div class="text-muted small">${notes.join("<br>")}</div>` : "") +
		`<div class="text-muted small">${__(
			"Codes that already exist are kept as they are."
		)}</div>`
	);
}

function missing_count(labels) {
	return labels.filter((label) => !label.has_image).length;
}

function warnings(plan) {
	const out = [];
	if ((plan.rows_without_bucket || []).length) {
		out.push(
			__("Row(s) {0} have no bucket allocated yet -- nothing to label there.", [
				plan.rows_without_bucket.join(", "),
			])
		);
	}
	if ((plan.spray_buckets_without_bunches || []).length) {
		out.push(
			__("Spray bucket(s) with no graded bunches: {0}.", [
				plan.spray_buckets_without_bunches.join(", "),
			])
		);
	}
	if ((plan.unknown_bunches || []).length) {
		out.push(
			__("Bunch id(s) graded but with no Bunch QR Code record: {0}.", [
				plan.unknown_bunches.join(", "),
			])
		);
	}
	return out;
}
