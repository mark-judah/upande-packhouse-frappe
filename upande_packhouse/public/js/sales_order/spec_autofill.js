// Autofill Sales Order By Specification — thin client over upande_packhouse.spec_autofill.
// One unified picker for EVERY spec (straight box, mixed box, mixed bunch), built around
// APPROVED COLOUR (not box item) -- that's the actual point of the popup: the customer's
// spec says "N varieties are approved for White, M for Red, ..."; for each colour the
// operator ticks whichever of the approved varieties they have the best stock of, one or
// several. Scales to a spec with many colours: one collapsible section per colour (closed
// by default), each a plain checkbox list annotated with LIVE shelf availability -- then a
// single fill-in table below, built live from whatever got ticked, one row per (colour,
// variety) to size with Stems/Box + Boxes. Box shape (bunch type/length/pack rate) is a
// separate axis -- most specs have exactly one, applied by default; a spec with more than
// one offers a per-colour pack selector too. The server decides row shaping (mix/bunch
// groups, packrate field, warehouse routing).
//
// ONE WINDOW, MANY SPECS (open_spec_picker). An order almost never consists of a single
// spec -- the same customer order carries several, and filling them one Specification
// link at a time (add row, pick spec, fill, repeat) is the slow path. So the picker body
// is built as a reusable SECTION (build_spec_section below) and the window carries a
// Specifications multiselect at the top: pick as many as the order needs and each one's
// variety picker stacks underneath, live. Two ways in, same window:
//
//   * clicking the Specification cell on an items row -- the cell opens the window
//     instead of its own link dropdown (bind_spec_cell), so there is no one-spec pick
//     to make first, or
//   * Actions > Add from Specifications.
//
// One "Add to Order" appends every picked spec's lines to THIS order in one go, with
// mix/bunch groups kept distinct per spec (append_spec_batch).

frappe.ui.form.on("Sales Order", {
	onload(frm) {
		set_spec_query(frm);
	},
	refresh(frm) {
		set_spec_query(frm);
		bind_spec_cell(frm);
		// Roses only, and draft only. A Specification describes a rose box --
		// colours, bunch types, stems per bunch, pack rates -- so it means
		// nothing on a Coffee or Dairy order, where the picker would only ever
		// offer an empty list; and a submitted/cancelled order's items are
		// frozen, so offering the filler there would end in a save error.
		if (is_roses_order(frm) && frm.doc.docstatus === 0) {
			frm.add_custom_button(
				__("Add from Specifications"),
				() => {
					open_spec_picker(frm, null, []);
				},
				__("Actions")
			);
		}
	},
	customer(frm) {
		set_spec_query(frm);
	},
	validate(frm) {
		// Roses only: a Specification is a rose-box document, so a Coffee or
		// Dairy line legitimately has none and must not be nagged about it.
		if (!is_roses_order(frm)) return;
		const missing = [];
		(frm.doc.items || []).forEach((it, i) => {
			if (it.item_code && !it.custom_line) missing.push(i + 1);
		});
		if (missing.length) {
			// Non-modal toast so it never competes with a save/submit error modal.
			frappe.show_alert(
				{
					message: __("Row(s) {0} have no Specification.", [missing.join(", ")]),
					indicator: "orange",
				},
				5
			);
		}
	},
});

// This customer's Active specs only (expired Temporary specs are set Inactive by the daily job).
function set_spec_query(frm) {
	frm.set_query("custom_line", "items", () => {
		return { filters: spec_filters(frm) };
	});
}

// Same test as warehouse_routing.js's own is_roses -- kept local so neither
// file depends on the other's load order. custom_business_unit is what the
// visible form field and a Floriday-origin order carry; business_unit is the
// real accounting dimension (accounting_dimension_sync.js mirrors them).
function is_roses_order(frm) {
	return (frm.doc.business_unit || frm.doc.custom_business_unit) === "Roses";
}

function spec_filters(frm) {
	const filters = { status: "Active" };
	if (frm.doc.customer) filters.customer = frm.doc.customer;
	return filters;
}

// The customer's Active specs, minus the ones already pilled on this dialog.
//
// MultiSelectPills means to do that itself, but doesn't:
// ControlMultiSelectPills.get_data ends with
//
//     if (data) data.filter((d) => !values.includes(d));
//
// which throws the filtered array away and returns the unfiltered one (and
// compares option OBJECTS against the selected strings besides, so it would
// match nothing even if the result were kept). A spec therefore stays in the
// dropdown after it has been picked, and a second pick is only swallowed later
// by validate(), with no sign of why. Filtering here is the visible answer.
function spec_link_options(frm, txt, picked) {
	const taken = new Set((picked || []).filter(Boolean));
	return frappe.db
		.get_link_options("Specifications", txt, spec_filters(frm))
		.then((options) => (options || []).filter((o) => !taken.has(o.value)));
}

// Fallback only. With bind_spec_cell in place the cell never reaches its own
// dropdown, so this fires just for a spec set some other way (another script,
// a paste into the grid). Rows built by append_spec_batch don't reach it --
// they go straight onto add_child, which fires no trigger.
frappe.ui.form.on("Sales Order Item", {
	custom_line(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.custom_line || frm.__spa_picker_open) return;
		// This row's spec is only the STARTING pick -- more can be ticked in the
		// popup, and this scratch row is dropped once the lines are appended.
		open_spec_picker(frm, cdn, [row.custom_line]);
	},
});

