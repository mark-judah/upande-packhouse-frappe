// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

// "Generate QR Codes" (Actions): every QR code this OPL's buckets move
// through -- each bucket, every bunch graded from them, the shelves they sit
// on, and one trolley per remote farm still to be trucked in. The codes are
// drawn and stored server-side (upande_packhouse.api.opl_qr_codes); records
// that already carry an image are reused untouched -- their label is already
// printed and stuck on something physical.
//
// The result dialog is a slideshow: one label at a time, big enough to read
// and scan off the screen, stepped through with the arrow keys (Home/End jump
// to the first/last). A filmstrip underneath jumps straight to any label, and
// a Buckets/Bunches/Shelves/Trolleys switch jumps between groups.
// Printing is one label per page.

const OPL_QR_API = "upande_packhouse.api.opl_qr_codes";
// Namespace for the document-level keydown handler, so it can be removed
// cleanly when the dialog closes and never stacks up across re-opens.
const KEY_NS = ".upande_qr_slideshow";

// Slideshow / summary order. `key` is the plan field holding that kind.
const QR_KINDS = [
	{ kind: "bucket", key: "bucket_labels", one: __("Bucket"), many: __("Buckets"), pill: "blue" },
	{ kind: "bunch", key: "bunch_labels", one: __("Bunch"), many: __("Bunches"), pill: "purple" },
	{ kind: "shelf", key: "shelf_labels", one: __("Shelf"), many: __("Shelves"), pill: "orange" },
	{
		kind: "trolley",
		key: "trolley_labels",
		one: __("Trolley"),
		many: __("Trolleys"),
		pill: "cyan",
	},
];

frappe.ui.form.on("Order Pick List", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Generate QR Codes"), () => open_plan(frm), __("Actions"));

		if (!frm.doc.sales_order) return;

		frm.add_custom_button(
			__("Sales Allocation"),
			() => {
				// The allocation page moves its delivery window onto the order's
				// own delivery date -- read it off the Sales Order, not this OPL.
				frappe.db
					.get_value("Sales Order", frm.doc.sales_order, "delivery_date")
					.then((r) => {
						frappe.route_options = {
							sales_order: frm.doc.sales_order,
							farm: frm.doc.farm,
							delivery_date: (r.message && r.message.delivery_date) || "",
						};
						frappe.set_route("sales-allocation");
					});
			},
			__("Actions")
		);
	},
});

function all_labels(plan) {
	return QR_KINDS.reduce((out, k) => out.concat(plan[k.key] || []), []);
}

// Storable codes still to be drawn -- trolleys (and shelves with no record)
// are drawn every time, so they never count as missing.
function missing_count(labels) {
	return labels.filter((label) => label.doctype && !label.has_image).length;
}

function open_plan(frm) {
	frappe
		.call({
			method: `${OPL_QR_API}.plan`,
			args: { opl: frm.doc.name },
			freeze: true,
			freeze_message: __("Reading buckets, bunches, shelves and trolleys..."),
		})
		.then((r) => {
			const plan = r && r.message;
			if (!plan) return;

			if (!all_labels(plan).length) {
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
	frappe
		.call({
			method: `${OPL_QR_API}.generate`,
			args: { opl: frm.doc.name },
			freeze: true,
			freeze_message: __("Generating {0} QR code(s)...", [all_labels(plan).length]),
		})
		.then((r) => {
			const result = r && r.message;
			if (!result) return;
			show_labels(frm, result);
		});
}

function show_labels(frm, result) {
	const labels = all_labels(result);
	// The print sheet keeps its one-label-per-page layout; only the on-screen
	// preview is a slideshow.
	const sheet = labels.map((label) => label_html(label, label.image)).join("");

	const d = new frappe.ui.Dialog({
		title: __("QR Codes for {0}", [result.opl]),
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "sheet" }],
		primary_action_label: __("Print"),
		primary_action() {
			print_sheet(result, sheet);
		},
	});

	const counts = QR_KINDS.filter((k) => (result[k.key] || []).length)
		.map((k) => `${result[k.key].length} ${k.many.toLowerCase()}`)
		.join(", ");
	const notes = [
		counts,
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
			slideshow_html(labels) +
			preview_css()
	);

	// Arrow keys are only listened for while this dialog is open.
	d.onhide = () => $(document).off(KEY_NS);
	d.show();
	slideshow(d, $wrapper, labels);
	frm.reload_doc();
}

// ---- Slideshow ---------------------------------------------------------

function kind_of(label) {
	return QR_KINDS.find((k) => k.kind === label.kind) || QR_KINDS[0];
}

// [{kind, start, count}] for each kind present, in slideshow order.
function label_groups(labels) {
	const groups = [];
	labels.forEach((label, i) => {
		const last = groups[groups.length - 1];
		if (last && last.kind === label.kind) last.count++;
		else groups.push({ kind: label.kind, start: i, count: 1 });
	});
	return groups;
}

function slideshow_html(labels) {
	const slides = labels
		.map(
			(label, i) =>
				`<div class="upande-qr-slide" data-index="${i}" data-kind="${
					label.kind
				}">${slide_body(label)}</div>`
		)
		.join("");

	const thumbs = labels
		.map((label, i) => {
			const src = label.image;
			const title = frappe.utils.escape_html(String(first_line(label) || i + 1));
			const inner = src ? `<img src="${src}" alt="">` : `<span>?</span>`;
			return `<button type="button" class="upande-qr-thumb" data-index="${i}" data-kind="${label.kind}" title="${title}">${inner}</button>`;
		})
		.join("");

	// The group switch only earns its place when there is something to
	// switch between.
	const groups = label_groups(labels);
	const switcher =
		groups.length > 1
			? `<div class="upande-qr-groups btn-group btn-group-sm" role="group">${groups
					.map(
						(g) =>
							`<button type="button" class="btn btn-default upande-qr-group" data-kind="${
								g.kind
							}" data-start="${g.start}">${kind_of(g).many} (${g.count})</button>`
					)
					.join("")}</div>`
			: "";

	return `<div class="upande-qr-show" tabindex="-1">
		<div class="upande-qr-toolbar">
			${switcher}
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

