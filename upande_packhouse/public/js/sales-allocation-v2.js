// Sales Allocation in the packhouse v2 shell (www/sales-allocation-v2.html).
//
// Layout:
//   [ compact toolbar: location · delivery ]
//   [ orders rail ] [ order lines | bucket table ] [ action bar ]
//
// Only the selected order line's bucket table renders, so allocating a bucket
// no longer rebuilds every line's table.
// Short blocking decisions (downgrade reason, substitute variety) stay dialogs.
// Server calls go to the same whitelisted methods as the desk page.
window.frappe = window.frappe || {};
frappe.pages = frappe.pages || {};
frappe.pages["sales-allocation-v2"] = frappe.pages["sales-allocation-v2"] || {};
// Started by the www page once Frappe's web runtime, the controls and the dialogs are in.
frappe.pages["sales-allocation-v2"].boot = function (root) {
	const P = frappe.pages["sales-allocation-v2"];
	const page = {
		main: $(root),
		// the topbar's refresh button is the page's Refresh
		add_inner_button: (_label, fn) => PH.onRefresh(fn),
	};
	P.add_styles();
	P.make(page);
	// deep links come in on the URL: ?sales_order=&delivery_date=&sales_order_item=&farm=
	const q = new URLSearchParams(window.location.search);
	if (q.get("sales_order")) {
		frappe.route_options = Object.fromEntries(q.entries());
		P.on_page_show();
	}
};

// A deep link to one order: pre-selects the location + moves the delivery window onto
// THAT order's delivery date, then auto-selects it once the order list loads.
frappe.pages["sales-allocation-v2"].on_page_show = function () {
	const P = frappe.pages["sales-allocation-v2"];
	// Route options travel through the URL, so a missing value comes back as
	// the TEXT "null" / "undefined" -- and a date control handed "undefined"
	// pops "Date undefined must be in format". Keep only real values.
	const opt = (key) => {
		const v = frappe.route_options && frappe.route_options[key];
		return v && v !== "null" && v !== "undefined" ? String(v) : null;
	};
	const sales_order = opt("sales_order");
	if (!sales_order) return;
	const delivery_date = opt("delivery_date");
	const deeplink = {
		sales_order,
		item: opt("sales_order_item"),
		farm: opt("farm"),
		delivery_date: /^\d{4}-\d{2}-\d{2}$/.test(delivery_date || "") ? delivery_date : null,
	};
	frappe.route_options = null;
	P._run_deeplink(deeplink);
};
frappe.pages["sales-allocation-v2"]._run_deeplink = function (deeplink) {
	const P = frappe.pages["sales-allocation-v2"];
	P._deeplinked = true; // the order's own day wins over the default
	const proceed = () => {
		// Prefer the location that actually owns this order's farm; fall back
		// to whatever's already selected, else the first configured location.
		let loc_name = null;
		if (deeplink.farm && P.location_config) {
			const loc = P.location_config.locations.find((l) =>
				(l.farms || []).includes(deeplink.farm)
			);
			if (loc) loc_name = loc.name;
		}
		if (!loc_name) {
			loc_name =
				P.selected_location ||
				(P.location_config &&
					P.location_config.locations[0] &&
					P.location_config.locations[0].name);
		}
		if (!loc_name) {
			frappe.msgprint(__("No shelf locations configured — cannot open this order here."));
			return;
		}
		// Show the order's own delivery day, so it stays listed after the
		// deep-link has been consumed (refreshes, allocating). The server
		// lists the deep-linked order whatever the window says anyway. No
		// delivery date (order gone, or not passed): keep the current window
		// rather than blanking it.
		if (deeplink.delivery_date) {
			P.set_date_filter("delivery_start", deeplink.delivery_date);
			P.set_date_filter("delivery_end", deeplink.delivery_date);
		}
		P._pending_select_order = deeplink.sales_order;
		P._deeplink_item = deeplink.item || null; // open on this variety's line, not the first
		if (P.selected_location === loc_name) {
			P.load_sales_orders();
		} else {
			P.select_location(loc_name); // itself calls load_sales_orders()
		}
	};
	if (P.location_config) {
		proceed();
	} else {
		// First-ever page visit — location_config is still loading async.
		let tries = 0;
		const wait = setInterval(() => {
			tries += 1;
			if (P.location_config) {
				clearInterval(wait);
				proceed();
			} else if (tries > 50) {
				clearInterval(wait);
			}
		}, 150);
	}
};

