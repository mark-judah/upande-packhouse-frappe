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

frappe.ui.form.on("Sales Order", {
	onload(frm) {
		set_spec_query(frm);
	},
	refresh(frm) {
		set_spec_query(frm);
	},
	customer(frm) {
		set_spec_query(frm);
	},
	validate(frm) {
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
		const filters = { status: "Active" };
		if (frm.doc.customer) filters.customer = frm.doc.customer;
		return { filters };
	});
}

frappe.ui.form.on("Sales Order Item", {
	custom_line(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.custom_line) return;
		frappe
			.call({
				method: "upande_packhouse.spec_autofill.get_spec_fill_data",
				args: { spec: row.custom_line },
			})
			.then((r) => {
				if (r.message) open_spec_dialog(frm, cdn, r.message);
			});
	},
});

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
			const selections = [];
			d.$wrapper.find(".spa-fill-row").each(function () {
				const $r = $(this);
				selections.push({
					line_idx: parseInt($r.attr("data-line-idx"), 10),
					box_idx: parseInt($r.attr("data-box-idx"), 10) || 0,
					variety: $r.attr("data-variety"),
					stems: cint($r.find(".spa-stems").val()),
					boxes: cint($r.find(".spa-boxes").val()),
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
			const boxCell = singleBox
				? `<span class="spa-meta">${boxMeta(bi)}</span>`
				: `<select class="spa-box form-control input-sm">${boxOptHtml}</select>`;
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
      .spa-stems,.spa-boxes{height:28px;width:80px}
      .spa-empty-fill{color:#8a8780;text-align:center;padding:14px 0;font-size:12px}
      .spa-foot{margin-top:12px;padding-top:10px;border-top:1px solid var(--border-color,#e2e4e9);font-size:13px;text-align:right;color:#3a3a34}
      .spa-badge{float:left;font-size:11px;color:#8a8780;text-transform:uppercase;letter-spacing:.04em}
    </style>
    <div class="spa">
      <div class="spa-sections">${sectionsHtml}</div>
      <div class="spa-fill-wrap">
        <div class="spa-fill-title">${__("Fill in quantities for what you ticked above")}</div>
        <table class="spa-fill-tbl">
          <thead><tr>
            <th>${__("Colour")}</th><th>${__("Variety")}</th>
            <th>${__("Stems/Box")}</th><th>${__("Boxes")}</th><th style="text-align:right">${__(
		"Stems"
	)}</th>
          </tr></thead>
          <tbody class="spa-fill-tbody"><tr><td colspan="5" class="spa-empty-fill">${__(
				"Tick varieties above to add them here"
			)}</td></tr></tbody>
        </table>
      </div>
      <div class="spa-foot"><span class="spa-badge">${esc(kind)}</span>
        Total: <b class="spa-tot-boxes">0</b> boxes &middot; <b class="spa-tot-stems">0</b> stems</div>
    </div>`;

	d.fields_dict.grid.$wrapper.html(html);

	const boxByIdx = {};
	boxOptions.forEach((bi) => {
		boxByIdx[bi.idx] = bi;
	});
	const lineByIdx = {};
	lines.forEach((line) => {
		lineByIdx[line.idx] = line;
	});

	const $w = d.$wrapper;

	function currentBoxIdx($acc) {
		const $sel = $acc.find(".spa-box");
		return $sel.length ? parseInt($sel.val(), 10) : 0;
	}

	function syncTable() {
		const $tbody = $w.find(".spa-fill-tbody");
		const existing = {};
		$tbody.find(".spa-fill-row").each(function () {
			const $r = $(this);
			existing[$r.attr("data-key")] = {
				stems: $r.find(".spa-stems").val(),
				boxes: $r.find(".spa-boxes").val(),
			};
		});

		const rows = [];
		$w.find(".spa-acc").each(function () {
			const $acc = $(this);
			const idx = parseInt($acc.attr("data-idx"), 10);
			const line = lineByIdx[idx];
			const boxIdx = currentBoxIdx($acc);
			const bi = boxByIdx[boxIdx] || boxOptions[0];
			const packrate = bi.pack_rate || bi.stems_per_bunch || 0;

			$acc.find(".spa-vcb:checked").each(function () {
				const $cb = $(this);
				const variety = $cb.data("variety");
				const key = idx + "::" + variety;
				const prev = existing[key];
				rows.push(`
                <tr class="spa-fill-row" data-key="${esc(
					key
				)}" data-line-idx="${idx}" data-box-idx="${boxIdx}" data-variety="${esc(variety)}">
                  <td>${esc(line.colour)}</td>
                  <td>${esc($cb.data("item-name") || variety)}</td>
                  <td><input type="number" class="spa-stems form-control input-sm" min="0" value="${
						prev ? prev.stems : packrate
					}"></td>
                  <td><input type="number" class="spa-boxes form-control input-sm" min="0" value="${
						prev ? prev.boxes : 1
					}"></td>
                  <td class="spa-c spa-row-stems" style="text-align:right">0</td>
                </tr>`);
			});
		});

		$tbody.html(
			rows.length
				? rows.join("")
				: `<tr><td colspan="5" class="spa-empty-fill">${__(
						"Tick varieties above to add them here"
				  )}</td></tr>`
		);
		recompute();
	}

	function recompute() {
		let tb = 0,
			ts = 0;
		$w.find(".spa-fill-row").each(function () {
			const $r = $(this);
			const stems = cint($r.find(".spa-stems").val());
			const boxes = cint($r.find(".spa-boxes").val());
			const total = stems * boxes;
			$r.find(".spa-row-stems").text(nfmt(total));
			tb += boxes;
			ts += total;
		});
		$w.find(".spa-tot-boxes").text(nfmt(tb));
		$w.find(".spa-tot-stems").text(nfmt(ts));
	}

	$w.on("click", ".spa-acc-head", function (e) {
		if ($(e.target).is("select,option")) return; // don't collapse when picking a pack size
		$(this).closest(".spa-acc").toggleClass("spa-acc-open");
	});
	$w.on("change", ".spa-vcb", syncTable);
	$w.on("change", ".spa-box", syncTable);
	$w.on("input change", ".spa-fill-tbody .spa-stems,.spa-fill-tbody .spa-boxes", recompute);

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
function open_bunch_spec_dialog(frm, trigger_cdn, data) {
	const bunches = data.bunches || [];
	if (!bunches.length) {
		frappe.msgprint(__("Specification {0} has no bunches defined.", [data.spec]));
		return;
	}
	const kind = data.is_mixed_box ? "Mixed Box" : "Straight Box";

	const fields = [{ fieldtype: "HTML", fieldname: "grid" }];
	const d = new frappe.ui.Dialog({
		title: __("Fill Order from {0}", [data.spec_name || data.spec]),
		size: "extra-large",
		fields: fields,
		primary_action_label: __("Add to Order"),
		primary_action() {
			const selections = [];
			d.$wrapper.find(".spb-card").each(function () {
				const $c = $(this);
				const boxes = cint($c.find(".spb-boxes").val());
				if (boxes <= 0) return;
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
			if (!selections.length) {
				frappe.msgprint(__("Enter a box count for at least one bunch."));
				return;
			}
			d.hide();
			// Fresh, collision-free starting point -- a bunch-aware spec can
			// describe several distinct boxes (see mix_group_for_spec's
			// comment), so this never reuses an existing group; the server
			// allocates one group per bunch_id from here.
			append_rows(
				frm,
				trigger_cdn,
				data.spec,
				selections,
				null,
				next_group(frm, "custom_mix_group")
			);
		},
	});

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
		const radioName = `spb-pick-${esc(b.bunch_id)}-${si}`;
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
        <div class="spb-card" data-bunch-id="${esc(b.bunch_id)}">
            <div class="spb-head">
                <span class="spb-id">${esc(b.bunch_id)}</span>
                ${badge}
                <label class="spb-boxes-label">${__("Boxes")}
                    <input type="number" min="0" class="spb-boxes form-control input-sm" value="0">
                </label>
            </div>
            ${(b.slots || []).map((slot, si) => slotHtml(b, slot, si)).join("")}
            <div class="spb-foot">${nfmt(perBoxStems)} ${__("stems / box")}</div>
        </div>`;
	};

	const html = `
    <style>
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
    </style>
    <div class="spa">
      <div class="spa-badge" style="float:left;font-size:11px;color:#8a8780;text-transform:uppercase;letter-spacing:.04em;margin-bottom:10px;">${esc(
			kind
		)}</div>
      <div style="clear:both"></div>
      ${bunches.map(cardHtml).join("")}
    </div>`;

	d.fields_dict.grid.$wrapper.html(html);
	d.show();
}

function next_group(frm, field) {
	let max = 0;
	(frm.doc.items || []).forEach((r) => {
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
// an unrelated bunch into it -- see append_rows's bunch-aware call site,
// which always starts from a fresh, collision-free counter instead and lets
// build_spec_rows allocate one group per bunch_id server-side.
function mix_group_for_spec(frm, spec) {
	const existing = (frm.doc.items || []).find(
		(r) => r.custom_line === spec && r.custom_mixed_box && r.custom_mix_group
	);
	if (existing) return cint(existing.custom_mix_group);
	return next_group(frm, "custom_mix_group");
}

function append_rows(frm, trigger_cdn, spec, selections, source_warehouse, mix_group_hint) {
	frappe
		.call({
			method: "upande_packhouse.spec_autofill.build_spec_rows",
			args: {
				spec: spec,
				selections: JSON.stringify(selections),
				next_mix_group: mix_group_hint,
				next_bunch_group: next_group(frm, "custom_bunch_group"),
				source_warehouse: source_warehouse,
			},
		})
		.then((r) => {
			const rows = (r.message && r.message.rows) || [];
			if (!rows.length) {
				frappe.msgprint(__("Nothing to add."));
				return;
			}
			frm.doc.items = (frm.doc.items || []).filter((row) => row.name !== trigger_cdn); // drop scratch row
			rows.forEach((data) => {
				Object.assign(frm.add_child("items"), data);
			}); // direct assign — no re-fire
			frm.refresh_field("items");
			frm.script_manager.trigger("calculate_taxes_and_totals");
			recompute_order_summary(frm); // box_math.js — add_child doesn't reliably fire items_add
			frappe.show_alert(
				{
					message: __("Added {0} line(s) from {1}", [rows.length, spec]),
					indicator: "green",
				},
				3
			);
		});
}
