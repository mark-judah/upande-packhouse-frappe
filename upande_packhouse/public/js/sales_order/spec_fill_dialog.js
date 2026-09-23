/* =====================================================================
   Upande · Fill Order from Spec dialog — shared by Desk (spec_autofill.js)
   and the Sales Order dashboard (www/sales-order.html), so both surfaces
   use the exact same UI and the exact same backend calls
   (spec_autofill.get_spec_fill_data / build_spec_rows). Loaded on Desk via
   hooks.py's doctype_js, and on the dashboard via a plain <script src>
   (frappe.ui.Dialog exists in frappe-web.bundle.js too, so this works
   unmodified in both places).

   Visual design adapted from the pasted "Universal Dashboard" reference,
   with one deliberate interaction change from that reference: a colour
   there could have several varieties ticked on independently, each its own
   additive line. Here a colour is one Approved-Variety SLOT (see
   Specifications' colour/is_primary model) -- every candidate in it shares
   ONE stems_per_bunch/pack_rate, so at most one is ever actually delivered;
   the others are substitutes, not additional ingredients. So selection is
   single-pick per colour (defaulting to the primary candidate), not a
   free multi-select -- picking two candidates in the same colour would
   otherwise produce two lines with the same rate for no real reason.

   window.upande_open_spec_fill_dialog(fillData, opts)
     fillData = the exact object spec_autofill.get_spec_fill_data returns.
     opts.onSubmit(selections) -- selections = [{bunch_id, boxes, picks}],
       the exact shape spec_autofill.build_spec_rows expects. Return `false`
       (or a Promise resolving false) to keep the dialog open (e.g. the
       caller's own save failed and the popup should stay so the user can
       adjust and retry).
     opts.onCancel() -- called when the dialog is dismissed without submitting.
   ===================================================================== */