// The Specification cell on an items row is a doorway to the picker, not a
// field to type into. Left alone, clicking it opens the grid's inline editor
// and drops the Link's own awesomplete list, forcing a one-spec pick just to
// get the window open -- and the window then asks the same question again,
// with a multiselect.
//
// So the cell is intercepted at the CLICK, in the CAPTURE phase. That matters:
// the grid binds its own click handler directly on the cell (grid_row.js's
// make_column), which builds the control and focuses it, and a delegated
// bubble-phase handler would run after all that had already happened. Taking
// the event on the way down lets preventDefault + stopPropagation stop the
// editor being opened at all -- no control built, no list, no focus to fight
// over -- and then the window opens with the row's own spec as its first pill.
//
// The expanded row form (the pencil) has no static cell, so its Link control
// is caught on focus instead; the window guard stops the two overlapping.
function bind_spec_cell(frm) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (!grid || !grid.wrapper) return;

	const $grid = $(grid.wrapper);
	const el = $grid.get(0);

	// refresh rebinds; never stack handlers (N windows per click)
	if (frm.__spa_cell_capture) {
		el.removeEventListener("click", frm.__spa_cell_capture, true);
		frm.__spa_cell_capture = null;
	}
	$grid.off(".spa_cell");
	if (frm.doc.docstatus !== 0) return;

	function open_for(node) {
		const cdn = $(node).closest(".grid-row").attr("data-name");
		const row = cdn ? locals["Sales Order Item"][cdn] : null;
		// Nothing may be left focused, or closing the window would hand focus
		// straight back and reopen it on the spot.
		document.activeElement && document.activeElement.blur();
		const grid_row = cdn && grid.grid_rows_by_docname[cdn];
		if (grid_row) grid_row.toggle_editable_row(false);
		open_spec_picker(frm, cdn || null, row && row.custom_line ? [row.custom_line] : []);
	}

	frm.__spa_cell_capture = function (e) {
		const cell =
			e.target && e.target.closest('.grid-static-col[data-fieldname="custom_line"]');
		// Only a real data row: the heading row and the column filter row carry
		// the same data-fieldname but no docname behind them.
		if (!cell || cell.classList.contains("search")) return;
		if (!$(cell).closest(".grid-row").attr("data-name")) return;
		e.preventDefault();
		e.stopPropagation();
		open_for(cell);
	};
	el.addEventListener("click", frm.__spa_cell_capture, true);

	$grid.on("focusin.spa_cell", 'input[data-fieldname="custom_line"]', function () {
		if (frm.__spa_picker_open) return;
		open_for(this);
	});
}

function esc(s) {
	return frappe.utils && frappe.utils.escape_html ? frappe.utils.escape_html(s || "") : s || "";
}
function nfmt(n) {
	return format_number(n || 0, null, 0);
}

// ----------------------------- shared styles -----------------------------

const SPA_STYLES = `
    <style>
      .spa-acc{border:1px solid var(--border-color,#e2e4e9);border-radius:6px;margin-bottom:6px;overflow:hidden}
      .spa-acc-head{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:pointer;user-select:none;background:var(--fg-color,#fff)}
      .spa-acc-head:hover{background:var(--subtle-fg,#f8f8f6)}
      .spa-acc-caret{display:inline-flex;transition:transform .15s;color:#8a8780}
      .spa-acc-open .spa-acc-caret{transform:rotate(90deg)}
      .spa-colour{font-weight:600;font-size:13px}
      .spa-acc-count{font-size:11px;color:#8a8780}
      .spa-acc-warn{font-size:11px;color:#b45309}
      .spa-meta{font-size:11px;color:#8a8780;margin-left:auto}
      .spa-acc-head .spa-box{margin-left:auto;height:26px;font-size:12px}
      .spa-chip{display:inline-block;font-size:10px;padding:1px 6px;border-radius:9px;background:#f4f3ef;color:#5a5a52}
      .spa-chip-mix{background:rgba(10,10,10,.08);color:#0a0a0a}
      .spa-acc-body{display:none;padding:2px 10px 10px;border-top:1px solid var(--border-color,#f0f1f3)}
      .spa-acc-open .spa-acc-body{display:block}
      .spa-checklist{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:2px 12px;margin-top:8px}
      .spa-vcheck{display:flex;align-items:baseline;gap:6px;padding:4px 2px;cursor:pointer;font-size:12px}
      .spa-vcheck input{margin:0;flex:none}
      .spa-vcheck-name{font-weight:600}
      .spa-vcheck-avail{font-size:11px;color:#16a34a;margin-left:auto}
      .spa-vcheck-empty .spa-vcheck-avail{color:#b45309}
      .spa-fill-wrap{margin-top:14px}
      .spa-fill-title{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#8a8780;margin-bottom:6px}
      .spa-fill-tbl{width:100%;border-collapse:collapse;font-size:13px}
      .spa-fill-tbl th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#8a8780;text-align:left;padding:6px 8px;border-bottom:1px solid var(--border-color,#e2e4e9)}
      .spa-fill-tbl td{padding:6px 8px;border-bottom:1px solid var(--border-color,#f0f1f3);vertical-align:middle}
      .spa-c{text-align:center}
      .spa-stems,.spa-boxes{height:28px;width:80px}
      .spa-empty-fill{color:#8a8780;text-align:center;padding:14px 0;font-size:12px}
      .spa-foot{margin-top:12px;padding-top:10px;border-top:1px solid var(--border-color,#e2e4e9);font-size:13px;text-align:right;color:#3a3a34}
      /* The bunch picker's foot carries the spec's single Boxes control as
         well as the totals, so it lays out as a row; the flat picker's foot
         has only text and is unaffected by these. */
      .spa-foot-row{display:flex;align-items:center;gap:12px;justify-content:flex-end;flex-wrap:wrap}
      .spa-foot-row .spb-boxes-label{margin:0}
      .spa-foot-tot{white-space:nowrap}
      .spa-badge{float:left;font-size:11px;color:#8a8780;text-transform:uppercase;letter-spacing:.04em}
      .spb-card{border:1px solid var(--border-color,#e2e4e9);border-radius:6px;margin-bottom:10px;overflow:hidden}
      .spb-head{display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--subtle-fg,#f8f8f6);flex-wrap:wrap}
      .spb-id{font-weight:600;font-size:13px}
      .spb-boxes-label{display:flex;align-items:center;gap:6px;font-size:11px;color:#8a8780;margin:0 0 0 auto}
      .spb-boxes{width:70px;height:26px;font-size:12px}
      .spb-slot{padding:6px 10px;border-top:1px solid var(--border-color,#f0f1f3)}
      .spb-slot-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:12px;margin-bottom:4px}
      .spb-slot-colour{font-weight:600}
      .spb-slot-meta{font-size:11px;color:#8a8780}
      .spb-cands{display:flex;flex-direction:column;gap:2px}
      .spb-cand{display:flex;align-items:baseline;gap:6px;font-size:12.5px;padding:2px 0;cursor:pointer}
      .spb-cand-solo{cursor:default}
      .spb-cand input{margin:0;flex:none}
      .spb-cand-name{font-weight:600}
      .spb-cand-avail{font-size:11px;color:#16a34a;margin-left:auto}
      .spb-cand-empty{color:#b45309}
      .spb-foot{padding:4px 10px 8px;font-size:11px;color:#8a8780;text-align:right;border-top:1px solid var(--border-color,#f0f1f3)}
      .spx-spec{border:1px solid var(--border-color,#e2e4e9);border-left:4px solid var(--spx-accent,var(--border-color,#e2e4e9));border-radius:6px;margin-bottom:14px;overflow:hidden}
      .spx-head{display:flex;align-items:center;gap:8px;padding:9px 10px;cursor:pointer;user-select:none;background:var(--spx-tint,var(--subtle-fg,#f8f8f6))}
      .spx-head:hover{background:var(--spx-tint-strong,var(--fg-hover-color,#f1f1ee))}
      .spx-swatch{width:9px;height:9px;border-radius:50%;background:var(--spx-accent,#8a8780);flex:none}
      .spx-body{border-top:1px solid var(--spx-tint-strong,var(--border-color,#f0f1f3))}
      .spx-caret{display:inline-flex;transition:transform .15s;color:#8a8780}
      .spx-open .spx-caret{transform:rotate(90deg)}
      .spx-title{font-weight:600;font-size:13px}
      .spx-sub{font-size:10px;padding:1px 6px;border-radius:9px;background:#f4f3ef;color:#5a5a52;text-transform:uppercase;letter-spacing:.04em}
      .spx-tot{margin-left:auto;font-size:11px;color:#16a34a}
      .spx-body{display:none;padding:10px}
      .spx-open .spx-body{display:block}
      .spx-grand{margin-top:6px;padding-top:10px;border-top:1px solid var(--border-color,#e2e4e9);font-size:14px;text-align:right}
      .spx-error{border:1px solid #f0d8a8;background:#fdf6e7;color:#8a5a00;border-radius:6px;padding:8px 10px;margin:0 10px 10px;font-size:12px}
      .spx-failed{--spx-accent:#d97706}
      .spx-failed .spx-head{cursor:default;background:#fdf6e7}
      .spx-empty{color:#8a8780;text-align:center;padding:20px 0;font-size:12px}
      .spx-hint{font-size:11px;color:#8a8780;margin:-6px 0 4px}
    </style>`;