frappe.pages["sales-allocation-v2"].add_styles = function () {
	if (document.getElementById("sales-allocation-v2-styles")) return;
	// ufd-modern font stack (Poppins + JetBrains Mono) — matches every other
	// Upande dashboard. Loaded once, applied only inside .ufd-sa2 so the rest
	// of the Frappe Desk chrome (sidebar/topbar/other pages) is untouched.
	if (!document.getElementById("sales-allocation-v2-fonts")) {
		const fontLink = document.createElement("link");
		fontLink.id = "sales-allocation-v2-fonts";
		fontLink.rel = "stylesheet";
		fontLink.href =
			"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600&family=Poppins:wght@400;500;600;700&display=swap";
		document.head.appendChild(fontLink);
	}
	const styleEl = document.createElement("style");
	styleEl.id = "sales-allocation-v2-styles";
	styleEl.textContent = `
        /* ═══ ufd-modern tokens (scoped — never touches the rest of Desk) ═══ */
        .ufd-sa2 {
            --ink:#0a0a0a; --ink-1:#1a1a18; --ink-2:#2a2a26; --ink-3:#3a3a34;
            --ink-4:#5a5a52; --ink-mute:#8a8780; --ink-faint:#b8b6ae;
            --bg:#f4f3ef; --surface:#fafaf6; --surface-2:#ffffff;
            --hairline:rgba(10,10,10,0.07);
            --shadow-card:0 1px 0 rgba(10,10,10,0.04), 0 8px 32px -16px rgba(10,10,10,0.10);
            --shadow-hover:0 1px 0 rgba(10,10,10,0.06), 0 24px 48px -24px rgba(10,10,10,0.18);
            --signal:#228883; --signal-soft:rgba(34,136,131,0.10);
            --good:#1a8a3a; --good-soft:rgba(26,138,58,0.10);
            --warn:#9a5a00; --warn-2:#f59e0b; --warn-soft:rgba(245,158,11,0.12);
            --bad:#c4302b; --bad-2:#7a2218; --bad-soft:rgba(196,48,43,0.10);
            --grad-ink:linear-gradient(135deg,#0a0a0a 0%,#3a3a34 100%);
            --sans:'Poppins',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            --mono:'JetBrains Mono',ui-monospace,monospace;
            font-family:var(--sans);
        }
        .ufd-sa2, .ufd-sa2 input, .ufd-sa2 select, .ufd-sa2 button { font-family:var(--sans); }

        /* ═══ Shell ═══ */
        .ufd-sa2.sales-allocation-container {
            padding: 16px 20px 20px;
            background: var(--bg);
            height: calc(100vh - 96px);
            min-height: 620px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .ufd-sa2 .sa-shell {
            flex: 1;
            min-height: 0;
            display: grid;
            grid-template-columns: 320px minmax(0,1fr);
            /* Without an explicit row track, a single-row grid sizes to its
               content's natural height (auto) instead of stretching to fill
               .sa-shell's own (flex:1) height -- so a long bucket table or
               order list just grows .sa-detail/.sa-rail past the viewport,
               pushing the action bar (and the rail's own bottom) off-screen
               with no scrollbar, fixable only by zooming the whole page
               out. minmax(0,1fr) gives the row a definite height instead, so
               it stretches to fill .sa-shell exactly and the descendants'
               own min-height:0 + overflow-y:auto regions (.sa-work,
               .sa-lines, .sales-order-list) scroll internally -- the header,
               toolbar, and action bar stay pinned in view at any zoom. */
            grid-template-rows: minmax(0, 1fr);
            gap: 12px;
        }

        /* ═══ Compact toolbar (one line) ═══ */
        .ufd-sa2 .sa-toolbar {
            background: var(--surface-2);
            border: 1px solid var(--hairline);
            border-radius: 14px;
            padding: 8px 14px;
            display: flex;
            align-items: center;
            gap: 8px 16px;
            flex-wrap: wrap;
            box-shadow: var(--shadow-card);
        }
        .ufd-sa2 .sa-tb-group { display: flex; align-items: center; gap: 7px; }
        .ufd-sa2 .sa-tb-label {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); white-space: nowrap;
        }
        .ufd-sa2 .sa-tb-div { width: 1px; height: 22px; background: var(--hairline); }
        .ufd-sa2 .sa-day-prog { display: flex; align-items: center; gap: 10px; min-width: 260px; }
        .ufd-sa2 .sa-day-prog .bar { width: 180px; height: 8px; border-radius: 999px; background: rgba(10,10,10,0.08); overflow: hidden; }
        .ufd-sa2 .sa-day-prog .bar i { display: block; height: 100%; width: 0; border-radius: 999px; background: var(--grad-ink); transition: width .4s ease; }
        .ufd-sa2 .sa-day-prog.done .bar i { background: var(--good); }
        .ufd-sa2 .sa-day-prog .pct { font: 600 12px var(--sans); color: var(--ink); min-width: 36px; text-align: right; }
        .ufd-sa2 .sa-day-prog .what { font-size: 11px; color: var(--ink-mute); white-space: nowrap; max-width: 160px; overflow: hidden; text-overflow: ellipsis; }
        .ufd-sa2 .sa-to { font-size: 11px; color: var(--ink-faint); }
        .ufd-sa2 .location-buttons { display: flex; gap: 3px; background: var(--surface); border: 1px solid var(--hairline); border-radius: 999px; padding: 2px; }
        .ufd-sa2 .location-btn {
            padding: 5px 13px;
            border: 0;
            border-radius: 999px;
            background: transparent;
            cursor: pointer;
            font: 600 12px var(--sans);
            color: var(--ink-4);
            white-space: nowrap;
            transition: all 0.15s;
        }
        .ufd-sa2 .location-btn:hover { color: var(--ink); }
        .ufd-sa2 .location-btn:disabled { cursor: not-allowed; opacity: .45; }
        .ufd-sa2 .location-btn.active:disabled { opacity: 1; }
        .ufd-sa2 .location-btn.active { background: var(--grad-ink); color: #fafaf6; }
        .ufd-sa2 .location-btn .lb-sub { display: none; }
        .ufd-sa2 .location-info {
            margin-left: auto;
            font-size: 11px;
            color: var(--ink-mute);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 340px;
        }
        .ufd-sa2 .sa-inp {
            padding: 5px 9px;
            border: 1px solid var(--hairline);
            border-radius: 8px;
            font-size: 12px;
            background: var(--surface);
            color: var(--ink-3);
            outline: none;
        }
        .ufd-sa2 .sa-inp:focus { border-color: var(--ink-faint); background: var(--surface-2); }
        .ufd-sa2 .sa-date { width: 130px; }
        .ufd-sa2 .sa-date .frappe-control,
        .ufd-sa2 .sa-date .form-group { margin: 0; }
        .ufd-sa2 .sa-date .control-label,
        .ufd-sa2 .sa-date .help-box,
        .ufd-sa2 .sa-date .tooltip-content { display: none; }
        .ufd-sa2 .sa-date input.form-control {
            height: auto;
            padding: 5px 9px;
            border: 1px solid var(--hairline);
            border-radius: 8px;
            font-size: 12px;
            background: var(--surface);
            color: var(--ink-3);
            box-shadow: none;
        }
        .ufd-sa2 .sa-date input.form-control:focus { border-color: var(--ink-faint); background: var(--surface-2); }
        .ufd-sa2 .sa-ghost-btn {
            padding: 5px 12px;
            border: 1px solid var(--hairline);
            border-radius: 999px;
            background: transparent;
            cursor: pointer;
            font: 600 11px var(--sans);
            color: var(--ink-4);
            white-space: nowrap;
            transition: all 0.15s;
        }
        .ufd-sa2 .sa-ghost-btn:hover { background: var(--ink); border-color: var(--ink); color: #fafaf6; }

        /* ═══ Orders rail ═══ */
        .ufd-sa2 .sa-rail {
            background: var(--surface-2);
            border-radius: 18px;
            box-shadow: var(--shadow-card);
            padding: 14px 14px 8px;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        .ufd-sa2 .rail-head {
            display: flex; align-items: baseline; justify-content: space-between;
            gap: 8px; margin-bottom: 10px;
        }
        .ufd-sa2 .rail-head h3 { margin: 0; font: 600 14px var(--sans); color: var(--ink); letter-spacing: -.2px; }
        .ufd-sa2 .rail-head #resultsCountV2 { font-size: 11px; color: var(--ink-mute); white-space: nowrap; }
        /* minmax(0,1fr) + min-width:0 lets the selects shrink to the rail width
           instead of overflowing to their content width and being clipped. */
        .ufd-sa2 .rail-filters { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; margin-bottom: 10px; }
        .ufd-sa2 .rail-filters > * { min-width: 0; }
        .ufd-sa2 .rail-filters .rf-row { display: flex; gap: 6px; min-width: 0; }
        .ufd-sa2 .rail-filters .rf-row > * { flex: 1 1 0; min-width: 0; }
        .ufd-sa2 .rail-filters .rf-row .rf-clear { flex: 0 0 auto; }
        .ufd-sa2 .rf-stages { display: flex; gap: 4px; padding: 3px; border-radius: 999px; background: rgba(10,10,10,0.05); }
        .ufd-sa2 .rf-stages button { flex: 1 1 auto; min-width: 0; display: inline-flex; align-items: center; justify-content: center; gap: 4px; padding: 5px 7px; border: 0; border-radius: 999px; background: transparent; font: 500 11px var(--sans); color: var(--ink-mute); cursor: pointer; white-space: nowrap; }
        .ufd-sa2 .rf-stages button:hover { color: var(--ink); }
        .ufd-sa2 .rf-stages button.is-on { background: var(--ink); color: #fff; }
        .ufd-sa2 .rf-stages .n { font: 600 10px var(--sans); padding: 0 5px; border-radius: 999px; background: rgba(10,10,10,0.08); }
        .ufd-sa2 .rf-stages button.is-on .n { background: rgba(255,255,255,0.18); }
        .ufd-sa2 .rail-filters .sa-inp { padding: 6px 10px; width: 100%; box-sizing: border-box; }
        .ufd-sa2 .rf-clear {
            width: 30px; flex: 0 0 30px;
            border: 1px solid var(--hairline); border-radius: 8px;
            background: var(--surface); color: var(--ink-mute);
            cursor: pointer; font-size: 13px; line-height: 1;
        }
        .ufd-sa2 .rf-clear:hover { background: var(--bad); border-color: var(--bad); color: #fff; }
        /* thin scrollbars on the page's lists and panes */
        .ufd-sa2 .sales-order-list, .ufd-sa2 .sv-body, .ufd-sa2 .sa-work, .ufd-sa2 .sa-lines {
            scrollbar-width: thin; scrollbar-color: rgba(10,10,10,0.18) transparent;
        }
        .ufd-sa2 .sales-order-list::-webkit-scrollbar, .ufd-sa2 .sv-body::-webkit-scrollbar,
        .ufd-sa2 .sa-work::-webkit-scrollbar, .ufd-sa2 .sa-lines::-webkit-scrollbar { width: 6px; height: 6px; }
        .ufd-sa2 .sales-order-list::-webkit-scrollbar-thumb, .ufd-sa2 .sv-body::-webkit-scrollbar-thumb,
        .ufd-sa2 .sa-work::-webkit-scrollbar-thumb, .ufd-sa2 .sa-lines::-webkit-scrollbar-thumb { background: rgba(10,10,10,0.18); border-radius: 999px; }
        .ufd-sa2 .sales-order-list::-webkit-scrollbar-button, .ufd-sa2 .sv-body::-webkit-scrollbar-button,
        .ufd-sa2 .sa-work::-webkit-scrollbar-button, .ufd-sa2 .sa-lines::-webkit-scrollbar-button { display: none; }
        .ufd-sa2 .sales-order-list {
            flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden;
            margin: 0 -4px; padding: 2px 4px 8px;
        }

        /* ─── Order card ─── */
        .ufd-sa2 .sales-order-card {
            border: 1px solid var(--hairline);
            border-radius: 12px;
            padding: 10px 12px;
            margin-bottom: 7px;
            cursor: pointer;
            background: var(--surface-2);
            transition: all 0.15s;
        }
        .ufd-sa2 .sales-order-card:hover { border-color: var(--ink); box-shadow: var(--shadow-card); }
        .ufd-sa2 .sales-order-card.selected { border-color: transparent; box-shadow: inset 0 0 0 2px var(--ink); }
        .ufd-sa2 .soc-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .ufd-sa2 .soc-id { font: 600 12px var(--sans); color: var(--ink); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .ufd-sa2 .soc-pri { font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .4px; padding: 2px 7px; border-radius: 999px; }
        .ufd-sa2 .pri-high { background: var(--bad-soft); color: var(--bad); }
        .ufd-sa2 .pri-medium { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa2 .pri-low { background: rgba(10,10,10,0.05); color: var(--ink-mute); }
        .ufd-sa2 .soc-line {
            font-size: 11px; color: var(--ink-4); margin-top: 4px;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .ufd-sa2 .soc-line.muted { color: var(--ink-mute); font-size: 10px; }
        .ufd-sa2 .soc-plan { display: flex; gap: 6px; margin-top: 4px; }
        .ufd-sa2 .soc-seq, .ufd-sa2 .soc-team { font: 600 10px var(--sans); padding: 1px 7px; border-radius: 999px; }
        .ufd-sa2 .soc-seq { background: var(--ink); color: #fff; }
        .ufd-sa2 .soc-team { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa2 .soc-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; font-size: 10px; color: var(--ink-mute); }
        .ufd-sa2 .soc-bar { height: 4px; border-radius: 999px; background: rgba(10,10,10,0.06); margin-top: 4px; overflow: hidden; display: flex; }
        .ufd-sa2 .soc-bar-prop { height: 100%; background: repeating-linear-gradient(135deg, var(--good) 0 3px, rgba(26,138,58,0.35) 3px 6px); }
        .ufd-sa2 .soc-prop { color: var(--good); font-weight: 600; }
        .ufd-sa2 .soc-boxes { display: flex; justify-content: space-between; gap: 8px; margin-top: 6px; font: 600 11px var(--sans); color: var(--ink-3); }
        .ufd-sa2 .soc-boxshort { color: var(--bad); }
        .ufd-sa2 .soc-boxleft { color: var(--warn); }
        .ufd-sa2 .soc-boxok { color: var(--good); }

        /* ═══ Detail pane ═══ */
        .ufd-sa2 .sa-detail {
            background: var(--surface-2);
            border-radius: 18px;
            box-shadow: var(--shadow-card);
            min-height: 0;
            display: grid;
            /* Row order matches the DOM order below: head, toolbar, then the scrollable body. */
            grid-template-rows: auto auto minmax(0,1fr);
            overflow: hidden;
        }
        .ufd-sa2 .sa-detail-head {
            padding: 13px 20px;
            border-bottom: 1px solid var(--hairline);
            display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
        }
        .ufd-sa2 .sa-detail-toolbar {
            padding: 9px 20px;
            border-bottom: 1px solid var(--hairline);
            background: var(--surface);
            display: flex; align-items: center; gap: 8px 18px; flex-wrap: wrap;
        }
        .ufd-sa2 .sa-detail-body { min-height: 0; display: grid; grid-template-columns: 226px minmax(0,1fr); }
        .ufd-sa2 .sa-lines {
            min-height: 0; overflow-y: auto; padding: 12px;
            border-right: 1px solid var(--hairline); background: var(--surface);
        }
        .ufd-sa2 .sa-lines-title {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); margin: 2px 0 9px 2px;
        }
        .ufd-sa2 .sa-work { min-height: 0; overflow-y: auto; padding: 16px 20px 22px; }

        /* ─── Order line cards ─── */
        .ufd-sa2 .line-card {
            border: 1px solid var(--hairline);
            background: var(--surface-2);
            border-radius: 10px;
            padding: 9px 11px;
            margin-bottom: 6px;
            cursor: pointer;
            transition: all 0.15s;
        }
        .ufd-sa2 .line-card:hover { border-color: var(--ink-faint); }
        .ufd-sa2 .line-card.active { border-color: transparent; box-shadow: inset 0 0 0 2px var(--ink); }
        .ufd-sa2 .line-card .lc-name { font: 600 12px var(--sans); color: var(--ink); line-height: 1.35; }
        .ufd-sa2 .line-card .lc-meta { font-size: 10px; color: var(--ink-mute); margin-top: 2px; }
        .ufd-sa2 .line-card .lc-row { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; color: var(--ink-mute); margin-top: 3px; }
        .ufd-sa2 .line-card .lc-row > span:last-child { color: var(--ink-3); font-weight: 600; }
        .ufd-sa2 .line-card .lc-foot { display: flex; align-items: center; justify-content: space-between; gap: 6px; margin-top: 4px; }
        .ufd-sa2 .line-card .lc-pct { font: 600 11px var(--sans); color: var(--ink-3); }
        .ufd-sa2 .line-card .lc-tag {
            display: inline-block; font: 600 9px var(--sans); text-transform: uppercase;
            letter-spacing: .3px; padding: 2px 7px; border-radius: 999px; margin-top: 5px;
        }
        .ufd-sa2 .line-bar { height: 4px; border-radius: 999px; background: rgba(10,10,10,0.06); margin-top: 7px; overflow: hidden; }

        /* ─── Item header ─── */
        .ufd-sa2 .item-head { display: flex; align-items: flex-start; gap: 14px; flex-wrap: wrap; margin-bottom: 12px; }
        .ufd-sa2 .ih-title { font: 600 15px var(--sans); color: var(--ink); letter-spacing: -.2px; }
        .ufd-sa2 .ih-title .ih-code { font: 400 12px var(--mono); color: var(--ink-mute); margin-left: 6px; }
        .ufd-sa2 .ih-pills { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin-top: 7px; }
        .ufd-sa2 .ih-actions { margin-left: auto; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
        .ufd-sa2 .pill {
            display: inline-block; font: 600 10px var(--sans); letter-spacing: .2px;
            padding: 3px 9px; border-radius: 999px; white-space: nowrap;
        }
        .ufd-sa2 .pill-ink { background: var(--grad-ink); color: #fafaf6; }
        .ufd-sa2 .pill-signal { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa2 .pill-line { background: var(--ink-4); color: #fafaf6; }
        .ufd-sa2 .pill-name { background: var(--ink); color: #fafaf6; }
        .ufd-sa2 .pill-mono { background: var(--surface); color: var(--ink-mute); font-family: var(--mono); font-weight: 500; }
        .ufd-sa2 .pill-good { background: var(--good-soft); color: var(--good); }

        /* ─── Stat strip ─── */
        .ufd-sa2 .stat-strip {
            display: flex; border: 1px solid var(--hairline); border-radius: 12px;
            overflow: hidden; margin-bottom: 12px; background: var(--surface);
        }
        .ufd-sa2 .stat { flex: 1; min-width: 0; padding: 9px 13px; border-right: 1px solid var(--hairline); }
        .ufd-sa2 .stat:last-child { border-right: 0; }
        .ufd-sa2 .stat .st-l { font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .8px; color: var(--ink-faint); }
        .ufd-sa2 .stat .st-v { font: 600 15px var(--sans); color: var(--ink-2); margin-top: 3px; }
        .ufd-sa2 .stat .st-v small { font: 400 10px var(--sans); color: var(--ink-mute); margin-left: 2px; }

        .ufd-sa2 .note-band {
            border-radius: 10px; padding: 8px 12px; margin-bottom: 10px;
            font-size: 11.5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
        }
        .ufd-sa2 .note-good { background: var(--good-soft); color: var(--good); }
        .ufd-sa2 .confirmed-chip-alloc {
            display: inline-block; padding: 2px 9px; border-radius: 999px;
            background: var(--signal-soft); color: var(--signal); font-size: 10.5px; font-weight: 600;
        }
        .ufd-sa2 .note-band .nb-right { margin-left: auto; font-size: 10.5px; opacity: 0.85; }

        .ufd-sa2 .loading-state, .ufd-sa2 .empty-state { text-align: center; padding: 40px 20px; color: var(--ink-mute); font-size: 13px; }

        /* ─── Bucket table ─── */
        .ufd-sa2 .allocation-grid-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
        .ufd-sa2 .allocation-grid-table thead { background: var(--surface-2); position: sticky; top: -16px; z-index: 5; }
        .ufd-sa2 .allocation-grid-table th {
            padding: 9px 10px; text-align: left;
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .8px;
            color: var(--ink-faint); border-bottom: 1px solid var(--hairline); white-space: nowrap;
        }
        .ufd-sa2 .allocation-grid-table td {
            padding: 9px 10px; border-bottom: 1px solid var(--hairline);
            vertical-align: middle; color: var(--ink-3);
        }
        .ufd-sa2 .allocation-grid-table tbody tr:hover { background: rgba(10,10,10,0.02); }
        .ufd-sa2 .allocation-grid-table tbody tr.downgrade-bucket { background: var(--warn-soft); box-shadow: inset 3px 0 0 var(--warn-2); }
        .ufd-sa2 .allocation-grid-table tbody tr.previously-allocated-bucket { background: rgba(10,10,10,0.03); color: var(--ink-mute); box-shadow: inset 3px 0 0 var(--ink-faint); }
        .ufd-sa2 .allocation-grid-table tbody tr.preferred-farm-row { box-shadow: inset 3px 0 0 var(--signal); }
        .ufd-sa2 .allocation-grid-table tbody tr.awaiting-transfer-row { background: var(--warn-soft); box-shadow: inset 3px 0 0 var(--warn-2); }
        .ufd-sa2 .grid-badge {
            display: inline-block; padding: 2px 8px; border-radius: 999px;
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .3px;
        }
        .ufd-sa2 .badge-exact { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa2 .badge-downgrade { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa2 .badge-transit { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa2 .badge-preferred { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa2 .badge-awaiting { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa2 .badge-need { background: var(--bad-soft); color: var(--bad); }
        .ufd-sa2 .badge-ok { background: var(--good-soft); color: var(--good); }

        /* ─── Bucket visibility diagnostics ─── */
        .ufd-sa2 .bd-trigger {
            display: inline-block; margin: 0 0 10px;
            padding: 5px 11px; border: 1px dashed var(--ink-faint); border-radius: 999px;
            background: var(--surface-2); color: var(--ink-mute); font: 600 10.5px var(--sans);
            cursor: pointer; transition: all 0.15s;
        }
        .ufd-sa2 .bd-trigger:hover { border-color: var(--signal); color: var(--signal); }
        .ufd-sa2 .bd-trigger.has-issues { border-style: solid; border-color: var(--warn-2); color: var(--warn); background: var(--warn-soft); }
        .ufd-sa2 .bucket-diag { margin-bottom: 12px; }
        .ufd-sa2 .bd-loading { font-size: 11.5px; color: var(--ink-faint); padding: 6px 2px; }
        .ufd-sa2 .bd-strip {
            display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px 4px;
            background: var(--surface); border: 1px solid var(--hairline); border-radius: 10px; margin-bottom: 6px;
        }
        .ufd-sa2 .bd-summary { flex: 0 0 100%; font: 600 10px var(--sans); text-transform: uppercase; letter-spacing: .6px; color: var(--ink-faint); margin-bottom: 4px; }
        .ufd-sa2 .bd-chip {
            display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px 5px 8px;
            border-radius: 999px; font-size: 11px; font-weight: 600; cursor: pointer;
            border: 1px solid transparent; transition: all 0.15s; background: var(--surface-2);
            margin-bottom: 6px;
        }
        .ufd-sa2 .bd-chip .bd-count { opacity: 0.75; font-weight: 700; }
        .ufd-sa2 .bd-chip .bd-stems { font-weight: 400; opacity: 0.6; font-size: 10px; }
        .ufd-sa2 .bd-chip:hover { border-color: currentColor; }
        .ufd-sa2 .bd-chip.reason-eligible { background: var(--good-soft); color: var(--good); }
        .ufd-sa2 .bd-chip.reason-fully_allocated { background: var(--surface-2); color: var(--ink-mute); border-color: var(--hairline); }
        .ufd-sa2 .bd-chip.reason-too_short, .ufd-sa2 .bd-chip.reason-wrong_cut_stage { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa2 .bd-chip.reason-past_discard_age, .ufd-sa2 .bd-chip.reason-past_max_age { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa2 .bd-chip.reason-pending_discard { background: var(--bad-soft); color: var(--bad); }
        .ufd-sa2 .bd-chip.reason-in_transit { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa2 .bd-chip.reason-remote_farm { background: rgba(10,10,10,0.05); color: var(--ink-mute); }
        .ufd-sa2 .bd-chip .bd-hint {
            opacity: 0.6; font-weight: 600; font-size: 9px; text-transform: uppercase;
            letter-spacing: .4px; margin-left: 2px; text-decoration: underline;
        }

        /* ─── Farm + mix filters (single row in detail toolbar) ─── */
        .ufd-sa2 .farm-filter-bar, .ufd-sa2 .mix-filter-bar {
            background: none; border: 0; padding: 0; margin: 0;
            display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
        }
        .ufd-sa2 .farm-filter-bar label.title, .ufd-sa2 .mix-filter-bar label.title {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); white-space: nowrap; margin: 0;
        }
        .ufd-sa2 .farm-checkbox-group { display: flex; gap: 5px; flex-wrap: wrap; }
        .ufd-sa2 .farm-checkbox-item {
            display: flex; align-items: center; gap: 5px;
            padding: 4px 10px; border: 1px solid var(--hairline); border-radius: 999px;
            background: var(--surface-2); cursor: pointer; font-size: 11px; color: var(--ink-3);
            transition: all 0.15s; user-select: none;
        }
        .ufd-sa2 .farm-checkbox-item:hover { border-color: var(--ink-faint); }
        .ufd-sa2 .farm-checkbox-item.checked { background: var(--grad-ink); color: #fafaf6; border-color: transparent; }
        .ufd-sa2 .farm-checkbox-item.checked .farm-badge { background: rgba(255,255,255,0.2); color: #fafaf6; }
        .ufd-sa2 .farm-badge {
            font-size: 8.5px; padding: 1px 6px; border-radius: 999px; text-transform: uppercase;
            background: rgba(10,10,10,0.06); color: var(--ink-mute);
        }
        .ufd-sa2 .farm-badge.sales { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa2 .farm-badge.remote { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa2 .mix-filter-bar select {
            padding: 4px 11px; border: 1px solid var(--hairline); border-radius: 999px;
            font-size: 11.5px; background: var(--surface-2); color: var(--ink-3); max-width: 260px;
        }
        .ufd-sa2 .mix-filter-bar .mix-count { font-size: 10.5px; color: var(--ink-mute); }

        /* ─── Per-item cut stage filter ─── */
        .ufd-sa2 .item-batch-filter {
            display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
            padding: 0 0 9px; margin-bottom: 0; font-size: 11px;
        }
        .ufd-sa2 .item-batch-filter .ibf-label {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); white-space: nowrap;
        }
        .ufd-sa2 .item-batch-filter .ibf-group { display: flex; align-items: center; gap: 4px; }
        .ufd-sa2 .item-batch-filter select.ibf-select {
            padding: 3px 8px; border: 1px solid var(--hairline); border-radius: 999px;
            font-size: 11px; background: var(--surface-2); color: var(--ink-3); cursor: pointer;
        }
        .ufd-sa2 .item-batch-filter .ibf-range-sep { font-size: 10px; color: var(--ink-faint); }
        .ufd-sa2 .item-batch-filter .ibf-clear {
            padding: 3px 10px; border: 1px solid var(--hairline); border-radius: 999px;
            background: var(--surface-2); cursor: pointer; font-size: 10px; color: var(--ink-4);
            transition: all 0.15s;
        }
        .ufd-sa2 .item-batch-filter .ibf-clear:hover { background: var(--bad); border-color: var(--bad); color: #fff; }
        .ufd-sa2 .item-batch-filter .ibf-count { margin-left: auto; font-size: 10.5px; color: var(--ink-mute); }
        .ufd-sa2 .item-batch-filter .ibf-spec-pill {
            padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 600;
            background: var(--signal-soft); color: var(--signal);
        }
        .ufd-sa2 .item-batch-filter .ibf-spec-pill small {
            font-weight: 400; text-transform: uppercase; letter-spacing: .3px;
            opacity: 0.75; font-size: 9px; margin-left: 3px;
        }

        /* ─── Team select ─── */
        .ufd-sa2 .item-team-select {
            font-size: 11.5px; padding: 4px 11px; border-radius: 999px;
            background: var(--surface-2); color: var(--ink-3); cursor: pointer;
        }
        .ufd-sa2 .item-team-select.is-set { border: 1px solid var(--good); }
        .ufd-sa2 .item-team-select.is-unset { border: 1px solid var(--bad); }

        /* ─── Substitute picker ─── */
        .ufd-sa2 .substitute-btn, .ufd-sa2 .sa-mini-btn {
            padding: 4px 12px; border: 1px solid var(--hairline); border-radius: 999px;
            background: var(--surface-2); color: var(--ink-4); cursor: pointer;
            font: 600 11px var(--sans); transition: all 0.15s; white-space: nowrap;
        }
        .ufd-sa2 .substitute-btn:hover, .ufd-sa2 .sa-mini-btn:hover { background: var(--ink); border-color: var(--ink); color: #fafaf6; }
        .ufd-sa2 #detailToolbarV2 .dt-auto { margin-left: auto; display: inline-flex; gap: 8px; align-items: center; }
        .ufd-sa2 .sa-mini-btn.primary { background: var(--grad-ink); border-color: transparent; color: #fafaf6; }
        .ufd-sa2 .sa-mini-btn.primary:hover:not(:disabled) { box-shadow: 0 4px 14px rgba(10,10,10,0.28); transform: translateY(-1px); filter: brightness(1.25); }
        .ufd-sa2 .sa-mini-btn, .ufd-sa2 .sa-tab-btn { transition: transform .12s, box-shadow .12s, filter .12s, opacity .12s; }
        .ufd-sa2 .sa-mini-btn:disabled, .ufd-sa2 .sa-tab-btn:disabled { opacity: .4; cursor: not-allowed; box-shadow: none; transform: none; }
        .ufd-sa2 .sub-variety-card {
            border: 1px solid var(--hairline); border-radius: 12px; padding: 11px 14px;
            margin-bottom: 7px; cursor: pointer; transition: all 0.15s;
            display: flex; justify-content: space-between; align-items: center; gap: 12px;
        }
        .ufd-sa2 .sub-variety-card:hover { border-color: var(--ink-faint); }
        .ufd-sa2 .sub-variety-card.recommended { box-shadow: inset 3px 0 0 var(--signal); background: var(--signal-soft); }
        .ufd-sa2 .sub-variety-card .sub-name { font-weight: 600; font-size: 13px; color: var(--ink); }
        .ufd-sa2 .sub-variety-card .sub-meta { font-size: 11px; color: var(--ink-mute); margin-top: 2px; }
        .ufd-sa2 .sub-variety-card .sub-badge {
            font-size: 9px; padding: 2px 8px; border-radius: 999px;
            font-weight: 600; text-transform: uppercase; white-space: nowrap;
        }

        /* ─── Buttons & dialogs, scoped so the rest of Desk is untouched ─── */
        .ufd-sa2 .btn { font-family: var(--sans); border-radius: 999px !important; font-weight: 600; }
        .ufd-sa2 .btn-primary, .ufd-sa2 .btn-primary:focus { background: var(--grad-ink) !important; border-color: transparent !important; box-shadow: 0 2px 8px rgba(10,10,10,0.15); }
        .ufd-sa2 .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 14px rgba(10,10,10,0.22); }
        .ufd-sa2 .btn-default { background: var(--surface-2) !important; border-color: var(--hairline) !important; color: var(--ink-3) !important; }
        .ufd-sa2 .btn-default:hover { background: var(--ink) !important; color: #fafaf6 !important; }
        .ufd-sa2 .btn-danger, .ufd-sa2 .btn-danger:focus { background: var(--bad) !important; border-color: transparent !important; }
        .ufd-sa2 .btn-danger:hover { background: var(--bad-2) !important; }
        .modal-dialog.ufd-sa2-modal .modal-content { border-radius: 18px; border: 0; box-shadow: var(--shadow-hover); font-family: var(--sans); }
        .modal-dialog.ufd-sa2-modal .modal-header { border-bottom: 1px solid var(--hairline); }
        .modal-dialog.ufd-sa2-modal .modal-title { font: 600 16px var(--sans); color: var(--ink); letter-spacing:-.3px; }
        .modal-dialog.ufd-sa2-modal .modal-footer { border-top: 1px solid var(--hairline); }
        /* the team cell keeps one size whether it shows the name or the list, so the table never reflows */
        /* the team cell keeps one size; the team list floats over the table, so nothing moves */
        .modal-dialog.ufd-sa2-modal .ca-team-cell { display: flex; align-items: center; gap: 4px; width: 150px; height: 30px; }
        .modal-dialog.ufd-sa2-modal .ca-team-name { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .modal-dialog.ufd-sa2-modal .ca-team-name.is-none { color: var(--bad); font-weight: 600; }
        .modal-dialog.ufd-sa2-modal .ca-team-name.is-changed { padding: 3px 8px; border-radius: 999px; background: rgba(26,138,58,0.12); color: #1a8a3a; font-weight: 600; }
        .modal-dialog.ufd-sa2-modal table tr.is-editing > td { background: rgba(34,136,131,0.10) !important; }
        .modal-dialog.ufd-sa2-modal table tr.is-editing > td:first-child { box-shadow: inset 3px 0 0 #228883; }
        .modal-dialog.ufd-sa2-modal tr.is-editing .ca-team-edit { background: rgba(10,10,10,0.08); color: var(--ink); }
        .modal-dialog.ufd-sa2-modal .ca-team-edit { flex: none; display: inline-grid; place-items: center; width: 26px; height: 26px; border: 0; border-radius: 8px; background: transparent; color: var(--ink-mute); cursor: pointer; }
        .modal-dialog.ufd-sa2-modal .ca-team-edit:hover { background: rgba(10,10,10,0.06); color: var(--ink); }
        .modal-dialog.ufd-sa2-modal .ca-team-edit svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
        .modal-dialog.ufd-sa2-modal .ca-team-menu { position: fixed; z-index: 1100; box-sizing: border-box; max-height: 280px; overflow-y: auto; overflow-x: hidden; padding: 4px; background: #fff; border: 1px solid var(--hairline); border-radius: 12px; box-shadow: 0 12px 32px rgba(10,10,10,0.14); display: flex; flex-direction: column; gap: 2px; }
        .modal-dialog.ufd-sa2-modal .ca-team-opt { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; min-width: 0; padding: 7px 8px; border: 0; border-radius: 8px; background: transparent; font: 500 13px var(--sans); color: var(--ink); text-align: left; cursor: pointer; }
        .modal-dialog.ufd-sa2-modal .ca-team-opt:hover, .modal-dialog.ufd-sa2-modal .ca-team-opt:focus-visible { background: rgba(10,10,10,0.05); outline: none; }
        .modal-dialog.ufd-sa2-modal .ca-team-opt.is-on { font-weight: 600; }
        .modal-dialog.ufd-sa2-modal .ca-team-opt span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .modal-dialog.ufd-sa2-modal .ca-team-opt svg { flex: none; }
        .modal-dialog.ufd-sa2-modal .ca-team-opt svg { width: 14px; height: 14px; fill: none; stroke: var(--good); stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; visibility: hidden; }
        .modal-dialog.ufd-sa2-modal .ca-team-opt.is-on svg { visibility: visible; }
        .modal-dialog.ufd-sa2-modal, .modal-dialog.ufd-sa2-modal input, .modal-dialog.ufd-sa2-modal select, .modal-dialog.ufd-sa2-modal textarea { font-family: var(--sans); }
        .modal-dialog.ufd-sa2-modal .control-input, .modal-dialog.ufd-sa2-modal textarea.form-control { border-radius: 10px; border-color: var(--hairline); }
        /* ─── Shortages tab ─── */
        .ufd-sa2 .sa-tab-btn { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border: 1px solid var(--hairline); border-radius: 999px; background: transparent; cursor: pointer; font: 600 11px var(--sans); color: var(--ink-4); white-space: nowrap; transition: all .15s; }
        .ufd-sa2 .sa-tab-btn:hover:not(:disabled) { border-color: var(--ink-faint); color: var(--ink); }
        .ufd-sa2 .sa-tab-btn.active { background: var(--ink); border-color: var(--ink); color: #fafaf6; }
        .ufd-sa2 .sa-tab-btn.active:hover:not(:disabled) { color: #fafaf6; border-color: var(--ink); filter: brightness(1.35); box-shadow: 0 4px 14px rgba(10,10,10,0.28); transform: translateY(-1px); }
        .ufd-sa2 .sa-tab-btn .cnt { font: 600 10px var(--sans); padding: 0 6px; border-radius: 999px; background: var(--bad-soft); color: var(--bad); }
        .ufd-sa2 .sa-tab-btn.active .cnt { background: rgba(255,255,255,0.18); color: #fff; }
        .ufd-sa2 .sa-tb-end { margin-left: auto; }
        .ufd-sa2 .sa-confirm-btn:not(:disabled) { background: var(--good); border-color: var(--good); color: #fff; }
        .ufd-sa2 .sa-confirm-btn:not(:disabled):hover { filter: brightness(1.08); color: #fff; }
        .ufd-sa2 .sa-confirm-btn:disabled { opacity: .45; cursor: default; }
        .ufd-sa2 .sa-confirm-btn .cnt { background: rgba(255,255,255,0.22); color: #fff; }
        .ufd-sa2 .sv-count { font-size: 11px; color: var(--ink-mute); white-space: nowrap; }
        .ufd-sa2 .sv-detail { grid-template-rows: auto minmax(0,1fr); }
        .ufd-sa2 .sv-head { padding: 13px 20px; border-bottom: 1px solid var(--hairline); display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
        .ufd-sa2 .sv-head h2 { margin: 0; font: 600 16px var(--sans); color: var(--ink); letter-spacing: -.3px; }
        .ufd-sa2 .sv-head .sv-sub { font-size: 12px; color: var(--ink-mute); }
        .ufd-sa2 .sv-head .sv-open { margin-left: auto; }
        .ufd-sa2 .sv-body { min-height: 0; overflow-y: auto; scrollbar-width: thin; padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }
        .ufd-sa2 .soc-short { font: 600 11px var(--sans); color: var(--bad); white-space: nowrap; }
        .ufd-sa2 .sv-st { flex: none; font: 600 10px var(--sans); white-space: nowrap; padding: 2px 8px; border-radius: 999px; }
        .ufd-sa2 .sv-st--bad { background: var(--bad-soft); color: var(--bad); }
        .ufd-sa2 .sv-st--ok { background: var(--good-soft); color: var(--good); }
        .ufd-sa2 #salesAllocationDetailV2 { position: relative; }
        /* one order (Auto-allocate order): its name and customer are in the header already, and the steps
           fit the bucket area without scrolling sideways */
        .ufd-sa2 .sa-flow--order .cs-table th:nth-child(2), .ufd-sa2 .sa-flow--order .cs-table td:nth-child(2),
        .ufd-sa2 .sa-flow--order .dg-table th:nth-child(2), .ufd-sa2 .sa-flow--order .dg-table td:nth-child(2),
        .ufd-sa2 .sa-flow--order .ss-table th:nth-child(2), .ufd-sa2 .sa-flow--order .ss-table td:nth-child(2),
        .ufd-sa2 .sa-flow--order .ss-table tr.ss-order { display: none; }
        .ufd-sa2 .sa-flow--order .ss-table tr.ss-line[hidden] { display: table-row; }
        .ufd-sa2 .sa-flow--order .dg-table th, .ufd-sa2 .sa-flow--order .dg-table td { white-space: normal; padding-left: 8px; padding-right: 8px; }
        .ufd-sa2 .sa-flow--order .dg-reason { max-width: 170px; }
        .ufd-sa2 .sa-flow--order .mv-to.ss-table th, .ufd-sa2 .sa-flow--order .mv-to.ss-table td { padding-left: 4px; padding-right: 4px; }
        .ufd-sa2 .sa-flow--order .mv-to.ss-table td:first-child, .ufd-sa2 .sa-flow--order .mv-to.ss-table th:first-child { min-width: 120px; }
        .ufd-sa2 .sa-flow--order .mv-to.ss-table th.num { width: 48px; }
        .ufd-sa2 .sa-flow.sa-flow--order { right: auto; bottom: auto; border-radius: 12px; box-shadow: 0 0 0 1px var(--hairline); }
        .ufd-sa2 .sa-flow { position: absolute; inset: 0; z-index: 6; background: var(--surface-2, #fff); border-radius: 18px; }
        .ufd-sa2 .sa-day-run { display: flex; align-items: center; justify-content: center; }
        .ufd-sa2 .sa-step { flex-direction: column; min-height: 0; }
        .ufd-sa2 .sa-step__head { padding: 16px 20px 12px; border-bottom: 1px solid var(--hairline); display: flex; align-items: baseline; gap: 12px; }
        .ufd-sa2 .sa-step__head .sa-step__close { margin-left: auto; }
        .ufd-sa2 .ss-table th, .ufd-sa2 .ss-table td { padding: 6px 6px; }
        .ufd-sa2 .mv-to.ss-table th { white-space: normal; vertical-align: bottom; line-height: 1.25; }
        .ufd-sa2 .mv-to.ss-table th.num { width: 58px; }
        .ufd-sa2 .mv-to.ss-table td.ss-cust { max-width: 140px; white-space: normal; }
        .ufd-sa2 .mv-to.ss-table td:first-child, .ufd-sa2 .mv-to.ss-table th:first-child { width: auto; white-space: normal; min-width: 170px; }
        .ufd-sa2 .ss-table .ss-act { padding: 4px 10px; }
        .ufd-sa2 .ss-table .ds-why { white-space: normal; }
        .ufd-sa2 .sa-step__body { overflow-x: hidden; }
        .ufd-sa2 .sa-step__head h3 { margin: 0; font: 600 16px var(--sans); color: var(--ink); letter-spacing: -.3px; }
        .ufd-sa2 .sa-step__head .sa-step__n { font-size: 11px; color: var(--ink-mute); }
        .ufd-sa2 .sa-step__body { flex: 1; min-height: 0; overflow-y: auto; scrollbar-width: thin; padding: 14px 20px; }
        .ufd-sa2 .sa-step__foot { padding: 12px 20px; border-top: 1px solid var(--hairline); display: flex; justify-content: flex-end; gap: 8px; }
        .ufd-sa2 .sa-step__foot .sa-mini-btn { padding: 7px 16px; }
        .ufd-sa2 .sa-step__body .mv-wrap { overflow: visible; }
        .ufd-sa2 .sa-step__body thead th { position: sticky; top: -14px; z-index: 3; background: var(--surface-2, #fff); box-shadow: inset 0 -1px 0 var(--hairline); }
        .ufd-sa2 .sa-step__msg { font: 500 13px var(--sans); color: var(--ink); margin: 2px 0 12px; }
        .ufd-sa2 .sa-day-run__card { width: min(520px, 100%); padding: 28px; display: flex; flex-direction: column; gap: 12px; }
        .ufd-sa2 .sa-day-run__title { font: 600 15px var(--sans); color: var(--ink); letter-spacing: -.2px; }
        .ufd-sa2 .sa-day-run__bar { height: 10px; border-radius: 999px; background: rgba(10,10,10,0.08); overflow: hidden; }
        .ufd-sa2 .sa-day-run__bar i { display: block; height: 100%; width: 0; border-radius: 999px; background: var(--grad-ink); transition: width .4s ease; }
        .ufd-sa2 .sa-day-run.done .sa-day-run__bar i { background: var(--good); }
        /* getting ready: a moving bar until the first order is counted */
        .ufd-sa2 .sa-day-run.prep .sa-day-run__bar i { width: 30% !important; transition: none; animation: saPrep 1.1s ease-in-out infinite; }
        .ufd-sa2 .sa-day-run.prep .pct { display: none; }
        @keyframes saPrep { 0% { transform: translateX(-100%); } 100% { transform: translateX(340%); } }
        .ufd-sa2 .sa-day-run__meta { display: flex; gap: 12px; align-items: baseline; font-size: 12px; color: var(--ink-mute); min-width: 0; }
        .ufd-sa2 .sa-day-run__meta .pct { font: 600 13px var(--sans); color: var(--ink); flex: none; }
        .ufd-sa2 .sa-day-run__meta .what { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .ufd-sa2 .sv-skel { flex-shrink: 0; height: 64px; border-radius: 12px; margin-bottom: 7px; background: linear-gradient(90deg, rgba(10,10,10,.04), rgba(10,10,10,.08), rgba(10,10,10,.04)); background-size: 200% 100%; animation: svsk 1.2s infinite; }
        @keyframes svsk { to { background-position: -200% 0; } }
        /* one block per variety: a plain card with a clear edge, its header ruled off from the rest */
        .ufd-sa2 .ds-line { flex-shrink:0; background:#fff; border:1px solid rgba(10,10,10,0.14); border-radius:12px; overflow:hidden; }
        .ufd-sa2 .ds-head { display:flex; align-items:center; gap:10px; padding:11px 14px; background:#fff; border-bottom:1px solid rgba(10,10,10,0.08); font-size:12px; color:var(--ink-3); flex-wrap:wrap; }
        .ufd-sa2 .ds-head strong { font-size:13px; }
        .ufd-sa2 .ds-head strong { color:var(--ink); }
        .ufd-sa2 .ds-short { margin-left:auto; color:var(--bad); font-weight:600; }
        .ufd-sa2 .ds-short--ok { color:var(--good); }
        .ufd-sa2 .ds-short .ds-ok { color: var(--good); }
        .ufd-sa2 .ds-short .ds-bad { color: var(--bad); }
        .ufd-sa2 .ds-short .ds-dot { color: var(--ink-faint); }
        .ufd-sa2 .ds-why.ds-why--ok, .ufd-sa2 .ds-why--ok { background:var(--good-soft); color:var(--good); }
        .ufd-sa2 .ds-why { font-size:11px; padding:2px 8px; border-radius:999px; background:var(--warn-soft); color:var(--warn); }
        .ufd-sa2 .ds-donors { width:100%; font-size:12px; border-collapse:collapse; }
        .ufd-sa2 .ds-donors th { font-weight:500; color:var(--ink-mute); text-align:left; padding:6px 14px; }
        .ufd-sa2 .ds-donors td { padding:6px 14px; border-top:1px solid var(--hairline); color:var(--ink-3); }
        .ufd-sa2 .mv-modal.modal-dialog { width: fit-content; min-width: 560px; max-width: calc(100vw - 32px); }
        .ufd-sa2 .mv-wrap { overflow-x: auto; }
        /* a card never grows wider than the list: long names cut off, the bottom line wraps */
        .ufd-sa2 .sv-card { min-width: 0; max-width: 100%; box-sizing: border-box; }
        .ufd-sa2 .sv-card .soc-top { min-width: 0; }
        /* the shortages list is wider than the orders list: a card's figures fit on one line */
        .ufd-sa2 .sa-shell.sv-shell { grid-template-columns: 400px minmax(0,1fr); }
        .ufd-sa2 .sv-card .soc-line { margin-top: 2px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .ufd-sa2 .sv-tip { position: fixed; z-index: 30; max-width: 320px; padding: 10px 12px; border-radius: 12px; background: #fff; color: var(--ink); border: 1px solid rgba(10,10,10,0.16); box-shadow: 0 12px 32px rgba(10,10,10,0.12); font: 500 11px var(--sans); opacity: 0; pointer-events: none; transform: translateY(4px); transition: opacity .12s ease, transform .12s ease; }
        .ufd-sa2 .sv-tip.is-on { opacity: 1; transform: none; }
        .ufd-sa2 .ds-why--mix { cursor: help; }
        .ufd-sa2 .sv-tip__tbl { border-collapse: collapse; font-size: 12px; }
        .ufd-sa2 .sv-tip__tbl th { font: 600 10px var(--sans); color: var(--ink-mute); text-align: right; padding: 0 0 4px 16px; }
        .ufd-sa2 .sv-tip__tbl td { padding: 2px 0 2px 16px; text-align: right; white-space: nowrap; }
        .ufd-sa2 .sv-tip__tbl td:first-child { padding-left: 0; text-align: left; }
        .ufd-sa2 .sv-tip__tbl small { color: var(--ink-mute); font-size: 10px; }
        .ufd-sa2 .sv-tip__tbl .is-bad { color: var(--bad); font-weight: 600; }
        .ufd-sa2 .sv-tip__mix { display: flex; flex-direction: column; gap: 4px; min-width: 220px; }
        .ufd-sa2 .sv-tip__mix > div { display: flex; justify-content: space-between; gap: 16px; font-size: 12px; }
        .ufd-sa2 .sv-tip__mix small { color: var(--ink-mute); font-size: 10px; }
        .ufd-sa2 .sv-tip__mix .is-bad { color: var(--bad); font-weight: 600; }
        .ufd-sa2 .sv-tip__mix .is-ok { color: var(--good); font-weight: 600; }
        .ufd-sa2 .sv-tip__so { font: 600 10px var(--mono, var(--sans)); letter-spacing: .4px; color: var(--ink-mute); margin-bottom: 6px; }
        .ufd-sa2 .sv-tip__vs { display: flex; flex-wrap: wrap; gap: 4px; }
        .ufd-sa2 .sv-tip__vs span { padding: 3px 8px; border-radius: 999px; background: rgba(10,10,10,0.04); border: 1px solid rgba(10,10,10,0.10); white-space: nowrap; }
        .ufd-sa2 .sv-tip__vs small { margin-left: 4px; color: var(--ink-mute); font-size: 10px; }
        .ufd-sa2 .sv-prog { height: 4px; border-radius: 999px; background: rgba(10,10,10,0.07); overflow: hidden; margin: 10px 0 6px; }
        .ufd-sa2 .sv-prog i { display: block; height: 100%; background: var(--warn-2, #b7791f); border-radius: 999px; }
        .ufd-sa2 .sv-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font: 500 11px var(--sans); color: var(--ink-3); min-width: 0; }
        .ufd-sa2 .sv-meta > span { white-space: nowrap; }
        .ufd-sa2 .sv-meta .sv-st { margin-left: auto; }
        .ufd-sa2 .sv-meta .is-bad { color: var(--bad); }
        .ufd-sa2 .sv-meta .is-coming { color: var(--signal); }
        .ufd-sa2 .ds-places { flex-basis: 100%; border-collapse: collapse; font-size: 12px; color: var(--ink-3); }
        .ufd-sa2 .ds-places td { padding: 4px 12px 4px 0; white-space: nowrap; }
        .ufd-sa2 .ds-places td.num { text-align: right; }
        .ufd-sa2 .ds-places strong { color: var(--ink); }
        .ufd-sa2 .ds-stage { font: 600 11px var(--sans); color: var(--ink); }
        .ufd-sa2 .ds-coming { margin: 0; padding: 4px 14px; border-radius: 0; border-bottom: 1px solid rgba(10,10,10,0.08); color: var(--ink-3); font-size: 12px; display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; }
        .ufd-sa2 .mv-sides { display: grid; grid-template-columns: 1fr auto 1fr; gap: 12px; align-items: stretch; margin-bottom: 14px; }
        .ufd-sa2 .mv-arrow { align-self: center; font: 600 18px var(--sans); color: var(--ink-mute); }
        .ufd-sa2 .mv-side { border: 1px solid var(--hairline); border-radius: 12px; padding: 12px 14px; background: #fff; min-width: 0; }
        .ufd-sa2 .mv-side.is-from { border-color: var(--warn-2, #b7791f); }
        .ufd-sa2 .mv-side.is-to { border-color: var(--good); }
        .ufd-sa2 .mv-side { position: relative; }
        .ufd-sa2 .mv-sides { margin-top: 10px; }
        .ufd-sa2 .mv-side__tag { position: absolute; top: -9px; left: 14px; padding: 0 8px; background: #fff; font: 600 10px var(--sans); text-transform: uppercase; letter-spacing: 1px; line-height: 18px; }
        .ufd-sa2 .mv-side.is-from .mv-side__tag { color: var(--warn-2, #b7791f); }
        .ufd-sa2 .mv-side.is-to .mv-side__tag { color: var(--good); }
        .ufd-sa2 .mv-side__h { font: 600 13px var(--sans); color: var(--ink); }
        .ufd-sa2 .mv-side__sub { font-size: 11px; color: var(--ink-mute); margin: 2px 0 8px; }
        .ufd-sa2 .mv-side__r { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; padding: 3px 0; color: var(--ink-3); }
        .ufd-sa2 .mv-side__r b { font-weight: 600; color: var(--ink); text-align: right; }
        .ufd-sa2 .mv-need { color: var(--bad); font-weight: 600; }
        .ufd-sa2 .mv-ok { color: var(--good); font-weight: 600; }
        .ufd-sa2 .mv-to tbody tr { cursor: pointer; }
        .ufd-sa2 .ds-head .ds-sub { margin-left: 8px; }
        .ufd-sa2 .mv-to th, .ufd-sa2 .mv-to td { white-space: nowrap; vertical-align: middle; }
        .ufd-sa2 .mv-to td:first-child, .ufd-sa2 .mv-to th:first-child { width: 28px; padding-right: 0; }
        .ufd-sa2 .mv-to tbody tr:has(input:checked) td { background: var(--tint, #f6f5f1); }
        .ufd-sa2 .ds-donors .ds-so { font-size:11px; color:var(--ink-mute); margin-top:2px; }
        .ufd-sa2 .ds-donors td.num, .ufd-sa2 .ds-donors th.num { text-align:right; }
        .ufd-sa2 .ca-exp { border:0; background:transparent; width:22px; height:22px; padding:0; margin:0 6px 0 -4px; border-radius:6px; display:inline-grid; place-items:center; vertical-align:middle; cursor:pointer; color:var(--ink-mute); }
        .ufd-sa2 .ca-exp:hover { background:var(--hairline); color:var(--ink); }
        .ufd-sa2 .ca-oname, .ufd-sa2 .ca-mname { cursor:pointer; }
        .ufd-sa2 tr.ca-order, .ufd-sa2 tr.ca-group:has(.ca-mexp) { cursor:pointer; }
        .ufd-sa2 tr.ss-line td { background:#fafaf8; }
        .ufd-sa2 tr.ss-line td:first-child { padding-left:42px; }
        .ufd-sa2 .ss-acts { white-space: nowrap; }
        .ufd-sa2 .sum-table tr.sum-total td { border-top: 2px solid var(--hairline-2); }
        .ufd-sa2 .mv-to tr.sub-grp td { background: rgba(10,10,10,.04); font: 600 10.5px var(--sans); text-transform: uppercase; letter-spacing: .6px; color: var(--ink-mute); padding-top: 8px; padding-bottom: 6px; cursor: default; }
        .ufd-sa2 .ss-handled { display: inline-block; padding: 5px 12px; border-radius: 999px; background: rgba(10,10,10,.06); color: var(--ink-mute); font: 600 11px var(--sans); }
        .ufd-sa2 select.ss-with { width: 240px; max-width: 240px; }
        body.sa2-steps #alert-container { bottom: 110px !important; }
        .ufd-sa2 .sa-dlg-confirm { display: flex; align-items: center; gap: 8px; padding: 12px 20px; border-top: 1px solid var(--hairline); }
        .ufd-sa2 .sa-dlg-confirm__q { flex: 1; font: 500 13px var(--sans); color: var(--ink); }
        .ufd-sa2 .sa-dlg-confirm .sa-mini-btn { padding: 7px 16px; }
        body.sa2-busy .ufd-sa2 #salesOrderListV2 .sales-order-card { cursor: default; }
        .ufd-sa2 .ss-table tr.ss-order { cursor: pointer; user-select: none; }
        .ufd-sa2 .ss-table tr.ss-order:hover td { background: rgba(10,10,10,.03); }
        .ufd-sa2 .ss-table .ca-oname:hover { text-decoration: none; }
        .ufd-sa2 .ss-table tr.ss-order.is-open td { background: rgba(10,10,10,.06); }
        .ufd-sa2 .sub-sum { display: flex; gap: 10px; margin-bottom: 12px; }
        .ufd-sa2 .sub-sum > div { flex: 1; border: 1px solid var(--hairline); border-radius: 10px; padding: 8px 12px; display: flex; flex-direction: column; gap: 2px; }
        .ufd-sa2 .sub-sum span { font-size: 11px; color: var(--ink-mute); }
        .ufd-sa2 .sub-sum b { font: 600 15px var(--sans); color: var(--ink); }
        .ufd-sa2 .sub-short { color: var(--bad); }
        .ufd-sa2 .ss-acts .ss-act + .ss-act { margin-left: 6px; }
        .ufd-sa2 .ss-act.on { background: var(--grad-ink); border-color: transparent; color: #fafaf6; }
        .ufd-sa2 .dg-reason, .ufd-sa2 select[data-v], .ufd-sa2 select[data-a] { border:1px solid var(--hairline-2); border-radius:8px; padding:4px 8px; font:500 12px var(--sans); background:#fff; max-width:220px; }
        .ufd-sa2 .ca-mname:hover { text-decoration:underline; text-underline-offset:3px; }
        .ufd-sa2 .ca-oname:hover { text-decoration:none; }
        .ufd-sa2 .ca-exp svg { width:14px; height:14px; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; transition:transform .15s; }
        .ufd-sa2 .ca-exp[aria-expanded="true"] svg { transform:rotate(90deg); }
        .ufd-sa2 tr.ca-group td { background:#fafaf8; font-size:12px; padding-top:8px; padding-bottom:8px; }
        .ufd-sa2 tr.ca-group td:nth-child(2) { padding-left:42px; }
        .ufd-sa2 tr.ca-group:not(.ca-var) td:nth-child(2):has(.ca-mexp) { padding-left:20px; }
        .ufd-sa2 .ca-kind { font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:var(--ink-mute); margin-right:4px; }
        .ufd-sa2 .ca-vars { display:flex; flex-wrap:wrap; gap:4px 12px; margin-top:4px; color:var(--ink-mute); }
        .ufd-sa2.ca-release .mv-wrap { overflow-x: hidden; }
        .ufd-sa2 .mv-to tr.ca-group td { white-space: normal; }
        .ufd-sa2 tr.ca-group.ca-var td { padding-top:4px; padding-bottom:4px; border-top:0; color:var(--ink-mute); }
        .ufd-sa2 tr.ca-group.ca-var td:nth-child(2) { padding-left:72px; }
        .ufd-sa2 .ds-none { padding:9px 14px; font-size:12px; color:var(--ink-mute); }

        /* ─── Narrow screens ─── */
        @media (max-width: 1240px) {
            .ufd-sa2.sales-allocation-container { height: auto; }
            .ufd-sa2 .sa-shell, .ufd-sa2 .sa-shell.sv-shell { grid-template-columns: 1fr; }
            .ufd-sa2 .sa-rail { max-height: 380px; }
            .ufd-sa2 .sa-detail { min-height: 72vh; }
            .ufd-sa2 .sa-detail-body { grid-template-columns: 1fr; }
            .ufd-sa2 .sa-lines { border-right: 0; border-bottom: 1px solid var(--hairline); max-height: 200px; }
            .ufd-sa2 .stat-strip { flex-wrap: wrap; }
            .ufd-sa2 .stat { flex: 1 0 33%; }
        }

        /* Option lists of the page's dropdowns (a native list takes no CSS). */
        .sa-sel-menu {
            position: fixed !important; z-index: 1060; max-height: 320px !important; overflow-y: auto !important;
            padding: 4px; background: #fff; border: 1px solid rgba(10,10,10,0.08); border-radius: 10px;
            box-shadow: 0 1px 0 rgba(10,10,10,0.04), 0 16px 40px -12px rgba(10,10,10,0.22);
            font: 500 12px 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #3a3a34;
        }
        .sa-sel-menu:focus { outline: none; }
        .sa-sel-opt {
            display: flex; align-items: center; justify-content: space-between; gap: 10px;
            padding: 7px 10px; border-radius: 7px; cursor: pointer; white-space: nowrap;
        }
        .sa-sel-opt.active { background: #f4f3ef; color: #0a0a0a; }
        .sa-sel-opt.selected { color: #0a0a0a; font-weight: 600; }
        .sa-sel-opt.placeholder { color: #8a8780; }
        .sa-sel-opt.disabled { color: #b8b6ae; cursor: default; }
        .sa-sel-check { display: inline-flex; color: #228883; }
    `;
	document.head.appendChild(styleEl);
	frappe.pages["sales-allocation-v2"].styled_options();
	frappe.pages["sales-allocation-v2"].downgrade_help();
};

