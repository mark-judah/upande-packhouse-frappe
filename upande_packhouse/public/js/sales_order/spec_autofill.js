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

function open_spec_dialog(frm, trigger_cdn, data) {
	if (data.bunch_aware) {
		open_bunch_spec_dialog(frm, trigger_cdn, data);
		return;
	}
	const lines = data.lines || [];
	const boxOptions = data.box_options || [];
	if (!lines.length) {
		frappe.msgprint(__("Specification {0} has no approved colours.", [data.spec]));
		return;
	}
	if (!boxOptions.length) {
		frappe.msgprint(__("Specification {0} has no box items.", [data.spec]));
		return;
	}

	// box-type badge helps the salesperson see what they're building
	const kind = data.is_mixed_box ? "Mixed Box" : "Straight Box";
	const singleBox = boxOptions.length === 1;

	const fields = [{ fieldtype: "HTML", fieldname: "grid" }];

	const d = new frappe.ui.Dialog({
		title: __("Fill Order from {0}", [data.spec_name || data.spec]),
		size: "extra-large",
		fields: fields,
		primary_action_label: __("Add to Order"),
		primary_action() {
			// A spec is one box -- one shared Boxes count for every colour
			// ticked, not a per-row quantity (each row can still point at its
			// own box_idx/Stems-per-box; that's real per-colour pack data).
			const boxes = cint(d.$wrapper.find(".spa-order-boxes").val());
			if (boxes <= 0) {
				frappe.msgprint(__("Enter how many boxes to add."));
				return;
			}
			const selections = [];
			d.$wrapper.find(".spa-fill-row").each(function () {
				const $r = $(this);
				selections.push({
					line_idx: parseInt($r.attr("data-line-idx"), 10),
					box_idx: parseInt($r.attr("data-box-idx"), 10) || 0,
					variety: $r.attr("data-variety"),
					stems: cint($r.find(".spa-stems").val()),
					boxes,
				});
			});
			if (!selections.length) {
				frappe.msgprint(__("Tick at least one variety."));
				return;
			}
			d.hide();
			append_rows(
				frm,
				trigger_cdn,
				data.spec,
				selections,
				null,
				mix_group_for_spec(frm, data.spec)
			);
		},
	});

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
      .spa-stems{height:28px;width:80px}
      .spa-empty-fill{color:#8a8780;text-align:center;padding:14px 0;font-size:12px}
      .spa-order-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 12px;margin-top:10px;border:1px solid var(--border-color,#e2e4e9);border-radius:6px;background:var(--subtle-fg,#f8f8f6)}
      .spa-order-note{font-size:12px;color:#8a8780}
      .spa-order-boxes-label{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;margin-left:auto}
      .spa-order-boxes{width:80px;height:30px;font-size:13px}
      .spa-foot{margin-top:12px;padding-top:10px;border-top:1px solid var(--border-color,#e2e4e9);font-size:13px;text-align:right;color:#3a3a34}
      .spa-badge{float:left;font-size:11px;color:#8a8780;text-transform:uppercase;letter-spacing:.04em}
    </style>
    <div class="spa">
      <div class="spa-sections">${sectionsHtml}</div>
      <div class="spa-order-head">
        <span class="spa-order-note">${__(
			"This spec is one box -- the Boxes count applies to every colour ticked above"
		)}</span>
        <label class="spa-order-boxes-label">${__("Boxes")}
          <input type="number" min="0" class="spa-order-boxes form-control" value="1">
        </label>
      </div>
      <div class="spa-fill-wrap">
        <div class="spa-fill-title">${__("Fill in quantities for what you ticked above")}</div>
        <table class="spa-fill-tbl">
          <thead><tr>
            <th>${__("Colour")}</th><th>${__("Variety")}</th>
            <th>${__("Stems/Box")}</th><th style="text-align:right">${__("Stems")}</th>
          </tr></thead>
          <tbody class="spa-fill-tbody"><tr><td colspan="4" class="spa-empty-fill">${__(
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

	function syncTable() {
		const $tbody = $root.find(".spa-fill-tbody");
		const existing = {};
		$tbody.find(".spa-fill-row").each(function () {
			const $r = $(this);
			existing[$r.attr("data-key")] = { stems: $r.find(".spa-stems").val() };
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
						prev ? prev.stems : packrate
					}"></td>
                  <td class="spa-c spa-row-stems" style="text-align:right">0</td>
                </tr>`);
				});
			});
		});

		$tbody.html(
			rows.length
				? rows.join("")
				: `<tr><td colspan="4" class="spa-empty-fill">${__(
						"Tick varieties above to add them here"
				  )}</td></tr>`
		);
		recompute();
	}

	// A spec is one box: the single .spa-order-boxes field is the only
	// source of "how many", applied to every ticked row -- not summed
	// per-row the way a per-row Boxes input used to be (that's what made
	// three ticked colours read as "3 boxes" instead of one).
	function recompute() {
		const boxes = cint($w.find(".spa-order-boxes").val());
		let ts = 0;
		$w.find(".spa-fill-row").each(function () {
			const $r = $(this);
			const stems = cint($r.find(".spa-stems").val());
			const total = stems * boxes;
			$r.find(".spa-row-stems").text(nfmt(total));
			ts += total;
		});
		$w.find(".spa-tot-boxes").text(nfmt(boxes));
		$w.find(".spa-tot-stems").text(nfmt(ts));
	}

	$w.on("click", ".spa-acc-head", function (e) {
		if ($(e.target).is("select,option")) return; // don't collapse when picking a pack size
		$(this).closest(".spa-acc").toggleClass("spa-acc-open");
	});
	$w.on("change", ".spa-vcb", syncTable);
	$w.on("change", ".spa-box", syncTable);
	$w.on("input change", ".spa-fill-tbody .spa-stems", recompute);
	$w.on("input change", ".spa-order-boxes", recompute);

	d.show();
	syncTable();
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
// open_spec_dialog falls through to the flat colour picker, unchanged.
// Presentation lives in spec_fill_dialog.js (window.upande_open_spec_fill_dialog),
// shared verbatim with the Sales Order dashboard -- this function is now just
// the Desk-side wiring: hand it the server's fill data, and on submit, hand
// its selections straight to build_spec_rows via append_rows, exactly as
// before.
function open_bunch_spec_dialog(frm, trigger_cdn, data) {
	if (!(data.bunches || []).length) {
		frappe.msgprint(__("Specification {0} has no bunches defined.", [data.spec]));
		return;
	}
	window.upande_open_spec_fill_dialog(data, {
		onSubmit(selections) {
			// Fresh, collision-free starting point -- the server allocates
			// ONE mix_group for this whole call (every Mono Bunch component
			// of this one box shares it), not one per bunch_id.
			append_rows(frm, trigger_cdn, data.spec, selections, null, next_group(frm, "custom_mix_group"));
		},
	});
}

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