const USFD_CSS = `
.usfd{--ink:#0a0a0a;--ink-2:#2a2a26;--ink-3:#3a3a34;--ink-4:#5a5a52;--ink-mute:#8a8780;--ink-faint:#b8b6ae;--bg:#f4f3ef;--surface:#fafaf6;--surface-2:#fff;--hairline:rgba(10,10,10,.06);--shadow-card:0 1px 0 rgba(10,10,10,.04),0 8px 32px -16px rgba(10,10,10,.10);--shadow-hover:0 1px 0 rgba(10,10,10,.06),0 24px 48px -24px rgba(10,10,10,.18);--signal:#228883;--neg:#c4302b;--grad-ink:linear-gradient(135deg,#0a0a0a 0%,#3a3a34 100%);--sans:'Poppins',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  display:flex;flex-direction:column;max-height:calc(100vh - 64px);background:var(--bg);font:400 14px/1.55 var(--sans);color:var(--ink-3);-webkit-font-smoothing:antialiased;text-align:left}
.usfd *{box-sizing:border-box}
.usfd button,.usfd input{font-family:var(--sans)}
.usfd button:focus-visible,.usfd input:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
.usfd-head{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;padding:26px 30px 20px;background:var(--surface-2);border-bottom:1px solid var(--hairline)}
.usfd-eyebrow{font:500 11px var(--sans);text-transform:uppercase;letter-spacing:2.2px;color:var(--ink-mute);margin-bottom:8px;display:inline-flex;align-items:center;gap:10px}
.usfd-eyebrow::before{content:"";width:18px;height:1px;background:var(--ink-3)}
.usfd-title{margin:0;font:600 26px/1.1 var(--sans);color:var(--ink);letter-spacing:-.7px}
.usfd-title span{font-weight:500;color:var(--ink-mute);margin-left:6px}
.usfd-sub{margin-top:6px;font:500 12px var(--sans);color:var(--ink-mute);letter-spacing:.3px}
.usfd-x{width:38px;height:38px;border:0;border-radius:50%;background:rgba(10,10,10,.04);color:var(--ink-4);display:grid;place-items:center;cursor:pointer;transition:all .2s;flex:none}
.usfd-x:hover{background:var(--ink);color:#fafaf6}
.usfd-x svg{width:15px;height:15px}
.usfd-scroll{flex:1;min-height:0;overflow-y:auto;padding:22px 30px 8px}
.usfd-summary{display:grid;grid-template-columns:1fr auto;gap:14px;margin-bottom:18px}
.usfd-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.usfd-stat{background:var(--surface-2);border-radius:16px;padding:14px 18px;box-shadow:var(--shadow-card)}
.usfd-stat small{display:block;font:500 10px var(--sans);text-transform:uppercase;letter-spacing:1.5px;color:var(--ink-mute);margin-bottom:6px}
.usfd-stat b{font:600 22px/1 var(--sans);color:var(--ink);letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.usfd-stat em{font:400 12px var(--sans);font-style:normal;color:var(--ink-mute);margin-left:4px}
.usfd-boxes{background:var(--grad-ink);color:#fafaf6;border-radius:16px;padding:12px 16px 12px 20px;display:flex;align-items:center;gap:16px;box-shadow:var(--shadow-card)}
.usfd-boxes small{display:block;font:500 10px var(--sans);text-transform:uppercase;letter-spacing:1.5px;color:rgba(250,250,246,.6);margin-bottom:2px}
.usfd-boxes span{font-size:11.5px;color:rgba(250,250,246,.75);max-width:160px;display:block;line-height:1.35}
.usfd-step{display:flex;align-items:center;background:rgba(250,250,246,.10);border-radius:999px;padding:4px}
.usfd-step button{width:30px;height:30px;border:0;border-radius:50%;background:transparent;color:#fafaf6;font-size:17px;line-height:1;cursor:pointer}
.usfd-step button:hover{background:rgba(250,250,246,.15)}
.usfd-step input{width:42px;border:0;background:transparent;color:#fafaf6;text-align:center;font:600 17px var(--sans);-moz-appearance:textfield}
.usfd-step input:focus{outline:none}
.usfd input::-webkit-outer-spin-button,.usfd input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.usfd-bunch{background:var(--surface-2);border-radius:18px;box-shadow:var(--shadow-card);margin-bottom:14px;overflow:hidden}
.usfd-bunch-hd{display:flex;align-items:center;gap:10px;padding:13px 20px;border-bottom:1px solid var(--hairline);flex-wrap:wrap}
.usfd-bunch-id{font:600 13px var(--sans);color:var(--ink)}
.usfd-chip{display:inline-flex;align-items:center;padding:3px 10px;font:500 11px var(--sans);background:rgba(10,10,10,.05);color:var(--ink-4);border-radius:99px;white-space:nowrap}
.usfd-chip.is-ink{background:var(--ink);color:#fafaf6}
.usfd-bunch-stems{margin-left:auto;font-size:11.5px;color:var(--ink-mute)}
.usfd-bunch-stems b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
.usfd-col{padding:14px 20px}
.usfd-col + .usfd-col{border-top:1px dashed var(--hairline)}
.usfd-col-hd{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.usfd-sw{width:13px;height:13px;border-radius:50%;flex:none;box-shadow:inset 0 0 0 1px rgba(10,10,10,.12)}
.usfd-col-name{font:600 14px var(--sans);color:var(--ink);letter-spacing:-.2px}
.usfd-vgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:9px}
.usfd-v{position:relative;display:flex;flex-direction:column;align-items:flex-start;gap:2px;text-align:left;padding:13px 16px;border:0;background:var(--surface);border-radius:14px;cursor:pointer;transition:background .2s,box-shadow .2s,transform .2s;box-shadow:inset 0 0 0 1px var(--hairline)}
.usfd-v:hover{background:#fff;box-shadow:var(--shadow-hover);transform:translateY(-1px)}
.usfd-v.is-on{background:#fff;box-shadow:inset 0 0 0 1.5px var(--ink),var(--shadow-card)}
.usfd-v.is-out{opacity:.72}
.usfd-tick{position:absolute;top:12px;right:12px;width:20px;height:20px;border-radius:50%;box-shadow:inset 0 0 0 1.5px var(--ink-faint);display:grid;place-items:center;color:transparent;transition:all .2s}
.usfd-tick svg{width:11px;height:11px}
.usfd-v.is-on .usfd-tick{background:var(--grad-ink);box-shadow:none;color:#fafaf6}
.usfd-v__name{font:600 13.5px var(--sans);color:var(--ink);padding-right:26px}
.usfd-v__badge{font:600 9.5px var(--sans);text-transform:uppercase;letter-spacing:.6px;color:var(--signal);margin-top:1px}
.usfd-v.is-on .usfd-v__badge.sub{color:var(--ink-mute)}
.usfd-v__qty{font:600 19px/1.2 var(--sans);color:var(--ink);letter-spacing:-.4px;font-variant-numeric:tabular-nums;margin-top:3px}
.usfd-v__qty small{font:400 11px var(--sans);color:var(--ink-mute);letter-spacing:0}
.usfd-v.is-out .usfd-v__qty{color:var(--ink-faint)}
.usfd-v__wh{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.usfd-v__wh span{display:inline-flex;gap:5px;padding:2px 8px;border-radius:99px;background:rgba(34,136,131,.10);color:var(--signal);font:500 10.5px var(--sans)}
.usfd-v__wh b{font-weight:600;font-variant-numeric:tabular-nums}
.usfd-v__wh span.usfd-v__none{background:rgba(196,48,43,.10);color:var(--neg)}
.usfd-empty{padding:26px;text-align:center;color:var(--ink-mute);font-size:13px;background:var(--surface);border-radius:14px;margin-bottom:14px}
.usfd-foot{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:15px 30px;background:var(--surface-2);border-top:1px solid var(--hairline)}
.usfd-foot__sum{font-size:12.5px;color:var(--ink-mute)}
.usfd-foot__sum b{font:600 19px var(--sans);color:var(--ink);letter-spacing:-.4px;font-variant-numeric:tabular-nums;margin-right:4px}
.usfd-warn{margin-left:12px;color:var(--neg);font-weight:500}
.usfd-foot__act{display:flex;gap:8px}
.usfd-btn{border:0;border-radius:999px;padding:11px 22px;font:500 13px var(--sans);background:var(--grad-ink);color:#fafaf6;cursor:pointer;box-shadow:0 4px 14px rgba(10,10,10,.20);transition:all .2s}
.usfd-btn:hover{transform:translateY(-1px)}
.usfd-btn.ghost{background:rgba(10,10,10,.04);color:var(--ink-4);box-shadow:none}
.usfd-btn.ghost:hover{background:rgba(10,10,10,.08);color:var(--ink)}
.usfd-btn:disabled{opacity:.35;cursor:not-allowed;transform:none}
@media (prefers-reduced-motion:reduce){.usfd *{transition:none!important}}
@media (max-width:900px){.usfd-summary{grid-template-columns:1fr}.usfd-stats{grid-template-columns:repeat(2,1fr)}.usfd-head,.usfd-foot{padding-left:18px;padding-right:18px}.usfd-scroll{padding:16px 18px 8px}.usfd-foot{flex-wrap:wrap}}
.usfd-modal .modal-dialog{max-width:1080px}
.usfd-modal .modal-content{border:0;border-radius:22px;overflow:hidden;background:#f4f3ef;box-shadow:0 40px 80px -30px rgba(10,10,10,.45)}
.usfd-modal .modal-header,.usfd-modal .modal-footer{display:none!important}
.usfd-modal .modal-body{padding:0!important}
.usfd-modal .form-layout,.usfd-modal .form-page,.usfd-modal .form-section,.usfd-modal .section-body,.usfd-modal .form-column,.usfd-modal .frappe-control,.usfd-modal .form-group{padding:0!important;margin:0!important;border:0!important}

/* Standalone overlay -- used where frappe.ui.Dialog isn't usable (website
   pages: the class exists in frappe-web.bundle.js, but its field machinery
   depends on frappe.ui.form.make_control, which that bundle doesn't ship --
   confirmed live, "frappe.ui.form.make_control is not a function"). Same
   .usfd content, just its own backdrop instead of a Bootstrap modal. */
.usfd-overlay-backdrop{position:fixed;inset:0;background:rgba(10,10,10,.5);z-index:2147483002;display:none;align-items:center;justify-content:center;padding:24px}
.usfd-overlay-backdrop.show{display:flex}
.usfd-overlay-backdrop .usfd{width:100%;max-width:1080px;max-height:calc(100vh - 48px);border-radius:22px;box-shadow:0 40px 80px -30px rgba(10,10,10,.45)}
`;