function slide_body(label) {
	const src = label.image;
	const kind = kind_of(label);
	const lines = (label.lines || []).filter((line) => line);
	const text = lines
		.map(
			(line, i) =>
				`<div class="${
					i === 0 ? "upande-qr-id" : "upande-qr-line"
				}">${frappe.utils.escape_html(String(line))}</div>`
		)
		.join("");
	const code = src
		? `<img class="upande-qr-big" src="${src}" alt="">`
		: `<div class="upande-qr-big upande-qr-missing">${__("No image")}</div>`;
	const state = label.has_image
		? `<span class="indicator-pill gray">${__("Already on record")}</span>`
		: label.doctype
		? `<span class="indicator-pill green">${__("New")}</span>`
		: "";
	return `${code}
		<div class="upande-qr-details">
			<div class="upande-qr-badges">
				<span class="indicator-pill ${kind.pill}">${kind.one}</span>
				${state}
			</div>
			${text}
		</div>`;
}

function first_line(label) {
	return (label.lines || []).filter((line) => line)[0];
}

function slideshow(d, $root, labels) {
	const total = labels.length;
	const groups = label_groups(labels);
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

		const group = groups.find((g) => i >= g.start && i < g.start + g.count);
		$groups.removeClass("btn-primary").addClass("btn-default");
		$groups
			.filter(`[data-kind="${group.kind}"]`)
			.removeClass("btn-default")
			.addClass("btn-primary");

		$counter.html(
			`<strong>${__("{0} {1} of {2}", [
				kind_of(group).one,
				i - group.start + 1,
				group.count,
			])}</strong>` +
				(groups.length > 1 ? ` <span class="text-muted">(${i + 1} / ${total})</span>` : "")
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
		/* Where one kind stops and the next starts in the strip. */
		.upande-qr-thumb[data-kind="bucket"] + .upande-qr-thumb:not([data-kind="bucket"]),
		.upande-qr-thumb[data-kind="bunch"] + .upande-qr-thumb:not([data-kind="bunch"]),
		.upande-qr-thumb[data-kind="shelf"] + .upande-qr-thumb:not([data-kind="shelf"]) {
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
	const body = QR_KINDS.filter((k) => (plan[k.key] || []).length)
		.map((k) => {
			const labels = plan[k.key];
			const missing = missing_count(labels);
			const note = labels.some((label) => label.doctype)
				? __("{0} without a code yet", [missing])
				: __("drawn fresh each time");
			return (
				`<tr><td>${__("{0} QR codes", [k.one])}</td><td class="text-right">${
					labels.length
				}</td>` + `<td class="text-muted">${note}</td></tr>`
			);
		})
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

function warnings(plan) {
	const out = [];
	if ((plan.rows_without_bucket || []).length) {
		out.push(
			__("Row(s) {0} have no bucket allocated yet -- nothing to label there.", [
				plan.rows_without_bucket.join(", "),
			])
		);
	}
	if ((plan.buckets_without_bunches || []).length) {
		out.push(
			__("Bucket(s) with no graded bunches yet: {0}.", [
				plan.buckets_without_bunches.join(", "),
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