// One colour per spec in the picker window, so where one spec's block ends and
// the next begins is readable at a glance -- a 4px bar down the left edge of
// the whole card (head AND body, so it reads as one run), a matching tint
// behind the header and a dot beside the name.
//
// Hues only: the accent is built from the hue at fixed saturation/lightness,
// and the tints are the SAME colour at low alpha rather than a baked-in pale
// shade, so one set of values works on the light and dark desk themes alike.
// 52% lightness stays visible on both. Eight well-separated hues, cycled --
// and colour is never the only cue, the spec's own name is right there, so a
// repeat after eight (or colour-blindness) costs nothing.
const SPX_HUES = [211, 27, 152, 291, 340, 190, 47, 258];

function spx_accent_style(i) {
	const h = SPX_HUES[i % SPX_HUES.length];
	return [
		`--spx-accent:hsl(${h} 64% 52%)`,
		`--spx-tint:hsl(${h} 64% 52% / 0.10)`,
		`--spx-tint-strong:hsl(${h} 64% 52% / 0.18)`,
	].join(";");
}

// ----------------------------- picker sections -----------------------------

// A SECTION is one spec's picker body, mountable on its own (single-spec
// dialog) or stacked with others (multi-spec dialog). Everything it does is
// scoped to the DOM node it's bound to -- no dialog-wide selectors -- so N
// sections on one window never read each other's checkboxes.
//
// Contract: { html, bind($root, onchange), collect(), totals(), empty_message }
// or { error } when the spec can't be filled at all.
function build_spec_section(data, uid) {
	return data.bunch_aware ? bunch_section(data, uid) : flat_section(data, uid);
}

function spec_kind(data) {
	return data.is_mixed_box ? "Mixed Box" : "Straight Box";
}