const USFD_SWATCH = {
	lilac: "#b9a3d9", lavender: "#b9a3d9", purple: "#7d5aa6", pink: "#ec9fbc", "hot pink": "#e0508a", cerise: "#d23a78",
	white: "#f3f1ea", cream: "#f3e6c4", red: "#c4302b", yellow: "#f2cf5b", orange: "#f0934a", peach: "#f5c1a0",
	salmon: "#f2a08a", green: "#a3c98f", blue: "#4c78b8", fuchsia: "#c23b8a",
};
function usfdSwatch(name) {
	const k = String(name || "").toLowerCase().trim();
	if (USFD_SWATCH[k]) return USFD_SWATCH[k];
	const hit = Object.keys(USFD_SWATCH).find((x) => k.startsWith(x));
	return hit ? USFD_SWATCH[hit] : "#b8b6ae";
}

function usfdMount(root, fillData, opts) {
	opts = opts || {};
	const esc = (s) =>
		String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
	const fmt = (n) => (Number(n) || 0).toLocaleString("en-US");
	const plural = (n, a, b) => (n === 1 ? a : b);
	const ICON_X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
	const ICON_TICK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';

	const bunches = (fillData.bunches || []).map((b) => ({
		bunch_id: b.bunch_id,
		is_mixed: !!b.is_mixed,
		slots: (b.slots || []).map((slot) => ({
			colour: slot.colour || "",
			candidates: slot.candidates || [],
			stems_per_bunch: slot.stems_per_bunch || 0,
			bunches_per_box: slot.bunches_per_box || 0,
			pack_rate: slot.pack_rate || 0,
			length: slot.length || "",
			// Default pick: the primary candidate (candidates are already
			// sorted primary-first by _bunches_from_spec) -- a colour is
			// never left unpicked, it just starts on the default.
			picked: (slot.candidates || [])[0] ? slot.candidates[0].variety : null,
		})),
	}));
	const specStemsPerBox = bunches.reduce((sum, b) => sum + b.slots.reduce((s2, s) => s2 + (s.pack_rate || 0), 0), 0);

	const st = { boxes: Math.max(1, +fillData.boxes || 1) };

	root.innerHTML = `<div class="usfd">
    <div class="usfd-head">
      <div>
        <div class="usfd-eyebrow">Fill order from spec</div>
        <h2 class="usfd-title">${esc(fillData.spec_name || fillData.spec || "")}</h2>
        <div class="usfd-sub">${esc(fillData.spec || "")}${fillData.is_mixed_box ? " · Mixed Box" : ""}</div>
      </div>
      <button type="button" class="usfd-x" data-act="close" aria-label="Close">${ICON_X}</button>
    </div>
    <div class="usfd-scroll">
      <div class="usfd-summary">
        <div class="usfd-stats" data-slot="stats"></div>
        <div class="usfd-boxes">
          <div><small>Boxes</small><span>This spec is one box — every bunch below combines into it</span></div>
          <div class="usfd-step">
            <button type="button" data-act="box-" aria-label="Fewer boxes">−</button>
            <input type="number" min="1" step="1" data-role="boxes" value="${st.boxes}" aria-label="Boxes">
            <button type="button" data-act="box+" aria-label="More boxes">+</button>
          </div>
        </div>
      </div>
      <div data-slot="bunches"></div>
    </div>
    <div class="usfd-foot" data-slot="foot"></div>
  </div>`;

	const $ = (s) => root.querySelector(`[data-slot="${s}"]`);

	function pickedLines() {
		const out = [];
		bunches.forEach((b) => {
			b.slots.forEach((slot) => {
				const cand = (slot.candidates || []).find((c) => c.variety === slot.picked);
				if (cand) out.push({ bunch: b, slot, cand });
			});
		});
		return out;
	}

	function renderStats() {
		const lines = pickedLines();
		const colN = bunches.reduce((n, b) => n + b.slots.length, 0);
		$("stats").innerHTML = [
			["Bunches", `${bunches.length}`],
			["Colours", `${colN}`],
			["Stems / box", fmt(specStemsPerBox)],
			["Total stems", fmt(specStemsPerBox * st.boxes)],
		]
			.map(([k, v]) => `<div class="usfd-stat"><small>${k}</small><b>${v}</b></div>`)
			.join("");
	}

	function tile(bi, si, cand, ci, isPicked) {
		const wh = (cand.by_farm ? Object.keys(cand.by_farm) : []).sort((x, y) => cand.by_farm[y] - cand.by_farm[x]);
		const whHtml = wh.length
			? wh.map((f) => `<span>${esc(f)}<b>${fmt(cand.by_farm[f])}</b></span>`).join("")
			: '<span class="usfd-v__none">Out of stock</span>';
		return `<button type="button" class="usfd-v${isPicked ? " is-on" : ""}${(cand.available || 0) ? "" : " is-out"}"
        data-act="pick" data-bi="${bi}" data-si="${si}" data-variety="${esc(cand.variety)}" aria-pressed="${isPicked}">
      <span class="usfd-tick">${ICON_TICK}</span>
      <span class="usfd-v__name">${esc(cand.item_name || cand.variety)}</span>
      <span class="usfd-v__badge${ci === 0 ? "" : " sub"}">${ci === 0 ? "Primary" : "Substitute"}</span>
      <span class="usfd-v__qty">${fmt(cand.available)}<small> stems</small></span>
      <span class="usfd-v__wh">${whHtml}</span>
    </button>`;
	}

	function bunchHtml(b, bi) {
		const stems = b.slots.reduce((s, sl) => s + (sl.pack_rate || 0), 0);
		const badge = b.is_mixed
			? `<span class="usfd-chip is-ink">Mixed Bunch · ${b.slots.length} colours</span>`
			: `<span class="usfd-chip">Mono Bunch</span>`;
		return `<div class="usfd-bunch" data-bi="${bi}">
      <div class="usfd-bunch-hd">
        <span class="usfd-bunch-id">Bunch ${esc(b.bunch_id)}</span>
        ${badge}
        <span class="usfd-bunch-stems"><b>${fmt(stems)}</b> stems / box</span>
      </div>
      ${b.slots
			.map(
				(slot, si) => `
        <div class="usfd-col">
          <div class="usfd-col-hd">
            <span class="usfd-sw" style="background:${usfdSwatch(slot.colour)}"></span>
            <span class="usfd-col-name">${esc(slot.colour || "(no colour)")}</span>
            <span class="usfd-chip">${esc(slot.length)} · ${slot.stems_per_bunch}/bunch</span>
          </div>
          <div class="usfd-vgrid">${
				(slot.candidates || []).length
					? slot.candidates.map((c, ci) => tile(bi, si, c, ci, c.variety === slot.picked)).join("")
					: '<div class="usfd-empty" style="grid-column:1/-1">No approved variety for this colour</div>'
			}</div>
        </div>`
			)
			.join("")}
    </div>`;
	}

	function renderBunches() {
		$("bunches").innerHTML = bunches.length
			? bunches.map(bunchHtml).join("")
			: '<div class="usfd-empty">This specification has no bunches defined.</div>';
	}

	function renderFoot() {
		const lines = pickedLines();
		const over = lines.filter((l) => (l.slot.pack_rate || 0) * st.boxes > (l.cand.available || 0));
		const ok = lines.length > 0 && lines.every((l) => l.cand);
		$("foot").innerHTML = `<div class="usfd-foot__sum"><b>${fmt(specStemsPerBox * st.boxes)}</b> stems · ${lines.length} ${plural(
			lines.length,
			"line",
			"lines"
		)} · ${st.boxes} ${plural(st.boxes, "box", "boxes")}${
			over.length ? `<span class="usfd-warn">${over.length} ${plural(over.length, "line exceeds", "lines exceed")} shelf stock</span>` : ""
		}</div>
      <div class="usfd-foot__act"><button type="button" class="usfd-btn ghost" data-act="close">Cancel</button><button type="button" class="usfd-btn" data-act="submit"${
			ok ? "" : " disabled"
		}>Add to order</button></div>`;
	}

	const all = () => {
		renderStats();
		renderBunches();
		renderFoot();
	};
	const setBoxes = (n) => {
		st.boxes = Math.max(1, parseInt(n, 10) || 1);
		root.querySelector('[data-role="boxes"]').value = st.boxes;
		renderStats();
		renderFoot();
	};

	root.addEventListener("click", (e) => {
		const b = e.target.closest("[data-act]");
		if (!b || !root.contains(b)) return;
		const act = b.dataset.act;
		if (act === "close") return opts.onCancel && opts.onCancel();
		if (act === "pick") {
			bunches[+b.dataset.bi].slots[+b.dataset.si].picked = b.dataset.variety;
			return all();
		}
		if (act === "box-") return setBoxes(st.boxes - 1);
		if (act === "box+") return setBoxes(st.boxes + 1);
		if (act === "submit") {
			const selections = bunches.map((bch) => {
				const picks = {};
				bch.slots.forEach((slot) => {
					if (slot.picked) picks[slot.colour] = slot.picked;
				});
				return { bunch_id: bch.bunch_id, boxes: st.boxes, picks };
			});
			opts.onSubmit && opts.onSubmit(selections);
		}
	});
	root.addEventListener("change", (e) => {
		if (e.target.dataset.role === "boxes") setBoxes(e.target.value);
	});

	all();
	return { getBoxes: () => st.boxes };
}