// Every dropdown on the page opens a styled option list instead of the browser's: the
// <select> stays as it is (value, classes, onchange), only its list is drawn here.
// The "?" on a downgrade bucket: on hover or focus, the other orders of the same day that ordered this
// variety at this length, and how short they end up if these stems are downgraded.
frappe.pages["sales-allocation-v2"].downgrade_help = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (P._dg_help_ready) return;
	P._dg_help_ready = true;
	const n = (v) => Math.round(v || 0).toLocaleString();
	const esc = (v) => frappe.utils.escape_html(v || "");
	let tip = null;
	const hide = () => {
		if (tip) tip.remove();
		tip = null;
	};
	const show = (el) => {
		hide();
		const item = (P.order_items || []).find((i) => i.sales_order_item === el.dataset.soItem);
		const c = item && (item.downgrade_conflicts || {})[el.dataset.length];
		if (!c) return;
		const take = parseFloat(el.dataset.take) || 0;
		const short_now = Math.max(0, c.needed - c.available);
		const short_after = Math.max(0, c.needed - Math.max(0, c.available - take));
		tip = document.createElement("div");
		tip.className = "sa-dg-tip";
		tip.innerHTML =
			`<div class="sa-dg-tip__head">${esc(item.item_name || item.item_code)} ${esc(
				el.dataset.length
			)}</div>` +
			`<div class="sa-dg-tip__nums"><span><b>${n(c.needed)}</b> needed by ${
				c.orders.length
			} order${c.orders.length === 1 ? "" : "s"}</span><span><b>${n(
				c.available
			)}</b> on the shelf</span></div>` +
			`<div class="sa-dg-tip__verdict ${short_after > short_now ? "is-bad" : ""}">${
				short_after > short_now
					? `Downgrading ${n(take)} stems shorts them by <b>${n(short_after)}</b>${
							short_now ? ` (${n(short_now)} before)` : ""
					  }`
					: `Downgrading ${n(take)} stems leaves enough for them`
			}</div>` +
			`<table>${c.orders
				.map(
					(o) =>
						`<tr><td><b>${esc(o.customer)}</b><small>${esc(
							o.order_name || o.sales_order
						)}${o.spec ? " · " + esc(o.spec) : ""}</small></td><td>${n(
							o.stems
						)}</td></tr>`
				)
				.join("")}</table>`;
		document.body.appendChild(tip);
		const r = el.getBoundingClientRect();
		const w = tip.offsetWidth,
			h = tip.offsetHeight;
		let left = Math.min(window.innerWidth - w - 8, Math.max(8, r.left + r.width / 2 - w / 2));
		let top = r.bottom + 6;
		if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
		tip.style.left = left + "px";
		tip.style.top = top + "px";
	};
	document.addEventListener("mouseover", (e) => {
		const el = e.target.closest && e.target.closest(".ufd-sa2 .sa-dg-help");
		if (el) show(el);
	});
	document.addEventListener("mouseout", (e) => {
		if (e.target.closest && e.target.closest(".ufd-sa2 .sa-dg-help")) hide();
	});
	document.addEventListener("focusin", (e) => {
		const el = e.target.closest && e.target.closest(".ufd-sa2 .sa-dg-help");
		if (el) show(el);
	});
	document.addEventListener("focusout", hide);
	window.addEventListener("scroll", hide, true);
	$("<style>")
		.text(
			`.ufd-sa2 .sa-dg-help { display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:50%; background:var(--warn-soft); color:var(--warn); font:700 10px var(--sans); cursor:help; vertical-align:middle; margin-left:2px; }
             .ufd-sa2 .sa-dg-help:hover, .ufd-sa2 .sa-dg-help:focus { background:var(--warn); color:#fff; outline:none; }
             .sa-dg-tip { position:fixed; z-index:2100; width:320px; max-width:calc(100vw - 16px); background:#fff; color:#1a1a1a; border-radius:12px; box-shadow:0 12px 32px rgba(10,10,10,.18), 0 0 0 1px rgba(10,10,10,.06); padding:12px 14px; font:400 12px/1.4 var(--sans, sans-serif); pointer-events:none; }
             .sa-dg-tip__head { font-weight:600; font-size:13px; margin-bottom:6px; }
             .sa-dg-tip__nums { display:flex; gap:14px; color:#6b6b6b; margin-bottom:6px; }
             .sa-dg-tip__nums b { color:#1a1a1a; }
             .sa-dg-tip__verdict { padding:6px 9px; border-radius:8px; background:#eef6ef; color:#2f6b3a; margin-bottom:8px; }
             .sa-dg-tip__verdict.is-bad { background:#fbecea; color:#b3261e; }
             .sa-dg-tip table { width:100%; border-collapse:collapse; }
             .sa-dg-tip td { padding:5px 0; border-top:1px solid rgba(10,10,10,.06); vertical-align:top; }
             .sa-dg-tip td:last-child { text-align:right; font-weight:600; white-space:nowrap; padding-left:10px; }
             .sa-dg-tip small { display:block; color:#6b6b6b; }`
		)
		.appendTo("head");
};
frappe.pages["sales-allocation-v2"].styled_options = function () {
	if (frappe.pages["sales-allocation-v2"]._styled_options) return;
	frappe.pages["sales-allocation-v2"]._styled_options = true;
	const CHECK =
		'<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
	let open = null;
	const close = () => {
		if (!open) return;
		open.menu.remove();
		open = null;
	};
	const choose = (sel, i) => {
		const o = sel.options[i];
		if (!o || o.disabled) return;
		const changed = sel.selectedIndex !== i;
		sel.selectedIndex = i;
		close();
		sel.focus({ preventScroll: true });
		if (changed) sel.dispatchEvent(new Event("change", { bubbles: true }));
	};
	const show = (sel) => {
		close();
		const menu = document.createElement("div");
		menu.className = "sa-sel-menu";
		menu.setAttribute("role", "listbox");
		menu.tabIndex = -1;
		const shown = [];
		menu.innerHTML = Array.from(sel.options)
			.map((o, i) => {
				if (o.hidden) return "";
				shown.push(i);
				const on = i === sel.selectedIndex;
				const cls = [
					"sa-sel-opt",
					on ? "selected" : "",
					o.disabled ? "disabled" : "",
					o.value === "" ? "placeholder" : "",
				].join(" ");
				return `<div class="${cls}" role="option" data-i="${i}" aria-selected="${on}"><span>${frappe.utils.escape_html(
					o.text
				)}</span><span class="sa-sel-check">${on ? CHECK : ""}</span></div>`;
			})
			.join("");
		document.body.appendChild(menu);
		let active = shown.indexOf(sel.selectedIndex);
		const highlight = () => {
			menu.querySelectorAll(".sa-sel-opt").forEach((el, k) =>
				el.classList.toggle("active", k === active)
			);
			const el = menu.querySelectorAll(".sa-sel-opt")[active];
			if (!el) return;
			// Scroll the list only: a page scroll closes the menu.
			if (el.offsetTop < menu.scrollTop) menu.scrollTop = el.offsetTop - 4;
			else if (el.offsetTop + el.offsetHeight > menu.scrollTop + menu.clientHeight)
				menu.scrollTop = el.offsetTop + el.offsetHeight - menu.clientHeight + 4;
		};
		const r = sel.getBoundingClientRect();
		menu.style.minWidth = Math.max(r.width, 160) + "px";
		const h = Math.min(menu.scrollHeight, 320);
		menu.style.left =
			Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + "px";
		menu.style.top =
			(window.innerHeight - r.bottom < h + 12 && r.top > h + 12
				? r.top - h - 6
				: r.bottom + 6) + "px";
		menu.addEventListener("mousedown", (e) => e.preventDefault());
		menu.addEventListener("click", (e) => {
			const el = e.target.closest(".sa-sel-opt");
			if (el) choose(sel, +el.dataset.i);
		});
		menu.addEventListener("mousemove", (e) => {
			const el = e.target.closest(".sa-sel-opt");
			const k = el ? shown.indexOf(+el.dataset.i) : -1;
			if (k >= 0 && k !== active) {
				active = k;
				highlight();
			}
		});
		menu.addEventListener("keydown", (e) => {
			if (e.key === "ArrowDown") active = Math.min(active + 1, shown.length - 1);
			else if (e.key === "ArrowUp") active = Math.max(active - 1, 0);
			else if (e.key === "Enter") return e.preventDefault(), choose(sel, shown[active]);
			else if (e.key === "Escape" || e.key === "Tab")
				return close(), sel.focus({ preventScroll: true });
			else return;
			e.preventDefault();
			highlight();
		});
		open = { sel, menu };
		highlight();
		menu.focus({ preventScroll: true });
	};
	document.addEventListener(
		"mousedown",
		(e) => {
			const sel = e.target.closest && e.target.closest(".ufd-sa2 select");
			if (open && !open.menu.contains(e.target) && open.sel !== sel) close();
			if (!sel || sel.disabled || sel.multiple || e.button !== 0) return;
			e.preventDefault();
			if (open && open.sel === sel) return close();
			sel.focus({ preventScroll: true });
			show(sel);
		},
		true
	);
	// The keys that open a native list open this one instead.
	document.addEventListener(
		"keydown",
		(e) => {
			const sel = e.target.closest && e.target.closest(".ufd-sa2 select");
			if (!sel || sel.disabled || sel.multiple || open) return;
			if (
				e.key === "Enter" ||
				e.key === " " ||
				(e.altKey && e.key === "ArrowDown") ||
				e.key === "F4"
			) {
				e.preventDefault();
				show(sel);
			}
		},
		true
	);
	window.addEventListener(
		"scroll",
		(e) => open && !open.menu.contains(e.target) && close(),
		true
	);
	window.addEventListener("resize", close);
};

frappe.pages["sales-allocation-v2"].make = function (page) {
	frappe.pages["sales-allocation-v2"].page = page;
	let $container = $('<div class="sales-allocation-container ufd-sa2"></div>').appendTo(
		page.main
	);
	page.add_inner_button(__("Refresh"), function () {
		// Refresh starts again from tomorrow's deliveries, like a reload.
		frappe.pages["sales-allocation-v2"].reset_to_tomorrow(true);
	});
	$container.html(`
        <div class="sa-toolbar">
            <div class="sa-tb-group">
                <span class="sa-tb-label">Location</span>
                <div class="location-buttons" id="locationButtonsV2">
                    <span style="padding:5px 12px;font-size:11px;color:var(--ink-mute);">Loading…</span>
                </div>
            </div>
            <div class="sa-tb-div"></div>
            <div class="sa-tb-group">
                <span class="sa-tb-label">Delivery</span>
                <div class="sa-date" data-filter="delivery_start"></div>
                <span class="sa-to">to</span>
                <div class="sa-date" data-filter="delivery_end"></div>
            </div>
            <div class="sa-tb-div"></div>
            <button class="sa-mini-btn primary" id="autoAllocateDayV2">Allocate all</button>
            <button class="sa-tab-btn" id="shortTabV2">Shortages<span class="cnt" style="display:none;"></span></button>
            <div class="sa-day-prog" id="dayProgV2" style="display:none;">
                <div class="bar"><i></i></div>
                <span class="pct">0%</span>
                <span class="what"></span>
            </div>
            <button class="sa-tab-btn sa-confirm-btn sa-tb-end" id="confirmDayV2" disabled>Release allocated orders<span class="cnt" style="display:none;"></span></button>
        </div>
        <div class="sa-shell sv-shell" id="shortViewV2" style="display:none;">
            <aside class="sa-rail">
                <div class="rail-head"><h3>Shortages</h3><div class="sv-count"></div></div>
                <div class="sales-order-list" id="shortListV2"></div>
            </aside>
            <section class="sa-detail sv-detail" id="shortDetailV2"></section>
        </div>
        <div class="sa-shell" id="mainShellV2">
            <aside class="sa-rail">
                <div class="rail-head">
                    <h3>Orders</h3>
                    <div id="resultsCountV2"></div>
                </div>
                <div class="rail-filters">
                    <input type="text" id="orderSearchInputV2" class="sa-inp" placeholder="Search order, customer, variety…">
                    <div class="rf-row">
                        <select id="priorityFilterV2" class="sa-inp">
                            <option value="">All priorities</option>
                            <option value="High">High</option>
                            <option value="Medium">Medium</option>
                            <option value="Low">Low</option>
                        </select>
                        <select id="boxTypeFilterV2" class="sa-inp">
                            <option value="">All box types</option>
                            <option value="mixed">Mixed only</option>
                            <option value="straight">Straight only</option>
                        </select>
                    </div>
                    <div class="rf-row">
                        <select id="itemGroupFilterV2" class="sa-inp"><option value="">All item groups</option></select>
                        <button class="rf-clear" id="clearFiltersV2" title="Clear filters">&times;</button>
                    </div>
                    <div class="rf-stages" id="allocStagesV2" role="group" aria-label="Allocation">
                        <button type="button" data-stage="0">Not allocated<span class="n"></span></button>
                        <button type="button" data-stage="1">Partly<span class="n"></span></button>
                        <button type="button" data-stage="2">Full<span class="n"></span></button>
                    </div>
                </div>
                <div class="sales-order-list" id="salesOrderListV2">
                    <div class="empty-state"><p>Select a location to begin.</p></div>
                </div>
            </aside>
            <section class="sa-detail" id="salesAllocationDetailV2">
                <header class="sa-detail-head" id="detailHeadV2"></header>
                <div class="sa-detail-toolbar" id="detailToolbarV2" style="display:none;"></div>
                <div class="sa-detail-body">
                    <nav class="sa-lines" id="linesRailV2"></nav>
                    <div class="sa-work" id="allocationWorkspaceV2"></div>
                </div>
                <div class="sa-flow sa-day-run" id="dayLoadV2" style="display:none;">
                    <div class="sa-day-run__card">
                        <div class="sa-day-run__title"></div>
                        <div class="sa-day-run__bar"><i></i></div>
                        <div class="sa-day-run__meta"><span class="pct">0%</span><span class="what"></span></div>
                    </div>
                </div>
                <div class="sa-flow sa-step" id="dayStepV2" style="display:none;"></div>
            </section>
        </div>
    `);
	// State
	const P = frappe.pages["sales-allocation-v2"];
	P.current_sales_orders = [];
	P.selected_order = null;
	P.order_items = [];
	P.allocations = [];
	P.selected_location = null;
	P.location_config = null;
	P.selected_farms = [];
	// When checked, hides the cut-stage mismatch filter entirely so buckets
	// that don't match the order line's spec cut stage become selectable.
	P.bypass_cut_stage = false;
	// Which order line is showing its bucket table in the right pane
	P.selected_item = null;
	P._preserve_item = null;
	// Mix group filter ("" = show all, otherwise custom_mix_group number as string)
	P.selected_mix_group = "";
	// Per-item batch filter state: keyed by sales_order_item
	// Each entry: { cut_stage_min: '', cut_stage_max: '' }
	P.item_filters = {};
	P.item_teams = {};
	// Real Packing Teams (not a hardcoded list) -- populated by
	// _populate_filter_options, which already bundles this in with the
	// length/item-group option lists. See get_order_filter_options.
	P.available_teams = P.available_teams || [""];
	const tomorrow = frappe.datetime.add_days(frappe.datetime.get_today(), 1);
	P.filters = {
		search: "",
		priority: "",
		box_type: "",
		item_group: "",
		delivery_start: tomorrow,
		delivery_end: tomorrow,
	};
	P.make_date_filters();
	// The browser's date is only a first guess; the server's (EAT) decides.
	P.reset_to_tomorrow(false);
	P.render_allocation_grid();
	P.load_location_config();
	P._populate_filter_options();
	$("#autoAllocateDayV2").on("click", () => P.auto_allocate_day());
	$("#shortTabV2").on("click", () => P.toggle_shortages());
	$("#confirmDayV2").on("click", () => P.confirm_day_allocation());
	$("#orderSearchInputV2").on("input", function () {
		P.filters.search = $(this).val();
		P.apply_filters();
	});
	$("#priorityFilterV2").on("change", function () {
		P.filters.priority = $(this).val();
		P.apply_filters();
	});
	$("#boxTypeFilterV2").on("change", function () {
		P.filters.box_type = $(this).val();
		P.apply_filters();
	});
	$("#itemGroupFilterV2").on("change", function () {
		P.filters.item_group = $(this).val();
		P.apply_filters();
	});
	// which orders show by allocation: not allocated and partly allocated to start with
	P.show_stages = P._default_stages();
	// Full stands alone: picking it hides the open orders, picking Not allocated or Partly hides the full ones
	$("#allocStagesV2").on("click", "button", function () {
		const st = +$(this).data("stage");
		const on = !P.show_stages[st];
		if (st === 2 && on) P.show_stages = { 0: false, 1: false, 2: true };
		else {
			P.show_stages[st] = on;
			if (on) P.show_stages[2] = false;
		}
		if (!Object.values(P.show_stages).some(Boolean)) P.show_stages = P._default_stages();
		P.apply_filters();
	});
	$("#clearFiltersV2").on("click", function () {
		P.show_stages = P._default_stages();
		$("#orderSearchInputV2, #priorityFilterV2, #boxTypeFilterV2, #itemGroupFilterV2").val("");
		P.filters = {
			search: "",
			priority: "",
			box_type: "",
			item_group: "",
			delivery_start: tomorrow,
			delivery_end: tomorrow,
		};
		P.set_date_filter("delivery_start", tomorrow);
		P.set_date_filter("delivery_end", tomorrow);
		if (P.selected_location) P.load_sales_orders();
	});
};

// Frappe Date controls for the delivery window, built the way page.add_field
// builds toolbar filters (input only, no label). Each one writes its own key
// in P.filters and reloads the list -- but only when the value really
// changed. set_date_filter uses set_input, which neither validates nor fires
// change, so setting a date from code never triggers a reload of its own.
frappe.pages["sales-allocation-v2"].make_date_filters = function () {
	const P = frappe.pages["sales-allocation-v2"];
	P.date_controls = {};
	$(".ufd-sa2 .sa-date[data-filter]").each(function () {
		const key = $(this).attr("data-filter");
		const control = frappe.ui.form.make_control({
			parent: $(this),
			df: {
				fieldtype: "Date",
				fieldname: key,
				label: "",
				placeholder: __("Any date"),
				input_class: "input-xs",
				change() {
					const value = control.get_value() || "";
					if (value === (P.filters[key] || "")) return;
					P.filters[key] = value;
					if (P.selected_location) P.load_sales_orders();
				},
			},
			only_input: true,
		});
		control.refresh();
		control.set_input(P.filters[key] || "");
		P.date_controls[key] = control;
	});
};
frappe.pages["sales-allocation-v2"].set_date_filter = function (key, value) {
	const P = frappe.pages["sales-allocation-v2"];
	P.filters[key] = value || "";
	const control = P.date_controls && P.date_controls[key];
	if (control) control.set_input(P.filters[key]);
};

// Put the delivery window on tomorrow by the server's clock (EAT), never the
// browser's. `refresh` (the Refresh button) always reloads and also leaves a
// deep-linked order's day; on page load it only corrects the first guess and
// reloads when the server's day differs.
frappe.pages["sales-allocation-v2"].reset_to_tomorrow = function (refresh) {
	const P = frappe.pages["sales-allocation-v2"];
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.default_delivery_date",
		callback: function (r) {
			const tomorrow = r.message;
			if (!tomorrow || (!refresh && P._deeplinked)) return;
			const changed =
				P.filters.delivery_start !== tomorrow || P.filters.delivery_end !== tomorrow;
			P._deeplinked = false;
			P.set_date_filter("delivery_start", tomorrow);
			P.set_date_filter("delivery_end", tomorrow);
			if (P.selected_location && (refresh || changed)) P.load_sales_orders();
		},
		error: function () {
			if (refresh && P.selected_location) P.load_sales_orders();
		},
	});
};

// jQuery scope for everything inside the detail pane (toolbar + lines + workspace).
frappe.pages["sales-allocation-v2"]._scope = function () {
	return $("#salesAllocationDetailV2");
};

// ─── LOCATION CONFIG ───
frappe.pages["sales-allocation-v2"].load_location_config = function () {
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_location_config",
		callback: function (r) {
			if (r.message && r.message.locations) {
				frappe.pages["sales-allocation-v2"].location_config = r.message;
				frappe.pages["sales-allocation-v2"].render_location_selector();
			} else {
				$("#locationButtonsV2").html(
					'<span style="padding:5px 12px;font-size:11px;color:var(--bad);">No locations configured</span>'
				);
			}
		},
		error: function () {
			$("#locationButtonsV2").html(
				'<span style="padding:5px 12px;font-size:11px;color:var(--bad);">Failed to load</span>'
			);
		},
	});
};
frappe.pages["sales-allocation-v2"].render_location_selector = function () {
	const config = frappe.pages["sales-allocation-v2"].location_config;
	if (!config || !config.locations) return;
	const html = config.locations
		.map((loc) => {
			const is_active =
				frappe.pages["sales-allocation-v2"].selected_location === loc.name ? "active" : "";
			return `<button class="location-btn ${is_active}" data-location="${loc.name}" title="${loc.farms.length} farm(s)">${loc.name}</button>`;
		})
		.join("");
	$("#locationButtonsV2").html(html);
	$(".location-btn").on("click", function () {
		// the location stays put while Allocate all or Release runs there
		if (frappe.pages["sales-allocation-v2"]._day_busy) return;
		frappe.pages["sales-allocation-v2"].select_location($(this).data("location"));
	});
	// Open on the location used last.
	if (!frappe.pages["sales-allocation-v2"].selected_location) {
		let last = null;
		try {
			last = localStorage.getItem("sales_allocation_v2_location");
		} catch (e) {
			last = null;
		}
		if (last && config.locations.some((l) => l.name === last)) {
			frappe.pages["sales-allocation-v2"].select_location(last);
		}
	}
};
frappe.pages["sales-allocation-v2"].select_location = function (location) {
	const P = frappe.pages["sales-allocation-v2"];
	P.selected_location = location;
	try {
		localStorage.setItem("sales_allocation_v2_location", location);
	} catch (e) {
		// private window: nothing to remember
	}
	const loc_config = P.location_config.locations.find((l) => l.name === location);
	P.selected_farms = loc_config ? [...loc_config.farms] : [];
	$(".location-btn").removeClass("active");
	$(`.location-btn[data-location="${location}"]`).addClass("active");
	P.selected_order = null;
	P.allocations = [];
	P.order_items = [];
	P.selected_item = null;
	P.render_allocation_grid();
	P.load_sales_orders();
	P._resume_day_progress();
};
// An Allocate all or Release still running here (the page was refreshed or opened meanwhile): show its bar
// again and follow it to the end.
frappe.pages["sales-allocation-v2"]._resume_day_progress = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const location = P.selected_location;
	if (!location) return;
	clearTimeout(P._day_poll);
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.get_day_state",
		args: { location, since: P._day_run || "" },
		callback: (r) => {
			const s = r.message;
			if (P.selected_location !== location) return;
			// finished: the run's own answer, so the page never waits on a realtime message
			if (s && s.done) {
				P._day_run = "";
				P._day_done(s);
				return;
			}
			if (!s) {
				// a run this page was following (opened or refreshed while it ran) has ended without its
				// answer reaching here: the bar is taken down, and after Allocate all its steps follow
				if (P._day_running && !P._day_finished) {
					const phase = P._resumed_phase;
					P._resumed_phase = "";
					P._day_running = false;
					P._day_finished = true;
					if (phase === "propose") {
						P._load_proposal();
						P._steps_after_allocate();
					} else {
						P._day_progress(1, 1, "", true);
						P._load_proposal();
						P._after_day_change();
					}
				} else if (!P._day_running) $("#dayProgV2").hide();
				return;
			}
			P._resumed_phase = s.phase || "propose";
			P._listen_day_allocation();
			if (s.planned) P._show_planned(s.planned);
			P._day_finished = false;
			P._day_running = true;
			P._day_progress(
				s.progress || 0,
				s.total || 1,
				s.queued ? (s.ahead ? __("{0} ahead", [s.ahead]) : "") : s.order || "",
				undefined,
				s.queued ? "queued" : s.stage || (s.phase === "confirm" ? "release" : "allocate")
			);
			// asked again until it is done: realtime, when the page has it, only makes it quicker
			P._day_poll = setTimeout(() => P._resume_day_progress(), 1500);
		},
	});
};
// ─── SALES ORDERS ───
frappe.pages["sales-allocation-v2"].load_sales_orders = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.short_mode && P._shortages_key !== P._short_key()) $("#shortTabV2 .cnt").hide();
	// read with the orders (in the background when the tab is closed), so it opens with them
	P._load_shortages();
	if (P._proposal_key !== P._short_key()) P._load_proposal();
	if (!P.selected_location) {
		$("#salesOrderListV2").html(
			'<div class="empty-state"><p>Select a location to begin.</p></div>'
		);
		return;
	}
	$("#salesOrderListV2").html('<div class="loading-state">Loading orders…</div>');
	// Only the latest request may paint the list (and consume a deep-link):
	// a load still in flight from the page's first visit or a location click
	// used to land after the deep-link's own load, find no such order in its
	// old date window, and report it missing.
	const request_id = (P._orders_request_id = (P._orders_request_id || 0) + 1);
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_pending_sales_orders",
		args: {
			delivery_start: P.filters.delivery_start || null,
			delivery_end: P.filters.delivery_end || null,
			sales_order: P._pending_select_order || null,
			location: P.selected_location || null,
		},
		callback: function (r) {
			if (request_id !== P._orders_request_id) return;
			if (r.message && r.message.length) {
				P.current_sales_orders = r.message;
				P._fill_item_groups();
				P.apply_filters();
			} else {
				P.current_sales_orders = [];
				P._fill_item_groups();
				$("#resultsCountV2").text("0 orders");
				$("#salesOrderListV2").html(
					'<div class="empty-state"><p>No pending orders for these dates.</p></div>'
				);
			}
			if (P._pending_select_order) {
				const target = P._pending_select_order;
				P._pending_select_order = null;
				if ((P.current_sales_orders || []).some((o) => o.name === target)) {
					P.select_order(target, { force: 1 });
				} else {
					P._explain_missing_order(target);
				}
			}
		},
	});
};
// The deep-linked order is requested regardless of the date window, so if it
// is still missing the order itself is not allocatable -- say why.
frappe.pages["sales-allocation-v2"]._explain_missing_order = function (sales_order) {
	frappe
		.call("frappe.client.get_value", {
			doctype: "Sales Order",
			filters: { name: sales_order },
			fieldname: ["docstatus", "status"],
		})
		.then((r) => {
			const so = r && r.message;
			let reason;
			if (!so || so.docstatus === undefined) {
				reason = __(
					"{0} no longer exists (it may have been deleted), or you cannot read it.",
					[sales_order]
				);
			} else if (so.docstatus === 0) {
				reason = __("{0} is still a draft — submit it to allocate.", [sales_order]);
			} else if (so.docstatus === 2) {
				reason = __("{0} is cancelled.", [sales_order]);
			} else {
				reason = __("{0} is {1}, so it is no longer open for allocation.", [
					sales_order,
					__(so.status),
				]);
			}
			frappe.show_alert({ message: reason, indicator: "orange" }, 8);
		});
};
// Fill the Length and Item-group dropdowns from the masters (all stem lengths, all
// ordered item groups) — so every value is selectable even when no loaded order uses
// it. Fetched once on page load; any current selection is kept if still valid.
// The item groups the orders on the list carry: the filter offers only those, so it changes with the
// location and the delivery dates. A group no longer there is let go.
frappe.pages["sales-allocation-v2"]._fill_item_groups = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const esc = frappe.utils.escape_html;
	const groups = [
		...new Set(
			(P.current_sales_orders || []).flatMap((o) =>
				(o.item_groups || "")
					.split(", ")
					.map((g) => g.trim())
					.filter(Boolean)
			)
		),
	].sort((a, b) => a.localeCompare(b));
	if (P.filters.item_group && !groups.includes(P.filters.item_group)) P.filters.item_group = "";
	$("#itemGroupFilterV2")
		.html(
			`<option value="">${__("All item groups")}</option>` +
				groups.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("")
		)
		.val(P.filters.item_group || "");
};
frappe.pages["sales-allocation-v2"]._populate_filter_options = function () {
	const P = frappe.pages["sales-allocation-v2"];
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_order_filter_options",
		callback: function (r) {
			const o = (r && r.message) || {};
			// Real Packing Teams, bundled into this same response (see
			// get_order_filter_options) rather than a separate async fetch --
			// this call already gates real interaction with the page, so by
			// the time an operator can select an order, this has landed.
			P.available_teams = [""].concat(o.teams || []);
			P.team_locations = o.team_locations || {};
			P.render_allocation_grid();
		},
	});
};
frappe.pages["sales-allocation-v2"].apply_filters = function () {
	const P = frappe.pages["sales-allocation-v2"];
	let orders = P.current_sales_orders;
	if (P.filters.search) {
		const term = P.filters.search.toLowerCase();
		orders = orders.filter(
			(o) =>
				(o.name || "").toLowerCase().includes(term) ||
				(o.customer || "").toLowerCase().includes(term) ||
				(o.custom_order_name || "").toLowerCase().includes(term) ||
				(o.item_codes || "").toLowerCase().includes(term)
		);
	}
	if (P.filters.priority)
		orders = orders.filter((o) => (o.custom_priority || "Low") === P.filters.priority);
	if (P.filters.box_type === "mixed") orders = orders.filter((o) => (o.has_mixed || 0) === 1);
	else if (P.filters.box_type === "straight")
		orders = orders.filter((o) => (o.has_straight || 0) === 1);
	if (P.filters.item_group)
		orders = orders.filter(
			(o) => (o.item_groups || "").split(", ").indexOf(P.filters.item_group) !== -1
		);
	// not allocated first, then short, then fully allocated -- by the cover the card shows (boxes
	// allocated and proposed); the delivery order within each, as the list comes
	const stage = (o) => {
		const pct = P._cover_pct(o, P._order_list_pct(o));
		return pct <= 0 ? 0 : pct < 100 ? 1 : 2;
	};
	// the allocation toggles: each shows how many of the orders above it holds
	const shown = P.show_stages || P._default_stages();
	const counts = [0, 0, 0];
	orders.forEach((o) => counts[stage(o)]++);
	$("#allocStagesV2 button").each(function () {
		const st = +$(this).data("stage");
		$(this)
			.toggleClass("is-on", !!shown[st])
			.attr("aria-pressed", !!shown[st])
			.find(".n")
			.text(counts[st]);
	});
	orders = orders.filter((o) => shown[stage(o)]);
	orders = orders
		.map((o, i) => [o, i])
		.sort((a, b) => stage(a[0]) - stage(b[0]) || a[1] - b[1])
		.map((x) => x[0]);
	const total = (P.current_sales_orders || []).length;
	$("#resultsCountV2").text(
		orders.length === total ? `${total} orders` : `${orders.length} of ${total}`
	);
	P.render_orders(orders);
};
frappe.pages["sales-allocation-v2"]._default_stages = function () {
	return { 0: true, 1: true, 2: false };
};
frappe.pages["sales-allocation-v2"].render_orders = function (orders) {
	const $list = $("#salesOrderListV2");
	const selected = frappe.pages["sales-allocation-v2"].selected_order;
	if (!orders.length) {
		$list.html('<div class="empty-state"><p>No orders match these filters.</p></div>');
		return;
	}
	const html = orders
		.map((order) => {
			const priority = order.custom_priority || "Low";
			const pri_class = "pri-" + priority.toLowerCase();
			const is_selected = selected === order.name ? "selected" : "";
			const pct = frappe.pages["sales-allocation-v2"]._order_list_pct(order);
			// Full boxes: allocated, proposed by Allocate all, and this session's for the open order.
			const full = frappe.pages["sales-allocation-v2"]._full_boxes(order);
			const cover = frappe.pages["sales-allocation-v2"]._cover_pct(order, pct);
			const bar_color = frappe.pages["sales-allocation-v2"]._cover_color(cover);
			const title = order.custom_order_name || order.name;
			const plan =
				order.planning_sequence || order.planning_team
					? `<div class="soc-plan">${
							order.planning_sequence
								? `<span class="soc-seq">#${
										order.planning_position || order.planning_sequence
								  }</span>`
								: ""
					  }${
							order.planning_team
								? `<span class="soc-team">${order.planning_team}</span>`
								: ""
					  }</div>`
					: "";
			return `
            <div class="sales-order-card ${is_selected}" data-order="${order.name}">
                <div class="soc-top">
                    <span class="soc-id" title="${title}">${title}</span>
                    <span class="soc-pri ${pri_class}">${priority}</span>
                </div>
                <div class="soc-line" title="${order.customer || ""}">${
				order.customer || "—"
			}</div>${plan}
                <div class="soc-line muted" title="${order.item_codes || ""}">${
				order.item_codes || "—"
			}</div>
                ${
					order.boxes_required
						? `<div class="soc-boxes"><span>${__("{0}/{1} boxes full", [
								full,
								order.boxes_required,
						  ])}</span>${
								order.boxes_required > full
									? `<span class="soc-boxleft">${__("{0} to allocate", [
											order.boxes_required - full,
									  ])}</span>`
									: ""
						  }</div>`
						: ""
				}
                <div class="soc-foot">
                    <span>${order.name} · ${frappe.datetime.str_to_user(
				order.delivery_date
			)}</span>
                    <span class="soc-pct" style="font-weight:600;color:${bar_color};">${cover}%</span>
                </div>
                <div class="soc-bar"><div class="soc-bar-done" style="width:${cover}%;height:100%;background:${bar_color};"></div></div>
            </div>`;
		})
		.join("");
	$list.html(html);
	$list.find(".sales-order-card").on("click", function () {
		if (frappe.pages["sales-allocation-v2"]._day_busy) return;
		frappe.pages["sales-allocation-v2"].select_order($(this).data("order"));
	});
};
// ─── SELECT ORDER & LOAD ITEMS ───
// opts.keep  — keep the currently selected order line after a reload
// opts.force — skip the "unconfirmed session allocations" guard
frappe.pages["sales-allocation-v2"].select_order = function (order_name, opts) {
	const P = frappe.pages["sales-allocation-v2"];
	opts = opts || {};
	if (!P.selected_location) {
		frappe.msgprint(__("Please select a location first"));
		return;
	}
	const switching = P.selected_order && P.selected_order !== order_name;
	// Picks not confirmed yet are held for their order, not discarded, and come back when it is reopened.
	if (switching) P._hold_session();
	P._preserve_item = opts.keep ? P.selected_item : null;
	if (P._deeplink_item) {
		P._preserve_item = P._deeplink_item;
		P._deeplink_item = null;
	}
	P.allocations = [];
	P._restore = P._proposed_order(order_name) || null;
	P._items_for = null;
	P.selected_order = order_name;
	P.selected_item = null;
	// Reset filters
	P.item_filters = {};
	P.selected_mix_group = "";
	P._auto_mix = true;
	P.apply_filters();
	P._load_available_filters_and_open_dialog();
};
frappe.pages["sales-allocation-v2"].close_detail = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const finish = () => {
		P.allocations = [];
		P.selected_order = null;
		P.order_items = [];
		P.selected_item = null;
		P.item_teams = {};
		P.apply_filters();
		P.render_allocation_grid();
	};
	P._hold_session();
	finish();
};
// ─── Allocated, waiting confirmation: held on the server as a Pending Allocation per order ───
// Save the open order's picks (the buckets are held for it); none cancels its Pending Allocation.
frappe.pages["sales-allocation-v2"]._hold_session = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const so = P.selected_order;
	if (!so || !P.selected_location) return Promise.resolve();
	const allocations = [...(P.allocations || [])];
	const existing = P._proposed_order(so);
	if (!allocations.length && !existing) return Promise.resolve();
	const teams = { ...(P.item_teams || {}) };
	const boxes = P._session_boxes();
	// the card keeps its progress at once; the server answer refreshes it
	const orders = ((P.proposal && P.proposal.orders) || []).filter((o) => o.sales_order !== so);
	if (allocations.length) {
		const lines = {};
		allocations.forEach(
			(a) =>
				(lines[a.sales_order_item] =
					(lines[a.sales_order_item] || 0) + (parseFloat(a.qty) || 0))
		);
		const o = (P.current_sales_orders || []).find((r) => r.name === so) || {};
		orders.push({
			sales_order: so,
			order_name: o.custom_order_name || (existing && existing.order_name) || "",
			order_boxes: (existing && existing.order_boxes) || o.boxes_required || 0,
			full_after: (existing && existing.full_after) || boxes,
			team_names: [...new Set(Object.values(teams).filter(Boolean))],
			customer: o.customer || "",
			delivery_date: o.delivery_date || "",
			sequence: o.planning_sequence || 0,
			source: (existing && existing.source) || "Picked",
			boxes,
			stems: Object.values(lines).reduce((t, v) => t + v, 0),
			lines,
			allocations,
			teams,
		});
	}
	P._set_proposal({ orders });
	return frappe
		.call({
			method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.save_pending",
			args: {
				location: P.selected_location,
				sales_order: so,
				allocations: JSON.stringify(allocations),
				teams: JSON.stringify(teams),
				boxes,
			},
		})
		.then(() => P._load_proposal());
};
// This order's own picks waiting confirmation come back into the session (others' are already off the buckets).
frappe.pages["sales-allocation-v2"]._apply_held = function () {
	const P = frappe.pages["sales-allocation-v2"];
	// the latest allocation waiting release for the order (Allocate all or Auto-allocate order may have
	// saved it after the order was clicked)
	const latest = P._proposed_order(P.selected_order);
	if (latest) P._restore = latest;
	if (!P._restore) return;
	const valid = new Set((P.order_items || []).map((i) => i.sales_order_item));
	P.allocations = (P._restore.allocations || []).filter((a) => valid.has(a.sales_order_item));
	P.allocations.forEach((a) =>
		P._take_from_bucket(a.item_code, a.bucket_id, a.stem_length || "", parseFloat(a.qty) || 0)
	);
};
// ─── Load items and render the detail pane ───
frappe.pages["sales-allocation-v2"]._load_available_filters_and_open_dialog = function () {
	const P = frappe.pages["sales-allocation-v2"];
	// Filters are per-item and derived from batch data client-side
	P._fetch_items_and_open_dialog();
};
frappe.pages["sales-allocation-v2"]._fetch_items_and_open_dialog = function () {
	const P = frappe.pages["sales-allocation-v2"];
	$("#allocationWorkspaceV2").html('<div class="loading-state">Loading buckets…</div>');
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_sales_order_items_with_buckets",
		args: {
			sales_order: P.selected_order,
			location: P.selected_location,
			selected_farms: JSON.stringify(P.selected_farms),
			bypass_cut_stage: P.bypass_cut_stage ? 1 : 0,
		},
		callback: function (r) {
			if (r.message && r.message.length) {
				P.order_items = r.message.map((item) => ({
					...item,
					batches: (item.batches || []).map((b) => ({
						...b,
						original_available_qty: b.available_qty || 0,
					})),
				}));
				P._apply_held();
				P._items_for = P.selected_order;
				P.show_allocation_panel();
			} else {
				P.order_items = [];
				P.selected_item = null;
				P.render_allocation_grid();
				frappe.msgprint(__("No items with confirmed stems for this location."));
			}
		},
		error: function () {
			P.order_items = [];
			P.render_allocation_grid();
			frappe.msgprint(__("Failed to load items for allocation."));
		},
	});
};
// ─── ALLOCATION PANEL ───
frappe.pages["sales-allocation-v2"].show_allocation_panel = function () {
	const P = frappe.pages["sales-allocation-v2"];
	// Per-line team selections (sales_order_item -> team): each line starts from the
	// team already on its Order Pick List, so an allocated order shows its team.
	// A line not on a pick list yet takes the team Allocation Planning confirmed it for.
	P.item_teams = {};
	(P.order_items || []).forEach((it) => {
		const team = it.opl_team || it.planning_team;
		if (team) P.item_teams[it.sales_order_item] = team;
	});
	if (P._restore) {
		Object.assign(P.item_teams, P._restore.item_teams || {});
		P._restore = null;
	}
	P.selected_item = P._preserve_item || null;
	P._preserve_item = null;
	P.render_allocation_grid();
};
// Back-compat alias in case anything else calls the old name.
frappe.pages["sales-allocation-v2"].show_allocation_dialog =
	frappe.pages["sales-allocation-v2"].show_allocation_panel;