// ---- two-step layout, built to stay usable at 15+ colours:
// 1. One collapsible section per approved COLOUR (closed by default --
//    with many colours, having them all open at once is the actual
//    unusable state). Each lists every approved variety as a plain
//    checkbox with its own stock right next to it -- real checkboxes,
//    not custom click-toggle cards, so ticking several under the same
//    colour just works (the previous card design had multi-select
//    breaking in some cases; native checkboxes have no such ambiguity).
// 2. A single fill-in table below, built live from whatever's ticked
//    above -- one row per (colour, variety) checked, each with its own
//    Stems/Box + Boxes to fill in. This is the actual "point of the
//    popup" table: what's ticked upstairs is what shows up here to size.
function flat_section(data, uid) {
	const lines = data.lines || [];
	const boxOptions = data.box_options || [];
	if (!lines.length) {
		return { error: __("Specification {0} has no approved colours.", [data.spec]) };
	}
	if (!boxOptions.length) {
		return { error: __("Specification {0} has no box items.", [data.spec]) };
	}

	const singleBox = boxOptions.length === 1;
	const boxMeta = (bi) => {
		const bunch = bi.is_mixed_bunch
			? '<span class="spa-chip spa-chip-mix">Mixed Bunch</span>'
			: '<span class="spa-chip">Mono Bunch</span>';
		return [
			bunch,
			bi.length ? esc(bi.length) : "",
			bi.stems_per_bunch ? `${bi.stems_per_bunch}/bunch` : "",
		]
			.filter(Boolean)
			.join(" · ");
	};
	const boxOptHtml = boxOptions
		.map(
			(bi) =>
				`<option value="${bi.idx}">${esc(
					[
						bi.bunch_type,
						bi.length,
						bi.stems_per_bunch ? `${bi.stems_per_bunch}/bunch` : "",
					]
						.filter(Boolean)
						.join(" · ")
				)}</option>`
		)
		.join("");

	const sectionsHtml = lines
		.map((line, li) => {
			const checksHtml = (line.approved || [])
				.map((a) => {
					const farms = Object.keys(a.by_farm || {})
						.sort((x, y) => a.by_farm[y] - a.by_farm[x])
						.map((f) => `${f}: ${nfmt(a.by_farm[f])}`)
						.join(" · ");
					const none = (a.available || 0) > 0 ? "" : " spa-vcheck-empty";
					return `
            <label class="spa-vcheck${none}">
              <input type="checkbox" class="spa-vcb" data-variety="${esc(
					a.variety
				)}" data-item-name="${esc(a.item_name || a.variety)}">
              <span class="spa-vcheck-name">${esc(a.item_name || a.variety)}</span>
              <span class="spa-vcheck-avail">${farms ? esc(farms) + " · " : ""}${nfmt(
						a.available
					)} ${__("stems")}</span>
            </label>`;
				})
				.join("");

			const bi = boxOptions[0];
			// When every variety under this colour knows its own box items,
			// a colour-wide pack selector would only be able to contradict
			// them -- so show what the spec says instead of offering a choice.
			const paired = (line.approved || []).every((a) => (a.box_idxs || []).length);
			let boxCell;
			if (singleBox) {
				boxCell = `<span class="spa-meta">${boxMeta(bi)}</span>`;
			} else if (paired) {
				boxCell = `<span class="spa-meta">${__("pack per spec")}</span>`;
			} else {
				boxCell = `<select class="spa-box form-control input-sm">${boxOptHtml}</select>`;
			}
			const bestAvail = Math.max(0, ...(line.approved || []).map((a) => a.available || 0));

			return `
        <div class="spa-acc${li === 0 ? " spa-acc-open" : ""}" data-idx="${line.idx}">
          <div class="spa-acc-head">
            <span class="spa-acc-caret">▸</span>
            <span class="spa-colour">${esc(line.colour)}</span>
            <span class="spa-acc-count">${(line.approved || []).length} ${__("varieties")}</span>
            ${bestAvail > 0 ? "" : `<span class="spa-acc-warn">${__("no shelf stock")}</span>`}
            ${boxCell}
          </div>
          <div class="spa-acc-body">
            <div class="spa-checklist">${checksHtml}</div>
          </div>
        </div>`;
		})
		.join("");

	const html = `
    <div class="spa" data-uid="${esc(uid)}">
      <div class="spa-sections">${sectionsHtml}</div>
      <div class="spa-fill-wrap">
        <div class="spa-fill-title">${__("Fill in quantities for what you ticked above")}</div>
        <table class="spa-fill-tbl">
          <thead><tr>
            <th>${__("Colour")}</th><th>${__("Variety")}</th><th>${__("Pack")}</th>
            <th>${__("Stems/Box")}</th><th>${__("Boxes")}</th><th style="text-align:right">${__(
		"Stems"
	)}</th>
          </tr></thead>
          <tbody class="spa-fill-tbody"><tr><td colspan="6" class="spa-empty-fill">${__(
				"Tick varieties above to add them here"
			)}</td></tr></tbody>
        </table>
      </div>
      <div class="spa-foot"><span class="spa-badge">${esc(spec_kind(data))}</span>
        Total: <b class="spa-tot-boxes">0</b> boxes &middot; <b class="spa-tot-stems">0</b> stems</div>
    </div>`;

	const boxByIdx = {};
	boxOptions.forEach((bi) => {
		boxByIdx[bi.idx] = bi;
	});
	const lineByIdx = {};
	const approvedByKey = {};
	lines.forEach((line) => {
		lineByIdx[line.idx] = line;
		(line.approved || []).forEach((a) => {
			approvedByKey[line.idx + "::" + a.variety] = a;
		});
	});

	const packLabel = (bi) =>
		[bi.length, bi.pack_rate ? `${bi.pack_rate}/box` : ""].filter(Boolean).join(" · ");

	let $root = null;
	let on_change = () => {};

	function currentBoxIdx($acc) {
		const $sel = $acc.find(".spa-box");
		return $sel.length ? parseInt($sel.val(), 10) : 0;
	}

	// The spec's ONE box count, read off whichever fill row is showing it --
	// every row carries the same value (see the sync handler in bind). A row
	// added after the operator has already typed a count must start at that
	// count, not at 1: nothing fires an input event for a row that is merely
	// rendered, so a stale 1 would survive all the way to build_spec_rows and
	// be rejected as "every colour must use the same Boxes count".
	function flat_shared_boxes() {
		const $any = $root && $root.find(".spa-fill-tbody .spa-boxes").first();
		const v = $any && $any.length ? cint($any.val()) : 0;
		return v > 0 ? v : 1;
	}

	function syncTable() {
		const $tbody = $root.find(".spa-fill-tbody");
		const existing = {};
		$tbody.find(".spa-fill-row").each(function () {
			const $r = $(this);
			existing[$r.attr("data-key")] = {
				stems: $r.find(".spa-stems").val(),
				boxes: $r.find(".spa-boxes").val(),
			};
		});

		const rows = [];
		$root.find(".spa-acc").each(function () {
			const $acc = $(this);
			const idx = parseInt($acc.attr("data-idx"), 10);
			const line = lineByIdx[idx];
			const boxIdx = currentBoxIdx($acc);
			const bi = boxByIdx[boxIdx] || boxOptions[0];
			const packrate = bi.pack_rate || bi.stems_per_bunch || 0;

			$acc.find(".spa-vcb:checked").each(function () {
				const $cb = $(this);
				const variety = $cb.data("variety");
				const approved = approvedByKey[idx + "::" + variety];
				// A variety is specified at EVERY box item the spec pairs it
				// with (see _box_idxs_by_variety server-side) -- most often
				// one, but a 62/72 spec pairs it with both, and each is its
				// own order line at its own stem length. Only when the spec's
				// two tables don't line up does box_idxs come back empty, and
				// the colour's own pack selector decides instead.
				const mine =
					approved && (approved.box_idxs || []).length ? approved.box_idxs : [boxIdx];

				mine.forEach((bx) => {
					const b = boxByIdx[bx] || bi;
					const rate = b.pack_rate || b.stems_per_bunch || packrate;
					const key = idx + "::" + variety + "::" + bx;
					const prev = existing[key];
					rows.push(`
                <tr class="spa-fill-row" data-key="${esc(
					key
				)}" data-line-idx="${idx}" data-box-idx="${bx}" data-variety="${esc(variety)}">
                  <td>${esc(line.colour)}</td>
                  <td>${esc($cb.data("item-name") || variety)}</td>
                  <td><span class="spa-meta spa-pack">${esc(packLabel(b))}</span></td>
                  <td><input type="number" class="spa-stems form-control input-sm" min="0" value="${
						prev ? prev.stems : rate
					}"></td>
                  <td><input type="number" class="spa-boxes form-control input-sm" min="0" value="${
						prev ? prev.boxes : flat_shared_boxes()
					}"></td>
                  <td class="spa-c spa-row-stems" style="text-align:right">0</td>
                </tr>`);
				});
			});
		});

		$tbody.html(
			rows.length
				? rows.join("")
				: `<tr><td colspan="6" class="spa-empty-fill">${__(
						"Tick varieties above to add them here"
				  )}</td></tr>`
		);
		recompute();
	}

	function recompute() {
		// Boxes is ONE number for the spec, so the footer shows it once -- not
		// the sum across fill rows. Summing made the total read N x the truth
		// (3 varieties at 5 boxes announced "15 boxes" while the order got 5).
		// Stems still add up per row, because each row's stems really are its
		// own contribution to that one box.
		const boxes = flat_shared_boxes_or_zero();
		let ts = 0;
		$root.find(".spa-fill-row").each(function () {
			const $r = $(this);
			const total = cint($r.find(".spa-stems").val()) * boxes;
			$r.find(".spa-row-stems").text(nfmt(total));
			ts += total;
		});
		$root.find(".spa-tot-boxes").text(nfmt(boxes));
		$root.find(".spa-tot-stems").text(nfmt(ts));
		on_change();
	}

	// Same shared read as flat_shared_boxes, but truthful about zero -- the
	// seeding helper substitutes 1 so a new row is usable, the totals must not.
	function flat_shared_boxes_or_zero() {
		const $any = $root && $root.find(".spa-fill-tbody .spa-boxes").first();
		return $any && $any.length ? cint($any.val()) : 0;
	}

	return {
		html,
		empty_message: __("Tick at least one variety."),
		bind($scope, onchange) {
			$root = $scope;
			on_change = onchange || (() => {});
			$root.on("click", ".spa-acc-head", function (e) {
				if ($(e.target).is("select,option")) return; // don't collapse when picking a pack size
				$(this).closest(".spa-acc").toggleClass("spa-acc-open");
			});
			$root.on("change", ".spa-vcb", syncTable);
			$root.on("change", ".spa-box", syncTable);
			$root.on("input change", ".spa-fill-tbody .spa-stems", recompute);
			// Boxes is ONE number for the spec, not one per fill row: a spec
			// describes a single box, so build_spec_rows throws "A spec is one
			// box -- every colour must use the same Boxes count" the moment two
			// rows disagree. Typing in any row therefore sets them all, which
			// makes that rule visible instead of letting the operator build a
			// payload the server will only reject on submit.
			$root.on("input change", ".spa-fill-tbody .spa-boxes", function () {
				const v = $(this).val();
				$root.find(".spa-fill-tbody .spa-boxes").not(this).val(v);
				recompute();
			});
			syncTable();
		},
		totals() {
			let boxes = 0,
				stems = 0;
			if (!$root) return { boxes, stems };
			// One shared count for the spec, same as recompute and the server.
			boxes = flat_shared_boxes_or_zero();
			$root.find(".spa-fill-row").each(function () {
				stems += boxes * cint($(this).find(".spa-stems").val());
			});
			return { boxes, stems };
		},
		collect() {
			const selections = [];
			if (!$root) return selections;
			// Every ticked row, at the spec's one box count. A row is excluded
			// by UNTICKING its variety (which removes it from the fill table),
			// never by zeroing its Boxes -- the sync handler copies any typed
			// value into every row, so a zero here means "nothing ordered from
			// this spec", not "skip this one line".
			const shared = flat_shared_boxes_or_zero();
			if (shared <= 0) return selections;
			$root.find(".spa-fill-row").each(function () {
				const $r = $(this);
				const boxes = shared;
				selections.push({
					line_idx: parseInt($r.attr("data-line-idx"), 10),
					box_idx: parseInt($r.attr("data-box-idx"), 10) || 0,
					variety: $r.attr("data-variety"),
					stems: cint($r.find(".spa-stems").val()),
					boxes,
				});
			});
			return selections;
		},
	};
}