function usfdInjectAssets() {
	if (!document.getElementById("usfd-font")) {
		const l = document.createElement("link");
		l.id = "usfd-font";
		l.rel = "stylesheet";
		l.href = "https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap";
		document.head.appendChild(l);
	}
	let s = document.getElementById("usfd-css");
	if (!s) {
		s = document.createElement("style");
		s.id = "usfd-css";
		document.head.appendChild(s);
	}
	s.textContent = USFD_CSS;
}

window.upande_open_spec_fill_dialog = function (fillData, opts) {
	opts = opts || {};
	usfdInjectAssets();
	const d = new frappe.ui.Dialog({
		title: fillData.spec_name || fillData.spec || "Fill Order",
		size: "extra-large",
		fields: [{ fieldtype: "HTML", fieldname: "usfd" }],
	});
	d.$wrapper.addClass("usfd-modal");
	let busy = false;
	usfdMount(d.fields_dict.usfd.$wrapper.get(0), fillData, {
		onCancel: () => {
			d.hide();
			opts.onCancel && opts.onCancel();
		},
		onSubmit: (selections) => {
			if (busy) return;
			busy = true;
			Promise.resolve(opts.onSubmit ? opts.onSubmit(selections) : null)
				.then((r) => {
					if (r !== false) d.hide();
				})
				.catch((err) => {
					console.error(err);
					frappe.msgprint(__("Could not add lines: {0}", [(err && err.message) || err]));
				})
				.finally(() => {
					busy = false;
				});
		},
	});
	d.show();
	return d;
};