// ─── FARM FILTER ───
frappe.pages["sales-allocation-v2"]._render_farm_filter = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.location_config) return "";
	const loc = P.location_config.locations.find((l) => l.name === P.selected_location);
	if (!loc || loc.farms.length <= 1) return "";
	const checks = loc.farm_details
		.map((f) => {
			const checked = P.selected_farms.includes(f.farm);
			const badge_class = f.sales_shelf ? "sales" : "remote";
			const badge_text = f.sales_shelf ? "Sales" : "Remote";
			return `
            <div class="farm-checkbox-item ${checked ? "checked" : ""}" data-farm="${f.farm}">
                <span>${f.farm}</span>
                <span class="farm-badge ${badge_class}">${badge_text}</span>
            </div>`;
		})
		.join("");
	return `
        <div class="farm-filter-bar">
            <label class="title" title="Changing farms reloads bucket data and clears session allocations">Farms</label>
            <div class="farm-checkbox-group" id="farmFilterGroupV2">${checks}</div>
        </div>`;
};
frappe.pages["sales-allocation-v2"]._bind_farm_filter = function () {
	const P = frappe.pages["sales-allocation-v2"];
	P._scope()
		.find(".farm-checkbox-item")
		.on("click", function () {
			const farm = $(this).data("farm");
			const idx = P.selected_farms.indexOf(farm);
			if (idx > -1) {
				if (P.selected_farms.length === 1) {
					frappe.msgprint(__("At least one farm must be selected."));
					return;
				}
				P.selected_farms.splice(idx, 1);
			} else {
				P.selected_farms.push(farm);
			}
			P.clear_allocations(true);
			P.item_filters = {};
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_sales_order_items_with_buckets",
				args: {
					sales_order: P.selected_order,
					location: P.selected_location,
					selected_farms: JSON.stringify(P.selected_farms),
					bypass_cut_stage: P.bypass_cut_stage ? 1 : 0,
				},
				freeze: false,
				callback: function (r) {
					if (r.message) {
						P.order_items = r.message.map((item) => ({
							...item,
							batches: (item.batches || []).map((b) => ({
								...b,
								original_available_qty: b.available_qty || 0,
							})),
						}));
					}
					P.render_allocation_grid();
				},
			});
		});
};
// ─── CUT STAGE BYPASS ───
frappe.pages["sales-allocation-v2"]._render_cutstage_bypass = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const checked = P.bypass_cut_stage ? "checked" : "";
	return `
        <div class="farm-filter-bar">
            <label class="title" title="Buckets hidden for not matching the order's cut stage become available again">
                <input type="checkbox" id="bypassCutStageCheckboxV2" ${checked}> Bypass Cut Stage
            </label>
        </div>`;
};
frappe.pages["sales-allocation-v2"]._bind_cutstage_bypass = function () {
	const P = frappe.pages["sales-allocation-v2"];
	P._scope()
		.find("#bypassCutStageCheckboxV2")
		.on("change", function () {
			P.bypass_cut_stage = $(this).is(":checked");
			P.clear_allocations(true);
			P.item_filters = {};
			P._bucket_diag_cache = {};
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_sales_order_items_with_buckets",
				args: {
					sales_order: P.selected_order,
					location: P.selected_location,
					selected_farms: JSON.stringify(P.selected_farms),
					bypass_cut_stage: P.bypass_cut_stage ? 1 : 0,
				},
				freeze: false,
				callback: function (r) {
					if (r.message) {
						P.order_items = r.message.map((item) => ({
							...item,
							batches: (item.batches || []).map((b) => ({
								...b,
								original_available_qty: b.available_qty || 0,
							})),
						}));
					}
					P.render_allocation_grid();
				},
			});
		});
};
// ─── MIX GROUP FILTER ───
frappe.pages["sales-allocation-v2"]._render_mix_filter = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const items = P.order_items || [];
	// Collect unique mix groups present in this order
	const seen = new Map();
	items.forEach((it) => {
		if (!it.custom_mixed_box) return;
		const key = String(it.custom_mix_group || "");
		if (!key) return;
		if (!seen.has(key)) {
			seen.set(key, it.custom_mix_name || `Group ${key}`);
		}
	});
	if (seen.size === 0) return "";
	const options = [...seen.entries()]
		.map(([key, label]) => {
			const selected = String(P.selected_mix_group || "") === key ? "selected" : "";
			const safe_label = frappe.utils.escape_html(label);
			return `<option value="${key}" ${selected}>${safe_label}</option>`;
		})
		.join("");
	const all_selected = !P.selected_mix_group ? "selected" : "";
	return `
        <div class="mix-filter-bar">
            <label class="title">Mix</label>
            <select id="mixGroupFilterV2">
                <option value="" ${all_selected}>All mixes and straight boxes</option>
                ${options}
            </select>
            <span class="mix-count">${seen.size} mix${seen.size === 1 ? "" : "es"}</span>
        </div>`;
};
frappe.pages["sales-allocation-v2"]._bind_mix_filter = function () {
	const P = frappe.pages["sales-allocation-v2"];
	P._scope()
		.find("#mixGroupFilterV2")
		.on("change", function () {
			P.selected_mix_group = $(this).val() || "";
			P.selected_item = null;
			P.render_allocation_grid();
		});
};
// ─── PER-ITEM CUT STAGE FILTER ───
// Fixed range steps from 1.5 to 4, always visible
frappe.pages["sales-allocation-v2"].CUT_STAGE_STEPS = [1.5, 2, 2.5, 3, 3.5, 4];
frappe.pages["sales-allocation-v2"]._get_item_filter = function (so_item) {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.item_filters[so_item]) {
		P.item_filters[so_item] = { cut_stage_min: "", cut_stage_max: "" };
	}
	return P.item_filters[so_item];
};
frappe.pages["sales-allocation-v2"]._render_per_item_filter = function (item) {
	const P = frappe.pages["sales-allocation-v2"];
	const so_item = item.sales_order_item;
	const batches = item.batches || [];
	// A line populated from a Specification is locked to that spec's cut
	// stage — the server already only sends back matching buckets, so there's
	// nothing to pick; show it as a fact, not a control.
	if (item.spec_cut_stage) {
		return `
            <div class="item-batch-filter spec-driven" data-so-item="${so_item}">
                <span class="ibf-label">Cut stage</span>
                <span class="ibf-spec-pill">${item.spec_cut_stage} <small>from spec</small></span>
                <span class="ibf-count">${batches.length} bucket${
			batches.length === 1 ? "" : "s"
		}</span>
            </div>`;
	}
	// no cut stage range on the line: every bucket the line can take is listed
	return "";
};
frappe.pages["sales-allocation-v2"]._bind_per_item_filters = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const $w = P._scope();
	// Cut stage range dropdowns
	$w.find("select.ibf-cs-select").on("change", function () {
		const so_item = $(this).data("so-item");
		const field = $(this).data("filter");
		const val = $(this).val();
		const f = P._get_item_filter(so_item);
		f[field] = val;
		P._apply_item_batch_visibility(so_item);
	});
	// Per-item clear button
	$w.find(".ibf-clear[data-so-item]").on("click", function () {
		const so_item = $(this).data("so-item");
		if (!so_item) return;
		P.item_filters[so_item] = { cut_stage_min: "", cut_stage_max: "" };
		const $bar = $w.find(`.item-batch-filter[data-so-item="${so_item}"]`);
		$bar.find("select.ibf-cs-select").val("");
		P._apply_item_batch_visibility(so_item);
	});
	// Substitute variety buttons
	$w.find(".substitute-btn").on("click", function () {
		const so_item = $(this).data("so-item");
		P._show_substitute_dialog(so_item);
	});
};
// ─── BUCKET VISIBILITY DIAGNOSTICS ───
// "There are buckets on the coldstore but they're not showing up here" — this
// answers *why*, instead of leaving the user staring at an empty table or a
// grid that looks short a few buckets. Cached per line so re-toggling doesn't
// re-hit the server; a farm-filter change busts the whole cache.
frappe.pages["sales-allocation-v2"]._bucket_diag_cache = {};
frappe.pages["sales-allocation-v2"]._bucket_diag_open = {};
frappe.pages["sales-allocation-v2"]._bind_bucket_diagnostics = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const $w = P._scope();
	$w.find(".bd-trigger[data-so-item]")
		.off("click")
		.on("click", function () {
			const so_item = $(this).data("so-item");
			const item = (P.order_items || []).find((i) => i.sales_order_item === so_item);
			if (!item) return;
			const $panel = $w.find(`#bucket-diag-${so_item}`);
			const is_open = $panel.is(":visible");
			if (is_open) {
				$panel.slideUp(120);
				P._bucket_diag_open[so_item] = false;
			} else {
				P._bucket_diag_open[so_item] = true;
				P._load_bucket_diagnostics(item, false);
			}
		});
};
frappe.pages["sales-allocation-v2"]._load_bucket_diagnostics = function (item, forceOpen) {
	const P = frappe.pages["sales-allocation-v2"];
	const so_item = item.sales_order_item;
	const $panel = P._scope().find(`#bucket-diag-${so_item}`);
	if (!$panel.length) return;
	const cache_key = [
		so_item,
		P.selected_location,
		(P.selected_farms || []).slice().sort().join(","),
		P.bypass_cut_stage ? 1 : 0,
	].join("|");
	const cached = P._bucket_diag_cache[cache_key];
	if (cached) {
		P._render_bucket_diag_strip(item, cached);
		$panel.show();
		return;
	}
	$panel.html('<div class="bd-loading">Checking shelf stock…</div>').show();
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_bucket_visibility_diagnostics",
		args: {
			sales_order_item: so_item,
			location: P.selected_location,
			selected_farms: JSON.stringify(P.selected_farms || []),
			bypass_cut_stage: P.bypass_cut_stage ? 1 : 0,
		},
		callback: function (r) {
			if (!r.message || !r.message.success) {
				$panel.html('<div class="bd-loading">Couldn\'t load bucket diagnostics.</div>');
				return;
			}
			P._bucket_diag_cache[cache_key] = r.message;
			// Line may have switched while the call was in flight.
			if (P.selected_item !== so_item && !forceOpen) return;
			P._render_bucket_diag_strip(item, r.message);
		},
	});
};
frappe.pages["sales-allocation-v2"]._render_bucket_diag_strip = function (item, data) {
	const P = frappe.pages["sales-allocation-v2"];
	const so_item = item.sales_order_item;
	const $panel = P._scope().find(`#bucket-diag-${so_item}`);
	if (!$panel.length) return;
	const $trigger = P._scope().find(`.bd-trigger[data-so-item="${so_item}"]`);
	const reasons = data.reasons || [];
	const blocking = reasons.filter((r) => r.code !== "eligible" && r.code !== "fully_allocated");
	$trigger.toggleClass("has-issues", blocking.length > 0);
	if (!data.total_buckets) {
		$panel.html(
			`<div class="bd-strip"><div class="bd-summary">No buckets on the shelf at all for this variety at ${
				data.required_length || "this length"
			} or otherwise, on the farms currently selected.</div></div>`
		);
		return;
	}
	const chips = reasons
		.map(
			(r) => `
        <span class="bd-chip reason-${r.code}" data-reason="${
				r.code
			}" data-so-item="${so_item}" title="Click to see the exact buckets">
            ${r.label}
            <span class="bd-count">${r.count}</span>
            <span class="bd-stems">(${r.stems.toLocaleString()} stems)</span>
            <span class="bd-hint">Details</span>
        </span>`
		)
		.join("");
	$panel.html(`
        <div class="bd-strip">
            <div class="bd-summary">${data.total_buckets} bucket${
		data.total_buckets === 1 ? "" : "s"
	} on the shelf for this variety — here's what's happening with each</div>
            ${chips}
        </div>`);
	$panel.find(".bd-chip").on("click", function () {
		const reason = $(this).data("reason");
		const group = (data.reasons || []).find((r) => r.code === reason);
		if (!group) return;
		P._show_bucket_diag_dialog(item, group);
	});
};
// A proper popup with the exact bucket list behind a reason — bucket, where it
// is, and the precise "why" sentence for that specific bucket (not just the
// group label), so the user can go check the physical bucket if something
// looks wrong.
frappe.pages["sales-allocation-v2"]._show_bucket_diag_dialog = function (item, group) {
	const rows = (group.buckets || [])
		.map(
			(b) => `
        <tr>
            <td><strong>${b.bucket_id || "-"}</strong></td>
            <td>${b.farm || "-"}</td>
            <td>${b.shelf || "-"}</td>
            <td>${b.stem_length || "?"}</td>
            <td>${b.stems || 0}</td>
            <td>${b.available_qty || 0}</td>
            <td>${b.age_days != null ? b.age_days + "d" : "-"}</td>
            <td>${b.cut_stage || "-"}</td>
            <td>${b.detail || ""}</td>
        </tr>`
		)
		.join("");
	const dialog = new frappe.ui.Dialog({
		title: `${group.label} — ${group.count} bucket${group.count === 1 ? "" : "s"} (${(
			group.stems || 0
		).toLocaleString()} stems)`,
		size: "extra-large",
		fields: [
			{
				fieldname: "buckets_html",
				fieldtype: "HTML",
				options: `
                <div class="bd-dialog-wrap" style="max-height:60vh;overflow:auto;">
                    <table class="allocation-grid-table" style="width:100%;">
                        <thead><tr>
                            <th>Bucket</th><th>Farm</th><th>Shelf</th><th>Length</th>
                            <th>Stems</th><th>Available</th><th>Age</th><th>Cut stage</th><th>Exact reason</th>
                        </tr></thead>
                        <tbody>${
							rows ||
							`<tr><td colspan="9" style="text-align:center;padding:20px;color:var(--ink-mute);">No buckets in this group.</td></tr>`
						}</tbody>
                    </table>
                </div>`,
			},
		],
	});
	dialog.show();
};
// ─── BATCH VISIBILITY ───
frappe.pages["sales-allocation-v2"]._batch_passes_filter = function (batch, item) {
	const P = frappe.pages["sales-allocation-v2"];
	const f = P._get_item_filter(item.sales_order_item);
	// Cut stage range — compares against shelf cut_stage only
	if (f.cut_stage_min !== "" || f.cut_stage_max !== "") {
		const raw = batch.cut_stage;
		// If the shelf has no cut_stage, hide this batch when filter is active
		if (raw == null || raw === "") return false;
		const cs = parseFloat(raw);
		if (isNaN(cs)) return false;
		if (f.cut_stage_min !== "" && cs < parseFloat(f.cut_stage_min)) return false;
		if (f.cut_stage_max !== "" && cs > parseFloat(f.cut_stage_max)) return false;
	}
	return true;
};
frappe.pages["sales-allocation-v2"]._has_active_filter = function (so_item) {
	const P = frappe.pages["sales-allocation-v2"];
	const f = P._get_item_filter(so_item);
	return f.cut_stage_min !== "" || f.cut_stage_max !== "";
};
frappe.pages["sales-allocation-v2"]._apply_item_batch_visibility = function (so_item) {
	const P = frappe.pages["sales-allocation-v2"];
	const $w = P._scope();
	const item = (P.order_items || []).find((i) => i.sales_order_item === so_item);
	if (!item) return;
	const has_filter = P._has_active_filter(so_item);
	const $table = $w.find(`table.allocation-grid-table[data-so-item="${so_item}"]`);
	let shown = 0,
		total = 0;
	$table.find("tbody tr").each(function () {
		const bucket_id = $(this).data("bucket-id");
		if (!bucket_id) return;
		const batch = (item.batches || []).find((b) => b.bucket_id === bucket_id);
		if (!batch) return;
		total += 1;
		if (!has_filter || P._batch_passes_filter(batch, item)) {
			$(this).show();
			shown += 1;
		} else {
			$(this).hide();
		}
	});
	$w.find(`.item-batch-filter[data-so-item="${so_item}"] .ibf-count`).text(
		`${shown} of ${total} buckets`
	);
};
// ─── SUBSTITUTE VARIETY ───
frappe.pages["sales-allocation-v2"]._show_substitute_dialog = function (so_item) {
	const P = frappe.pages["sales-allocation-v2"];
	const item = (P.order_items || []).find((i) => i.sales_order_item === so_item);
	if (!item) return;
	const original_color = item.color || "";
	const original_headsize = item.headsize || "";
	const sub_dialog = new frappe.ui.Dialog({
		title: `Substitute variety: ${item.item_name || item.item_code}`,
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "sub_content" }],
	});
	sub_dialog.$wrapper.addClass("ufd-sa2");
	sub_dialog.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal");
	// only the spec's options and the varieties of the same colour are offered (the server decides)
	const render_sub_content = function (varieties) {
		const current_html = `
            <div style="background:var(--surface);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:12px;color:var(--ink-3);">
                <strong>Current:</strong> ${item.item_name} (${item.item_code})
                ${original_color ? ` · Color: ${original_color}` : ""}
                ${original_headsize ? ` · Headsize: ${original_headsize}` : ""}
                · Qty: ${item.pending_stock_qty || 0} ${item.stock_uom || ""}
            </div>`;
		let list_html = "";
		const others = (varieties || []).filter((v) => v.item_code !== item.item_code);
		if (!others.length) {
			list_html = `<div class="empty-state"><p>${
				original_color
					? __("No spec option or {0} variety has stock here.", [original_color])
					: __("No spec option has stock here, and this variety has no colour set.")
			}</p></div>`;
		} else {
			list_html = varieties
				.map((v) => {
					const is_same = v.item_code === item.item_code;
					const rec_class = !is_same && (v.spec || v.same_color) ? "recommended" : "";
					const badges = [];
					if (is_same)
						badges.push(
							'<span class="sub-badge" style="background:rgba(10,10,10,0.06);color:var(--ink-mute);">Current</span>'
						);
					if (!is_same && v.spec)
						badges.push(
							`<span class="sub-badge" style="background:var(--signal-soft);color:var(--signal);">${
								v.primary ? __("Spec · primary") : __("Spec option")
							}</span>`
						);
					if (!is_same && v.same_color)
						badges.push(
							`<span class="sub-badge" style="background:var(--good-soft);color:var(--good);">${__(
								"Same colour"
							)}</span>`
						);
					const meta = [
						v.color ? `Color: ${v.color}` : "",
						v.headsize ? `Headsize: ${v.headsize}` : "",
						v.available_qty != null ? `Available: ${v.available_qty}` : "",
					]
						.filter(Boolean)
						.join(" · ");
					return `
                    <div class="sub-variety-card ${rec_class} ${
						is_same ? "" : "selectable"
					}" data-item-code="${v.item_code}" data-item-name="${
						v.item_name || v.item_code
					}" ${is_same ? 'style="opacity:0.55;cursor:default;"' : ""}>
                        <div>
                            <div class="sub-name">${v.item_name || v.item_code}</div>
                            <div class="sub-meta">${meta || "—"}</div>
                        </div>
                        <div>${badges.join(" ")}</div>
                    </div>`;
				})
				.join("");
		}
		sub_dialog.fields_dict.sub_content.$wrapper.html(current_html + list_html);
		// Bind selection
		sub_dialog.$wrapper.find(".sub-variety-card.selectable").on("click", function () {
			const new_code = $(this).data("item-code");
			const new_name = $(this).data("item-name");
			if (!new_code) return;
			const held = item.total_allocated_qty || 0;
			frappe.confirm(
				`Substitute <strong>${
					item.item_name
				}</strong> with <strong>${new_name}</strong> on this sales order?${
					held
						? ` ${held} allocated stems of ${item.item_name} will be unallocated.`
						: ""
				}`,
				() => {
					sub_dialog.hide();
					P._execute_substitute(so_item, new_code, new_name);
				}
			);
		});
	};
	const load_varieties = function () {
		sub_dialog.fields_dict.sub_content.$wrapper.html(
			'<div class="loading-state">Loading varieties…</div>'
		);
		const args = {
			sales_order: P.selected_order,
			sales_order_item: so_item,
			item_code: item.item_code,
			location: P.selected_location,
		};
		frappe.call({
			method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_substitute_varieties",
			args: args,
			callback: function (r) {
				render_sub_content(r.message || []);
			},
			error: function () {
				render_sub_content([]);
			},
		});
	};
	sub_dialog.show();
	load_varieties();
};
frappe.pages["sales-allocation-v2"]._execute_substitute = function (
	so_item,
	new_item_code,
	new_item_name
) {
	const P = frappe.pages["sales-allocation-v2"];
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.substitute_variety",
		args: {
			sales_order: P.selected_order,
			sales_order_item: so_item,
			new_item_code: new_item_code,
		},
		freeze: true,
		freeze_message: `Substituting with ${new_item_name}…`,
		callback: function (r) {
			if (r.message && r.message.success) {
				frappe.show_alert({
					message: `Substituted to ${new_item_name}`,
					indicator: "green",
				});
				// Reload the order items
				P.allocations = [];
				P.item_filters = {};
				P.select_order(P.selected_order, { keep: 1, force: 1 });
			} else {
				frappe.msgprint({
					title: "Substitution failed",
					message: r.message?.message || "Could not substitute variety.",
					indicator: "red",
				});
			}
		},
		error: function () {
			frappe.msgprint({
				title: "Error",
				message: "Substitution request failed.",
				indicator: "red",
			});
		},
	});
};
// ─── RENDER: DETAIL HEAD ───
frappe.pages["sales-allocation-v2"]._render_detail_head = function (items) {
	const P = frappe.pages["sales-allocation-v2"];
	const so = (P.current_sales_orders || []).find((o) => o.name === P.selected_order) || {};
	const meta = [
		so.customer,
		so.custom_order_name,
		so.delivery_date ? "delivery " + frappe.datetime.str_to_user(so.delivery_date) : "",
		`${(items || []).length} line(s)`,
	]
		.filter(Boolean)
		.join(" · ");
	return `
        <div style="flex:1;min-width:0;">
            <div style="font:600 15px var(--sans);color:var(--ink);letter-spacing:-.2px;">
                ${P.selected_order}
                <span class="pill pill-ink" style="margin-left:8px;">${
					P.selected_location || ""
				}</span>
            </div>
            <div style="font-size:11px;color:var(--ink-mute);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${meta}</div>
        </div>
        <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation-v2'].open_sales_order()">Sales Order</button>
        <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation-v2'].open_order_pick_list()">Order Pick List</button>
        <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation-v2'].close_detail()">Close</button>`;
};
frappe.pages["sales-allocation-v2"].open_sales_order = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_order) return;
	window.open("/app/sales-order/" + encodeURIComponent(P.selected_order), "_blank");
};
frappe.pages["sales-allocation-v2"].open_order_pick_list = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_order) return;
	// An order can have several OPLs (one per mix/bunch group), so the list
	// view filtered to this order is the right target, not a single doc.
	window.open(
		"/app/order-pick-list?sales_order=" + encodeURIComponent(P.selected_order),
		"_blank"
	);
};
// ─── RENDER: ORDER LINES RAIL ───
frappe.pages["sales-allocation-v2"]._render_lines_rail = function (items) {
	const P = frappe.pages["sales-allocation-v2"];
	if (!items.length) {
		return `<div class="sa-lines-title">Order lines</div>
                <div style="font-size:11px;color:var(--ink-mute);padding:4px 2px;">Nothing to show.</div>`;
	}
	const cards = items
		.map((item) => {
			const required = item.pending_stock_qty || 0;
			const grand = (item.total_allocated_qty || 0) + P._session_qty(item.sales_order_item);
			const pct = required > 0 ? Math.min(100, Math.round((grand / required) * 100)) : 0;
			const bar_color =
				pct >= 100 ? "var(--good)" : pct > 0 ? "var(--warn-2)" : "var(--bad)";
			const is_active = item.sales_order_item === P.selected_item ? "active" : "";
			let tag;
			if (item.custom_mixed_bunch) {
				tag = `<span class="lc-tag" style="background:var(--signal-soft);color:var(--signal);">Mixed bunch</span>`;
			} else if (item.custom_mixed_box) {
				tag = `<span class="lc-tag" style="background:var(--signal-soft);color:var(--signal);">Mixed box</span>`;
			} else {
				tag = `<span class="lc-tag" style="background:rgba(10,10,10,0.05);color:var(--ink-mute);">Straight</span>`;
			}
			const left = required - grand;
			const line_prop = Math.round(P._proposed_line(item.sales_order_item));
			// Stems, bunches and boxes: done / needed, and what Allocate all proposed for this line.
			const per_bunch = parseFloat(item.conversion_factor) || 1;
			const per_box = P._per_box(item);
			const fmt = (v) => Math.round(v).toLocaleString();
			// Allocated counts what Allocate all proposed too (it is allocated, waiting confirmation).
			const have = grand + line_prop;
			const lc_row = (label, done_n, total_n) =>
				`<div class="lc-row"><span>${label}</span><span>${fmt(done_n)}/${fmt(
					total_n
				)}</span></div>`;
			const lc_rows =
				lc_row(__("Stems"), have, required) +
				lc_row(
					__("Bunches"),
					Math.floor(have / per_bunch + 1e-9),
					Math.ceil(required / per_bunch - 1e-9)
				) +
				(per_box > 0
					? lc_row(
							__("Boxes"),
							Math.floor(have / per_box + 1e-9),
							Math.ceil(required / per_box - 1e-9)
					  )
					: "");
			const boxes = P._boxes_possible(item);
			const plan_flag = item.not_planned
				? `<span class="lc-tag" style="background:var(--warn-soft);color:var(--warn);margin-left:4px;">${__(
						"Not planned"
				  )}</span>`
				: "";
			const short_flag =
				left > 0 && !(item.batches || []).some((b) => (b.available_qty || 0) > 0)
					? `<span class="lc-tag" style="background:var(--warn-soft);color:var(--warn);margin-left:4px;">Short ${left}</span>` +
					  (boxes
							? `<span class="lc-tag" style="background:var(--warn-soft);color:var(--warn);margin-left:4px;">${
									boxes.can
							  } of ${boxes.total} box${boxes.total === 1 ? "" : "es"}</span>`
							: "")
					: "";
			return `
            <div class="line-card ${is_active}" data-so-item="${item.sales_order_item}">
                <div class="lc-name">${item.item_name || item.item_code || "?"}-${
				item.required_length || "No length"
			}</div>${lc_rows}
                <div class="lc-foot"><div>${tag}${plan_flag}${short_flag}</div><span class="lc-pct">${
				required ? Math.min(100, Math.round((have / required) * 100)) : 0
			}%</span></div>
                <div class="line-bar" style="display:flex;"><div style="width:${pct}%;height:100%;background:${bar_color};"></div>${
				line_prop && required
					? `<div class="soc-bar-prop" style="width:${Math.min(
							100 - pct,
							Math.round((line_prop / required) * 100)
					  )}%;"></div>`
					: ""
			}</div>
            </div>`;
		})
		.join("");
	return `<div class="sa-lines-title">Order lines</div>${cards}`;
};
frappe.pages["sales-allocation-v2"]._bind_lines_rail = function () {
	const P = frappe.pages["sales-allocation-v2"];
	P._scope()
		.find(".line-card[data-so-item]")
		.on("click", function () {
			const so_item = $(this).data("so-item");
			if (!so_item || so_item === P.selected_item) return;
			P.selected_item = so_item;
			P.render_allocation_grid();
		});
};
// ─── RENDER: ONE ORDER LINE (right pane) ───
frappe.pages["sales-allocation-v2"]._render_item_block = function (item) {
	const P = frappe.pages["sales-allocation-v2"];
	const allocated_session = P._session_qty(item.sales_order_item);
	const total_allocated = item.total_allocated_qty || 0;
	const grand_allocated = total_allocated + allocated_session;
	const required = item.pending_stock_qty || 0;
	const remaining = required - grand_allocated;
	const batches = item.batches || [];
	const preferred_farm = item.preferred_farm || "";
	const uom = item.stock_uom || "";
	// ── Pills: length, colour, box type, mix name, so-item id
	const pills = [];
	pills.push(`<span class="pill pill-ink">${item.required_length || "No length"}</span>`);
	if (item.color) pills.push(`<span class="pill pill-signal">${item.color}</span>`);
	if (item.custom_mixed_bunch) {
		pills.push(`<span class="pill pill-signal">Mixed bunch</span>`);
		// the mix's name is already in the Mix picker when one is chosen
		if (!P.selected_mix_group)
			pills.push(
				`<span class="pill pill-name" title="Internal group: ${
					item.custom_bunch_group || "?"
				}">${
					item.custom_mix_name
						? frappe.utils.escape_html(item.custom_mix_name)
						: "Bunch " + (item.custom_bunch_group || "?")
				}</span>`
			);
	} else if (item.custom_mixed_box) {
		pills.push(`<span class="pill pill-signal">Mixed box</span>`);
		if (!P.selected_mix_group)
			pills.push(
				`<span class="pill pill-name" title="Internal group: ${
					item.custom_mix_group || "?"
				}">${
					item.custom_mix_name
						? frappe.utils.escape_html(item.custom_mix_name)
						: "Group " + (item.custom_mix_group || "?")
				}</span>`
			);
	} else {
		pills.push(`<span class="pill pill-line">Straight box</span>`);
	}
	if (preferred_farm)
		pills.push(`<span class="pill pill-signal">Preferred: ${preferred_farm}</span>`);
	if (remaining <= 0) pills.push(`<span class="pill pill-good">Fully allocated</span>`);
	pills.push(`<span class="pill pill-mono">${item.sales_order_item || "?"}</span>`);
	// ── Actions: auto-allocate, substitute
	const box_lines = P._box_lines(item);
	const box_left = box_lines.reduce((s, it) => s + Math.max(0, P._line_left(it)), 0);
	const can_fifo = box_lines.some(
		(it) => P._line_left(it) > 0 && (it.batches || []).some((b) => (b.available_qty || 0) > 0)
	);
	const actions = `
        ${
			can_fifo && !item.plan_blocked
				? `<button class="sa-mini-btn primary" onclick="frappe.pages['sales-allocation-v2'].auto_allocate_fifo('${
						item.sales_order_item
				  }')">${
						box_lines.length > 1
							? `Auto-allocate ${box_lines.length} varieties · ${box_left}`
							: `Auto-allocate ${remaining}`
				  }</button>`
				: ""
		}