// One card per bunch_id (recipe) instead of one checkbox per variety, with
// a single Boxes input the way sync_packing_guide already requires every
// colour in the group to share. Inside a card, one slot per Box Item row:
// a Mixed Bunch's slots each have exactly one mandatory variety; a Mono
// Bunch's slot lists every variety approved for that colour, annotated
// with LIVE shelf availability -- the operator still picks whichever one
// has stock, same as the original flat picker, just scoped to the correct
// box shape instead of the spec's whole box_item palette. Used only when
// the spec has bunch_id filled in on every Approved Variety row (see
// get_spec_fill_data / _bunches_from_spec on the server); otherwise
// build_spec_section falls through to the flat colour picker, unchanged.
function bunch_section(data, uid) {
	const bunches = data.bunches || [];
	if (!bunches.length) {
		return { error: __("Specification {0} has no bunches defined.", [data.spec]) };
	}

	const farmsFor = (c) =>
		Object.keys(c.by_farm || {})
			.sort((x, y) => c.by_farm[y] - c.by_farm[x])
			.map((f) => `${f}: ${nfmt(c.by_farm[f])}`)
			.join(" · ");

	const candBadge = (c) =>
		`<span class="spb-cand-avail${(c.available || 0) > 0 ? "" : " spb-cand-empty"}">${
			farmsFor(c) ? esc(farmsFor(c)) + " · " : ""
		}${nfmt(c.available)} ${__("stems")}</span>`;

	const slotHtml = (b, slot, si) => {
		const candidates = slot.candidates || [];
		const best = candidates.reduce(
			(a, c) => ((c.available || 0) > (a.available || 0) ? c : a),
			candidates[0] || {}
		);
		const meta = [
			slot.length,
			slot.box_type,
			slot.bunches_per_box ? `${slot.bunches_per_box}/box` : "",
		]
			.filter(Boolean)
			.join(" · ");
		// The radio group name has to be unique across the WHOLE window, not
		// just this card -- several specs share one dialog now, and two specs
		// with the same bunch_id would otherwise form one radio group and
		// silently un-pick each other's slots.
		const radioName = `spb-pick-${esc(uid)}-${esc(b.bunch_id)}-${si}`;
		const candidatesHtml =
			candidates.length <= 1
				? `<div class="spb-cand spb-cand-solo">
                    <span class="spb-cand-name">${esc(
						(candidates[0] || {}).item_name || (candidates[0] || {}).variety || ""
					)}</span>
                    ${candBadge(candidates[0] || {})}
                  </div>`
				: candidates
						.map(
							(c) => `
                <label class="spb-cand">
                  <input type="radio" class="spb-pick" name="${radioName}" value="${esc(
								c.variety
							)}" ${c === best ? "checked" : ""}>
                  <span class="spb-cand-name">${esc(c.item_name || c.variety)}</span>
                  ${candBadge(c)}
                </label>`
						)
						.join("");
		return `
            <div class="spb-slot" data-colour="${esc(slot.colour)}"${
			candidates.length <= 1
				? ` data-only-variety="${esc((candidates[0] || {}).variety || "")}"`
				: ""
		}>
                <div class="spb-slot-head">
                    <span class="spb-slot-colour">${esc(slot.colour || __("(no colour)"))}</span>
                    <span class="spb-slot-meta">${esc(meta)} · ${slot.stems_per_bunch}/bunch</span>
                </div>
                <div class="spb-cands">${candidatesHtml}</div>
            </div>`;
	};

	const cardHtml = (b) => {
		const badge = b.is_mixed
			? `<span class="spa-chip spa-chip-mix">Mixed Bunch</span>`
			: `<span class="spa-chip">Mono Bunch</span>`;
		const perBoxStems = (b.slots || []).reduce((sum, s) => sum + (s.pack_rate || 0), 0);
		return `
        <div class="spb-card" data-bunch-id="${esc(b.bunch_id)}" data-per-box="${perBoxStems}">
            <div class="spb-head">
                <span class="spb-id">${esc(b.bunch_id)}</span>
                ${badge}
            </div>
            ${(b.slots || []).map((slot, si) => slotHtml(b, slot, si)).join("")}
            <div class="spb-foot">${nfmt(perBoxStems)} ${__("stems / box")}</div>
        </div>`;
	};

	// ONE Boxes control for the whole spec, not one per bunch card. A spec
	// describes a single box: its bunches are what go INTO that box, so they
	// cannot be ordered in different quantities. The server enforces exactly
	// that -- build_spec_rows throws "A spec is one box -- every bunch must
	// use the same Boxes count", and again if any bunch is missing from the
	// fill -- and spec_fill_dialog.js (the dashboard's picker) has always
	// worked this way. A per-card input could only ever produce a payload the
	// server rejects, so there is nothing for it to mean.
	const html = `
    <div class="spa" data-uid="${esc(uid)}">
      ${bunches.map(cardHtml).join("")}
      <div class="spa-foot spa-foot-row">
        <span class="spa-badge">${esc(spec_kind(data))}</span>
        <label class="spb-boxes-label">${__("Boxes")}
            <input type="number" min="0" class="spb-boxes form-control input-sm" value="0">
        </label>
        <span class="spa-foot-tot">Total: <b class="spa-tot-boxes">0</b> boxes &middot; <b class="spa-tot-stems">0</b> stems</span>
      </div>
    </div>`;

	let $root = null;
	let on_change = () => {};

	function shared_boxes() {
		return $root ? cint($root.find(".spb-boxes").val()) : 0;
	}

	function totals() {
		const boxes = shared_boxes();
		let stems = 0;
		if (!$root || boxes <= 0) return { boxes: boxes > 0 ? boxes : 0, stems };
		$root.find(".spb-card").each(function () {
			stems += boxes * cint($(this).attr("data-per-box"));
		});
		return { boxes, stems };
	}

	function recompute() {
		const t = totals();
		$root.find(".spa-tot-boxes").text(nfmt(t.boxes));
		$root.find(".spa-tot-stems").text(nfmt(t.stems));
		on_change();
	}

	return {
		html,
		empty_message: __("Enter a box count for this specification."),
		bind($scope, onchange) {
			$root = $scope;
			on_change = onchange || (() => {});
			$root.on("input change", ".spb-boxes", recompute);
			recompute();
		},
		totals,
		collect() {
			const selections = [];
			if (!$root) return selections;
			const boxes = shared_boxes();
			// Nothing ordered: say so with an empty payload rather than a
			// partial one. build_spec_rows treats boxes <= 0 as "no rows".
			if (boxes <= 0) return selections;
			// EVERY bunch, always. They are the contents of one box, so a fill
			// that omits one is not a smaller order -- it is an incomplete box,
			// which is why the server rejects it outright.
			$root.find(".spb-card").each(function () {
				const $c = $(this);
				const picks = {};
				$c.find(".spb-slot").each(function () {
					const $slot = $(this);
					const colour = $slot.attr("data-colour");
					const variety =
						$slot.find(".spb-pick:checked").val() || $slot.attr("data-only-variety");
					if (variety) picks[colour] = variety;
				});
				selections.push({ bunch_id: $c.attr("data-bunch-id"), boxes, picks });
			});
			return selections;
		},
	};
}

