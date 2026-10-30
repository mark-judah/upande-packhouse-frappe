// Page: sales-allocation-v2 — Sales Allocation v2 (follows Allocation Planning v2)
//
// Layout:
//   [ compact toolbar: location · delivery ]
//   [ orders rail ] [ order lines | bucket table ] [ action bar ]
//
// Only the selected order line's bucket table renders, so allocating a bucket
// no longer rebuilds every line's table.
// Short blocking decisions (downgrade reason, substitute variety) stay dialogs.
frappe.pages["sales-allocation-v2"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Sales Allocation v2",
		single_column: true,
	});
	frappe.pages["sales-allocation-v2"].add_styles();
	frappe.pages["sales-allocation-v2"].make(page);
};

// Fires on every visit (first load AND every return trip), unlike on_page_load
// which only fires once — so a "Sales Allocation" shortcut clicked from a
// second Sales Order while this page instance is already alive still lands
// here. Consumes frappe.route_options (set by the Sales Order / Order Pick
// List shortcut) to pre-select the location + move the delivery window onto
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
        .ufd-sa2 .rail-filters .sa-inp { padding: 6px 10px; width: 100%; box-sizing: border-box; }
        .ufd-sa2 .rf-clear {
            width: 30px; flex: 0 0 30px;
            border: 1px solid var(--hairline); border-radius: 8px;
            background: var(--surface); color: var(--ink-mute);
            cursor: pointer; font-size: 13px; line-height: 1;
        }
        .ufd-sa2 .rf-clear:hover { background: var(--bad); border-color: var(--bad); color: #fff; }
        .ufd-sa2 .sales-order-list {
            flex: 1; min-height: 0; overflow-y: auto;
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
        .ufd-sa2 .sales-order-card:hover { border-color: var(--ink-faint); box-shadow: var(--shadow-card); }
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
        .ufd-sa2 .soc-bar { height: 4px; border-radius: 999px; background: rgba(10,10,10,0.06); margin-top: 4px; overflow: hidden; }

        /* ═══ Detail pane ═══ */
        .ufd-sa2 .sa-detail {
            background: var(--surface-2);
            border-radius: 18px;
            box-shadow: var(--shadow-card);
            min-height: 0;
            display: grid;
            /* Row order matches the DOM order below: head, action bar,
               toolbar, then the scrollable body. The action bar used to be
               the last (auto) row, at the bottom of the pane -- on a long
               bucket table or order list that pane can run taller than the
               viewport, and this row being last meant it was the first
               thing pushed out of view, visible again only by zooming the
               whole page out. Pinning it right under the header instead
               (still its own fixed-height row, just moved up) means it's
               never dependent on how much the scrollable body grows. */
            grid-template-rows: auto auto auto minmax(0,1fr);
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
        .ufd-sa2 .sa-actionbar {
            display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
            padding: 11px 20px;
            border-bottom: 1px solid var(--hairline);
            background: var(--surface);
        }
        .ufd-sa2 .sa-actionbar .ab-actions { margin-left: auto; display: flex; gap: 8px; }

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
        .ufd-sa2 .sa-mini-btn.primary { background: var(--grad-ink); border-color: transparent; color: #fafaf6; }
        .ufd-sa2 .sa-mini-btn.primary:hover { box-shadow: 0 3px 10px rgba(10,10,10,0.2); }
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
        .modal-dialog.ufd-sa2-modal, .modal-dialog.ufd-sa2-modal input, .modal-dialog.ufd-sa2-modal select, .modal-dialog.ufd-sa2-modal textarea { font-family: var(--sans); }
        .modal-dialog.ufd-sa2-modal .control-input, .modal-dialog.ufd-sa2-modal textarea.form-control { border-radius: 10px; border-color: var(--hairline); }

        /* ─── Narrow screens ─── */
        @media (max-width: 1240px) {
            .ufd-sa2.sales-allocation-container { height: auto; }
            .ufd-sa2 .sa-shell { grid-template-columns: 1fr; }
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
            <div class="location-info" id="locationInfoV2" style="display:none;"></div>
        </div>
        <div class="sa-shell">
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
                        <select id="lengthFilterV2" class="sa-inp"><option value="">All lengths</option></select>
                        <select id="itemGroupFilterV2" class="sa-inp"><option value="">All item groups</option></select>
                    </div>
                    <div class="rf-row">
                        <select id="allocFilterV2" class="sa-inp">
                            <option value="">All allocation</option>
                            <option value="unallocated">Unallocated</option>
                            <option value="partial">Partly allocated</option>
                            <option value="fully">Fully allocated</option>
                        </select>
                        <button class="rf-clear" id="clearFiltersV2" title="Clear filters">&times;</button>
                    </div>
                </div>
                <div class="sales-order-list" id="salesOrderListV2">
                    <div class="empty-state"><p>Select a location to begin.</p></div>
                </div>
            </aside>
            <section class="sa-detail" id="salesAllocationDetailV2">
                <header class="sa-detail-head" id="detailHeadV2"></header>
                <div class="sa-actionbar" id="actionBarV2"></div>
                <div class="sa-detail-toolbar" id="detailToolbarV2" style="display:none;"></div>
                <div class="sa-detail-body">
                    <nav class="sa-lines" id="linesRailV2"></nav>
                    <div class="sa-work" id="allocationWorkspaceV2"></div>
                </div>
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
	P.order_team = "";
	// Real Packing Teams (not a hardcoded list) -- populated by
	// _populate_filter_options, which already bundles this in with the
	// length/item-group option lists. See get_order_filter_options.
	P.available_teams = P.available_teams || [""];
	const tomorrow = frappe.datetime.add_days(frappe.datetime.get_today(), 1);
	P.filters = {
		search: "",
		priority: "",
		box_type: "",
		length: "",
		item_group: "",
		alloc: "",
		delivery_start: tomorrow,
		delivery_end: tomorrow,
	};
	P.make_date_filters();
	// The browser's date is only a first guess; the server's (EAT) decides.
	P.reset_to_tomorrow(false);
	P.render_allocation_grid();
	P.load_location_config();
	P._populate_filter_options();
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
	$("#lengthFilterV2").on("change", function () {
		P.filters.length = $(this).val();
		P.apply_filters();
	});
	$("#itemGroupFilterV2").on("change", function () {
		P.filters.item_group = $(this).val();
		P.apply_filters();
	});
	$("#allocFilterV2").on("change", function () {
		P.filters.alloc = $(this).val();
		P.apply_filters();
	});
	$("#clearFiltersV2").on("click", function () {
		$(
			"#orderSearchInputV2, #priorityFilterV2, #boxTypeFilterV2, #lengthFilterV2, #itemGroupFilterV2, #allocFilterV2"
		).val("");
		P.filters = {
			search: "",
			priority: "",
			box_type: "",
			length: "",
			item_group: "",
			alloc: "",
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
	if (loc_config) {
		const farm_labels = loc_config.farm_details
			.map((f) => `${f.farm} (${f.sales_shelf ? "Sales" : "Remote"})`)
			.join(", ");
		$("#locationInfoV2").show().attr("title", farm_labels).html(`Farms: ${farm_labels}`);
	}
	P.selected_order = null;
	P.allocations = [];
	P.order_items = [];
	P.selected_item = null;
	P.render_allocation_grid();
	P.load_sales_orders();
};
// ─── SALES ORDERS ───
frappe.pages["sales-allocation-v2"].load_sales_orders = function () {
	const P = frappe.pages["sales-allocation-v2"];
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
				P.apply_filters();
			} else {
				P.current_sales_orders = [];
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
	frappe.db.get_value("Sales Order", sales_order, ["docstatus", "status"]).then((r) => {
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
frappe.pages["sales-allocation-v2"]._populate_filter_options = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const fill = (sel, values, keep, allLabel) => {
		const $s = $(sel);
		if (!$s.length) return;
		const cur = keep && values.indexOf(keep) !== -1 ? keep : "";
		$s.html(
			`<option value="">${allLabel}</option>` +
				(values || [])
					.map(
						(v) =>
							`<option value="${frappe.utils.escape_html(
								v
							)}">${frappe.utils.escape_html(v)}</option>`
					)
					.join("")
		);
		$s.val(cur);
	};
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_order_filter_options",
		callback: function (r) {
			const o = (r && r.message) || {};
			fill("#lengthFilterV2", o.lengths || [], P.filters.length, "All lengths");
			fill(
				"#itemGroupFilterV2",
				o.item_groups || [],
				P.filters.item_group,
				"All item groups"
			);
			// Real Packing Teams, bundled into this same response (see
			// get_order_filter_options) rather than a separate async fetch --
			// this call already gates real interaction with the page, so by
			// the time an operator can select an order, this has landed.
			P.available_teams = [""].concat(o.teams || []);
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
	if (P.filters.length)
		orders = orders.filter(
			(o) => (o.lengths || "").split(", ").indexOf(P.filters.length) !== -1
		);
	if (P.filters.item_group)
		orders = orders.filter(
			(o) => (o.item_groups || "").split(", ").indexOf(P.filters.item_group) !== -1
		);
	if (P.filters.alloc) {
		orders = orders.filter((o) => {
			const pct = P._order_list_pct(o);
			if (P.filters.alloc === "unallocated") return pct <= 0;
			if (P.filters.alloc === "partial") return pct > 0 && pct < 100;
			if (P.filters.alloc === "fully") return pct >= 100;
			return true;
		});
	}
	const total = (P.current_sales_orders || []).length;
	$("#resultsCountV2").text(
		orders.length === total ? `${total} orders` : `${orders.length} of ${total}`
	);
	P.render_orders(orders);
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
			const bar_color =
				pct >= 75 ? "var(--good)" : pct >= 50 ? "var(--warn-2)" : "var(--bad)";
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
                <div class="soc-foot">
                    <span>${order.name} · ${frappe.datetime.str_to_user(
				order.delivery_date
			)}</span>
                    <span style="font-weight:600;color:${bar_color};">${pct}%</span>
                </div>
                <div class="soc-bar"><div style="width:${pct}%;height:100%;background:${bar_color};"></div></div>
            </div>`;
		})
		.join("");
	$list.html(html);
	$list.find(".sales-order-card").on("click", function () {
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
	if (!opts.force && switching && (P.allocations || []).length) {
		frappe.confirm(
			__(
				"You have {0} unconfirmed stems allocated on <b>{1}</b>. Switching orders discards them. Continue?",
				[P._session_total(), P.selected_order]
			),
			() => P.select_order(order_name, { keep: opts.keep, force: 1 })
		);
		return;
	}
	P._preserve_item = opts.keep ? P.selected_item : null;
	if (P._deeplink_item) {
		P._preserve_item = P._deeplink_item;
		P._deeplink_item = null;
	}
	P.allocations = [];
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
		P.order_team = "";
		P.apply_filters();
		P.render_allocation_grid();
	};
	if ((P.allocations || []).length) {
		frappe.confirm(
			__("Discard {0} unconfirmed stems allocated on <b>{1}</b>?", [
				P._session_total(),
				P.selected_order,
			]),
			finish
		);
		return;
	}
	finish();
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
	const teams = [...new Set(Object.values(P.item_teams))];
	P.order_team = teams.length === 1 ? teams[0] : "";
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
	const steps = P.CUT_STAGE_STEPS;
	const f = P._get_item_filter(so_item);
	const shown = P._has_active_filter(so_item)
		? batches.filter((b) => P._batch_passes_filter(b, item)).length
		: batches.length;
	const min_opts = steps
		.map((s) => {
			const v = s.toString();
			return `<option value="${v}" ${f.cut_stage_min === v ? "selected" : ""}>${v}</option>`;
		})
		.join("");
	const max_opts = steps
		.map((s) => {
			const v = s.toString();
			return `<option value="${v}" ${f.cut_stage_max === v ? "selected" : ""}>${v}</option>`;
		})
		.join("");
	return `
        <div class="item-batch-filter" data-so-item="${so_item}">
            <span class="ibf-label">Cut stage</span>
            <div class="ibf-group">
                <select class="ibf-select ibf-cs-select" data-so-item="${so_item}" data-filter="cut_stage_min">
                    <option value="">Min</option>${min_opts}
                </select>
                <span class="ibf-range-sep">to</span>
                <select class="ibf-select ibf-cs-select" data-so-item="${so_item}" data-filter="cut_stage_max">
                    <option value="">Max</option>${max_opts}
                </select>
            </div>
            <button class="ibf-clear" data-so-item="${so_item}">Clear</button>
            <span class="ibf-count">${shown} of ${batches.length} buckets</span>
        </div>`;
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
	// Start with recommended filter (same color + headsize)
	let filter_active = !!(original_color || original_headsize);
	const sub_dialog = new frappe.ui.Dialog({
		title: `Substitute variety: ${item.item_name || item.item_code}`,
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "sub_content" }],
	});
	sub_dialog.$wrapper.addClass("ufd-sa2");
	sub_dialog.$wrapper.find(".modal-dialog").addClass("ufd-sa2-modal");
	const render_sub_content = function (varieties, show_filter) {
		let filter_html = "";
		if (original_color || original_headsize) {
			const filter_label = [
				original_color ? `Color: ${original_color}` : "",
				original_headsize ? `Headsize: ${original_headsize}` : "",
			]
				.filter(Boolean)
				.join(", ");
			filter_html = `
                <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;padding:9px 14px;background:${
					show_filter ? "var(--good-soft)" : "var(--surface)"
				};border-radius:10px;font-size:12px;">
                    <div>
                        <strong>Recommended:</strong> ${filter_label}
                        ${
							show_filter
								? '<span style="color:var(--good);margin-left:6px;">active</span>'
								: '<span style="color:var(--ink-mute);margin-left:6px;">cleared</span>'
						}
                    </div>
                    <button class="sa-mini-btn" id="toggleSubFilterV2">
                        ${show_filter ? "Show all varieties" : "Show recommended only"}
                    </button>
                </div>`;
		}
		const current_html = `
            <div style="background:var(--surface);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:12px;color:var(--ink-3);">
                <strong>Current:</strong> ${item.item_name} (${item.item_code})
                ${original_color ? ` · Color: ${original_color}` : ""}
                ${original_headsize ? ` · Headsize: ${original_headsize}` : ""}
                · Qty: ${item.pending_stock_qty || 0} ${item.stock_uom || ""}
            </div>`;
		let list_html = "";
		if (!varieties || !varieties.length) {
			list_html = '<div class="empty-state"><p>No varieties found.</p></div>';
		} else {
			list_html = varieties
				.map((v) => {
					const is_same = v.item_code === item.item_code;
					const is_recommended =
						(original_color && v.color === original_color) ||
						(original_headsize && v.headsize === original_headsize);
					const rec_class = is_recommended && !is_same ? "recommended" : "";
					const badges = [];
					if (is_same)
						badges.push(
							'<span class="sub-badge" style="background:rgba(10,10,10,0.06);color:var(--ink-mute);">Current</span>'
						);
					if (is_recommended && !is_same)
						badges.push(
							'<span class="sub-badge" style="background:var(--signal-soft);color:var(--signal);">Recommended</span>'
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
		sub_dialog.fields_dict.sub_content.$wrapper.html(current_html + filter_html + list_html);
		// Bind toggle
		sub_dialog.$wrapper.find("#toggleSubFilterV2").on("click", function () {
			filter_active = !filter_active;
			load_varieties();
		});
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
		if (filter_active) {
			if (original_color) args.color = original_color;
			if (original_headsize) args.headsize = original_headsize;
		}
		frappe.call({
			method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_substitute_varieties",
			args: args,
			callback: function (r) {
				render_sub_content(r.message || [], filter_active);
			},
			error: function () {
				render_sub_content([], filter_active);
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
	// Bar reflects the WHOLE order (all lines), not just the mix-group subset shown,
	// so it agrees with the order's card in the list and climbs as you allocate.
	const prog = P._order_progress(P.order_items);
	const required = prog.required,
		done = prog.done,
		pct = prog.pct;
	const bar_color = pct >= 75 ? "var(--good)" : pct >= 50 ? "var(--warn-2)" : "var(--bad)";
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
        ${
			P._order_mixed
				? `
        <div style="flex:0 0 auto;">
            <div style="font-size:9.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--ink-faint);margin-bottom:5px;">Packing team</div>
            <select class="item-team-select ${P.order_team || "" ? "is-set" : "is-unset"}"
                onchange="frappe.pages['sales-allocation-v2'].set_order_team(this.value)"
                title="Packing team for this mixed order (applies to every line)">
                ${(P.available_teams || [""])
					.map(
						(t) =>
							`<option value="${t}" ${
								t === (P.order_team || "") ? "selected" : ""
							}>${t || "Select team…"}</option>`
					)
					.join("")}
            </select>
        </div>`
				: ""
		}
        <div style="width:132px;flex-shrink:0;">
            <div style="display:flex;justify-content:space-between;font-size:9.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--ink-faint);margin-bottom:5px;">
                <span>Allocated</span><span style="font-weight:600;color:${bar_color};">${pct}%</span>
            </div>
            <div style="width:100%;height:4px;background:rgba(10,10,10,0.06);border-radius:999px;overflow:hidden;">
                <div style="width:${pct}%;height:100%;background:${bar_color};"></div>
            </div>
        </div>
        <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation-v2'].open_sales_order()">Sales Order</button>
        <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation-v2'].open_order_pick_list()">Order Pick List</button>
        <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation-v2'].close_detail()">Close</button>`;
};
frappe.pages["sales-allocation-v2"].open_sales_order = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_order) return;
	frappe.set_route("Form", "Sales Order", P.selected_order);
};
frappe.pages["sales-allocation-v2"].open_order_pick_list = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_order) return;
	// An order can have several OPLs (one per mix/bunch group), so the list
	// view filtered to this order is the right target, not a single doc.
	frappe.set_route("list", "Order Pick List", { sales_order: P.selected_order });
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
			const needs_team =
				P._session_qty(item.sales_order_item) > 0 &&
				!(P.item_teams || {})[item.sales_order_item];
			const team_flag =
				needs_team && !P._order_mixed
					? `<span class="lc-tag" style="background:var(--bad-soft);color:var(--bad);margin-left:4px;">No team</span>`
					: "";
			const left = required - grand;
			const boxes = P._boxes_possible(item);
			const filled = P._boxes_possible(item, true);
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
                <div class="lc-name">${item.item_name || item.item_code || "?"}</div>
                <div class="lc-meta">${
					item.required_length || "No length"
				} · ${grand}/${required} ${item.stock_uom || ""}</div>${
				filled
					? `<div class="lc-meta">${filled.can}/${filled.total} box${
							filled.total === 1 ? "" : "es"
					  } · ${P._box_make_up(item)}</div>`
					: ""
			}
                <div>${tag}${team_flag}${short_flag}</div>
                <div class="line-bar"><div style="width:${pct}%;height:100%;background:${bar_color};"></div></div>
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
// ─── RENDER: ACTION BAR ───
frappe.pages["sales-allocation-v2"]._render_action_bar = function () {
	const P = frappe.pages["sales-allocation-v2"];
	if (!P.selected_order) {
		return `<div style="font-size:12px;color:var(--ink-mute);">Nothing to confirm yet.</div>`;
	}
	const total = P._session_total();
	const lines = new Set((P.allocations || []).map((a) => a.sales_order_item)).size;
	const dg = (P.allocations || []).filter((a) => a.length_status === "downgrade");
	const dg_stems = dg.reduce((s, a) => s + (parseFloat(a.qty) || 0), 0);
	const dg_note = dg.length
		? ` · <span style="color:var(--warn);">${
				dg.length
		  } downgraded bucket(s), ${dg_stems.toLocaleString()} stems</span>`
		: "";
	return `
        <div style="font-size:12px;color:var(--ink-3);">
            Session
            <strong style="color:${
				total > 0 ? "var(--good)" : "var(--ink-mute)"
			};margin:0 3px;">${total.toLocaleString()}</strong>
            stems · <strong>${lines}</strong> line(s)${dg_note}
        </div>
        <div class="ab-actions">
            <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation-v2'].clear_all_and_render()">Clear all</button>
            <button class="sa-mini-btn primary" onclick="frappe.pages['sales-allocation-v2'].confirm_allocation()">Confirm allocation</button>
        </div>`;
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
	// ── Actions: auto-allocate, substitute, team
	const box_lines = P._box_lines(item);
	const box_left = box_lines.reduce((s, it) => s + Math.max(0, P._line_left(it)), 0);
	const can_fifo = box_lines.some(
		(it) => P._line_left(it) > 0 && (it.batches || []).some((b) => (b.available_qty || 0) > 0)
	);
	const _curTeam = (P.item_teams && P.item_teams[item.sales_order_item]) || "";
	const teams = P.available_teams || [""];
	const actions = `
        ${
			can_fifo
				? `<button class="sa-mini-btn primary" onclick="frappe.pages['sales-allocation-v2'].auto_allocate_fifo('${
						item.sales_order_item
				  }')">${
						box_lines.length > 1
							? `Auto-allocate ${box_lines.length} varieties · ${box_left}`
							: `Auto-allocate ${remaining}`
				  }</button>`
				: ""
		}
        <button class="substitute-btn" data-so-item="${item.sales_order_item}">Substitute</button>
        ${
			P._order_mixed
				? ""
				: `<select class="item-team-select ${
						_curTeam ? "is-set" : "is-unset"
				  }" data-so-item="${item.sales_order_item}"
            onchange="frappe.pages['sales-allocation-v2'].set_item_team('${
				item.sales_order_item
			}', this.value)"
            title="Packing team for this line">
            ${teams
				.map(
					(t) =>
						`<option value="${t}" ${t === _curTeam ? "selected" : ""}>${
							t || "Select team…"
						}</option>`
				)
				.join("")}
        </select>`
		}`;
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
            <div class="ih-actions">${actions}</div>
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
			if (
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
                    )">Allocate all</button>`;
			}
			if (allocated_here > 0 || session_alloc > 0) {
				actions_html += `
                    <button class="btn btn-xs btn-danger" onclick="frappe.pages['sales-allocation-v2'].unallocate_from_bucket(
                        '${item.sales_order_item}','${batch.bucket_id}',${
					allocated_here + session_alloc
				},'${esc_stem}'
                    )">Unallocate (${allocated_here + session_alloc})</button>`;
			}
			if (is_zero && !allocated_here && !session_alloc) {
				actions_html =
					'<span style="color:var(--ink-faint);font-size:11px;">No stock</span>';
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
		$("#actionBarV2").html(P._render_action_bar());
		return;
	}
	const all_items = P.order_items || [];
	// A mixed-box / mixed-bunch order is packed by ONE team, so its team is chosen
	// once at the top (see _render_detail_head) rather than per line.
	P._order_mixed = all_items.some((it) => it.custom_mixed_box || it.custom_mixed_bunch);
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
	const toolbar = P._render_farm_filter() + P._render_cutstage_bypass() + P._render_mix_filter();
	if (toolbar.trim()) $("#detailToolbarV2").html(toolbar).css("display", "flex");
	else $("#detailToolbarV2").empty().hide();
	$("#linesRailV2").html(P._render_lines_rail(items));
	if (item) {
		$("#allocationWorkspaceV2").html(P._render_item_block(item));
	} else if (P.selected_mix_group) {
		$("#allocationWorkspaceV2").html(
			'<div class="empty-state"><p>No items in the selected mix.</p></div>'
		);
	} else {
		$("#allocationWorkspaceV2").html(
			'<div class="empty-state"><p>No items with confirmed stems for this location.</p></div>'
		);
	}
	$("#actionBarV2").html(P._render_action_bar());
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
	const $card = $(`.sales-order-card[data-order="${P.selected_order}"]`);
	if ($card.length) {
		$card
			.find(".soc-foot span")
			.last()
			.css("color", color)
			.text(pct + "%");
		$card.find(".soc-bar > div").css({ width: pct + "%", background: color });
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
	const qty = Math.min(max_from_bucket, remaining);
	if (qty <= 0) {
		frappe.msgprint(__("Nothing to allocate."));
		return;
	}
	const item = P.order_items.find((i) => i.sales_order_item === so_item);
	if (!item) return;
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
	frappe.confirm(`Unallocate ${allocated_qty} stems from bucket ${bucket_id}?`, () => {
		// Only clear this length's pending allocation -- the same bucket can
		// hold this variety at another length with its own separate row.
		P.allocations = P.allocations.filter(
			(a) =>
				!(
					a.sales_order_item === so_item &&
					a.bucket_id === bucket_id &&
					a.stem_length === (stem_length || "")
				)
		);
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
		});
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
			primary_action_label: "Confirm auto-allocate",
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
		const qty = Math.min(available, remaining);
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
// ─── CONFIRM ALLOCATION ───
frappe.pages["sales-allocation-v2"].confirm_allocation = function () {
	const P = frappe.pages["sales-allocation-v2"];
	const allocations = P.allocations || [];
	if (!allocations.length) {
		frappe.msgprint(__("No allocations to confirm."));
		return;
	}
	if (!P.selected_location) {
		frappe.msgprint(__("Location not selected."));
		return;
	}
	const valid_so_items = new Set((P.order_items || []).map((i) => i.sales_order_item));
	const valid_allocations = allocations.filter((a) => valid_so_items.has(a.sales_order_item));
	if (!valid_allocations.length) {
		frappe.msgprint(__("No valid allocations."));
		return;
	}
	// Team is per line (per Sales Order Item). Every item being allocated needs one.
	const teams = P.item_teams || {};
	const missing = [...new Set(valid_allocations.map((a) => a.sales_order_item))].filter(
		(soi) => !teams[soi]
	);
	if (missing.length) {
		const names = missing.map((soi) => {
			const it = (P.order_items || []).find((i) => i.sales_order_item === soi);
			return it ? it.item_name || it.item_code || soi : soi;
		});
		// Jump to the first offending line so its team dropdown is on screen.
		P.selected_item = missing[0];
		P.render_allocation_grid();
		frappe.msgprint("Select a packing team for: " + names.join(", "));
		return;
	}
	// Every downgrade already has a reason by this point -- either typed into
	// the per-bucket dialog when it was picked, into the bulk auto-allocate
	// dialog, or auto-stamped "Amber time expired" for an amber-expired batch
	// (see _execute_fifo) -- so re-showing them all in a second "Review
	// downgrades" dialog here was purely duplicating a prompt the allocator
	// had already answered. Removed; go straight to sending the allocation.
	P._send_allocation(valid_allocations, teams);
};

frappe.pages["sales-allocation-v2"]._send_allocation = function (valid_allocations, teams) {
	const P = frappe.pages["sales-allocation-v2"];
	frappe.call({
		method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.allocate_stock_with_buckets",
		args: {
			sales_order: P.selected_order,
			allocations: valid_allocations,
			location: P.selected_location,
			teams: JSON.stringify(teams),
		},
		freeze: true,
		freeze_message: "Allocating stock…",
		callback: function (r) {
			if (r.message && r.message.success) {
				const results = r.message.pick_list_results || [];
				const messages = results
					.map((res) => {
						const link = `<a href="/app/order-pick-list/${
							res.name || "?"
						}" target="_blank"><strong>${res.name || "?"}</strong></a>`;
						if (res.status === "submitted")
							return `Pick list ${link} created and submitted`;
						if (res.status === "draft") return `Pick list ${link} created as draft`;
						if (res.status === "updated_existing") return `Pick list ${link} updated`;
						return "";
					})
					.filter(Boolean);
				frappe.msgprint({
					title: "Allocation complete",
					message: messages.length
						? messages.join("<br>")
						: "Allocation completed successfully.",
					indicator: "green",
				});
				P.allocations = [];
				P.selected_order = null;
				P.order_items = [];
				P.selected_item = null;
				P.item_teams = {};
				P.order_team = "";
				P.render_allocation_grid();
				P.load_sales_orders();
			} else {
				frappe.msgprint({
					title: "Allocation failed",
					message: r.message?.message || "Allocation failed.",
					indicator: "red",
				});
			}
		},
	});
};
// ─── HELPERS ───
// Mixed order: one team for the whole box/bunch. Set it once at the top and
// mirror it onto every line so the save payload and progress flags are unchanged.
frappe.pages["sales-allocation-v2"].set_order_team = function (team) {
	const P = frappe.pages["sales-allocation-v2"];
	P.order_team = team || "";
	P.item_teams = P.item_teams || {};
	(P.order_items || []).forEach((it) => {
		if (team) P.item_teams[it.sales_order_item] = team;
		else delete P.item_teams[it.sales_order_item];
	});
	P.render_allocation_grid();
};
frappe.pages["sales-allocation-v2"].set_item_team = function (so_item, team) {
	const P = frappe.pages["sales-allocation-v2"];
	P.item_teams = P.item_teams || {};
	if (team) P.item_teams[so_item] = team;
	else delete P.item_teams[so_item];
	// Refresh the select's state colour and the line rail's "no team" flag
	// without rebuilding the bucket table.
	const $w = P._scope();
	$w.find(`.item-team-select[data-so-item="${so_item}"]`)
		.toggleClass("is-set", !!team)
		.toggleClass("is-unset", !team);
	const all_items = P.order_items || [];
	const items = P.selected_mix_group
		? all_items.filter(
				(it) => String(it.custom_mix_group || "") === String(P.selected_mix_group)
		  )
		: all_items;
	$("#linesRailV2").html(P._render_lines_rail(items));
	P._bind_lines_rail();
};
frappe.pages["sales-allocation-v2"].clear_all_and_render = function () {
	const P = frappe.pages["sales-allocation-v2"];
	P.clear_allocations();
	P.render_allocation_grid();
};
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
	if (order.name === frappe.pages["sales-allocation-v2"].selected_order)
		return frappe.pages["sales-allocation-v2"]._order_progress().pct;
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