`;
	// the order's toolbar carries it, at the right (see render_allocation_grid)
	P._item_actions = actions;
	// ── Confirmed stems band
	const confirmed = item.confirmed_stems || 0;
	let confirmedBanner = "";
	if (confirmed > 0) {
		const chips = (item.confirmed_detail || [])
			.map(
				(d) =>
					`<span class="confirmed-chip-alloc">${
						d.farm
					}: ${d.stems.toLocaleString()}</span>`
			)
			.join("");
		const originalQty = item.original_ordered_qty || item.original_stock_qty || 0;
		const othersConfirmed = item.others_confirmed || 0;
		const totalAllConfirmed = item.total_all_confirmed || 0;
		confirmedBanner = `
            <div class="note-band note-good">
                <strong>Your confirmation: ${confirmed.toLocaleString()} stems</strong>
                ${chips}
                <span class="nb-right">
                    Ordered ${originalQty.toLocaleString()}${
			othersConfirmed > 0 ? ` · others ${othersConfirmed.toLocaleString()}` : ""
		} · total confirmed ${totalAllConfirmed.toLocaleString()}
                </span>
            </div>`;
	}
	const incomingBanner =
		(item.incoming_exact_stems || 0) > 0
			? `<div class="note-band note-good">
               <strong>${item.incoming_exact_stems} exact-length stems</strong>
               <span>(${item.required_length || "?"}) received but not yet shelved.</span>
           </div>`
			: "";
	let html = `
        <div class="item-head">
            <div style="min-width:0;">
                <div class="ih-title">${item.item_name || item.item_code || "Item"}${
		item.item_code &&
		item.item_code.trim().toLowerCase() !== (item.item_name || "").trim().toLowerCase()
			? `<span class="ih-code">${item.item_code}</span>`
			: ""
	}</div>
                <div class="ih-pills">${pills.join("")}</div>
            </div>

        </div>
        <div class="stat-strip">
            <div class="stat"><div class="st-l">To allocate</div><div class="st-v">${required.toLocaleString()}<small>${uom}</small></div></div>
            <div class="stat"><div class="st-l">Previously</div><div class="st-v">${total_allocated.toLocaleString()}</div></div>
            <div class="stat"><div class="st-l">This session</div><div class="st-v" style="color:${
				allocated_session > 0 ? "var(--good)" : "var(--ink-faint)"
			};">${allocated_session.toLocaleString()}</div></div>
            <div class="stat"><div class="st-l">Remaining</div><div class="st-v" style="color:${
				remaining > 0 ? "var(--warn)" : "var(--good)"
			};">${remaining.toLocaleString()}</div></div>
            <div class="stat"><div class="st-l">Available</div><div class="st-v" style="color:${
				(item.total_available_qty || 0) >= remaining ? "var(--good)" : "var(--bad)"
			};">${(item.total_available_qty || 0).toLocaleString()}</div></div>
        </div>
        ${confirmedBanner}${incomingBanner}`;
	// ── Cut stage filter + bucket table
	html += P._render_per_item_filter(item);
	html += `
        <button type="button" class="bd-trigger" data-so-item="${item.sales_order_item}">
            Why aren't more buckets showing?
        </button>
        <div id="bucket-diag-${item.sales_order_item}" class="bucket-diag" style="display:none;"></div>
        <table class="allocation-grid-table" data-so-item="${item.sales_order_item}">
            <thead><tr>
                <th>Age</th><th>Bucket</th><th>Farm</th><th>Shelf</th><th>Length</th>
                <th>Available</th><th>Allocated here</th><th>Session</th><th>Actions</th>
            </tr></thead>
            <tbody>`;
	if (!batches.length) {
		html += `<tr><td colspan="9" style="text-align:center;padding:24px 30px 10px;color:var(--ink-mute);">No compatible buckets found on the sales shelf right now.</td></tr>`;
	} else {
		batches.forEach((batch) => {
			const is_preferred = batch.shelf_farm === preferred_farm;
			const is_awaiting = batch.awaiting_transfer === 1;
			const is_zero = (batch.available_qty || 0) <= 0;
			const is_downgrade = batch.length_status === "downgrade";
			let row_class = "";
			if (is_awaiting) row_class = "awaiting-transfer-row";
			else if (is_downgrade) row_class = "downgrade-bucket";
			else if (is_preferred) row_class = "preferred-farm-row";
			if (is_zero && !batch.allocated_to_this_item)
				row_class += " previously-allocated-bucket";
			const session_alloc = P._session_bucket_qty(item.sales_order_item, batch.bucket_id);
			const allocated_here = batch.allocated_to_this_item || 0;
			let length_badge = is_downgrade
				? `<span class="grid-badge badge-downgrade">Downgrade</span>`
				: `<span class="grid-badge badge-exact">Exact</span>`;
			if (is_downgrade && batch.downgrade_approval === "amber_expired")
				length_badge += ` <span class="grid-badge badge-ok">Amber OK</span>`;
			else if (is_downgrade)
				length_badge += ` <span class="grid-badge badge-need">Needs approval</span>`;
			const conflict =
				is_downgrade && ((item.downgrade_conflicts || {})[batch.stem_length] || null);
			if (conflict && conflict.orders.length)
				length_badge += ` <span class="sa-dg-help" tabindex="0" data-so-item="${
					item.sales_order_item
				}" data-length="${frappe.utils.escape_html(
					batch.stem_length
				)}" data-take="${Math.min(
					batch.available_qty || 0,
					Math.max(0, remaining)
				)}" aria-label="Orders this downgrade would short">?</span>`;
			const farm_badge = is_preferred
				? `<span class="grid-badge badge-preferred">Preferred</span>`
				: "";
			const await_badge = is_awaiting
				? `<span class="grid-badge badge-awaiting">Remote shelf</span>`
				: "";
			// Already allocated to another line and on its way to the sales shelf
			// (server sets in_transit/transit_to -- see sales_allocation.py) --
			// still allocatable here, just flagged so the picker knows to expect
			// a short delay rather than finding it on the shelf right now.
			const transit_badge = batch.in_transit
				? `<span class="grid-badge badge-awaiting">In transit${
						batch.transit_to ? ` → ${batch.transit_to}` : ""
				  }</span>`
				: "";
			let actions_html = "";
			const esc_stem = (batch.stem_length || "").replace(/'/g, "\\'");
			if (item.plan_blocked && remaining > 0 && !allocated_here && !session_alloc) {
				actions_html += `<span class="lc-tag" style="background:var(--warn-soft);color:var(--warn);" title="${__(
					"Confirm this line in Allocation Planning first"
				)}">${__("Not planned")}</span>`;
			} else if (
				remaining > 0 &&
				(batch.available_qty || 0) > 0 &&
				!allocated_here &&
				!session_alloc
			) {
				const esc_bucket = (batch.bucket_id || "").replace(/'/g, "\\'");
				const esc_length = (batch.length_status || "exact").replace(/'/g, "\\'");
				actions_html += `
                    <button class="btn btn-xs btn-primary" onclick="frappe.pages['sales-allocation-v2'].allocate_from_bucket(
                        '${item.sales_order_item}','${esc_bucket}',${batch.available_qty},
                        '${uom}',${remaining},'${esc_length}','${esc_stem}'
                    )">Allocate</button>`;
			}
			// Released stems on a submitted pick list cannot be taken back here; this session's picks still can.
			const locked = item.opl_submitted ? allocated_here : 0;
			const can_undo = allocated_here + session_alloc - locked;
			if (can_undo > 0) {
				actions_html += `
                    <button class="btn btn-xs btn-danger" onclick="frappe.pages['sales-allocation-v2'].unallocate_from_bucket(
                        '${item.sales_order_item}','${batch.bucket_id}',${can_undo},'${esc_stem}'
                    )">Unallocate (${can_undo})</button>`;
			} else if (locked > 0) {
				actions_html += `
                    <button class="btn btn-xs btn-default" disabled style="opacity:.45;cursor:not-allowed;" title="${__(
						"Pick list {0} is submitted",
						[item.opl_name || ""]
					)}">Unallocate (${locked})</button>`;
			}
			if (is_zero && !allocated_here && !session_alloc) {
				const held = batch.held_for || [];
				actions_html = held.length
					? held
							.map(
								(h) =>
									`<span class="lc-tag" style="background:var(--warn-soft);color:var(--warn);white-space:nowrap;" title="${__(
										"{0} stems held for {1}",
										[
											Math.round(h.qty),
											frappe.utils.escape_html(h.sales_order),
										]
									)}">${__("Held · {0}", [
										frappe.utils.escape_html(h.sales_order),
									])}</span>`
							)
							.join(" ")
					: '<span style="color:var(--ink-faint);font-size:11px;">No stock</span>';
			}
			html += `
                <tr class="${row_class}" data-bucket-id="${batch.bucket_id || ""}">
                    <td>${batch.age_days != null ? batch.age_days : "?"}d</td>
                    <td><strong>${batch.bucket_id || "-"}</strong></td>
                    <td>${batch.shelf_farm || "-"} ${farm_badge}</td>
                    <td>${batch.shelf_location || "-"} ${await_badge} ${transit_badge}</td>
                    <td>${batch.stem_length || "N/A"} ${length_badge}</td>
                    <td style="color:${
						(batch.available_qty || 0) > 0 ? "var(--good)" : "var(--bad)"
					}">${batch.available_qty || 0}</td>
                    <td>${allocated_here > 0 ? `<strong>${allocated_here}</strong>` : "-"}</td>
                    <td>${
						session_alloc > 0
							? `<span style="color:var(--good)">${session_alloc}</span>`
							: "-"
					}</td>
                    <td>${actions_html || "-"}</td>
                </tr>`;
		});
	}
	html += `</tbody></table>`;
	return html;
};
// ─── RENDER THE WHOLE DETAIL PANE ───
// Kept under the old name so every existing call site still works.
frappe.pages["sales-allocation-v2"].render_allocation_grid = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!$("#salesAllocationDetailV2").length) return;
	if (!P.selected_order) {
		$("#detailHeadV2").html(
			'<div style="font:600 14px var(--sans);color:var(--ink-mute);">No order selected</div>'
		);
		$("#detailToolbarV2").empty().hide();
		$("#linesRailV2").empty();
		$("#allocationWorkspaceV2").html(
			'<div class="empty-state"><p>Pick a location, then choose an order on the left to start allocating.</p></div>'
		);
		return;
	}
	const all_items = P.order_items || [];
	// Opening an order: select the mix of the line it opens on (the one asked for, else the first
	// line still to allocate, else the first line), so a mixed box opens on its own mix.
	if (P._auto_mix && all_items.length) {
		P._auto_mix = false;
		const first =
			all_items.find(
				(it) => it.sales_order_item === (P._preserve_item || P.selected_item)
			) ||
			all_items.find((it) => P._line_left(it) > 0) ||
			all_items[0];
		if (first.custom_mixed_box && first.custom_mix_group) {
			P.selected_mix_group = String(first.custom_mix_group);
			if (!P.selected_item) P.selected_item = first.sales_order_item;
		}
	}
	// Apply mix-group filter (empty string = no filter, show everything)
	const items = P.selected_mix_group
		? all_items.filter(
				(it) => String(it.custom_mix_group || "") === String(P.selected_mix_group)
		  )
		: all_items;
	// Resolve which line is showing
	if (!items.some((i) => i.sales_order_item === P.selected_item)) {
		P.selected_item = items.length ? items[0].sales_order_item : null;
	}
	const item = items.find((i) => i.sales_order_item === P.selected_item);
	$("#detailHeadV2").html(P._render_detail_head(items));
	// the cut stage is bypassed by Allocate all's own second pass, not per order
	const toolbar = P._render_farm_filter() + P._render_mix_filter();
	if (toolbar.trim()) $("#detailToolbarV2").html(toolbar).css("display", "flex");
	else $("#detailToolbarV2").empty().hide();
	$("#linesRailV2").html(P._render_lines_rail(items));
	if (item) {
		$("#allocationWorkspaceV2").html(P._render_item_block(item));
		// Auto-allocate the whole order, at the right of the farms and mix toolbar: the same planning Allocate
		// all does (whole boxes, mixes by their scarcest variety), into the order's proposal for release
		$("#detailToolbarV2 .dt-auto").remove();
		const left = (P.order_items || []).reduce((t, it) => t + Math.max(0, P._line_left(it)), 0);
		if (left > 0)
			$("#detailToolbarV2")
				.append(
					`<span class="dt-auto"><button class="sa-mini-btn primary" data-auto-order>${__(
						"Auto-allocate order · {0}",
						[Math.round(left).toLocaleString()]
					)}</button></span>`
				)
				.css("display", "flex")
				.find("[data-auto-order]")
				.on("click", () => P._auto_allocate_order());
	} else if (P.selected_mix_group) {
		$("#allocationWorkspaceV2").html(
			'<div class="empty-state"><p>No items in the selected mix.</p></div>'
		);
	} else {
		$("#allocationWorkspaceV2").html(
			'<div class="empty-state"><p>No items with confirmed stems for this location.</p></div>'
		);
	}
	P._bind_farm_filter();
	P._bind_cutstage_bypass();
	P._bind_mix_filter();
	P._bind_lines_rail();
	P._bind_per_item_filters();
	P._bind_bucket_diagnostics();
	if (item) P._apply_item_batch_visibility(item.sales_order_item);
	if (item && (!(item.batches || []).length || P._bucket_diag_open[item.sales_order_item])) {
		// No compatible buckets at all — the diagnostic is the whole story here,
		// so surface it immediately. Also re-open it if the user had it open
		// before this re-render (e.g. after an allocate/unallocate action).
		P._bucket_diag_open[item.sales_order_item] = true;
		P._load_bucket_diagnostics(item, true);
	}
	// Keep this order's card in the left list in sync with live allocation.
	P._sync_order_card();
};
// Push the live progress of the selected order onto its list card (bar + %),
// and remember it on the order object so re-renders/filters stay consistent.
frappe.pages["sales-allocation-v2"]._sync_order_card = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_order) return;
	const pct = P._order_progress(P.order_items).pct;
	const ord = (P.current_sales_orders || []).find((o) => o.name === P.selected_order);
	if (ord) ord._live_pct = pct;
	const color = pct >= 75 ? "var(--good)" : pct >= 50 ? "var(--warn-2)" : "var(--bad)";
	P._update_confirm_btn();
	const $card = $(`.sales-order-card[data-order="${P.selected_order}"]`);
	if ($card.length) {
		const cover = P._cover_pct(ord || { name: P.selected_order }, pct);
		const cover_color = P._cover_color(cover);
		$card
			.find(".soc-pct")
			.css("color", cover_color)
			.text(cover + "%");
		$card.find(".soc-bar-done").css({ width: cover + "%", background: cover_color });
		if (ord && ord.boxes_required) {
			const full = P._full_boxes(ord);
			$card
				.find(".soc-boxes")
				.html(
					`<span>${__("{0}/{1} boxes full", [full, ord.boxes_required])}</span>${
						ord.boxes_required > full
							? `<span class="soc-boxleft">${__("{0} to allocate", [
									ord.boxes_required - full,
							  ])}</span>`
							: ""
					}`
				);
		}
	}
};
// ─── ALLOCATE FROM BUCKET ───
frappe.pages["sales-allocation-v2"].allocate_from_bucket = function (
	so_item,
	bucket_id,
	max_from_bucket,
	uom,
	remaining,
	length_status,
	stem_length
) {
	const P = frappe.pages["sales-allocation-v2"];
	const item = P.order_items.find((i) => i.sales_order_item === so_item);
	if (!item) return;
	// whole bunches only
	const per_bunch = parseFloat(item.conversion_factor) || 1;
	const qty = Math.floor(Math.min(max_from_bucket, remaining) / per_bunch + 1e-9) * per_bunch;
	if (qty <= 0) {
		frappe.msgprint(__("Less than one bunch of {0} left to allocate here.", [per_bunch]));
		return;
	}
	if (length_status === "downgrade") {
		const batch = (item.batches || []).find((b) => b.bucket_id === bucket_id);
		const approval = batch ? batch.downgrade_approval : "requires_approval";
		if (approval === "amber_expired") {
			P._do_allocate(so_item, bucket_id, qty, uom, length_status, "Amber time expired");
			return;
		}
		const age_days = batch ? batch.age_days : 0;
		const amber_time = item.amber_time || 3;
		const incoming = item.incoming_exact_stems || 0;
		let incoming_html = "";
		if (incoming > 0) {
			incoming_html = `<div style="background:var(--good-soft);border-radius:10px;padding:9px 12px;margin-top:10px;font-size:12px;color:var(--good);">
                <strong>${incoming} exact-length stems</strong> (${
				item.required_length || "?"
			}) received but not yet shelved.
            </div>`;
		}
		const d = new frappe.ui.Dialog({
			title: "Downgrade — reason required",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "info",
					options: `
                    <div style="background:var(--warn-soft);border-radius:10px;padding:12px 14px;margin-bottom:10px;color:var(--ink-3);font-size:12px;">
                        <strong>Bucket:</strong> ${bucket_id} (${stem_length}) — order requires ${
						item.required_length || "N/A"
					}<br>
                        <strong>Qty:</strong> ${qty} ${uom} · age ${age_days}d (amber at ${amber_time}d)
                    </div>
                    <div style="background:var(--bad-soft);border-radius:10px;padding:9px 12px;margin-bottom:6px;font-size:12px;color:var(--bad);">
                        Requires approval — bucket is only ${age_days} days old.
                    </div>${incoming_html}`,
				},
				{
					fieldtype: "Small Text",
					fieldname: "reason",
					label: "Downgrade reason",
					reqd: 1,
				},
			],
			primary_action_label: "Confirm",
			primary_action: function (vals) {
				if (!vals.reason || !vals.reason.trim()) {
					frappe.msgprint(__("Reason required."));
					return;
				}
				d.hide();
				P._do_allocate(so_item, bucket_id, qty, uom, length_status, vals.reason.trim());
			},
		});
		d.$wrapper.addClass("ufd-sa2");
		d.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal");
		d.show();
		setTimeout(() => d.fields_dict.reason.$input.focus(), 200);
	} else {
		P._do_allocate(so_item, bucket_id, qty, uom, length_status, "");
	}
};
frappe.pages["sales-allocation-v2"]._do_allocate = function (
	so_item,
	bucket_id,
	qty,
	uom,
	length_status,
	downgrade_reason
) {
	const P = frappe.pages["sales-allocation-v2"];
	const item = P.order_items.find((i) => i.sales_order_item === so_item);
	if (!item) return;
	const batch = (item.batches || []).find((b) => b.bucket_id === bucket_id);
	const batch_stem_length = batch ? batch.stem_length || "" : "";
	// Match on stem_length too -- the same bucket can carry this variety at
	// more than one length (Spray Roses graded straight into one bucket), so
	// bucket_id alone isn't enough to identify which batch this click is for.
	const existing = P.allocations.find(
		(a) =>
			a.sales_order_item === so_item &&
			a.bucket_id === bucket_id &&
			a.stem_length === batch_stem_length
	);
	if (existing) {
		existing.qty += qty;
		if (downgrade_reason) existing.downgrade_reason = downgrade_reason;
	} else {
		P.allocations.push({
			item_code: item.item_code || "",
			bucket_id,
			qty,
			sales_order_item: so_item,
			stem_length: batch ? batch.stem_length : "",
			warehouse: batch ? batch.warehouse : "",
			uom: item.uom || "",
			stock_uom: item.stock_uom || "",
			conversion_factor: item.conversion_factor || 1,
			length_status: length_status || "exact",
			downgrade_reason: downgrade_reason || "",
			available_exact_stems:
				length_status === "downgrade" ? item.incoming_exact_stems || 0 : 0,
		});
	}
	if (batch) P._take_from_bucket(item.item_code, bucket_id, batch.stem_length || "", qty);
	P.render_allocation_grid();
};
// Stems taken from a bucket for one line are gone for every line: two lines of the same
// variety (a straight line and a mixed-box line) each hold their own copy of the bucket's
// free stems, and taking from one copy only let the other line take them again
// ("Over-allocation on bucket ...: 40.0 available, 80.0 requested").
frappe.pages["sales-allocation-v2"]._take_from_bucket = function (
	item_code,
	bucket_id,
	stem_length,
	qty
) {
	const P = frappe.pages["sales-allocation-v2"];
	(P.order_items || []).forEach((it) => {
		if (it.item_code !== item_code) return;
		(it.batches || []).forEach((b) => {
			if (b.bucket_id === bucket_id && (b.stem_length || "") === stem_length)
				b.available_qty = Math.max(0, (b.available_qty || 0) - qty);
		});
	});
};
// ─── UNALLOCATE ───
frappe.pages["sales-allocation-v2"].unallocate_from_bucket = function (
	so_item,
	bucket_id,
	allocated_qty,
	stem_length
) {
	const P = frappe.pages["sales-allocation-v2"];
	const length = stem_length || "";
	const item = (P.order_items || []).find((i) => i.sales_order_item === so_item);
	const batch = ((item && item.batches) || []).find(
		(b) => b.bucket_id === bucket_id && (b.stem_length || "") === length
	);
	const mine = (a) =>
		a.sales_order_item === so_item && a.bucket_id === bucket_id && a.stem_length === length;
	// What this session picked (waiting release) and what is already released for the line.
	const picked = (P.allocations || [])
		.filter(mine)
		.reduce((t, a) => t + (parseFloat(a.qty) || 0), 0);
	// released stems on a submitted pick list stay; only this session's pick is undone then
	const released = item && item.opl_submitted ? 0 : (batch && batch.allocated_to_this_item) || 0;
	frappe.confirm(`Unallocate ${allocated_qty} stems from bucket ${bucket_id}?`, () => {
		// Only this length's pick -- the same bucket can hold this variety at another length.
		P.allocations = (P.allocations || []).filter((a) => !mine(a));
		if (picked && batch) {
			const back = (batch.available_qty || 0) + picked;
			batch.available_qty = batch.original_available_qty
				? Math.min(batch.original_available_qty, back)
				: back;
		}
		// the order's Pending Allocation follows, so the pick does not come back on reload
		const held = P._hold_session();
		if (!released) {
			P.render_allocation_grid();
			frappe.show_alert({
				message: __("Removed {0} stems of {1} from this allocation", [
					Math.round(picked),
					bucket_id,
				]),
				indicator: "green",
			});
			return;
		}
		held.then(() =>
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.unallocate_bucket_from_opl",
				args: { sales_order_item: so_item, bucket_id, stem_length },
				freeze: true,
				freeze_message: "Unallocating…",
				callback: function (r) {
					if (r.message && r.message.success)
						frappe.show_alert({ message: r.message.message, indicator: "green" });
					else
						frappe.msgprint({
							title: "Note",
							message: r.message?.message || "Partial success.",
							indicator: "orange",
						});
					P.select_order(P.selected_order, { keep: 1, force: 1 });
				},
				error: function () {
					frappe.msgprint({
						title: "Failed",
						message: "Unallocation failed. Please refresh.",
						indicator: "red",
					});
				},
			})
		);
	});
};
// ─── AUTO-ALLOCATE FIFO ───
frappe.pages["sales-allocation-v2"].auto_allocate_fifo = function (so_item) {
	const P = frappe.pages["sales-allocation-v2"];
	const item = P.order_items.find((i) => i.sales_order_item === so_item);
	if (!item) return;
	// A mixed box (or bunch) is packed together: fill every variety of it in one go.
	const lines = P._box_lines(item).filter((it) => P._line_left(it) > 0);
	if (!lines.length) {
		frappe.msgprint(__("Item already fully allocated."));
		return;
	}
	if (!lines.some((it) => (it.batches || []).some((b) => (b.available_qty || 0) > 0))) {
		frappe.msgprint(__("No compatible buckets available."));
		return;
	}
	const needs_reason_downgrades = [];
	lines.forEach((line) => {
		let to_check = P._line_left(line);
		for (const batch of (line.batches || []).filter((b) => (b.available_qty || 0) > 0)) {
			if (to_check <= 0) break;
			const qty = Math.min(batch.available_qty, to_check);
			if (
				batch.length_status === "downgrade" &&
				batch.downgrade_approval === "requires_approval"
			) {
				needs_reason_downgrades.push({
					...batch,
					planned_qty: qty,
					variety: line.item_name || line.item_code,
				});
			}
			to_check -= qty;
		}
	});
	const run = (reason) => P._execute_fifo_lines(lines, reason);
	if (needs_reason_downgrades.length) {
		const summary = needs_reason_downgrades
			.map(
				(d) =>
					`<li><strong>${d.bucket_id}</strong>${
						lines.length > 1 ? ` ${d.variety}` : ""
					} (${d.stem_length}, ${d.shelf_farm}) — ${d.planned_qty} stems</li>`
			)
			.join("");
		const incoming = lines.reduce((s, it) => s + (it.incoming_exact_stems || 0), 0);
		const d = new frappe.ui.Dialog({
			title: "Auto-allocate — downgrade reason required",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "info",
					options: `
                    <div style="background:var(--warn-soft);border-radius:10px;padding:12px 14px;margin-bottom:10px;color:var(--ink-3);font-size:12px;">
                        <strong>Downgrade buckets that will be used:</strong>
                        <ul style="margin:8px 0 0;padding-left:20px;">${summary}</ul>
                    </div>
                    ${
						incoming > 0
							? `<div style="background:var(--good-soft);border-radius:10px;padding:9px 12px;margin-bottom:6px;font-size:12px;color:var(--good);">
                        <strong>${incoming} exact-length stems</strong> received but not yet shelved.
                    </div>`
							: ""
					}`,
				},
				{
					fieldtype: "Small Text",
					fieldname: "reason",
					label: "Downgrade reason (applies to all downgraded buckets)",
					reqd: 1,
				},
			],
			primary_action_label: "Auto-allocate",
			primary_action: function (vals) {
				if (!vals.reason || !vals.reason.trim()) {
					frappe.msgprint(__("Reason required."));
					return;
				}
				d.hide();
				run(vals.reason.trim());
			},
		});
		d.$wrapper.addClass("ufd-sa2");
		d.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal");
		d.show();
		setTimeout(() => d.fields_dict.reason.$input.focus(), 200);
	} else {
		run("");
	}
};
// Every line packed in the same box as this one: the lines of its mixed box or mixed bunch,
// or just the line itself for a straight box.
frappe.pages["sales-allocation-v2"]._box_lines = function (item) {
	const P = frappe.pages["sales-allocation-v2"];
	const key = item.custom_mixed_bunch
		? ["bunch", item.custom_bunch_group]
		: item.custom_mixed_box
		? ["box", item.custom_mix_group]
		: null;
	if (!key || !key[1]) return [item];
	return (P.order_items || []).filter((it) =>
		key[0] === "bunch"
			? it.custom_mixed_bunch && String(it.custom_bunch_group) === String(key[1])
			: it.custom_mixed_box &&
			  !it.custom_mixed_bunch &&
			  String(it.custom_mix_group) === String(key[1])
	);
};
// Stems of a line in one box: its pack rate (the mix's per-box share for a mixed box or bunch),
// the same rule the allocator fills boxes by; else its stems over its boxes.
frappe.pages["sales-allocation-v2"]._per_box = function (it) {
	const n = parseInt(it.custom_number_of_boxes, 10) || 1;
	const rate = parseFloat(
		it.custom_mixed_box || it.custom_mixed_bunch
			? it.custom_packrate_mixed_box
			: it.custom_packrate
	);
	return rate > 0 ? rate : (it.original_ordered_qty || it.original_stock_qty || 0) / n;
};
// Whole boxes filled by what is allocated (with_available: plus what is still available): a
// straight line's own boxes; a mixed box or bunch only as many as its scarcest variety fills.
// What one box of this line is, in bunches (Bunches/Box on the spec = pack rate / stems a bunch).
// A mixed bunch's bunch is the bouquet: this line is the stems of its variety in each one.
frappe.pages["sales-allocation-v2"]._box_make_up = function (it) {
	const P = frappe.pages["sales-allocation-v2"];
	const per_bunch = parseFloat(it.conversion_factor) || 1;
	const bunches = Math.round(P._per_box(it) / per_bunch);
	return it.custom_mixed_bunch
		? `${bunches} bunches a box · ${per_bunch} stem${per_bunch === 1 ? "" : "s"} in each`
		: `${bunches} bunches of ${per_bunch} a box`;
};
frappe.pages["sales-allocation-v2"]._boxes_possible = function (item, allocated_only) {
	const P = frappe.pages["sales-allocation-v2"];
	const lines = P._box_lines(item);
	const total = parseInt(item.custom_number_of_boxes, 10) || 0;
	if (!total) return null;
	const can = Math.min(
		...lines.map((it) => {
			const n = parseInt(it.custom_number_of_boxes, 10) || total;
			const per_box = P._per_box(it);
			if (!per_box) return n;
			const have =
				(it.total_allocated_qty || 0) +
				P._session_qty(it.sales_order_item) +
				(allocated_only
					? 0
					: (it.batches || []).reduce(
							(s, b) => s + Math.max(0, b.available_qty || 0),
							0
					  ));
			return Math.min(n, Math.floor(have / per_box + 1e-9));
		})
	);
	return { can: Math.min(can, total), total };
};
// Where the run and its steps show: over the whole detail panel (Allocate all), or for one order over its
// bucket workspace only, its header, toolbar and lines staying in view.
frappe.pages["sales-allocation-v2"]._flow_area = function (on) {
	const $o = $("#dayLoadV2, #dayStepV2");
	const sec = document.getElementById("salesAllocationDetailV2");
	const ws = document.getElementById("allocationWorkspaceV2");
	if (!on || !sec || !ws) {
		$o.removeClass("sa-flow--order").css({ top: "", left: "", width: "", height: "" });
		return;
	}
	const a = sec.getBoundingClientRect();
	const b = ws.getBoundingClientRect();
	$o.addClass("sa-flow--order").css({
		top: b.top - a.top,
		left: b.left - a.left,
		width: b.width,
		height: b.height,
	});
};
// Auto-allocate order: the order is allocated like Allocate all does, then the same steps follow for it (cut
// stage, substitutes, downgrades with a reason), in the detail panel.
frappe.pages["sales-allocation-v2"]._auto_allocate_order = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const so = P.selected_order;
	if (!so || P._day_busy) return;
	const order = (P.current_sales_orders || []).find((o) => o.name === so) || {};
	const day = order.delivery_date || P.filters.delivery_start;
	P._flow_area(true);
	P._day_progress(0, 1, "", undefined, "order");
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.allocate_orders",
		args: { ...P._day_args(), sales_orders: JSON.stringify([so]) },
		callback: (r) => {
			const m = r.message || {};
			if (m.stems)
				frappe.show_alert({
					message: __("Allocated {0} stems to the order, waiting release", [
						Math.round(m.stems).toLocaleString(),
					]),
					indicator: "green",
				});
			P._steps_after_allocate({ sales_order: so, day }, m.stems || 0);
		},
		error: () => P._day_progress_reset(),
	});
};
frappe.pages["sales-allocation-v2"]._line_left = function (item) {
	const P = frappe.pages["sales-allocation-v2"];
	return (
		(item.pending_stock_qty || 0) -
		(item.total_allocated_qty || 0) -
		P._session_qty(item.sales_order_item)
	);
};
frappe.pages["sales-allocation-v2"]._execute_fifo_lines = function (lines, downgrade_reason) {
	const P = frappe.pages["sales-allocation-v2"];
	let count = 0;
	const short = [];
	lines.forEach((line) => {
		count += P._execute_fifo(line.sales_order_item, downgrade_reason, true);
		const left = P._line_left(line);
		if (left > 0) short.push(`${line.item_name || line.item_code} short ${left}`);
	});
	if (count > 0) P.render_allocation_grid();
	frappe.show_alert({
		message:
			`Auto-allocated ${count} stems` +
			(lines.length > 1 ? ` across ${lines.length} varieties` : "") +
			(short.length ? ` · ${short.join(", ")}` : "."),
		indicator: !count ? "red" : short.length ? "orange" : "green",
	});
};
frappe.pages["sales-allocation-v2"]._execute_fifo = function (so_item, downgrade_reason, quiet) {
	const P = frappe.pages["sales-allocation-v2"];
	const item = P.order_items.find((i) => i.sales_order_item === so_item);
	if (!item) return 0;
	const allocated_session = P._session_qty(so_item);
	const grand_allocated = (item.total_allocated_qty || 0) + allocated_session;
	const required = item.pending_stock_qty || 0;
	let remaining = required - grand_allocated;
	let count = 0;
	for (const batch of item.batches || []) {
		if (remaining <= 0) break;
		const available = batch.available_qty || 0;
		if (available <= 0) continue;
		// whole bunches only: a bucket gives what fills whole bunches
		const per_bunch = parseFloat(item.conversion_factor) || 1;
		const qty = Math.floor(Math.min(available, remaining) / per_bunch + 1e-9) * per_bunch;
		if (qty <= 0) continue;
		const is_downgrade = batch.length_status === "downgrade";
		const reason = is_downgrade
			? batch.downgrade_approval === "amber_expired"
				? "Amber time expired"
				: downgrade_reason
			: "";
		// A bucket can hold this same variety at more than one stem length
		// (Spray Roses graded straight in the field), so an order line's FIFO
		// fill can draw from the same bucket at two different lengths -- e.g.
		// an exact-match batch and a downgrade-eligible longer one. Matching
		// on bucket_id alone would merge those into one allocation row with a
		// single stem_length, silently losing which portion was which length.
		const existing = P.allocations.find(
			(a) =>
				a.sales_order_item === so_item &&
				a.bucket_id === batch.bucket_id &&
				a.stem_length === (batch.stem_length || "")
		);
		if (existing) {
			existing.qty += qty;
			if (reason) existing.downgrade_reason = reason;
		} else {
			P.allocations.push({
				item_code: item.item_code,
				bucket_id: batch.bucket_id,
				qty,
				sales_order_item: so_item,
				stem_length: batch.stem_length || "",
				warehouse: batch.warehouse || "",
				uom: item.uom || "",
				stock_uom: item.stock_uom || "",
				conversion_factor: item.conversion_factor || 1,
				length_status: batch.length_status || "exact",
				downgrade_reason: reason,
				available_exact_stems: is_downgrade ? item.incoming_exact_stems || 0 : 0,
			});
		}
		P._take_from_bucket(item.item_code, batch.bucket_id, batch.stem_length || "", qty);
		remaining -= qty;
		count += qty;
	}
	if (count > 0 && !quiet) {
		frappe.show_alert({
			message: `Auto-allocated ${count} ${item.stock_uom || ""}.`,
			indicator: "green",
		});
		P.render_allocation_grid();
	}
	return count;
};
// ─── HELPERS ───
frappe.pages["sales-allocation-v2"].clear_allocations = function (silent) {
	const P = frappe.pages["sales-allocation-v2"];
	(P.order_items || []).forEach((item) => {
		(item.batches || []).forEach((b) => {
			b.available_qty = b.original_available_qty || 0;
		});
	});
	P.allocations = [];
	if (!silent) frappe.show_alert({ message: "Session allocations cleared.", indicator: "blue" });
};
frappe.pages["sales-allocation-v2"]._session_qty = function (so_item) {
	return (frappe.pages["sales-allocation-v2"].allocations || [])
		.filter((a) => a.sales_order_item === so_item)
		.reduce((s, a) => s + (parseFloat(a.qty) || 0), 0);
};
// Whole-order allocation progress, by stems: previously-allocated (persisted) +
// this-session allocations, over what the order still needs. Drives the order
// header bar and the live list-card bar, so both climb as you allocate — for
// every order, remote farms included (this counts session + persisted stems, and
// never depends on whether a remote OPL has been submitted yet).
frappe.pages["sales-allocation-v2"]._order_progress = function (items) {
	const P = frappe.pages["sales-allocation-v2"];
	const list = items || P.order_items || [];
	let required = 0,
		done = 0;
	list.forEach((it) => {
		required += it.pending_stock_qty || 0;
		done += (it.total_allocated_qty || 0) + P._session_qty(it.sales_order_item);
	});
	const pct = required > 0 ? Math.min(100, Math.round((done / required) * 100)) : 0;
	return { required: required, done: done, pct: pct };
};
// Live percentage for an order in the list: the live session value once the order
// has been opened/allocated, else the stem-based figure from the backend.
frappe.pages["sales-allocation-v2"]._order_list_pct = function (order) {
	if (!order) return 0;
	const P = frappe.pages["sales-allocation-v2"];
	if (order.name === P.selected_order && P._items_for === order.name)
		return P._order_progress().pct;
	return order._live_pct != null ? order._live_pct : order.allocation_percentage || 0;
};
frappe.pages["sales-allocation-v2"]._session_total = function () {
	return (frappe.pages["sales-allocation-v2"].allocations || []).reduce(
		(s, a) => s + (parseFloat(a.qty) || 0),
		0
	);
};
frappe.pages["sales-allocation-v2"]._session_bucket_qty = function (so_item, bucket_id) {
	const match = (frappe.pages["sales-allocation-v2"].allocations || []).find(
		(a) => a.sales_order_item === so_item && a.bucket_id === bucket_id
	);
	return match ? parseFloat(match.qty) || 0 : 0;
};