// ----------------------------- picker dialog -----------------------------

// THE window. Which specs, and what to fill on them, are one question asked
// once: the multiselect at the top, and under it a variety picker per spec.
// Pick one and its picker is fetched and appended; pick another and it stacks
// below; remove one and its section is DETACHED, not destroyed -- pick it
// again and everything already typed into it is still there. A spec already
// pilled is dropped from the dropdown (spec_link_options), so the list only
// ever offers what is left to add.
//
// This was briefly two windows -- choose, then fill -- which meant the same
// list of specs was presented twice, the second time with every spec already
// chosen still in it. One window, asked once.
//
// One "Add to Order" appends every picked spec's lines to THIS order in one
// go, with mix/bunch groups kept distinct per spec (see append_spec_batch).
function open_spec_picker(frm, trigger_cdn, initial_specs) {
	// spec name -> entry, for every spec EVER ticked in this dialog (so
	// unticking is never destructive). An entry that failed to load carries
	// `error` and no section.
	const loaded = new Map();
	const pending = new Set(); // in-flight fetches, so a fast re-tick can't double-load
	let seq = 0;
	const opened_with = [...new Set((initial_specs || []).filter(Boolean))];

	// Declared before the Dialog, not `const d = new ...`: the multiselect's
	// get_data runs DURING construction (make_input -> setup_awesomplete ->
	// get_awesomplete_settings -> get_data), so a const would still be in its
	// temporal dead zone and reading it there throws
	// "Cannot access 'd' before initialization" -- taking the whole window
	// down before it can be shown. Declared this way it is merely undefined
	// for that one early call, which the readers below allow for.
	let d;
	d = new frappe.ui.Dialog({
		title: __("Add from Specifications"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "MultiSelectPills",
				fieldname: "specs",
				label: __("Specifications"),
				get_data(txt) {
					return spec_link_options(frm, txt, d ? d.get_value("specs") : []);
				},
				onchange() {
					sync_sections();
				},
			},
			{ fieldtype: "HTML", fieldname: "hint" },
			{ fieldtype: "HTML", fieldname: "grid" },
		],
		primary_action_label: __("Add to Order"),
		primary_action() {
			const batch = [];
			active().forEach((e) => {
				if (!e.section) return;
				const selections = e.section.collect();
				if (selections.length) batch.push({ data: e.data, selections });
			});
			if (!batch.length) {
				frappe.msgprint(
					selected().length
						? __(
								"Nothing picked yet — tick at least one variety and give it a box count."
						  )
						: __("Select at least one Specification.")
				);
				return;
			}
			d.hide();
			append_spec_batch(frm, trigger_cdn, batch);
		},
	});

	// Without a Customer the link query can't narrow to their specs (same
	// filters the items-row Specification link uses), so say so up front
	// rather than letting the list look wrong.
	d.fields_dict.hint.$wrapper.html(
		`<div class="spx-hint">${
			frm.doc.customer
				? __(
						"Pick every specification this order needs — each one gets its own variety picker below."
				  )
				: __(
						"No Customer is set yet, so every Active specification is offered. Set the Customer first to narrow this to their specs."
				  )
		}</div>`
	);

	const $w = d.fields_dict.grid.$wrapper;
	$w.html(
		SPA_STYLES +
			`<div class="spx">
        <div class="spx-list"></div>
        <div class="spx-empty">${__(
			"Pick a specification above and its varieties appear here."
		)}</div>
        <div class="spx-grand">${__("Order total")}: <b class="spx-tot-boxes">0</b> ${__(
				"boxes"
			)} &middot; <b class="spx-tot-stems">0</b> ${__("stems")}</div>
      </div>`
	);
	const $list = $w.find(".spx-list");

	// Only the spec header collapses -- a click inside the body belongs to that
	// spec's own picker (colour accordions, checkboxes, number inputs), and
	// .spx-head is never an ancestor of .spx-body, so the two never cross.
	$list.on("click", ".spx-head", function () {
		$(this).closest(".spx-spec").toggleClass("spx-open");
	});

	function selected() {
		// Same spec twice would just fight over its own mix group, so dedupe.
		if (!d) return [];
		return [...new Set((d.get_value("specs") || []).filter(Boolean))];
	}

	function active() {
		return selected()
			.map((s) => loaded.get(s))
			.filter(Boolean);
	}

	function sync_sections() {
		const specs = selected();
		const want = new Set(specs);
		// Detach (not remove) whatever got unticked -- keeps its inputs alive.
		loaded.forEach((e, spec) => {
			if (!want.has(spec)) e.$node.detach();
		});

		const missing = specs.filter((s) => !loaded.has(s) && !pending.has(s));
		if (missing.length) {
			missing.forEach((s) => pending.add(s));
			frappe.dom.freeze(__("Loading specification..."));
			Promise.all(missing.map(fetch_spec)).then(() => {
				missing.forEach((s) => pending.delete(s));
				frappe.dom.unfreeze();
				render();
			});
		}
		render();
	}

	// An incomplete spec throws server-side (see _require_clean_spec). That
	// modal is the answer for that one spec; the rest still load, and the dud
	// stays in the list as a banner so it's obvious which one failed.
	function fetch_spec(spec) {
		return frappe
			.call({
				method: "upande_packhouse.spec_autofill.get_spec_fill_data",
				args: { spec },
			})
			.then((r) => add_entry(spec, r && r.message))
			.catch(() => add_entry(spec, null));
	}

	function add_entry(spec, data) {
		const uid = "s" + seq++;
		const section = data ? build_spec_section(data, uid) : null;
		const error = data
			? section.error || null
			: __("Specification {0} can't be filled — see the message above.", [spec]);
		const title = esc((data && (data.spec_name || data.spec)) || spec);
		// seq is only ever bumped here, once per spec, so a spec keeps its colour
		// for the life of the window even when it is removed and picked again.
		const accent = spx_accent_style(seq - 1);
		const html = error
			? `<div class="spx-spec spx-failed" data-uid="${uid}">
           <div class="spx-head"><span class="spx-swatch"></span><span class="spx-title">${title}</span></div>
           <div class="spx-error">${error}</div>
         </div>`
			: `<div class="spx-spec spx-open" data-uid="${uid}" style="${accent}">
           <div class="spx-head">
             <span class="spx-caret">▸</span>
             <span class="spx-swatch"></span>
             <span class="spx-title">${title}</span>
             <span class="spx-sub">${esc(spec_kind(data))}</span>
             <span class="spx-tot"></span>
           </div>
           <div class="spx-body">${section.html}</div>
         </div>`;

		const $node = $(html).appendTo($list);
		const entry = { spec, data, uid, $node, section: error ? null : section, error };
		loaded.set(spec, entry);
		if (entry.section) entry.section.bind($node.find(".spx-body"), refresh_totals);
		return entry;
	}

	function render() {
		const specs = selected();
		// Re-append in pick order; appendTo MOVES an existing node, never clones,
		// so a re-ticked section keeps its handlers and its typed-in values.
		specs.forEach((s) => {
			const e = loaded.get(s);
			if (e) $list.append(e.$node);
		});
		$w.find(".spx-empty").toggle(!specs.length);
		refresh_totals();
	}

	function refresh_totals() {
		let boxes = 0,
			stems = 0;
		active().forEach((e) => {
			if (!e.section) return;
			const t = e.section.totals();
			boxes += t.boxes;
			stems += t.stems;
			e.$node
				.find(".spx-tot")
				.text(
					t.boxes || t.stems
						? `${nfmt(t.boxes)} ${__("boxes")} · ${nfmt(t.stems)} ${__("stems")}`
						: ""
				);
		});
		$w.find(".spx-tot-boxes").text(nfmt(boxes));
		$w.find(".spx-tot-stems").text(nfmt(stems));
	}

	// The fallback custom_line trigger must not open a SECOND window if
	// something sets a row's spec while this one is up.
	frm.__spa_picker_open = true;
	d.$wrapper.on("hidden.bs.modal", () => {
		frm.__spa_picker_open = false;
	});

	d.show();

	if (opened_with.length) {
		d.set_value("specs", opened_with); // fires onchange -> sync_sections
	} else {
		render();
	}
	return d;
}