// Website-page entry point (the Sales Order dashboard): no frappe.ui.Dialog,
// just this page's own backdrop-div pattern (matching every other popup
// already in www/sales-order.html) with the same usfdMount content inside.
window.upande_open_spec_fill_overlay = function (fillData, opts) {
	opts = opts || {};
	usfdInjectAssets();
	let backdrop = document.getElementById("usfdOverlayBackdrop");
	if (!backdrop) {
		backdrop = document.createElement("div");
		backdrop.id = "usfdOverlayBackdrop";
		backdrop.className = "usfd-overlay-backdrop";
		const inner = document.createElement("div");
		inner.id = "usfdOverlayInner";
		backdrop.appendChild(inner);
		document.body.appendChild(backdrop);
		backdrop.addEventListener("mousedown", (e) => {
			if (e.target === backdrop) backdrop.classList.remove("show");
		});
	}
	const root = document.getElementById("usfdOverlayInner");
	let busy = false;
	usfdMount(root, fillData, {
		onCancel: () => {
			backdrop.classList.remove("show");
			opts.onCancel && opts.onCancel();
		},
		onSubmit: (selections) => {
			if (busy) return;
			busy = true;
			Promise.resolve(opts.onSubmit ? opts.onSubmit(selections) : null)
				.then((r) => {
					if (r !== false) backdrop.classList.remove("show");
				})
				.catch((err) => {
					console.error(err);
					alert("Could not add lines: " + ((err && err.message) || err));
				})
				.finally(() => {
					busy = false;
				});
		},
	});
	backdrop.classList.add("show");
};