// ─── AUTO-ALLOCATE ALL ORDERS OF THE DELIVERY WINDOW ───
frappe.pages["sales-allocation-v2"]._day_args = function () {
	const P = frappe.pages["sales-allocation-v2"];
	return {
		location: P.selected_location,
		delivery_start: P.filters.delivery_start,
		delivery_end: P.filters.delivery_end || P.filters.delivery_start,
		selected_farms: JSON.stringify(P.selected_farms || []),
	};
};
frappe.pages["sales-allocation-v2"].auto_allocate_day = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_location || !P.filters.delivery_start) {
		frappe.msgprint(__("Select a location and delivery date."));
		return;
	}
	P._listen_day_allocation();
	// the open order's picks are held first, so Allocate all leaves them (and their buckets) alone
	P._day_finished = false;
	P._live_planned = {};
	P._live_planned_key = "";
	P._flow_area(false);
	P._day_progress(0, 1, "", undefined, "prepare");
	// a pick that cannot be held (already held elsewhere) must not stop Allocate all
	P._hold_session()
		.catch(() => {})
		.then(() =>
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.auto_allocate_day",
				args: P._day_args(),
				callback: (r) =>
					r.message &&
					(r.message.queued
						? ((P._day_run = r.message.run || ""), P._resume_day_progress())
						: r.message.done && P._day_done(r.message)),
				error: () => P._day_progress_reset(),
			})
		);
};
// The order cards fill as Allocate all plans each order.
frappe.pages["sales-allocation-v2"]._show_planned = function (planned) {
	const P = frappe.pages["sales-allocation-v2"];
	const key = JSON.stringify(planned);
	if (key === P._live_planned_key) return;
	P._live_planned_key = key;
	P._live_planned = planned;
	P.apply_filters();
};
frappe.pages["sales-allocation-v2"]._listen_day_allocation = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (P._day_listening) return;
	P._day_listening = true;
	frappe.realtime.on("sa2_day_allocation", (m) => {
		if (!m.done) {
			if (m.planned) P._show_planned(m.planned);
			if (!P._day_finished)
				P._day_progress(
					m.progress,
					m.total,
					m.order,
					undefined,
					m.stage || (m.phase === "confirm" ? "release" : "allocate")
				);
			return;
		}
		P._day_done(m);
	});
};
// A finished Allocate all or Release: from realtime or from the request's own answer, whichever comes
// first; the other is ignored.
frappe.pages["sales-allocation-v2"]._day_done = function (m) {
	const P = frappe.pages["sales-allocation-v2"];
	clearTimeout(P._day_poll);
	if (P._day_finished) return;
	P._day_finished = true;
	P._day_running = false;
	// Allocate all goes straight on in the panel: the cut stage, then substitutes, then downgrades
	if (m.phase !== "confirm" && !m.error) P._steps_after_allocate(null, m.new_stems || 0);
	else P._day_progress(1, 1, "", !m.error);
	// the realtime message only carries counts: the proposal and the shortages are read again
	if (!m.error && !("shortages" in m)) {
		P._load_proposal();
		frappe.call({
			method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.get_day_shortages",
			args: P._day_args(),
			callback: (r) => {
				const list = Array.isArray(r.message)
					? r.message
					: (r.message || {}).shortages || [];
				P._set_shortages(list);
				const n = (v) => Math.round(v || 0).toLocaleString();
				if (m.phase === "confirm") {
					frappe.show_alert({
						message: __("Released {0} orders · {1} boxes ({2} stems)", [
							m.saved || 0,
							n(m.boxes),
							n(m.stems),
						]),
						indicator: Object.keys(m.errors || {}).length ? "orange" : "green",
					});
					// what has no complete box yet stays allocated, waiting release
					if ((m.waiting || []).length)
						frappe.show_alert({
							message: __(
								"{0} orders have no complete box yet: they stay allocated",
								[m.waiting.length]
							),
							indicator: "orange",
						});
					P._after_day_change();
				} else if (m.new_orders) {
					frappe.show_alert({
						message: __("Proposed {0} boxes ({1} stems) for {2} orders", [
							n(m.new_boxes),
							n(m.new_stems),
							m.new_orders,
						]),
						indicator: "green",
					});
				} else if (m.orders_count) {
					frappe.show_alert({
						message: __("{0} orders are waiting to be released.", [m.orders_count]),
						indicator: "green",
					});
				}
			},
		});
		return;
	}
	{
		if (m.error) {
			frappe.msgprint({
				title: m.phase === "confirm" ? __("Release failed") : __("Allocate all failed"),
				message: m.error,
				indicator: "red",
			});
			return;
		}
		const n = (v) => Math.round(v || 0).toLocaleString();
		if (m.phase === "confirm") {
			const failed = Object.entries(m.errors || {});
			frappe.show_alert({
				message: __("Released {0} orders · {1} boxes ({2} stems)", [
					m.saved || 0,
					n(m.boxes),
					n(m.stems),
				]),
				indicator: failed.length ? "orange" : "green",
			});
			if (failed.length)
				frappe.msgprint({
					title: __("Not saved"),
					message: failed
						.map(
							([so, why]) =>
								`<strong>${frappe.utils.escape_html(
									so
								)}</strong>: ${frappe.utils.escape_html(why)}`
						)
						.join("<br>"),
					indicator: "orange",
				});
			P._set_proposal(m.proposal);
			P._after_day_change();
		} else {
			if (m.new_orders) {
				frappe.show_alert({
					message: __("Proposed {0} boxes ({1} stems) for {2} orders", [
						n(m.new_boxes),
						n(m.new_stems),
						m.new_orders,
					]),
					indicator: "green",
				});
			} else if ((m.orders || []).length) {
				// nothing new: every order that could be allocated is waiting to be released
				const d = frappe.msgprint({
					title: __("Everything is allocated"),
					message: __("{0} orders · {1} boxes are waiting to be released.", [
						m.orders.length,
						n(m.boxes),
					]),
					indicator: "green",
					primary_action: {
						label: __("Release"),
						action: () => {
							d.hide();
							P._set_proposal(m);
							P._open_confirm_dialog();
						},
					},
				});
			} else {
				// Nothing proposed: say why, the commonest reason first.
				const why = {};
				(m.shortages || []).forEach((x) => (why[x.why] = (why[x.why] || 0) + 1));
				const top = Object.entries(why).sort((a, b) => b[1] - a[1])[0];
				frappe.msgprint({
					title: __("Nothing to allocate"),
					message: top
						? __("{0} of {1} lines: {2}", [top[1], (m.shortages || []).length, top[0]])
						: __("No open lines for this date."),
					indicator: "orange",
				});
			}
			P._set_proposal(m);
		}
		P._set_shortages(m.shortages || []);
	}
};
frappe.pages["sales-allocation-v2"]._day_progress_reset = function () {
	frappe.pages["sales-allocation-v2"]._flow_area(false);
	$("body").removeClass("sa2-busy");
	$("body").removeClass("sa2-steps");
	frappe.pages["sales-allocation-v2"]._live_planned = null;
	$("#dayProgV2").hide();
	$("#dayLoadV2").hide();
	$("#dayStepV2").hide().empty();
	$("#autoAllocateDayV2, #shortTabV2").prop("disabled", false);
	frappe.pages["sales-allocation-v2"]._day_busy = false;
	$(".location-btn").prop("disabled", false);
	frappe.pages["sales-allocation-v2"]._set_proposal(
		frappe.pages["sales-allocation-v2"].proposal
	);
};
// ─── CONFIRM ALLOCATED ORDERS: save the proposal Allocate all made ───
frappe.pages["sales-allocation-v2"]._set_proposal = function (p) {
	const P = frappe.pages["sales-allocation-v2"];
	P.proposal = p && (p.orders || []).length ? p : null;
	P._proposal_key = P._short_key();
	P._update_confirm_btn();
	if ((P.current_sales_orders || []).length) P.apply_filters();
};
// Stems Allocate all proposed for a line, not confirmed yet.
frappe.pages["sales-allocation-v2"]._proposed_line = function (soi) {
	const P = frappe.pages["sales-allocation-v2"];
	return ((P.proposal && P.proposal.orders) || [])
		.filter((o) => o.sales_order !== P.selected_order)
		.reduce((t, o) => t + ((o.lines || {})[soi] || 0), 0);
};
frappe.pages["sales-allocation-v2"]._proposed_order = function (name) {
	const P = frappe.pages["sales-allocation-v2"];
	return P.proposal && P.proposal.orders.find((x) => x.sales_order === name);
};
// What a card shows: the share covered -- allocated plus proposed by Allocate all -- by boxes when the order
// has them, else by stems.
frappe.pages["sales-allocation-v2"]._cover_pct = function (order, pct) {
	const P = frappe.pages["sales-allocation-v2"];
	if (order.boxes_required) {
		return Math.round((P._full_boxes(order) / order.boxes_required) * 100);
	}
	return Math.min(100, Math.round(pct + P._proposed_pct(order, pct)));
};
// Whole boxes an order has: allocated (saved), proposed by Allocate all, and -- for the open order -- what
// this session adds before it is confirmed.
frappe.pages["sales-allocation-v2"]._full_boxes = function (order) {
	const P = frappe.pages["sales-allocation-v2"];
	const waiting = P._proposed_order(order.name);
	// the open order's picks waiting confirmation are its session; another order's are its Pending Allocation
	// (until the open order's lines have loaded, its Pending Allocation stands in, so the card does not flicker)
	// while Allocate all runs, the boxes it has planned so far
	const planned = P._live_planned && P._live_planned[order.name];
	const live =
		order.name === P.selected_order && P._items_for === order.name
			? P._session_boxes()
			: planned != null
			? planned
			: (waiting && waiting.boxes) || 0;
	return Math.min(order.boxes_required || 0, (order.boxes_full || 0) + live);
};
// Whole boxes this session's picks add to the open order (a mixed box or bunch counted once).
frappe.pages["sales-allocation-v2"]._session_boxes = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const seen = new Set();
	let extra = 0;
	(P.order_items || []).forEach((it) => {
		const lines = P._box_lines(it);
		const key = lines
			.map((l) => l.sales_order_item)
			.sort()
			.join("|");
		if (seen.has(key)) return;
		seen.add(key);
		const boxes = (with_session) =>
			Math.min(
				...lines.map((l) => {
					const n = parseInt(l.custom_number_of_boxes, 10) || 1;
					const per_box = P._per_box(l);
					if (!per_box) return n;
					const have =
						(l.total_allocated_qty || 0) +
						(with_session ? P._session_qty(l.sales_order_item) : 0);
					return Math.min(n, Math.floor(have / per_box + 1e-9));
				})
			);
		extra += boxes(true) - boxes(false);
	});
	return extra;
};