// ----------------------------- appending -----------------------------

function next_group(frm, field, extra_rows) {
	let max = 0;
	(frm.doc.items || []).concat(extra_rows || []).forEach((r) => {
		max = Math.max(max, cint(r[field]));
	});
	return max + 1;
}

// custom_mix_group has to stay the SAME integer every time the FLAT (legacy,
// non-bunch-aware) picker fills a Mixed Box line for this spec on this order
// -- not a fresh one per popup click -- or the same physical box splits
// across several unrelated groups. This only holds for the legacy path,
// where one spec == one box: a bunch-aware spec can describe SEVERAL
// distinct boxes under one spec (confirmed real data: XPOL TOSCA_02720_10's
// bunch "1" and bunch "2" are two separate Mixed Box recipes), so reusing
// whatever mix_group this spec already has on the form would risk merging
// an unrelated bunch into it -- see append_spec_batch's bunch-aware branch,
// which always starts from a fresh, collision-free counter instead and lets
// build_spec_rows allocate one group per bunch_id server-side.
//
// extra_rows is what earlier specs in the SAME "Add to Order" click already
// produced but haven't been pushed onto frm.doc.items yet -- without it, two
// flat Mixed Box specs filled together would both be handed max+1 off the
// unchanged form and collapse into one group.
function mix_group_for_spec(frm, spec, extra_rows) {
	const existing = (frm.doc.items || [])
		.concat(extra_rows || [])
		.find((r) => r.custom_line === spec && r.custom_mixed_box && r.custom_mix_group);
	if (existing) return cint(existing.custom_mix_group);
	return next_group(frm, "custom_mix_group", extra_rows);
}

// Build every spec's rows (server-side, one call per spec, strictly in order
// so each spec's group numbers account for the ones before it), then append
// the whole lot to the form in a single pass.
function append_spec_batch(frm, trigger_cdn, batch, source_warehouse) {
	const acc = [];

	function build(i) {
		if (i >= batch.length) return Promise.resolve();
		const { data, selections } = batch[i];
		const mix_group_hint = data.bunch_aware
			? next_group(frm, "custom_mix_group", acc)
			: mix_group_for_spec(frm, data.spec, acc);
		return frappe
			.call({
				method: "upande_packhouse.spec_autofill.build_spec_rows",
				args: {
					spec: data.spec,
					selections: JSON.stringify(selections),
					next_mix_group: mix_group_hint,
					next_bunch_group: next_group(frm, "custom_bunch_group", acc),
					source_warehouse: source_warehouse,
				},
			})
			.then((r) => {
				((r.message && r.message.rows) || []).forEach((row) => acc.push(row));
				return build(i + 1);
			});
	}

	build(0).then(() => {
		if (!acc.length) {
			frappe.msgprint(__("Nothing to add."));
			return;
		}
		// Drop the scratch row the Specification link was picked on, plus any
		// untouched blank row the grid left lying around -- the toolbar route
		// has no trigger row of its own, and an item-less row only blocks
		// Save. "Untouched" is deliberate: a row someone is halfway through
		// typing (boxes/packrate entered, item_code not yet) is theirs, not
		// ours to bin.
		frm.doc.items = (frm.doc.items || []).filter(
			(row) => row.name !== trigger_cdn && !is_untouched_row(row)
		);
		acc.forEach((row) => {
			Object.assign(frm.add_child("items"), row);
		}); // direct assign — no re-fire
		frm.refresh_field("items");
		frm.script_manager.trigger("calculate_taxes_and_totals");
		recompute_order_summary(frm); // box_math.js — add_child doesn't reliably fire items_add
		// Same reason: these rows never fire items_add, so nothing else would
		// give them the order's source warehouse and truck (warehouse_routing.js).
		// The server repeats this on save for any route that skips the form.
		apply_roses_routing(frm);
		const specs = batch.map((b) => b.data.spec_name || b.data.spec);
		frappe.show_alert(
			{
				message:
					batch.length === 1
						? __("Added {0} line(s) from {1}", [acc.length, specs[0]])
						: __("Added {0} line(s) from {1} specifications", [
								acc.length,
								batch.length,
						  ]),
				indicator: "green",
			},
			4
		);
	});
}

function is_untouched_row(row) {
	return !(
		row.item_code ||
		row.custom_line ||
		row.custom_number_of_boxes ||
		row.custom_packrate ||
		row.custom_packrate_mixed_box ||
		row.qty
	);
}