frappe.pages["sales-allocation-v2"]._cover_color = function (pct) {
	return pct >= 100 ? "var(--good)" : pct > 0 ? "var(--warn)" : "var(--ink-mute)";
};
frappe.pages["sales-allocation-v2"]._proposed_pct = function (order, pct) {
	const P = frappe.pages["sales-allocation-v2"];
	const o = P.proposal && P.proposal.orders.find((x) => x.sales_order === order.name);
	if (!o) return 0;
	// By boxes when the order has them (what the card counts), else by stems.
	const share = order.boxes_required
		? o.boxes / order.boxes_required
		: order.ordered_stems
		? o.stems / order.ordered_stems
		: 0;
	return Math.round(Math.min(100 - pct, share * 100));
};
frappe.pages["sales-allocation-v2"]._load_proposal = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_location || !P.filters.delivery_start) {
		P._set_proposal(null);
		return Promise.resolve();
	}
	const key = P._short_key();
	// a promise: what waits on it (the release dialog) opens on the server's full proposal
	return new Promise((done) =>
		frappe.call({
			method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.get_day_proposal",
			args: P._day_args(),
			callback: (r) => {
				if (key === P._short_key()) P._set_proposal(r.message);
			},
			always: () => done(),
		})
	);
};
frappe.pages["sales-allocation-v2"]._update_confirm_btn = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const orders = (P.proposal && P.proposal.orders) || [];
	const unsaved =
		P.selected_order &&
		(P.allocations || []).length &&
		!orders.some((o) => o.sales_order === P.selected_order)
			? 1
			: 0;
	const count = orders.length + unsaved;
	$("#confirmDayV2").prop("disabled", !count);
	$("#confirmDayV2 .cnt")
		.text(count)
		.toggle(count > 0);
};
frappe.pages["sales-allocation-v2"].confirm_day_allocation = function () {
	const P = frappe.pages["sales-allocation-v2"];
	// the open order's picks are saved first, so the list is everything waiting confirmation
	P._hold_session().then(() => P._open_confirm_dialog());
};
// An order waiting release that has a line with no team (never planned, none chosen).
// The order's team in the release dialog: its team (one for the whole order), changeable; "Choose a
// team" when it has none, or when its lines go to different teams.
// The order's team in the release dialog: its name and a pencil; the pencil opens the team list
// under the name, nothing in the row moves.
frappe.pages["sales-allocation-v2"]._team_select = function (o, i) {
	const P = frappe.pages["sales-allocation-v2"];
	const esc = frappe.utils.escape_html;
	const named = (o.team_names || []).filter(Boolean);
	const shown = P._needs_team(o) ? __("No team") : named.join(", ");
	return `<span class="ca-team-cell" data-i="${i}">
		<span class="ca-team-name${P._needs_team(o) ? " is-none" : ""}">${esc(shown)}</span>
		<button type="button" class="ca-team-edit" title="${__("Change team")}" aria-label="${__(
		"Change team"
	)}" aria-haspopup="listbox"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg></button>
	</span>`;
};
// A mix or a straight line can go to its own team: one order packed by several teams.
frappe.pages["sales-allocation-v2"]._group_team = function (g, i, gi) {
	const esc = frappe.utils.escape_html;
	return `<span class="ca-team-cell" data-i="${i}" data-g="${gi}">
		<span class="ca-team-name${g.team ? "" : " is-none"}">${esc(g.team || __("No team"))}</span>
		<button type="button" class="ca-team-edit" title="${__("Change team")}" aria-label="${__(
		"Change team"
	)}" aria-haspopup="listbox"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg></button>
	</span>`;
};
// Downgrades: every line still short that longer stems on the shelves could fill, and whether taking them
// shorts an order that ordered that longer length. Those that short nobody are ticked.
// After Allocate all: 1. the orders the cut stage held back, confirmed by the planner; 2. the short lines
// another variety can fill, substituted or added to; 3. the downgrades, each with its reason.
// `scope` {sales_order, day}: the same steps for one order (Auto-allocate order), on its own delivery day.
// `normal`: the stems the run itself allocated (without any bypass); the steps' own are added as they go and
// shown together at the end.
frappe.pages["sales-allocation-v2"]._steps_after_allocate = function (scope, normal) {
	const P = frappe.pages["sales-allocation-v2"];
	const location = P.selected_location;
	if (scope && !scope.sales_order) scope = null;
	const tally = { normal: normal || 0, bypass: 0, substitutes: 0, downgrades: 0 };
	const TALLY_OF = {
		apply_cut_stage: "bypass",
		allocate_orders: "substitutes",
		apply_downgrades: "downgrades",
	};
	const only = scope ? { sales_orders: JSON.stringify([scope.sales_order]) } : {};
	const call = (method, args) =>
		new Promise((res) =>
			frappe.call({
				method:
					"upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation." +
					method,
				args: {
					...P._day_args(),
					...(scope && scope.day
						? { delivery_start: scope.day, delivery_end: scope.day }
						: {}),
					...(args || {}),
				},
				callback: (r) => {
					const m = r && r.message;
					if (m && TALLY_OF[method]) tally[TALLY_OF[method]] += parseFloat(m.stems) || 0;
					res(m);
				},
				error: () => res(null),
				// after the answer's own callback: a read that failed without one still lets the wizard go on
				always: () => setTimeout(() => res(null), 50),
			})
		);
	const n = (v) => Math.round(v || 0).toLocaleString();
	// the bar stays up between the steps, so the run never looks finished before it is
	const busy = (stage) => P._day_progress(0, 1, "", undefined, stage);
	const live = () => P.selected_location === location;
	// what each part allocated, before the panel closes
	const finish = () => {
		if (!live()) return close();
		const rows = [
			[__("Allocation"), tally.normal],
			[__("Bypass cut stage"), tally.bypass],
			[__("Substitutes and added varieties"), tally.substitutes],
			[__("Downgrades"), tally.downgrades],
		];
		const total = rows.reduce((t, x) => t + x[1], 0);
		P._step_dialog(
			__("Allocated"),
			`<div class="mv-wrap"><table class="ds-donors mv-to sum-table"><thead><tr><th></th><th>${__(
				"Step"
			)}</th><th class="num">${__("Stems allocated")}</th></tr></thead><tbody>${rows
				.map(
					(x) =>
						`<tr><td></td><td>${x[0]}</td><td class="num">${
							x[1] > 0 ? `<strong>${n(x[1])}</strong>` : "—"
						}</td></tr>`
				)
				.join("")}<tr class="sum-total"><td></td><td><strong>${__(
				"Total, waiting release"
			)}</strong></td><td class="num"><strong>${n(
				total
			)}</strong></td></tr></tbody></table></div>`,
			__("Done"),
			(d) => d.next(),
			close,
			false
		);
	};
	const close = () => {
		P._flow_close = null;
		$("body").removeClass("sa2-steps");
		P._live_planned = null;
		P._live_planned_key = "";
		$("#dayStepV2").hide().empty();
		P._day_progress(1, 1, "", true);
		P._flow_area(false);
		P._load_shortages();
		// the order opens again once the saved allocation is read, so its picks show as allocated
		P._load_proposal().then(() => {
			P._after_day_change();
			if (scope) P.select_order(scope.sales_order);
		});
	};
	const downgrades = () => {
		busy("downgrades");
		call("get_downgrade_options", only).then((r) => {
			if (!live()) return finish();
			if (r && (r.options || []).length)
				P._downgrade_dialog(r.options, r.reasons, call, busy, finish);
			else finish();
		});
	};
	const substitutes = () => {
		busy("substitutes");
		(scope
			? call("get_order_substitute_candidates", { sales_order: scope.sales_order })
			: call("get_substitute_candidates")
		).then((list) => {
			if (!live()) return finish();
			if (list && list.length) P._substitute_step(list, call, downgrades);
			else downgrades();
		});
	};
	P._flow_close = close;
	busy("cutcheck");
	call("get_cut_stage_candidates", only).then((list) => {
		if (!live()) return finish();
		P._cut_stage_step(list || [], (chosen) => {
			if (!chosen.length) return substitutes();
			busy("bypass");
			call("apply_cut_stage", { sales_orders: JSON.stringify(chosen) }).then((r) => {
				if (r && r.stems)
					frappe.show_alert({
						message: __("Allocated {0} stems of another cut stage to {1} orders", [
							n(r.stems),
							r.orders || 0,
						]),
						indicator: "green",
					});
				substitutes();
			});
		});
	});
};
// One step of Allocate all, in the detail panel: its table, Skip, and the step's action.
frappe.pages["sales-allocation-v2"]._step_dialog = function (
	title,
	body,
	primary,
	on_primary,
	on_next,
	skip
) {
	const P = frappe.pages["sales-allocation-v2"];
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	$("#dayLoadV2").hide();
	$("body").addClass("sa2-steps");
	const $w = $("#dayStepV2")
		.html(
			`<div class="sa-step__head"><h3>${esc(
				title
			)}</h3><button type="button" class="sa-mini-btn sa-step__close">${__(
				"Close"
			)}</button></div>
			<div class="sa-step__body">${body}</div>
			<div class="sa-step__foot">${
				skip === false
					? ""
					: `<button type="button" class="sa-mini-btn sa-step__skip">${esc(
							skip || __("Skip")
					  )}</button>`
			}<button type="button" class="sa-mini-btn primary sa-step__go">${esc(
				primary
			)}</button></div>`
		)
		.css("display", "flex");
	let moved = false;
	const d = {
		$wrapper: $w,
		next: () => {
			if (moved) return;
			moved = true;
			on_next();
		},
	};
	$w.find(".sa-step__skip").on("click", () => d.next());
	// Close stops the wizard here: what was saved so far stays, the next steps are not shown
	$w.find(".sa-step__close").on("click", () => {
		moved = true;
		(P._flow_close || (() => $w.hide().empty()))();
	});
	$w.find(".sa-step__go").on("click", () => on_primary(d));
	return d;
};
// Step 1: enable the cut-stage bypass for the orders still short that buckets of another cut stage fill.
frappe.pages["sales-allocation-v2"]._cut_stage_step = function (list, next) {
	let chosen = [];
	const P = frappe.pages["sales-allocation-v2"];
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const n = (v) => Math.round(v || 0).toLocaleString();
	const rows = list
		.map(
			(o, i) => `<tr>
				<td><input type="checkbox" data-c="${i}" checked></td>
				<td><strong>${esc(P._order_label(o.order_name))}</strong><div class="ds-so">${esc(
				o.customer
			)}</div></td>
				<td>${o.lines
					.map(
						(l) =>
							`<div>${esc(l.variety)} ${esc(l.length)} · <span class="ds-so">${esc(
								l.cut_stage || "—"
							)} → ${esc(l.stages.join(", ") || "—")}</span> · ${n(l.stems)}</div>`
					)
					.join("")}</td>
				<td class="num">${n(o.boxes)}</td>
				<td class="num">${n(o.stems)}</td>
			</tr>`
		)
		.join("");
	if (!list.length) {
		P._step_dialog(
			__("Enable bypass cut stage?"),
			`<div class="sa-step__msg">${__("No orders require a cut stage bypass.")}</div>`,
			__("Continue"),
			(d) => d.next(),
			() => next([]),
			false
		);
		return;
	}
	const d = P._step_dialog(
		__("Enable bypass cut stage?"),
		`<div class="sa-step__msg">${
			list.length === 1
				? __(
						"1 order has stock at another cut stage. Allocate it without checking the cut stage?"
				  )
				: __(
						"{0} orders have stock at another cut stage. Allocate them without checking the cut stage?",
						[list.length]
				  )
		}</div><div class="mv-wrap"><table class="ds-donors mv-to cs-table"><thead><tr><th><input type="checkbox" class="cs-all" checked></th><th>${__(
			"Order"
		)}</th><th>${__("Ordered cut stage → buckets")}</th><th class="num">${__(
			"Boxes"
		)}</th><th class="num">${__(
			"Stems"
		)}</th></tr></thead><tbody>${rows}</tbody></table></div>`,
		__("Yes"),
		(d) => {
			chosen = [];
			d.$wrapper.find("input[data-c]:checked").each(function () {
				chosen.push(list[$(this).data("c")].sales_order);
			});
			d.next();
		},
		() => next(chosen)
	);
	d.$wrapper.find(".cs-all").on("change", function () {
		d.$wrapper.find("input[data-c]").prop("checked", this.checked);
	});
};
// Step 2: the short lines another variety can fill. Each button acts at once on its line: Add variety keeps
// what the line holds and adds the variety for the rest; Substitute replaces the line's variety. The order
// is then allocated again and its lines read again.
frappe.pages["sales-allocation-v2"]._substitute_step = function (list, call, next) {
	const P = frappe.pages["sales-allocation-v2"];
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const n = (v) => Math.round(v || 0).toLocaleString();
	const open = new Set();
	const done = {};
	// lines already substituted or added to here: no second suggestion, just what was done
	const handled = {};
	let place_of = null;
	let lines = list.slice();
	// each line's shortage when the step opened (a line made here: when it first showed)
	const first_short = {};
	const note_first = () =>
		lines.forEach((x) => {
			if (!(x.sales_order_item in first_short)) first_short[x.sales_order_item] = x.short;
		});
	note_first();
	// what the best of Substitute (the line on the variety with the most free stems) or Add variety (that
	// variety for the stems the line cannot fill itself) leaves short: {left, how}
	const most_adds = (x) => Math.max(0, ...(x.options || []).map((o) => o.adds || 0));
	const best = (x) => {
		const most = Math.max(0, ...(x.options || []).map((o) => o.free || 0));
		const by_sub = Math.max(0, (x.needed || x.short) - most);
		const adds = Math.max(0, ...(x.options || []).map((o) => o.adds || 0));
		const by_add = adds > 0 ? Math.max(0, x.short - adds) : x.short;
		return by_add <= by_sub
			? { left: by_add, how: __("Add variety") }
			: { left: by_sub, how: __("Substitute") };
	};
	// a line in a mix whose own variety has the stock: it is short only because another variety of its mix
	// is; that variety is the one to add to or substitute
	const waits_on = (x) =>
		lines
			.filter(
				(y) =>
					x.mix_group &&
					y.sales_order === x.sales_order &&
					y.mix_group === x.mix_group &&
					y.sales_order_item !== x.sales_order_item &&
					y.add_stems > 0
			)
			.map((y) => y.variety);
	const cover_cell = (x) => {
		if (handled[x.sales_order_item]) return "<td></td>";
		if (!(x.add_stems > 0)) {
			const w = waits_on(x);
			return `<td><span class="ds-why ds-why--ok">${
				w.length
					? __("Own stock · waiting on {0}", [esc(w.join(", "))])
					: __("Own stock fills it")
			}</span></td>`;
		}
		const b = best(x);
		return `<td>${
			b.left <= 0.5
				? `<span class="ds-why ds-why--ok">${__("{0} fills it", [b.how])}</span>`
				: b.left < x.short - 0.5
				? `<span class="ds-why">${__("{0} leaves {1}", [b.how, n(b.left)])}</span>`
				: `<span class="ds-why">${__("Not enough stock")}</span>`
		}</td>`;
	};
	const line = (x, i) => `<tr class="ss-line" data-of="${esc(x.sales_order)}" ${
		open.has(x.sales_order) ? "" : "hidden"
	}>
				<td>${esc(x.variety)} ${esc(x.length)}${
		x.mix
			? `<div class="ds-so">${esc(x.mix)} · ${
					x.boxes === 1 ? __("1 box") : __("{0} boxes", [x.boxes])
			  }</div>`
			: ""
	}</td>
				<td></td>
				<td class="num">${n(x.needed)}</td>
				<td class="num">${n(x.held)}</td>
				<td class="num ds-so">${n(first_short[x.sales_order_item])}</td>
				<td class="num"><strong>${n(x.short)}</strong></td>
				${cover_cell(x)}
				${
					handled[x.sales_order_item]
						? `<td class="ss-acts" colspan="2"><span class="ss-handled">${esc(
								handled[x.sales_order_item]
						  )}</span></td>`
						: `<td class="ss-acts"><button type="button" class="sa-mini-btn ss-act" data-a="${i}" data-act="replace">${__(
								"Substitute"
						  )}</button></td><td class="ss-acts">${
								most_adds(x) > 0
									? `<button type="button" class="sa-mini-btn ss-act" data-a="${i}" data-act="add">${__(
											"Add · {0}",
											[n(most_adds(x))]
									  )}</button>`
									: ""
						  }</td>`
				}
			</tr>`;
	const body = () => {
		// one row per order, opening to its short lines
		const orders = [];
		lines.forEach((x, i) => {
			let o = orders.find((y) => y.sales_order === x.sales_order);
			if (!o)
				orders.push(
					(o = {
						sales_order: x.sales_order,
						order_name: x.order_name,
						customer: x.customer,
						lines: [],
					})
				);
			o.lines.push(i);
		});
		Object.keys(done).forEach((so) => {
			if (!orders.some((o) => o.sales_order === so))
				orders.push({ ...done[so].order, lines: [] });
		});
		// what each line and order leaves short at best: the ones that can be filled come first
		const line_left = (i) =>
			handled[lines[i].sales_order_item] ? lines[i].short : best(lines[i]).left;
		orders.forEach((o) => {
			o.lines.sort((a, b) => line_left(a) - line_left(b) || a - b);
			o.left = o.lines.reduce((t, i) => t + line_left(i), 0);
		});
		// sorted once, when the step opens: an order keeps its place after its lines change, so the list
		// does not jump while the planner works down it
		if (!place_of) {
			orders.sort((a, b) => (a.left > 0.5) - (b.left > 0.5) || a.left - b.left);
			place_of = {};
			orders.forEach((o, k) => (place_of[o.sales_order] = k));
		} else
			orders.sort(
				(a, b) =>
					(place_of[a.sales_order] ?? 1e9) - (place_of[b.sales_order] ?? 1e9) ||
					a.left - b.left
			);
		const rows = orders
			.map(
				(o) => `<tr class="ss-order${
					open.has(o.sales_order) ? " is-open" : ""
				}" data-x="${esc(o.sales_order)}">
				<td><button type="button" class="ca-exp" data-x="${esc(o.sales_order)}" aria-expanded="${open.has(
					o.sales_order
				)}" aria-label="${__(
					"Show the lines"
				)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button><strong class="ca-oname" data-x="${esc(
					o.sales_order
				)}">${esc(P._order_label(o.order_name))}</strong> <span class="ds-so">${
					o.lines.length === 1 ? __("1 line") : __("{0} lines", [o.lines.length])
				}</span></td>
				<td class="ds-so ss-cust">${esc(o.customer)}</td>
				<td class="num">${n(o.lines.reduce((t, i) => t + (lines[i].needed || 0), 0))}</td>
				<td class="num">${n(o.lines.reduce((t, i) => t + (lines[i].held || 0), 0))}</td>
				<td class="num ds-so">${n(
					o.lines.reduce((t, i) => t + (first_short[lines[i].sales_order_item] || 0), 0)
				)}</td>
				<td class="num"><strong>${n(o.lines.reduce((t, i) => t + (lines[i].short || 0), 0))}</strong></td>
				<td>${(() => {
					// the order's short stems left once each line takes its best option
					const left = o.lines.reduce(
						(t, i) =>
							t +
							(handled[lines[i].sales_order_item]
								? lines[i].short
								: best(lines[i]).left),
						0
					);
					return left <= 0.5
						? `<span class="ds-why ds-why--ok">${__("Can be filled")}</span>`
						: `<span class="ds-why">${__("{0} left at best", [n(left)])}</span>`;
				})()}</td>
				<td colspan="2">${(done[o.sales_order] ? done[o.sales_order].notes : [])
					.map((t) => `<span class="ds-why ds-why--ok">${esc(t)}</span>`)
					.join(" ")}</td>
			</tr>${o.lines.map((i) => line(lines[i], i)).join("")}`
			)
			.join("");
		return `<div class="mv-wrap"><table class="ds-donors mv-to ss-table"><thead><tr><th>${__(
			"Order · variety"
		)}</th><th>${__("Customer")}</th><th class="num">${__(
			"Required"
		)}</th><th class="num">${__("Allocated")}</th><th class="num">${__(
			"Short at start"
		)}</th><th class="num">${__("Short now")}</th><th>${__("Can cover")}</th><th>${__(
			"Substitute"
		)}</th><th>${__("Add variety")}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
	};
	const d = P._step_dialog(
		__("Substitutes"),
		body(),
		__("Continue"),
		(d) => d.next(),
		next,
		false
	);
	// the body is drawn again in place: where the planner had scrolled to stays
	const render = () => {
		const $b = d.$wrapper.find(".sa-step__body");
		const top = $b.scrollTop();
		$b.html(body());
		$b.scrollTop(top);
		bind();
	};
	const act = (i, kind, variety, stems) => {
		const x = lines[i];
		if (!variety) return;
		const $row = d.$wrapper.find(`button.ss-act[data-a="${i}"]`).closest("tr");
		$row.find("button, select").prop("disabled", true);
		$row.find(`button.ss-act[data-act="${kind}"]`).addClass("on").text(__("Working…"));
		const p =
			kind === "add"
				? call("add_variety", {
						sales_order_item: x.sales_order_item,
						location: P.selected_location,
						variety,
				  })
				: new Promise((res) =>
						frappe.call({
							method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.substitute_variety",
							args: {
								sales_order: x.sales_order,
								sales_order_item: x.sales_order_item,
								new_item_code: variety,
							},
							callback: (r) => {
								const m = r.message || {};
								if (m.success === false) {
									frappe.show_alert({
										message: m.message || __("Could not substitute."),
										indicator: "red",
									});
									return res(null);
								}
								res(m);
							},
							error: () => res(null),
						})
				  );
		p.then((r) => {
			if (!r) return render();
			const note =
				kind === "add"
					? __("{0} added to {1} · {2}", [variety, x.variety, n(stems)])
					: __("{0} now {1}", [x.variety, variety]);
			done[x.sales_order] = done[x.sales_order] || {
				order: {
					sales_order: x.sales_order,
					order_name: x.order_name,
					customer: x.customer,
				},
				notes: [],
			};
			done[x.sales_order].notes.push(note);
			if (kind === "add") {
				handled[x.sales_order_item] = __("{0} added", [variety]);
				(r.parts || [])
					.filter((pt) => pt.variety === variety)
					.forEach(
						(pt) => (handled[pt.sales_order_item] = __("Added · {0}", [x.variety]))
					);
			} else handled[x.sales_order_item] = __("Now {0}", [variety]);
			frappe.show_alert({ message: note, indicator: "green" });
			// the order is allocated again the way Allocate all does, then its short lines are read again
			return call("allocate_orders", { sales_orders: JSON.stringify([x.sales_order]) })
				.then(() =>
					call("get_order_substitute_candidates", {
						location: P.selected_location,
						sales_order: x.sales_order,
					})
				)
				.then((fresh) => {
					lines = lines
						.filter((y) => y.sales_order !== x.sales_order)
						.concat(fresh || []);
					note_first();
					open.clear();
					open.add(x.sales_order);
					render();
				});
		});
	};
	const bind = () => {
		// each action is confirmed first, so a stray click changes nothing
		d.$wrapper.find("button.ss-act").on("click", function () {
			const i = $(this).data("a");
			const kind = $(this).data("act");
			const x = lines[i];
			if (!x) return;
			const order = P._order_label(x.order_name);
			// the variety is chosen in the dialog (its colour and free stems), then confirmed
			P._substitute_short(x, kind === "add", (o, dlg) => {
				const variety = o.variety;
				// confirmed inside the same dialog: a second dialog on top darkens the page twice
				P._confirm_in_dialog(
					dlg,
					kind === "add"
						? __("Add {0} to {1} on {2} for {3} stems?", [
								`<b>${esc(variety)}</b>`,
								esc(`${x.variety} ${x.length}`),
								`<b>${esc(order)}</b>`,
								n(o.adds),
						  ])
						: __("Substitute {0} with {1} on {2}?", [
								esc(`${x.variety} ${x.length}`),
								`<b>${esc(variety)}</b>`,
								`<b>${esc(order)}</b>`,
						  ]),
					kind === "add" ? __("Yes, add") : __("Yes, substitute"),
					() => {
						dlg.hide();
						act(i, kind, variety, o.adds);
					}
				);
			});
		});
		// a click anywhere on an order's row opens or closes its lines
		d.$wrapper.find(".ss-table tr.ss-order").on("click", function () {
			const x = String($(this).data("x"));
			const $b = d.$wrapper
				.find(".ss-table .ca-exp")
				.filter((_i, b) => $(b).data("x") === x);
			const now = !open.has(x);
			// one order open at a time: opening one closes the others
			open.clear();
			d.$wrapper.find(".ss-table .ca-exp").attr("aria-expanded", false);
			d.$wrapper.find(".ss-table tr.ss-order").removeClass("is-open");
			$(this).toggleClass("is-open", now);
			d.$wrapper.find("tr.ss-line").prop("hidden", true);
			if (now) open.add(x);
			$b.attr("aria-expanded", now);
			d.$wrapper
				.find("tr.ss-line")
				.filter((_i, r) => $(r).data("of") === x)
				.prop("hidden", !now);
		});
	};
	bind();
};
// Ask in the dialog itself: its footer turns into the question with No and Yes; No puts it back.
frappe.pages["sales-allocation-v2"]._confirm_in_dialog = function (d, html, yes_label, on_yes) {
	const $foot = d.$wrapper.find(".modal-footer");
	const $bar = $(`<div class="sa-dlg-confirm">
		<div class="sa-dlg-confirm__q">${html}</div>
		<button type="button" class="sa-mini-btn sa-dlg-no">${__("No")}</button>
		<button type="button" class="sa-mini-btn primary sa-dlg-yes"></button>
	</div>`);
	$bar.find(".sa-dlg-yes").text(yes_label);
	const back = () => {
		$bar.remove();
		$foot.show();
		d.$wrapper.find("input[name=sub-to]").prop("disabled", false);
	};
	$foot.hide().after($bar);
	d.$wrapper.find("input[name=sub-to]").prop("disabled", true);
	$bar.find(".sa-dlg-no").on("click", back);
	$bar.find(".sa-dlg-yes").on("click", () => {
		back();
		on_yes();
	});
	d.$wrapper.one("hidden.bs.modal", () => $bar.remove());
};
frappe.pages["sales-allocation-v2"]._downgrade_dialog = function (
	list,
	reasons,
	call,
	busy,
	done
) {
	const P = frappe.pages["sales-allocation-v2"];
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const n = (v) => Math.round(v || 0).toLocaleString();
	const pick = (i) =>
		`<select class="dg-reason" data-r="${i}"><option value=""></option>${(reasons || [])
			.map((r) => `<option>${esc(r)}</option>`)
			.join("")}</select>`;
	const rows = list
		.map(
			(x, i) => `<tr>
				<td><input type="checkbox" data-d="${i}" ${x.shorts.length ? "" : "checked"}></td>
				<td><strong>${esc(P._order_label(x.order_name))}</strong><div class="ds-so">${esc(
				x.customer
			)}</div></td>
				<td>${esc(x.variety)}</td>
				<td>${esc(x.ordered_length)} → <strong>${esc(x.length)}</strong></td>
				<td class="num">${n(x.stems)}</td>
				<td>${pick(i)}</td>
				<td>${
					x.shorts.length
						? `<span class="ds-why">${__("Shorts")}</span> ${x.shorts
								.map(
									(s) =>
										`<span class="ds-so">${esc(
											P._order_label(s.order_name)
										)} · ${n(s.stems)}</span>`
								)
								.join(" ")}`
						: `<span class="ds-why ds-why--ok">${__("Shorts nobody")}</span>`
				}</td>
			</tr>`
		)
		.join("");
	const d = P._step_dialog(
		__("Downgrades"),
		`<div class="ds-line"><div class="mv-wrap"><table class="ds-donors mv-to dg-table">
			<thead><tr><th><input type="checkbox" class="dg-all"></th><th>${__("Order")}</th><th>${__(
			"Variety"
		)}</th><th>${__("Ordered → Downgrade")}</th><th class="num">${__("Stems")}</th><th>${__(
			"Reason"
		)} ${`<select class="dg-reason dg-all-r"><option value=""></option>${(reasons || [])
			.map((r) => `<option>${esc(r)}</option>`)
			.join("")}</select>`}</th><th>${__("Shorts another order")}</th></tr></thead>
			<tbody>${rows}</tbody>
		</table></div></div>`,
		__("Allocate downgrades"),
		(d) => {
			const picks = [];
			d.$wrapper.find("input[data-d]:checked").each(function () {
				const x = list[$(this).data("d")];
				x.buckets.forEach((b) =>
					picks.push({
						sales_order_item: x.sales_order_item,
						bucket_id: b.bucket_id,
						length: x.length,
						qty: b.qty,
						reason: d.$wrapper.find(`select[data-r="${$(this).data("d")}"]`).val(),
					})
				);
			});
			if (!picks.length) return d.next();
			if (picks.some((x) => !x.reason))
				return frappe.show_alert({
					message: __("Choose a downgrade reason for each line ticked."),
					indicator: "orange",
				});
			busy("dgapply");
			call("apply_downgrades", { picks: JSON.stringify(picks) }).then((m) => {
				if (m)
					frappe.show_alert({
						message: __("Downgraded {0} stems for {1} orders", [
							n(m.stems),
							m.orders || 0,
						]),
						indicator: "green",
					});
				d.next();
			});
		},
		done
	);
	d.$wrapper.find(".dg-all").on("change", function () {
		d.$wrapper.find("input[data-d]").prop("checked", this.checked);
	});
	// one reason for every line, or each its own
	d.$wrapper.find(".dg-all-r").on("change", function () {
		d.$wrapper.find("select[data-r]").val(this.value);
	});
};
frappe.pages["sales-allocation-v2"]._needs_team = function (o) {
	return !(o.team_names || []).length || (o.team_names || []).includes("");
};
frappe.pages["sales-allocation-v2"]._open_confirm_dialog = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const all = (P.proposal && P.proposal.orders) || [];
	if (!all.length) return;
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const n = (v) => Math.round(v || 0).toLocaleString();
	const ticked = () =>
		d.$wrapper
			.find("input[data-i]:checked")
			.map((i, el) => all[$(el).data("i")])
			.get();
	const d = new frappe.ui.Dialog({
		title: __("Release allocated orders"),
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "body" }],
		primary_action_label: __("Release"),
		primary_action: () => {
			const chosen = ticked().map((x) => x.sales_order);
			if (!chosen.length) {
				frappe.msgprint(__("Select at least one order."));
				return;
			}
			// orders with a line that has no team stay on the list until one is chosen; the rest go now
			const no_team = ticked().filter((x) => P._needs_team(x));
			const go = ticked()
				.filter((x) => !P._needs_team(x))
				.map((x) => x.sales_order);
			if (!go.length) {
				frappe.msgprint(
					__("Choose a packing team for {0}.", [
						no_team.map((x) => x.order_name || x.sales_order).join(", "),
					])
				);
				return;
			}
			if (no_team.length)
				frappe.show_alert({
					message: __("{0} orders without a team stay to be released", [no_team.length]),
					indicator: "orange",
				});
			chosen.splice(0, chosen.length, ...go);
			d.hide();
			P._listen_day_allocation();
			P._day_finished = false;
			P._day_progress(0, chosen.length, "", undefined, "release");
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.confirm_day_allocation",
				args: { ...P._day_args(), sales_orders: JSON.stringify(chosen) },
				callback: (r) =>
					r.message &&
					(r.message.queued
						? ((P._day_run = r.message.run || ""), P._resume_day_progress())
						: r.message.done && P._day_done(r.message)),
				error: () => P._day_progress_reset(),
			});
		},
		secondary_action_label: __("Discard"),
		secondary_action: () => {
			const chosen = ticked().map((x) => x.sales_order);
			if (!chosen.length) return;
			frappe.confirm(
				__("Discard what is allocated for {0} orders? Their buckets become free.", [
					chosen.length,
				]),
				() =>
					frappe.call({
						method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.discard_day_proposal",
						args: {
							location: P.selected_location,
							sales_orders: JSON.stringify(chosen),
						},
						callback: () => {
							d.hide();
							if (chosen.includes(P.selected_order)) {
								P._restore = null;
								P.clear_allocations(true);
							}
							P._load_proposal();
							P._after_day_change();
						},
					})
			);
		},
	});
	d.$wrapper.addClass("ufd-sa2");
	d.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal mv-modal");
	const rows = all
		.map(
			(o, i) => `<tr class="ca-order">
				<td><input type="checkbox" data-i="${i}" checked></td>
				<td><button type="button" class="ca-exp" data-x="${i}" aria-expanded="false" aria-label="${__(
				"Show what is released"
			)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button><strong class="ca-oname" data-x="${i}">${esc(
				P._order_label(o.order_name) || o.sales_order
			)}</strong>${o.order_name ? `<div class="ds-so">${esc(o.sales_order)}</div>` : ""}</td>
				<td>${esc(o.customer)}</td>
				<td>${esc(frappe.datetime.str_to_user(o.delivery_date))}</td>
				<td>${P._team_select(o, i)}</td>
				<td class="num">${n(o.boxes)}</td>
				<td>${
					o.order_boxes
						? `<span class="ds-why${
								o.full_after >= o.order_boxes ? " ds-why--ok" : ""
						  }">${
								o.full_after >= o.order_boxes ? __("Full") : __("Partial")
						  }</span> <span class="ds-so">${
								o.full_before != null && o.full_before !== o.full_after
									? n(o.full_before) + " → "
									: ""
						  }${n(o.full_after)}/${n(o.order_boxes)} ${__("boxes")}</span>`
						: ""
				}</td>
				<td class="num">${n(o.stems)}</td>
			</tr>${(o.groups || [])
				.map(
					(g, gi) => `<tr class="ca-group" data-of="${i}" hidden>
				<td></td>
				<td colspan="3">${
					g.kind === "line"
						? ""
						: `<button type="button" class="ca-exp ca-mexp" data-m="${i}:${gi}" aria-expanded="false" aria-label="${__(
								"Show the varieties"
						  )}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button>`
				}<span class="ca-kind">${
						g.kind === "line"
							? __("Straight")
							: g.kind === "box"
							? __("Mixed box")
							: __("Mixed bunch")
					}</span> <strong${
						g.kind === "line" ? "" : ` class="ca-mname" data-m="${i}:${gi}"`
					}>${esc(g.label)}</strong></td>
				<td>${P._group_team(g, i, gi)}</td>
				<td class="num">${n(g.boxes)}</td>
				<td></td>
				<td class="num">${n(g.stems)}</td>
			</tr>${
				g.kind === "line"
					? ""
					: g.lines
							.map(
								(
									l
								) => `<tr class="ca-group ca-var" data-of="${i}" data-mix="${i}:${gi}" hidden>
				<td></td>
				<td colspan="3">${esc(l.variety)} ${esc(l.length)}</td>
				<td></td><td></td><td></td>
				<td class="num">${n(l.stems)}</td>
			</tr>`
							)
							.join("")
			}`
				)
				.join("")}`
		)
		.join("");
	d.fields_dict.body.$wrapper.html(`
		<div class="ds-line">
			<div class="mv-wrap"><table class="ds-donors mv-to">
				<thead><tr>
					<th><input type="checkbox" class="ca-all" checked></th>
					<th>${__("Order")}</th><th>${__("Customer")}</th><th>${__("Delivery")}</th>
					<th>${__("Team")}</th><th class="num">${__("Boxes")}</th><th>${__(
		"Allocation"
	)}</th><th class="num">${__("Stems")}</th>
				</tr></thead>
				<tbody>${rows}</tbody>
				<tfoot><tr>
					<th></th><th colspan="4" class="ca-count"></th>
					<th class="num ca-boxes"></th><th></th><th class="num ca-stems"></th>
				</tr></tfoot>
			</table></div>
		</div>`);
	const totals = () => {
		const chosen = ticked();
		d.$wrapper.find(".ca-count").text(__("{0} orders", [chosen.length]));
		d.$wrapper.find(".ca-boxes").text(n(chosen.reduce((s, o) => s + (o.boxes || 0), 0)));
		d.$wrapper.find(".ca-stems").text(n(chosen.reduce((s, o) => s + (o.stems || 0), 0)));
	};
	d.$wrapper.find(".ca-all").on("change", function () {
		d.$wrapper.find("input[data-i]").prop("checked", this.checked);
		totals();
	});
	d.$wrapper.find("input[data-i]").on("change", totals);
	// each order opens to what it releases: its mixes, varieties together, and its straight lines
	// an order opens to its mixes and straight lines; a mix opens to its varieties
	const toggle = (x) => {
		const $b = d.$wrapper.find(`.ca-exp[data-x="${x}"]`);
		const open = $b.attr("aria-expanded") !== "true";
		$b.attr("aria-expanded", open);
		d.$wrapper.find(`tr.ca-group[data-of="${x}"]:not(.ca-var)`).prop("hidden", !open);
		if (!open) {
			d.$wrapper.find(`tr.ca-var[data-of="${x}"]`).prop("hidden", true);
			d.$wrapper.find(`tr.ca-group[data-of="${x}"] .ca-mexp`).attr("aria-expanded", "false");
		}
	};
	const toggle_mix = (m) => {
		const $b = d.$wrapper.find(`.ca-mexp[data-m="${m}"]`);
		const open = $b.attr("aria-expanded") !== "true";
		$b.attr("aria-expanded", open);
		d.$wrapper.find(`tr.ca-var[data-mix="${m}"]`).prop("hidden", !open);
	};
	// a click anywhere on an order's row opens it, on a mix's row opens the mix; the tick box and the team
	// keep their own clicks
	const own = (e) => $(e.target).closest("input, .ca-team-cell, .ca-team-menu").length;
	d.$wrapper.find("tr.ca-order").on("click", function (e) {
		if (!own(e)) toggle($(this).find(".ca-exp").data("x"));
	});
	d.$wrapper.find("tr.ca-group:not(.ca-var)").on("click", function (e) {
		const m = $(this).find(".ca-mexp").data("m");
		if (m != null && !own(e)) toggle_mix(m);
	});
	// the order's team, chosen or changed here, goes on every line it releases
	// the pencil opens the team list under the name; picking a team saves it for the whole order
	// the row being changed stands out while its team list is open
	const close_menu = () => {
		d.$wrapper.find(".ca-team-menu").remove();
		d.$wrapper.find("tr.is-editing").removeClass("is-editing");
	};
	d.$wrapper.find(".ca-team-edit").on("click", function (e) {
		e.stopPropagation();
		const $cell = $(this).closest(".ca-team-cell");
		const key = $cell.data("i") + ":" + ($cell.data("g") ?? "");
		const open = d.$wrapper.find(".ca-team-menu").data("i") === key;
		close_menu();
		if (open) return;
		const o = all[$cell.data("i")];
		const g = $cell.data("g") !== undefined ? (o.groups || [])[$cell.data("g")] : null;
		const esc = frappe.utils.escape_html;
		const named = (o.team_names || []).filter(Boolean);
		const current = g ? g.team || "" : !P._needs_team(o) && named.length === 1 ? named[0] : "";
		// only the teams that pack at this location (a team with no location can pack anywhere)
		const here = (t) =>
			!(P.team_locations || {})[t] || P.team_locations[t] === P.selected_location;
		const teams = [
			...new Set([
				...(P.available_teams || []).filter((t) => t && here(t)),
				...(current ? [current] : []),
			]),
		];
		const r = $cell[0].getBoundingClientRect();
		const $menu = $(
			`<div class="ca-team-menu" role="listbox" aria-label="${__("Packing team")}">${teams
				.map(
					(t) =>
						`<button type="button" role="option" class="ca-team-opt${
							t === current ? " is-on" : ""
						}" aria-selected="${t === current}" data-team="${esc(t)}"><span>${esc(
							t
						)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg></button>`
				)
				.join("")}</div>`
		)
			.data("i", key)
			.css({ top: r.bottom + 4, left: r.left, width: r.width }); // within the Team column
		d.$wrapper.find(".modal-content").append($menu);
		$cell.closest("tr").addClass("is-editing");
		// keep it on screen: open upwards when there is no room below
		const m = $menu[0].getBoundingClientRect();
		if (m.bottom > window.innerHeight - 8)
			$menu.css({ top: Math.max(8, r.top - m.height - 4) });
		($menu.find(".is-on")[0] || $menu.find(".ca-team-opt")[0] || {}).focus?.();
		$menu.on("click", ".ca-team-opt", function () {
			const team = $(this).data("team");
			close_menu();
			if (!team || team === current) return;
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.set_pending_team",
				args: {
					location: P.selected_location,
					sales_order: o.sales_order,
					team,
					// a mix or a straight line: only its lines; the order's row: every line
					lines: g ? JSON.stringify(g.sois || []) : null,
				},
				callback: () => {
					const groups = o.groups || [];
					(g ? [g] : groups).forEach((x) => {
						x.team = team;
						(x.sois || []).forEach((s) => ((o.teams = o.teams || {})[s] = team));
					});
					if (!g) o.team_names = [team];
					else {
						const ts = groups.map((x) => x.team || "");
						o.team_names = [...new Set(ts)];
					}
					// a team changed here stays marked until the dialog closes
					const mark = ($c, text, none) =>
						$c
							.find(".ca-team-name")
							.text(text)
							.toggleClass("is-none", !!none)
							.addClass("is-changed")
							.attr("title", __("Changed"));
					mark($cell, team);
					const $order = d.$wrapper.find(
						`.ca-team-cell[data-i="${$cell.data("i")}"]:not([data-g])`
					);
					if (g) {
						// the order's row: the teams its parts go to, red while a part still has none
						const set = o.team_names.filter(Boolean);
						mark(
							$order,
							set.length ? set.join(", ") : __("No team"),
							P._needs_team(o)
						);
						if (P._needs_team(o))
							$order.find(".ca-team-name").removeClass("is-changed");
					} else
						d.$wrapper
							.find(`.ca-team-cell[data-i="${$cell.data("i")}"][data-g]`)
							.each(function () {
								mark($(this), team);
							});
				},
			});
		});
	});
	// a click anywhere else, Escape or scrolling the list closes the team list
	d.$wrapper.on("click", (e) => {
		if (!$(e.target).closest(".ca-team-menu").length) close_menu();
	});
	d.$wrapper.on(
		"keydown",
		(e) =>
			e.key === "Escape" &&
			d.$wrapper.find(".ca-team-menu").length &&
			(e.stopPropagation(), close_menu())
	);
	// scrolling anywhere in the dialog (scroll does not bubble: listen while it comes down)
	d.$wrapper[0].addEventListener("scroll", close_menu, true);
	d.$wrapper.on("hide.bs.modal", close_menu);
	totals();
	d.show();
	// the dialog keeps the width it opens with: opening an order only makes it taller
	d.$wrapper.addClass("ca-release");
	const lock = () => {
		const md = d.$wrapper.find(".modal-dialog")[0];
		if (md && !md.style.width) md.style.width = Math.ceil(md.offsetWidth) + "px";
	};
	// measured once it is open (while it animates in it is still at its smallest)
	d.$wrapper.one("shown.bs.modal", lock);
	setTimeout(lock, 400);
};
// While Allocate all or Release runs, the page shows how far it is: the step, the share done and the order
// it is on. The orders come back when the job is done.
// The order name without the sales order's number at its end ("SQ SHARBATLY - RIYADH 44526-03584" ->
// "SQ SHARBATLY - RIYADH 44526"), as the box label prints it.
frappe.pages["sales-allocation-v2"]._order_label = function (name) {
	const s = String(name || "");
	return s.replace(/\s*-\s*\d{5}$/, "").trim() || s;
};
frappe.pages["sales-allocation-v2"]._day_progress = function (
	done,
	total,
	order,
	finished,
	stage
) {
	const P = frappe.pages["sales-allocation-v2"];
	$("#dayProgV2").hide();
	$("#autoAllocateDayV2, #confirmDayV2, #shortTabV2").prop("disabled", finished === undefined);
	P._day_busy = finished === undefined;
	$(".location-btn").prop("disabled", P._day_busy);
	// while Allocate all (and its steps) runs, the order list only scrolls: no order is opened
	$("body").toggleClass("sa2-busy", P._day_busy);
	if (finished !== undefined) {
		$("#dayLoadV2").hide().removeClass("done");
		return;
	}
	if (stage) P._day_stage = stage;
	const TITLE = {
		queued: __("Waiting to start"),
		prepare: __("Getting the orders"),
		order: __("Allocating the order"),
		cutcheck: __("Checking cut stages"),
		bypass: __("Allocating any cut stage"),
		substitutes: __("Finding substitutes"),
		subapply: __("Adding the varieties"),
		downgrades: __("Finding downgrades"),
		dgapply: __("Allocating downgrades"),
		allocate: __("Allocating orders"),
		save: __("Saving the allocation"),
		shortages: __("Checking shortages"),
		release: __("Releasing orders"),
	};
	const st = P._day_stage || "allocate";
	const pct = Math.max(0, Math.min(100, Math.round((100 * (done || 0)) / (total || 1))));
	// allocating: n of the window's orders; releasing: n of the chosen orders
	const of_orders =
		st === "allocate" ? Math.floor(((total || 1) - 1) / 2) : st === "release" ? total : 0;
	const what = [
		of_orders && st !== "queued"
			? __("{0} of {1} orders", [Math.min(done || 0, of_orders), of_orders])
			: "",
		order || "",
	]
		.filter(Boolean)
		.join(" · ");
	const $run = $("#dayLoadV2");
	$run.find(".sa-day-run__title").text(TITLE[st] || TITLE.allocate);
	$run.find(".sa-day-run__bar i").css("width", pct + "%");
	$run.find(".pct").text(pct + "%");
	$run.find(".what").text(what);
	$run.toggleClass(
		"prep",
		[
			"prepare",
			"order",
			"cutcheck",
			"bypass",
			"substitutes",
			"subapply",
			"downgrades",
			"dgapply",
		].includes(st)
	);
	$("#dayStepV2").hide();
	if (P.short_mode) P.toggle_shortages(false);
	$run.css("display", "flex");
};
frappe.pages["sales-allocation-v2"]._after_day_change = function () {
	const P = frappe.pages["sales-allocation-v2"];
	P.allocations = [];
	P.selected_order = null;
	P.order_items = [];
	P.selected_item = null;
	P.item_teams = {};
	P.render_allocation_grid();
	P.load_sales_orders();
};
// ─── SHORTAGES TAB: the window's orders still short, their varieties, and buckets to take ───
frappe.pages["sales-allocation-v2"]._short_key = function () {
	return JSON.stringify(frappe.pages["sales-allocation-v2"]._day_args());
};
frappe.pages["sales-allocation-v2"].toggle_shortages = function (on) {
	const P = frappe.pages["sales-allocation-v2"];
	P.short_mode = on === undefined ? !P.short_mode : on;
	$("#shortTabV2").toggleClass("active", P.short_mode);
	$("#shortViewV2").toggle(P.short_mode);
	$("#mainShellV2").toggle(!P.short_mode);
	if (P.short_mode) P._load_shortages();
};
frappe.pages["sales-allocation-v2"]._load_shortages = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_location || !P.filters.delivery_start) {
		P.shortages = [];
		$("#shortTabV2 .cnt").hide();
		$("#shortViewV2 .sv-count").text("");
		$("#shortListV2").html(
			`<div class="empty-state"><p>${__("Select a location to begin.")}</p></div>`
		);
		$("#shortDetailV2").empty();
		return;
	}
	const key = P._short_key();
	// shortages already read for this location and window show at once; the fresh read replaces them
	const have = P._shortages_key === key && Array.isArray(P.shortages);
	if (P.short_mode) {
		if (have) P._render_shortages();
		else {
			$("#shortListV2").html('<div class="sv-skel"></div>'.repeat(5));
			$("#shortDetailV2").html(
				'<div class="sv-body"><div class="sv-skel"></div><div class="sv-skel"></div></div>'
			);
		}
	}
	// one read at a time for the same window: a second ask waits for the one already on its way
	if (P._short_inflight === key) return;
	P._short_inflight = key;
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.get_day_shortages",
		args: P._day_args(),
		callback: (r) => {
			if (key === P._short_key()) P._set_shortages(r.message || []);
		},
		always: () => {
			if (P._short_inflight === key) P._short_inflight = null;
		},
	});
};
frappe.pages["sales-allocation-v2"]._set_shortages = function (shortages) {
	const P = frappe.pages["sales-allocation-v2"];
	// Only lines really short: boxes the free buckets cannot reach, or stems no bucket has.
	P.shortages = (shortages || []).filter((x) => {
		const gb = x.group_boxes || {};
		return (
			gb.needed > (gb.coverable || gb.covered || 0) || (x.short || 0) - (x.usable || 0) > 0.5
		);
	});
	shortages = P.shortages;
	P._shortages_key = P._short_key();
	const orders = [...new Set(shortages.map((x) => x.sales_order))];
	$("#shortTabV2 .cnt")
		.text(orders.length)
		.toggle(orders.length > 0);
	if (!orders.includes(P.short_order)) P.short_order = orders[0] || null;
	if (P.short_mode) P._render_shortages();
};
// Where stock not on the shelf yet is: one row per stage, farm and warehouse, most stems first.
frappe.pages["sales-allocation-v2"]._coming_places = function (places, esc, n) {
	if (!(places || []).length) return "";
	const STAGE = {
		received: __("Received"),
		quarantine: __("Quarantine"),
		harvested: __("Harvested"),
	};
	return `<table class="ds-places"><tbody>${places
		.map(
			(r) => `<tr>
				<td><span class="ds-stage ds-stage--${esc(r.stage)}">${esc(STAGE[r.stage] || r.stage)}</span></td>
				<td>${esc(r.farm)}</td>
				<td>${esc(r.warehouse)}</td>
				<td class="num">${__("{0} buckets", [n(r.buckets)])}</td>
				<td class="num"><strong>${n(r.stems)}</strong></td>
			</tr>`
		)
		.join("")}</tbody></table>`;
};
frappe.pages["sales-allocation-v2"]._render_shortages = function () {
	const P = frappe.pages["sales-allocation-v2"];
	$("#svTipV2").removeClass("is-on");
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const n = (v) => Math.round(v || 0).toLocaleString();
	// Boxes short for an order: needed less the whole boxes covered (a mixed box or bunch counted once).
	const boxes_of = (lines) => {
		const ob = lines[0].order_boxes || { needed: 0, covered: 0, coverable: 0 };
		return ob.needed - (ob.coverable || ob.covered);
	};
	const by_order = {};
	(P.shortages || []).forEach((x) =>
		(by_order[x.sales_order] = by_order[x.sales_order] || []).push(x)
	);
	// stems of the line's variety and length not on the shelf yet: received, in quarantine, harvested
	const coming = (x) => {
		const c = x.incoming || {};
		return (c.received || 0) + (c.quarantine || 0) + (c.harvested || 0);
	};
	const names = Object.keys(by_order);
	const total = names.reduce((s, so) => s + boxes_of(by_order[so]), 0);
	$("#shortViewV2 .sv-count").text(
		names.length ? __("{0} orders · {1} boxes", [names.length, n(total)]) : ""
	);
	if (!names.length) {
		$("#shortListV2").html(`<div class="empty-state"><p>${__("No shortages.")}</p></div>`);
		$("#shortDetailV2").html(
			`<div class="sv-body"><div class="empty-state"><p>${__(
				"Every order is fully allocated."
			)}</p></div></div>`
		);
		return;
	}
	$("#shortListV2").html(
		names
			.map((so) => {
				const lines = by_order[so];
				const ob0 = lines[0].order_boxes || { needed: 0, covered: 0 };
				// Covered: boxes allocated or proposed, plus those the free buckets can still fill.
				const ob = { needed: ob0.needed, covered: ob0.coverable || ob0.covered };
				const short = boxes_of(lines);
				// a short card: the order, who and when, how far its boxes are, what is missing
				const pct = ob.needed ? Math.round((100 * ob.covered) / ob.needed) : 0;
				return `<div class="sales-order-card sv-card ${
					so === P.short_order ? "selected" : ""
				}" data-order="${esc(so)}">
					<div class="soc-top"><span class="soc-id">${esc(lines[0].order_name || so)}</span></div>
					<div class="soc-line">${esc(lines[0].customer)} · ${esc(
					frappe.datetime.str_to_user(lines[0].delivery_date)
				)}</div>
					<div class="sv-prog"><i style="width:${pct}%"></i></div>
					<div class="sv-meta"><span>${__("{0}/{1} boxes", [n(ob.covered), n(ob.needed)])}</span>${
					short > 0
						? `<span class="sv-st sv-st--bad">${
								short === 1 ? __("1 box short") : __("{0} boxes short", [n(short)])
						  }</span>`
						: `<span class="sv-st sv-st--ok">${__("In stock")}</span>`
				}</div>
				</div>`;
			})
			.join("")
	);
	$("#shortListV2 .sales-order-card").on("click", function () {
		P.short_order = $(this).data("order");
		P._render_shortages();
	});
	// hovering a card: its varieties, the stems each still needs and what is on its way
	$("#shortListV2 .sales-order-card")
		.on("mouseenter", function () {
			const ls = by_order[$(this).data("order")] || [];
			if (!ls.length) return;
			const r = this.getBoundingClientRect();
			let $tip = $("#svTipV2");
			if (!$tip.length)
				$tip = $('<div id="svTipV2" class="sv-tip" role="tooltip"></div>').appendTo(
					".ufd-sa2"
				);
			$tip.html(
				`<table class="sv-tip__tbl"><thead><tr><th></th><th>${__("To fill")}</th><th>${__(
					"On the way"
				)}</th></tr></thead><tbody>${ls
					.map((x) => {
						const fill = Math.max(0, (x.short || 0) - (x.usable || 0));
						return `<tr><td>${esc(x.item_name || x.item_code)} <small>${esc(
							x.length || ""
						)}</small></td><td class="is-bad">${fill ? n(fill) : "—"}</td><td>${
							coming(x) ? n(coming(x)) : "—"
						}</td></tr>`;
					})
					.join("")}</tbody></table>`
			)
				.css({ top: r.top, left: r.right + 10 })
				.addClass("is-on");
		})
		.on("mouseleave", () => $("#svTipV2").removeClass("is-on"));
	const lines = by_order[P.short_order] || [];
	const head = lines[0] || {};
	const cards = lines
		.map((x, i) => {
			// one row per order holding this variety: its buckets move together, as many as the line needs
			const groups = P._donor_groups(x);
			const donors = groups
				.map(
					(g, j) => `<tr>
						<td><strong>${esc(g.order_name || g.sales_order)}</strong>${
						g.order_name ? `<div class="ds-so">${esc(g.sales_order)}</div>` : ""
					}</td>
						<td>${esc(g.customer)}</td>
						<td>${esc(frappe.datetime.str_to_user(g.delivery_date))}</td>
						<td>${n(g.buckets.length)}</td>
						<td>${esc(g.lengths)}${g.longer ? ` <span class="ds-why">${__("Longer")}</span>` : ""}</td>
						<td class="num">${n(g.qty)}</td>
						<td class="num"><button class="sa-mini-btn primary" data-line="${i}" data-group="${j}">${__(
						"Move"
					)}</button></td>
					</tr>`
				)
				.join("");
			const gb = x.group_boxes || { needed: 0, covered: 0, coverable: 0 };
			const reach = gb.coverable || gb.covered;
			const fill = Math.max(0, (x.short || 0) - (x.usable || 0));
			const in_stock = !fill && gb.needed <= reach;
			// green for the boxes being processed, red for what has no stock yet
			const short_label = gb.needed
				? [
						`<span class="ds-ok">${__("{0} of {1} boxes being processed", [
							n(reach),
							n(gb.needed),
						])}</span>`,
						gb.needed > reach
							? `<span class="ds-bad">${__("{0} boxes no stock yet", [
									n(gb.needed - reach),
							  ])}</span>`
							: "",
						fill > 0
							? `<span class="ds-bad">${__("{0} stems to fill", [n(fill)])}</span>`
							: "",
				  ]
						.filter(Boolean)
						.join('<span class="ds-dot"> · </span>')
				: `<span class="ds-bad">${__("Short {0} of {1} stems", [
						n(x.short),
						n(x.needed),
				  ])}</span>`;
			return `<div class="ds-line">
				<div class="ds-head">
					<strong>${esc(x.item_name)}</strong>
					<span>${esc(x.length)}</span>
					${x.team ? `<span>${esc(x.team)}</span>` : ""}
					<span class="ds-why${in_stock ? " ds-why--ok" : ""}${
				String(x.box_group || "").startsWith("line:") ? "" : " ds-why--mix"
			}" data-mixof="${i}">${esc(x.why)}</span>
					<span class="ds-short${in_stock ? " ds-short--ok" : ""}">${short_label}</span>
					${
						in_stock
							? ""
							: `<button class="sa-mini-btn ds-sub" data-sub="${i}">${__(
									"Substitute"
							  )}</button>${
									// only a line short of its own stems takes another variety: one whose own
									// stock is there waits on the rest of its mix, not on a variety
									fill > 0
										? `<button class="sa-mini-btn ds-sub" data-add="${i}">${__(
												"Add variety"
										  )}</button>`
										: ""
							  }`
					}
				</div>
				${
					coming(x) > 0 && !in_stock
						? `<div class="ds-coming">${P._coming_places(
								x.incoming_places,
								esc,
								n
						  )}</div>`
						: ""
				}
				${
					donors
						? `<table class="ds-donors"><thead><tr>
							<th>${__("Allocated to")}</th><th>${__("Customer")}</th><th>${__("Delivery")}</th>
							<th>${__("Buckets")}</th><th>${__("Length")}</th>
							<th class="num">${__("Stems")}</th><th></th></tr></thead><tbody>${donors}</tbody></table>`
						: `<div class="ds-none">${__("No other order holds this variety")}</div>`
				}
			</div>`;
		})
		.join("");
	$("#shortDetailV2").html(`
		<header class="sv-head">
			<h2>${esc(head.order_name || P.short_order)}</h2>
			<span class="sv-sub">${esc(P.short_order)} · ${esc(head.customer)} · ${esc(
		frappe.datetime.str_to_user(head.delivery_date)
	)}${head.sequence ? " · #" + head.sequence : ""}</span>
			<button class="sa-mini-btn sv-open">${__("Open order")}</button>
		</header>
		<div class="sv-body">${cards}</div>`);
	$("#shortDetailV2 .sv-open").on("click", () => {
		const so = P.short_order;
		P.toggle_shortages(false);
		P.select_order(so);
	});
	$("#shortDetailV2 button[data-line]").on("click", function () {
		const x = lines[$(this).data("line")];
		P._move_from_order(x, P._donor_groups(x)[$(this).data("group")]);
	});
	$("#shortDetailV2 button[data-sub]").on("click", function () {
		P._substitute_short(lines[$(this).data("sub")]);
	});
	$("#shortDetailV2 button[data-add]").on("click", function () {
		P._substitute_short(lines[$(this).data("add")], true);
	});
	// a mixed box or bunch: hovering its tag lists the other varieties of the mix and what each still needs
	$("#shortDetailV2 .ds-why--mix")
		.on("mouseenter", function () {
			const x = lines[$(this).data("mixof")];
			const mix = lines.filter(
				(y) => y.box_group === x.box_group && y.sales_order_item !== x.sales_order_item
			);
			if (!mix.length) return;
			const r = this.getBoundingClientRect();
			let $tip = $("#svTipV2");
			if (!$tip.length)
				$tip = $('<div id="svTipV2" class="sv-tip" role="tooltip"></div>').appendTo(
					".ufd-sa2"
				);
			$tip.html(
				`<div class="sv-tip__so">${__(
					"Rest of the mix"
				)}</div><div class="sv-tip__mix">${mix
					.map((y) => {
						const fill = Math.max(0, (y.short || 0) - (y.usable || 0));
						return `<div><span>${esc(y.item_name || y.item_code)} <small>${esc(
							y.length || ""
						)}</small></span>${
							fill > 0
								? `<b class="is-bad">${__("{0} stems short", [n(fill)])}</b>`
								: `<b class="is-ok">${__("In stock")}</b>`
						}</div>`;
					})
					.join("")}</div>`
			)
				.css({ top: r.bottom + 6, left: r.left })
				.addClass("is-on");
		})
		.on("mouseleave", () => $("#svTipV2").removeClass("is-on"));
};
// Change a short line's variety: one its specification approves, or (no specification) a variety of the same
// crop whose stock is not moving.
frappe.pages["sales-allocation-v2"]._substitute_short = function (line, add, pick) {
	const P = frappe.pages["sales-allocation-v2"];
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const n = (v) => Math.round(v || 0).toLocaleString();
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.get_shortage_substitutes",
		args: { sales_order_item: line.sales_order_item, location: P.selected_location },
		callback: (r) => {
			const s = r.message || {};
			// the varieties that fill the line first, then those that fill part of it, then those that cannot
			// give a whole box; each group keeps the server's order
			const gives = (o) => (add ? o.adds || 0 : Math.min(o.free, s.needed));
			const group = (o) =>
				(add ? s.add_stems : s.needed) - gives(o) <= 0.5 ? 0 : gives(o) > 0 ? 1 : 2;
			const opts = (s.options || [])
				.map((o, k) => [o, k])
				.sort((a, b) => group(a[0]) - group(b[0]) || a[1] - b[1])
				.map((x) => x[0]);
			const GROUPS = [
				__("Fills the line"),
				__("Fills part of the line"),
				__("Not enough free"),
			];
			const spec = s.source === "spec";
			// nothing to offer: say why in the board's own dialog, not Frappe's message box
			if (!opts.length) {
				const why = spec
					? __("The specification approves no other variety for {0} {1}.", [
							s.variety,
							s.length,
					  ])
					: s.color
					? __("No free {0} stock at {1} or longer.", [s.color, s.length])
					: __("{0} has no colour set and no specification: nothing to match it with.", [
							s.variety,
					  ]);
				const e = new frappe.ui.Dialog({
					title: __(
						add ? "Add a variety to {0} {1} on {2}" : "Substitute {0} {1} on {2}",
						[s.variety, s.length, line.order_name || line.sales_order]
					),
					fields: [{ fieldtype: "HTML", fieldname: "body" }],
				});
				e.fields_dict.body.$wrapper.html(
					`<div class="empty-state"><p>${esc(why)}</p></div>`
				);
				e.$wrapper.addClass("ufd-sa2");
				e.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal mv-modal");
				e.show();
				return;
			}
			if (add && !(s.add_stems > 0)) {
				frappe.show_alert({
					message: __("{0} has the stock for its own share of the box", [s.variety]),
					indicator: "orange",
				});
				return;
			}
			const d = new frappe.ui.Dialog({
				title: __(add ? "Add a variety to {0} {1} on {2}" : "Substitute {0} {1} on {2}", [
					s.variety,
					s.length,
					line.order_name || line.sales_order,
				]),
				fields: [{ fieldtype: "HTML", fieldname: "body" }],
				// Add variety: the line keeps what its own variety holds or has free, the chosen variety takes
				// the rest. Substitute: the chosen variety replaces the line's.
				primary_action_label: add ? __("Add variety") : __("Substitute"),
				primary_action: () => {
					const o = opts[+d.$wrapper.find("input[name=sub-to]:checked").val()];
					if (!o) return;
					if (add && !(o.adds > 0))
						return frappe.show_alert({
							message: __("{0} has {1} stems free, less than the {2} it needs.", [
								o.variety,
								Math.round(o.free),
								Math.round(s.add_step || s.per_box),
							]),
							indicator: "orange",
						});
					if (pick) return pick(o, d);
					if (add) return P._add_variety(d, line, s, o);
					P._replace_variety(d, line, s, o);
				},
			});
			d.fields_dict.body.$wrapper.html(`
				<div class="sub-whole">
				${
					add
						? `<div class="sub-sum">
					<div class="sub-own"><span>${esc(s.variety)}</span><b>${n(s.needed - s.add_stems)}</b></div>
					<div><span>${__("Required")}</span><b>${n(s.needed)}</b></div>
					<div class="sub-pick"><span></span><b></b></div>
					<div class="sub-left"><span>${__("Still short")}</span><b></b></div>
				</div>`
						: `<div class="sub-sum">
					<div><span>${__("{0} now", [esc(s.variety)])}</span><b>${n(s.held)}</b></div>
					<div><span>${__("Required")}</span><b>${n(s.needed)}</b></div>
					<div class="sub-pick"><span></span><b></b></div>
					<div class="sub-left"><span>${__("Still short")}</span><b></b></div>
				</div>`
				}
				<div class="mv-wrap"><table class="ds-donors mv-to">
					<thead><tr><th></th><th>${__("Variety")}</th>
					<th class="num">${__("At {0}", [esc(s.length)])}</th>
					<th class="num">${__("Longer")}</th>
					<th class="num">${__("Non-moving")}</th>
					<th class="num">${__("Free stems")}</th>${
				add ? `<th class="num">${__("Adds")}</th>` : ""
			}<th class="num">${__("Total / required")}</th><th>${__("Result")}</th></tr></thead>
					<tbody>${opts
						.map(
							(o, i) => `${
								i === 0 || group(opts[i - 1]) !== group(o)
									? `<tr class="sub-grp"><td colspan="${add ? 9 : 8}">${esc(
											GROUPS[group(o)]
									  )}</td></tr>`
									: ""
							}<tr>
							<td><input type="radio" name="sub-to" value="${i}" ${i === 0 ? "checked" : ""}></td>
							<td><strong>${esc(o.variety)}</strong>${
								o.primary
									? ` <span class="ds-why ds-why--ok">${__("Primary")}</span>`
									: ""
							}${
								!spec && o.color
									? ` <span class="ds-so">${esc(o.color)}</span>`
									: ""
							}</td>
							<td class="num">${o.exact ? n(o.exact) : "—"}</td>
							<td class="num">${o.longer ? n(o.longer) : "—"}</td>
							<td class="num">${o.non_moving ? n(o.non_moving) : "—"}</td>
							<td class="num"><strong>${n(o.free)}</strong></td>${
								add ? `<td class="num"><strong>${n(o.adds)}</strong></td>` : ""
							}${P._sub_result(s, o, add)}
						</tr>`
						)
						.join("")}</tbody>
				</table></div>
				${
					!add && s.allocated > 0
						? `<p class="ds-none">${__(
								"{0} stems already allocated to this line are released.",
								[n(s.allocated)]
						  )}</p>`
						: ""
				}</div>
				`);
			// the chosen variety's own numbers: what it adds (or takes) and what the line is still short after
			const show_pick = () => {
				const o = opts[+d.$wrapper.find("input[name=sub-to]:checked").val()];
				if (!o) return;
				const gives = add ? o.adds || 0 : Math.min(o.free, s.needed);
				const left = add ? s.needed - P._add_total(s, o) : s.needed - gives;
				if (add) d.$wrapper.find(".sub-own b").text(n(P._add_own(s, o)));
				d.$wrapper
					.find(".sub-pick span")
					.text(add ? __("To {0}", [o.variety]) : __("{0} takes", [o.variety]));
				d.$wrapper.find(".sub-pick b").text(n(gives));
				d.$wrapper
					.find(".sub-left b")
					.text(n(Math.max(0, left)))
					.toggleClass("sub-short", left > 0.5);
			};
			d.$wrapper.find(".mv-to tbody tr").on("click", function () {
				$(this).find("input").prop("checked", true);
				show_pick();
			});
			show_pick();
			d.$wrapper.addClass("ufd-sa2");
			d.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal mv-modal");
			d.show();
		},
	});
};
// What the line ends with if this variety is taken: stems against what it needs, and whether that fills it.
// Add variety keeps the line's own stems and adds; Substitute starts the line again on the new variety.
// What the line keeps of its own variety when this variety is added: beside it in its boxes (share), what it
// holds and has free; taking whole later boxes, the boxes it keeps.
frappe.pages["sales-allocation-v2"]._add_own = function (s, o) {
	return o.mode === "share"
		? Math.min(s.needed - (o.adds || 0), (s.held || 0) + (s.own_free || 0))
		: s.needed - s.add_stems;
};
frappe.pages["sales-allocation-v2"]._add_total = function (s, o) {
	return Math.min(s.needed, frappe.pages["sales-allocation-v2"]._add_own(s, o) + (o.adds || 0));
};
frappe.pages["sales-allocation-v2"]._sub_result = function (s, o, add) {
	const n = (v) => Math.round(v || 0).toLocaleString();
	const total = add
		? frappe.pages["sales-allocation-v2"]._add_total(s, o)
		: Math.min(o.free, s.needed);
	const short = s.needed - total;
	return `<td class="num${short > 0.5 ? " sub-short" : ""}">${n(total)} / ${n(s.needed)}</td>
		<td>${
			add && !(o.adds > 0)
				? `<span class="ds-why">${__("Less than {0} stems", [
						n(s.add_step || s.per_box),
				  ])}</span>`
				: short > 0.5
				? `<span class="ds-why">${__("Short {0}", [n(short)])}</span>`
				: `<span class="ds-why ds-why--ok">${__("Fills the line")}</span>`
		}</td>`;
};
// Substitute: the chosen variety replaces the line's (what the line holds is released).
frappe.pages["sales-allocation-v2"]._replace_variety = function (d, line, s, o) {
	const P = frappe.pages["sales-allocation-v2"];
	d.hide();
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.substitute_variety",
		args: {
			sales_order: line.sales_order,
			sales_order_item: line.sales_order_item,
			new_item_code: o.variety,
		},
		freeze: true,
		callback: (res) => {
			const m = res.message || {};
			if (m.success === false) {
				frappe.msgprint(m.message || __("Could not substitute."));
				return;
			}
			frappe.show_alert({
				message: __("{0} now {1} on {2}", [
					s.variety,
					o.variety,
					line.order_name || line.sales_order,
				]),
				indicator: "green",
			});
			P._after_day_change();
			P._load_shortages();
		},
	});
};
// Add variety: the line keeps its own variety for what it holds or has free (whole boxes), the chosen
// variety takes the rest as a new line of the order.
frappe.pages["sales-allocation-v2"]._add_variety = function (d, line, s, o) {
	const P = frappe.pages["sales-allocation-v2"];
	if (!(o.adds > 0)) {
		frappe.show_alert({
			message: __("{0} has {1} stems free, less than the {2} it needs.", [
				o.variety,
				Math.round(o.free),
				Math.round(s.add_step || s.per_box),
			]),
			indicator: "orange",
		});
		return;
	}
	d.hide();
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.add_variety",
		args: {
			sales_order_item: line.sales_order_item,
			location: P.selected_location,
			variety: o.variety,
		},
		freeze: true,
		callback: () => {
			frappe.show_alert({
				message: __("{0} added to {1} for {2} stems of {3}", [
					o.variety,
					line.order_name || line.sales_order,
					Math.round(o.adds),
					s.variety,
				]),
				indicator: "green",
			});
			P._after_day_change();
			P._load_shortages();
		},
	});
};
// Stems rounded down to whole bunches.
frappe.pages["sales-allocation-v2"]._bunches = function (stems, per_bunch) {
	const b = parseFloat(per_bunch) || 1;
	return Math.floor((stems || 0) / b + 1e-9) * b;
};
// A short line's donors, one entry per order line holding the variety: {sales_order, ..., buckets, qty}.
frappe.pages["sales-allocation-v2"]._donor_groups = function (line) {
	const by = {};
	(line.donors || []).forEach((dn) => {
		const g = (by[dn.sales_order_item] = by[dn.sales_order_item] || {
			...dn,
			buckets: [],
			qty: 0,
			longer: false,
		});
		g.buckets.push(dn);
		g.qty += dn.qty || 0;
		g.longer = g.longer || !!dn.longer;
	});
	return Object.values(by).map((g) => ({
		...g,
		lengths: [...new Set(g.buckets.map((b) => b.stem_length))].join(", "),
	}));
};
// The box columns of one move-to row: full boxes before → after ("+" when a part box is left), and the
// result, with "Suggested" on the row that closes the box closest to full.
frappe.pages["sales-allocation-v2"]._move_cells = function (m, result, suggested, esc) {
	const r = result(m);
	return `<td>${m.to_before} → ${m.to_after}${m.to_open ? " +" : ""}</td>
		<td>${Math.round(m.from_have)} → ${Math.round(m.from_have - m.qty)}${
		m.from_after < m.from_before
			? ` · ${__("{0} full box(es) opened", [m.from_before - m.from_after])}`
			: ""
	}</td>
		<td><span class="ds-why${r[1] === "ok" ? " ds-why--ok" : ""}">${esc(r[0])}</span>${
		suggested ? ` <span class="ds-why ds-why--ok">${__("Suggested")}</span>` : ""
	}</td>`;
};
frappe.pages["sales-allocation-v2"]._move_from_order = function (line, group) {
	const P = frappe.pages["sales-allocation-v2"];
	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));
	const n = (v) => Math.round(v || 0).toLocaleString();
	// Every short line this order's buckets fit, the one it was opened from first, then in allocation order.
	const fits = (x) =>
		P._donor_groups(x).find((g) => g.sales_order_item === group.sales_order_item);
	const targets = (P.shortages || [])
		.filter((x) => x.short > 0 && fits(x))
		.sort(
			(a, b) =>
				(b.sales_order_item === line.sales_order_item) -
					(a.sales_order_item === line.sales_order_item) ||
				(a.sequence || 999) - (b.sequence || 999)
		);
	if (!targets.some((x) => x.sales_order_item === line.sales_order_item)) targets.unshift(line);
	// What a move does to the boxes: the stems that close whole boxes on the line it goes to (rather than
	// all it could take), and how many full boxes each side has before and after.
	const plan_for = (x) => {
		const most = P._bunches(Math.min((fits(x) || group).qty, x.short), x.per_bunch);
		const pb = x.per_box || 0,
			have = x.allocated || 0;
		const closing = pb ? Math.floor((have + most) / pb + 1e-9) * pb - have : 0;
		const dpb = group.line_per_box || 0,
			dhave = group.line_allocated || 0;
		const qty = Math.min(closing > 0 ? P._bunches(closing, x.per_bunch) || most : most, dhave);
		const full = (s, b) => (b ? Math.floor(s / b + 1e-9) : 0);
		const open = (s, b) => !!b && s % b > 0.5;
		// stems still needed to close the part box (0 when every box is full)
		const gap = (s, b) => (b && s % b > 0.5 ? b - (s % b) : 0);
		return {
			qty,
			pb,
			dpb,
			mixed: !!x.box_group && !String(x.box_group).startsWith("line:"),
			to_have: have,
			to_boxes: x.group_boxes
				? x.group_boxes.needed
				: pb
				? Math.ceil((x.needed || 0) / pb - 1e-9)
				: 0,
			to_need_after: gap(have + qty, pb),
			from_have: dhave,
			from_boxes: group.line_boxes || 0,
			from_need_after: gap(dhave - qty, dpb),
			to_before: full(have, pb),
			to_after: full(have + qty, pb),
			to_open: open(have + qty, pb),
			from_before: full(dhave, dpb),
			from_after: full(dhave - qty, dpb),
			from_open: open(dhave - qty, dpb),
			to_gap: pb ? (pb - (have % pb)) % pb || pb : Infinity,
		};
	};
	const qty_for = (x) => plan_for(x).qty;
	const result = (m) =>
		m.to_after > m.to_before && !m.to_open
			? [
					m.mixed
						? __("Fills its share of {0} box(es)", [m.to_after - m.to_before])
						: __("Fills {0} box(es)", [m.to_after - m.to_before]),
					"ok",
			  ]
			: m.to_open && m.from_open
			? [__("Leaves both boxes open"), "bad"]
			: m.from_open
			? [__("Leaves {0} box open", [group.order_name || group.sales_order]), "bad"]
			: [__("Leaves the box open"), "warn"];
	// suggest the box closest to full that this move closes; else the line it was opened from
	const best = targets
		.map((x, i) => ({ i, m: plan_for(x) }))
		.filter((o) => o.m.to_after > o.m.to_before && !o.m.to_open)
		.sort((a, b) => a.m.to_gap - b.m.to_gap)[0];
	const start = best ? best.i : 0;
	const longer_for = (x) => !!(fits(x) || group).longer;
	const picked = () => targets[+d.$wrapper.find("input[name=mv-to]:checked").val()];
	const d = new frappe.ui.Dialog({
		title: __("Move {0} {1} from {2}", [
			line.item_name || line.item_code,
			line.length || "",
			group.order_name || group.sales_order,
		]),
		size: "extra-large",
		fields: [
			{ fieldtype: "HTML", fieldname: "to" },
			{
				fieldtype: "Small Text",
				fieldname: "reason",
				label: __("Downgrade reason"),
				hidden: 1,
			},
		],
		primary_action_label: __("Move"),
		primary_action: (v) => {
			const to = picked();
			if (!to) return;
			const reason = (v.reason || "").trim();
			if (longer_for(to) && !reason) {
				frappe.msgprint(__("A downgrade reason is required for longer stems."));
				return;
			}
			d.hide();
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.day_allocation.move_from_order",
				args: {
					from_item: group.sales_order_item,
					to_item: to.sales_order_item,
					location: P.selected_location,
					stems: qty_for(to),
					downgrade_reason: reason || null,
				},
				freeze: true,
				freeze_message: __("Moving {0} stems…", [n(qty_for(to))]),
				callback: (r) => {
					const m = (r && r.message) || {};
					// an answer without stems moved is a refusal (the error is shown by Frappe), not a success
					if (!(m.moved > 0)) return;
					frappe.show_alert({
						message: __("Moved {0} stems from {1} to {2}", [
							n(m.moved),
							m.from || group.order_name || group.sales_order,
							m.to || to.order_name || to.sales_order,
						]),
						indicator: "green",
					});
					P._after_day_change();
					// the moved stems wait release: the release count changes
					P._load_proposal();
					P._load_shortages();
				},
			});
		},
	});
	const sides = (x) => {
		const m = plan_for(x);
		// a mixed box or bunch holds several varieties: this line only fills its own share of each box
		const mixed_to = !!x.box_group && !String(x.box_group).startsWith("line:");
		const mixed_from = !!group.line_mixed;
		const box = (have, pb) => (pb ? Math.floor(have / pb + 1e-9) + 1 : 1);
		const per = (pb, mixed) =>
			mixed ? __("{0} stems of each mixed box", [n(pb)]) : __("{0} stems a box", [n(pb)]);
		const full = (k, total, mixed) =>
			mixed
				? __("{0}/{1} boxes with its share", [k, total || "?"])
				: __("{0}/{1} boxes full", [k, total || "?"]);
		const state = (need, boxNo, mixed, ok_text) =>
			need > 0.5
				? `<span class="mv-need">${
						mixed
							? __("Box {0} short of its share by {1} stems", [boxNo, n(need)])
							: __("Box {0} open · needs {1} stems to fill", [boxNo, n(need)])
				  }</span>`
				: `<span class="mv-ok">${esc(ok_text)}</span>`;
		// "From" / "To" sit on the card's top border, like a legend
		const card = (cls, tag, title, sub, rows) => `<div class="mv-side ${cls}">
			<span class="mv-side__tag">${tag}</span>
			<div class="mv-side__h">${title}</div><div class="mv-side__sub">${sub}</div>
			${rows.map((r) => `<div class="mv-side__r"><span>${r[0]}</span><b>${r[1]}</b></div>`).join("")}
		</div>`;
		const from_now = m.dpb ? Math.floor(m.from_have / m.dpb + 1e-9) : 0;
		return `<div class="mv-sides">${card(
			"is-from",
			__("From"),
			esc(group.order_name || group.sales_order),
			`${esc(line.item_name || line.item_code)} ${esc(line.length || "")} · ${per(
				m.dpb,
				mixed_from
			)}`,
			[
				[
					__("Has now"),
					`${n(m.from_have)} ${__("stems")} · ${full(
						from_now,
						m.from_boxes,
						mixed_from
					)}`,
				],
				[__("Gives"), `${n(m.qty)} ${__("stems")}`],
				[
					__("After"),
					`${n(m.from_have - m.qty)} ${__("stems")} · ${full(
						m.from_after,
						m.from_boxes,
						mixed_from
					)}`,
				],
				[
					"",
					state(
						m.from_need_after,
						box(m.from_have - m.qty, m.dpb),
						mixed_from,
						mixed_from ? __("Its boxes keep their share") : __("Its boxes stay full")
					),
				],
			]
		)}<div class="mv-arrow">→</div>${card(
			"is-to",
			__("To"),
			esc(x.order_name || x.sales_order),
			`${esc(x.item_name || x.item_code || line.item_name || "")} ${esc(
				x.length || ""
			)} · ${per(m.pb, mixed_to)}`,
			[
				[
					__("Has now"),
					`${n(m.to_have)} ${__("stems")} · ${full(m.to_before, m.to_boxes, mixed_to)}`,
				],
				[__("Takes"), `${n(m.qty)} ${__("stems")}`],
				[
					__("After"),
					`${n(m.to_have + m.qty)} ${__("stems")} · ${full(
						m.to_after,
						m.to_boxes,
						mixed_to
					)}`,
				],
				[
					"",
					state(
						m.to_need_after,
						box(m.to_have + m.qty, m.pb),
						mixed_to,
						mixed_to ? __("Its share of the box filled") : __("Box filled")
					),
				],
			]
		)}</div>`;
	};
	d.fields_dict.to.$wrapper.html(`
		<div class="mv-sides-host"></div>
		<div class="mv-wrap"><table class="ds-donors mv-to">
			<thead><tr><th></th><th>${__("Move to")}</th><th>${__("Customer")}</th>
			<th>${__("Length")}</th><th class="num">${__("Short")}</th><th class="num">${__(
		"Moves"
	)}</th><th>${__("Boxes")}</th><th>${__("From stems")}</th><th></th></tr></thead>
			<tbody>${targets
				.map(
					(x, i) => `<tr>
					<td><input type="radio" name="mv-to" value="${i}" ${i === start ? "checked" : ""}></td>
					<td><strong>${esc(x.order_name || x.sales_order)}</strong>${
						x.order_name ? `<div class="ds-so">${esc(x.sales_order)}</div>` : ""
					}</td>
					<td>${esc(x.customer)}</td>
					<td>${esc(x.length)}</td>
					<td class="num">${n(x.short)}</td>
					<td class="num"><strong>${n(qty_for(x))}</strong></td>
					${P._move_cells(plan_for(x), result, best && best.i === i, esc)}
				</tr>`
				)
				.join("")}</tbody>
		</table></div>`);
	const sync = () => {
		const to = picked();
		if (to) d.$wrapper.find(".mv-sides-host").html(sides(to));
		d.set_df_property("reason", "hidden", to && longer_for(to) ? 0 : 1);
		d.set_df_property("reason", "reqd", to && longer_for(to) ? 1 : 0);
	};
	d.$wrapper.find("input[name=mv-to]").on("change", sync);
	d.$wrapper.find(".mv-to tbody tr").on("click", function (e) {
		if (e.target.tagName !== "INPUT")
			$(this).find("input").prop("checked", true).trigger("change");
	});
	d.$wrapper.addClass("ufd-sa2");
	d.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal mv-modal");
	d.show();
	sync();
};
