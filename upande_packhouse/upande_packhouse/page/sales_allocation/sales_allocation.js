// Page: sales-allocation — Stock Allocation Dashboard
//
// Layout:
//   [ compact toolbar: location · delivery · posting (collapsed) ]
//   [ orders rail ] [ order lines | bucket table ] [ action bar ]
//
// Only the selected order line's bucket table renders, so allocating a bucket
// no longer rebuilds every line's table.
// Short blocking decisions (downgrade reason, substitute variety) stay dialogs.
frappe.pages['sales-allocation'].on_page_load = function (wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: 'Stock Allocation Dashboard',
        single_column: true
    });
    frappe.pages['sales-allocation'].add_styles();
    frappe.pages['sales-allocation'].make(page);
};

frappe.pages['sales-allocation'].add_styles = function () {
    if (document.getElementById('sales-allocation-styles')) return;
    // ufd-modern font stack (Poppins + JetBrains Mono) — matches every other
    // Upande dashboard. Loaded once, applied only inside .ufd-sa so the rest
    // of the Frappe Desk chrome (sidebar/topbar/other pages) is untouched.
    if (!document.getElementById('sales-allocation-fonts')) {
        const fontLink = document.createElement('link');
        fontLink.id = 'sales-allocation-fonts';
        fontLink.rel = 'stylesheet';
        fontLink.href = 'https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600&family=Poppins:wght@400;500;600;700&display=swap';
        document.head.appendChild(fontLink);
    }
    const styleEl = document.createElement('style');
    styleEl.id = 'sales-allocation-styles';
    styleEl.textContent = `
        /* ═══ ufd-modern tokens (scoped — never touches the rest of Desk) ═══ */
        .ufd-sa {
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
        .ufd-sa, .ufd-sa input, .ufd-sa select, .ufd-sa button { font-family:var(--sans); }

        /* ═══ Shell ═══ */
        .ufd-sa.sales-allocation-container {
            padding: 16px 20px 20px;
            background: var(--bg);
            height: calc(100vh - 96px);
            min-height: 620px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .ufd-sa .sa-shell {
            flex: 1;
            min-height: 0;
            display: grid;
            grid-template-columns: 320px minmax(0,1fr);
            gap: 12px;
        }

        /* ═══ Compact toolbar (one line) ═══ */
        .ufd-sa .sa-toolbar {
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
        .ufd-sa .sa-tb-group { display: flex; align-items: center; gap: 7px; }
        .ufd-sa .sa-tb-label {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); white-space: nowrap;
        }
        .ufd-sa .sa-tb-div { width: 1px; height: 22px; background: var(--hairline); }
        .ufd-sa .sa-to { font-size: 11px; color: var(--ink-faint); }
        .ufd-sa .location-buttons { display: flex; gap: 3px; background: var(--surface); border: 1px solid var(--hairline); border-radius: 999px; padding: 2px; }
        .ufd-sa .location-btn {
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
        .ufd-sa .location-btn:hover { color: var(--ink); }
        .ufd-sa .location-btn.active { background: var(--grad-ink); color: #fafaf6; }
        .ufd-sa .location-btn .lb-sub { display: none; }
        .ufd-sa .location-info {
            margin-left: auto;
            font-size: 11px;
            color: var(--ink-mute);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 340px;
        }
        .ufd-sa .sa-inp {
            padding: 5px 9px;
            border: 1px solid var(--hairline);
            border-radius: 8px;
            font-size: 12px;
            background: var(--surface);
            color: var(--ink-3);
            outline: none;
        }
        .ufd-sa .sa-inp:focus { border-color: var(--ink-faint); background: var(--surface-2); }
        .ufd-sa .sa-date { width: 122px; }
        .ufd-sa .sa-ghost-btn {
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
        .ufd-sa .sa-ghost-btn:hover { background: var(--ink); border-color: var(--ink); color: #fafaf6; }

        /* ═══ Orders rail ═══ */
        .ufd-sa .sa-rail {
            background: var(--surface-2);
            border-radius: 18px;
            box-shadow: var(--shadow-card);
            padding: 14px 14px 8px;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        .ufd-sa .rail-head {
            display: flex; align-items: baseline; justify-content: space-between;
            gap: 8px; margin-bottom: 10px;
        }
        .ufd-sa .rail-head h3 { margin: 0; font: 600 14px var(--sans); color: var(--ink); letter-spacing: -.2px; }
        .ufd-sa .rail-head #resultsCount { font-size: 11px; color: var(--ink-mute); white-space: nowrap; }
        .ufd-sa .rail-filters { display: grid; grid-template-columns: 1fr; gap: 6px; margin-bottom: 10px; }
        .ufd-sa .rail-filters .rf-row { display: flex; gap: 6px; }
        .ufd-sa .rail-filters .rf-row select { flex: 1; min-width: 0; }
        .ufd-sa .rail-filters .sa-inp { padding: 6px 10px; }
        .ufd-sa .rf-clear {
            width: 30px; flex: 0 0 30px;
            border: 1px solid var(--hairline); border-radius: 8px;
            background: var(--surface); color: var(--ink-mute);
            cursor: pointer; font-size: 13px; line-height: 1;
        }
        .ufd-sa .rf-clear:hover { background: var(--bad); border-color: var(--bad); color: #fff; }
        .ufd-sa .sales-order-list {
            flex: 1; min-height: 0; overflow-y: auto;
            margin: 0 -4px; padding: 2px 4px 8px;
        }

        /* ─── Order card ─── */
        .ufd-sa .sales-order-card {
            border: 1px solid var(--hairline);
            border-radius: 12px;
            padding: 10px 12px;
            margin-bottom: 7px;
            cursor: pointer;
            background: var(--surface-2);
            transition: all 0.15s;
        }
        .ufd-sa .sales-order-card:hover { border-color: var(--ink-faint); box-shadow: var(--shadow-card); }
        .ufd-sa .sales-order-card.selected { border-color: transparent; box-shadow: inset 0 0 0 2px var(--ink); }
        .ufd-sa .soc-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .ufd-sa .soc-id { font: 600 12px var(--sans); color: var(--ink); }
        .ufd-sa .soc-pri { font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .4px; padding: 2px 7px; border-radius: 999px; }
        .ufd-sa .pri-high { background: var(--bad-soft); color: var(--bad); }
        .ufd-sa .pri-medium { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa .pri-low { background: rgba(10,10,10,0.05); color: var(--ink-mute); }
        .ufd-sa .soc-line {
            font-size: 11px; color: var(--ink-4); margin-top: 4px;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .ufd-sa .soc-line.muted { color: var(--ink-mute); font-size: 10px; }
        .ufd-sa .soc-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; font-size: 10px; color: var(--ink-mute); }
        .ufd-sa .soc-bar { height: 4px; border-radius: 999px; background: rgba(10,10,10,0.06); margin-top: 4px; overflow: hidden; }

        /* ═══ Detail pane ═══ */
        .ufd-sa .sa-detail {
            background: var(--surface-2);
            border-radius: 18px;
            box-shadow: var(--shadow-card);
            min-height: 0;
            display: grid;
            grid-template-rows: auto auto minmax(0,1fr) auto;
            overflow: hidden;
        }
        .ufd-sa .sa-detail-head {
            padding: 13px 20px;
            border-bottom: 1px solid var(--hairline);
            display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
        }
        .ufd-sa .sa-detail-toolbar {
            padding: 9px 20px;
            border-bottom: 1px solid var(--hairline);
            background: var(--surface);
            display: flex; align-items: center; gap: 8px 18px; flex-wrap: wrap;
        }
        .ufd-sa .sa-detail-body { min-height: 0; display: grid; grid-template-columns: 226px minmax(0,1fr); }
        .ufd-sa .sa-lines {
            min-height: 0; overflow-y: auto; padding: 12px;
            border-right: 1px solid var(--hairline); background: var(--surface);
        }
        .ufd-sa .sa-lines-title {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); margin: 2px 0 9px 2px;
        }
        .ufd-sa .sa-work { min-height: 0; overflow-y: auto; padding: 16px 20px 22px; }
        .ufd-sa .sa-actionbar {
            display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
            padding: 11px 20px;
            border-top: 1px solid var(--hairline);
            background: var(--surface);
        }
        .ufd-sa .sa-actionbar .ab-actions { margin-left: auto; display: flex; gap: 8px; }

        /* ─── Order line cards ─── */
        .ufd-sa .line-card {
            border: 1px solid var(--hairline);
            background: var(--surface-2);
            border-radius: 10px;
            padding: 9px 11px;
            margin-bottom: 6px;
            cursor: pointer;
            transition: all 0.15s;
        }
        .ufd-sa .line-card:hover { border-color: var(--ink-faint); }
        .ufd-sa .line-card.active { border-color: transparent; box-shadow: inset 0 0 0 2px var(--ink); }
        .ufd-sa .line-card .lc-name { font: 600 12px var(--sans); color: var(--ink); line-height: 1.35; }
        .ufd-sa .line-card .lc-meta { font-size: 10px; color: var(--ink-mute); margin-top: 2px; }
        .ufd-sa .line-card .lc-tag {
            display: inline-block; font: 600 9px var(--sans); text-transform: uppercase;
            letter-spacing: .3px; padding: 2px 7px; border-radius: 999px; margin-top: 5px;
        }
        .ufd-sa .line-bar { height: 4px; border-radius: 999px; background: rgba(10,10,10,0.06); margin-top: 7px; overflow: hidden; }

        /* ─── Item header ─── */
        .ufd-sa .item-head { display: flex; align-items: flex-start; gap: 14px; flex-wrap: wrap; margin-bottom: 12px; }
        .ufd-sa .ih-title { font: 600 15px var(--sans); color: var(--ink); letter-spacing: -.2px; }
        .ufd-sa .ih-title .ih-code { font: 400 12px var(--mono); color: var(--ink-mute); margin-left: 6px; }
        .ufd-sa .ih-pills { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin-top: 7px; }
        .ufd-sa .ih-actions { margin-left: auto; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
        .ufd-sa .pill {
            display: inline-block; font: 600 10px var(--sans); letter-spacing: .2px;
            padding: 3px 9px; border-radius: 999px; white-space: nowrap;
        }
        .ufd-sa .pill-ink { background: var(--grad-ink); color: #fafaf6; }
        .ufd-sa .pill-signal { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa .pill-line { border: 1px solid var(--hairline); color: var(--ink-4); }
        .ufd-sa .pill-mono { background: var(--surface); color: var(--ink-mute); font-family: var(--mono); font-weight: 500; }
        .ufd-sa .pill-good { background: var(--good-soft); color: var(--good); }

        /* ─── Stat strip ─── */
        .ufd-sa .stat-strip {
            display: flex; border: 1px solid var(--hairline); border-radius: 12px;
            overflow: hidden; margin-bottom: 12px; background: var(--surface);
        }
        .ufd-sa .stat { flex: 1; min-width: 0; padding: 9px 13px; border-right: 1px solid var(--hairline); }
        .ufd-sa .stat:last-child { border-right: 0; }
        .ufd-sa .stat .st-l { font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .8px; color: var(--ink-faint); }
        .ufd-sa .stat .st-v { font: 600 15px var(--sans); color: var(--ink-2); margin-top: 3px; }
        .ufd-sa .stat .st-v small { font: 400 10px var(--sans); color: var(--ink-mute); margin-left: 2px; }

        .ufd-sa .note-band {
            border-radius: 10px; padding: 8px 12px; margin-bottom: 10px;
            font-size: 11.5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
        }
        .ufd-sa .note-good { background: var(--good-soft); color: var(--good); }
        .ufd-sa .confirmed-chip-alloc {
            display: inline-block; padding: 2px 9px; border-radius: 999px;
            background: var(--signal-soft); color: var(--signal); font-size: 10.5px; font-weight: 600;
        }
        .ufd-sa .note-band .nb-right { margin-left: auto; font-size: 10.5px; opacity: 0.85; }

        .ufd-sa .loading-state, .ufd-sa .empty-state { text-align: center; padding: 40px 20px; color: var(--ink-mute); font-size: 13px; }

        /* ─── Bucket table ─── */
        .ufd-sa .allocation-grid-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
        .ufd-sa .allocation-grid-table thead { background: var(--surface-2); position: sticky; top: -16px; z-index: 5; }
        .ufd-sa .allocation-grid-table th {
            padding: 9px 10px; text-align: left;
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .8px;
            color: var(--ink-faint); border-bottom: 1px solid var(--hairline); white-space: nowrap;
        }
        .ufd-sa .allocation-grid-table td {
            padding: 9px 10px; border-bottom: 1px solid var(--hairline);
            vertical-align: middle; color: var(--ink-3);
        }
        .ufd-sa .allocation-grid-table tbody tr:hover { background: rgba(10,10,10,0.02); }
        .ufd-sa .allocation-grid-table tbody tr.downgrade-bucket { background: var(--warn-soft); box-shadow: inset 3px 0 0 var(--warn-2); }
        .ufd-sa .allocation-grid-table tbody tr.previously-allocated-bucket { background: rgba(10,10,10,0.03); color: var(--ink-mute); box-shadow: inset 3px 0 0 var(--ink-faint); }
        .ufd-sa .allocation-grid-table tbody tr.preferred-farm-row { box-shadow: inset 3px 0 0 var(--signal); }
        .ufd-sa .allocation-grid-table tbody tr.awaiting-transfer-row { background: var(--warn-soft); box-shadow: inset 3px 0 0 var(--warn-2); }
        .ufd-sa .grid-badge {
            display: inline-block; padding: 2px 8px; border-radius: 999px;
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: .3px;
        }
        .ufd-sa .badge-exact { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa .badge-downgrade { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa .badge-preferred { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa .badge-awaiting { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa .badge-need { background: var(--bad-soft); color: var(--bad); }
        .ufd-sa .badge-ok { background: var(--good-soft); color: var(--good); }

        /* ─── Farm + mix filters (single row in detail toolbar) ─── */
        .ufd-sa .farm-filter-bar, .ufd-sa .mix-filter-bar {
            background: none; border: 0; padding: 0; margin: 0;
            display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
        }
        .ufd-sa .farm-filter-bar label.title, .ufd-sa .mix-filter-bar label.title {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); white-space: nowrap; margin: 0;
        }
        .ufd-sa .farm-checkbox-group { display: flex; gap: 5px; flex-wrap: wrap; }
        .ufd-sa .farm-checkbox-item {
            display: flex; align-items: center; gap: 5px;
            padding: 4px 10px; border: 1px solid var(--hairline); border-radius: 999px;
            background: var(--surface-2); cursor: pointer; font-size: 11px; color: var(--ink-3);
            transition: all 0.15s; user-select: none;
        }
        .ufd-sa .farm-checkbox-item:hover { border-color: var(--ink-faint); }
        .ufd-sa .farm-checkbox-item.checked { background: var(--grad-ink); color: #fafaf6; border-color: transparent; }
        .ufd-sa .farm-checkbox-item.checked .farm-badge { background: rgba(255,255,255,0.2); color: #fafaf6; }
        .ufd-sa .farm-badge {
            font-size: 8.5px; padding: 1px 6px; border-radius: 999px; text-transform: uppercase;
            background: rgba(10,10,10,0.06); color: var(--ink-mute);
        }
        .ufd-sa .farm-badge.sales { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa .farm-badge.remote { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa .mix-filter-bar select {
            padding: 4px 11px; border: 1px solid var(--hairline); border-radius: 999px;
            font-size: 11.5px; background: var(--surface-2); color: var(--ink-3); max-width: 260px;
        }
        .ufd-sa .mix-filter-bar .mix-count { font-size: 10.5px; color: var(--ink-mute); }

        /* ─── Per-item cut stage filter ─── */
        .ufd-sa .item-batch-filter {
            display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
            padding: 0 0 9px; margin-bottom: 0; font-size: 11px;
        }
        .ufd-sa .item-batch-filter .ibf-label {
            font: 600 9px var(--sans); text-transform: uppercase; letter-spacing: 1px;
            color: var(--ink-faint); white-space: nowrap;
        }
        .ufd-sa .item-batch-filter .ibf-group { display: flex; align-items: center; gap: 4px; }
        .ufd-sa .item-batch-filter select.ibf-select {
            padding: 3px 8px; border: 1px solid var(--hairline); border-radius: 999px;
            font-size: 11px; background: var(--surface-2); color: var(--ink-3); cursor: pointer;
        }
        .ufd-sa .item-batch-filter .ibf-range-sep { font-size: 10px; color: var(--ink-faint); }
        .ufd-sa .item-batch-filter .ibf-clear {
            padding: 3px 10px; border: 1px solid var(--hairline); border-radius: 999px;
            background: var(--surface-2); cursor: pointer; font-size: 10px; color: var(--ink-4);
            transition: all 0.15s;
        }
        .ufd-sa .item-batch-filter .ibf-clear:hover { background: var(--bad); border-color: var(--bad); color: #fff; }
        .ufd-sa .item-batch-filter .ibf-count { margin-left: auto; font-size: 10.5px; color: var(--ink-mute); }

        /* ─── Team select ─── */
        .ufd-sa .item-team-select {
            font-size: 11.5px; padding: 4px 11px; border-radius: 999px;
            background: var(--surface-2); color: var(--ink-3); cursor: pointer;
        }
        .ufd-sa .item-team-select.is-set { border: 1px solid var(--good); }
        .ufd-sa .item-team-select.is-unset { border: 1px solid var(--bad); }

        /* ─── Substitute picker ─── */
        .ufd-sa .substitute-btn, .ufd-sa .sa-mini-btn {
            padding: 4px 12px; border: 1px solid var(--hairline); border-radius: 999px;
            background: var(--surface-2); color: var(--ink-4); cursor: pointer;
            font: 600 11px var(--sans); transition: all 0.15s; white-space: nowrap;
        }
        .ufd-sa .substitute-btn:hover, .ufd-sa .sa-mini-btn:hover { background: var(--ink); border-color: var(--ink); color: #fafaf6; }
        .ufd-sa .sa-mini-btn.primary { background: var(--grad-ink); border-color: transparent; color: #fafaf6; }
        .ufd-sa .sa-mini-btn.primary:hover { box-shadow: 0 3px 10px rgba(10,10,10,0.2); }
        .ufd-sa .sub-variety-card {
            border: 1px solid var(--hairline); border-radius: 12px; padding: 11px 14px;
            margin-bottom: 7px; cursor: pointer; transition: all 0.15s;
            display: flex; justify-content: space-between; align-items: center; gap: 12px;
        }
        .ufd-sa .sub-variety-card:hover { border-color: var(--ink-faint); }
        .ufd-sa .sub-variety-card.recommended { box-shadow: inset 3px 0 0 var(--signal); background: var(--signal-soft); }
        .ufd-sa .sub-variety-card .sub-name { font-weight: 600; font-size: 13px; color: var(--ink); }
        .ufd-sa .sub-variety-card .sub-meta { font-size: 11px; color: var(--ink-mute); margin-top: 2px; }
        .ufd-sa .sub-variety-card .sub-badge {
            font-size: 9px; padding: 2px 8px; border-radius: 999px;
            font-weight: 600; text-transform: uppercase; white-space: nowrap;
        }

        /* ─── Buttons & dialogs, scoped so the rest of Desk is untouched ─── */
        .ufd-sa .btn { font-family: var(--sans); border-radius: 999px !important; font-weight: 600; }
        .ufd-sa .btn-primary, .ufd-sa .btn-primary:focus { background: var(--grad-ink) !important; border-color: transparent !important; box-shadow: 0 2px 8px rgba(10,10,10,0.15); }
        .ufd-sa .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 14px rgba(10,10,10,0.22); }
        .ufd-sa .btn-default { background: var(--surface-2) !important; border-color: var(--hairline) !important; color: var(--ink-3) !important; }
        .ufd-sa .btn-default:hover { background: var(--ink) !important; color: #fafaf6 !important; }
        .ufd-sa .btn-danger, .ufd-sa .btn-danger:focus { background: var(--bad) !important; border-color: transparent !important; }
        .ufd-sa .btn-danger:hover { background: var(--bad-2) !important; }
        .modal-dialog.ufd-sa-modal .modal-content { border-radius: 18px; border: 0; box-shadow: var(--shadow-hover); font-family: var(--sans); }
        .modal-dialog.ufd-sa-modal .modal-header { border-bottom: 1px solid var(--hairline); }
        .modal-dialog.ufd-sa-modal .modal-title { font: 600 16px var(--sans); color: var(--ink); letter-spacing:-.3px; }
        .modal-dialog.ufd-sa-modal .modal-footer { border-top: 1px solid var(--hairline); }
        .modal-dialog.ufd-sa-modal, .modal-dialog.ufd-sa-modal input, .modal-dialog.ufd-sa-modal select, .modal-dialog.ufd-sa-modal textarea { font-family: var(--sans); }
        .modal-dialog.ufd-sa-modal .control-input, .modal-dialog.ufd-sa-modal textarea.form-control { border-radius: 10px; border-color: var(--hairline); }

        /* ─── Narrow screens ─── */
        @media (max-width: 1240px) {
            .ufd-sa.sales-allocation-container { height: auto; }
            .ufd-sa .sa-shell { grid-template-columns: 1fr; }
            .ufd-sa .sa-rail { max-height: 380px; }
            .ufd-sa .sa-detail { min-height: 72vh; }
            .ufd-sa .sa-detail-body { grid-template-columns: 1fr; }
            .ufd-sa .sa-lines { border-right: 0; border-bottom: 1px solid var(--hairline); max-height: 200px; }
            .ufd-sa .stat-strip { flex-wrap: wrap; }
            .ufd-sa .stat { flex: 1 0 33%; }
        }
    `;
    document.head.appendChild(styleEl);
};

frappe.pages['sales-allocation'].make = function (page) {
    frappe.pages['sales-allocation'].page = page;
    let $container = $('<div class="sales-allocation-container ufd-sa"></div>').appendTo(page.main);
    page.add_inner_button(__('Refresh'), function () {
        frappe.pages['sales-allocation'].load_sales_orders();
    });
    $container.html(`
        <div class="sa-toolbar">
            <div class="sa-tb-group">
                <span class="sa-tb-label">Location</span>
                <div class="location-buttons" id="locationButtons">
                    <span style="padding:5px 12px;font-size:11px;color:var(--ink-mute);">Loading…</span>
                </div>
            </div>
            <div class="sa-tb-div"></div>
            <div class="sa-tb-group">
                <span class="sa-tb-label">Delivery</span>
                <input type="date" id="deliveryStartDate" class="sa-inp sa-date">
                <span class="sa-to">to</span>
                <input type="date" id="deliveryEndDate" class="sa-inp sa-date">
            </div>
            <div class="sa-tb-group" id="postingWrap" style="display:none;">
                <span class="sa-tb-label">Posting</span>
                <input type="date" id="orderStartDate" class="sa-inp sa-date">
                <span class="sa-to">to</span>
                <input type="date" id="orderEndDate" class="sa-inp sa-date">
            </div>
            <button class="sa-ghost-btn" id="togglePosting">Add posting date</button>
            <div class="location-info" id="locationInfo" style="display:none;"></div>
        </div>
        <div class="sa-shell">
            <aside class="sa-rail">
                <div class="rail-head">
                    <h3>Orders</h3>
                    <div id="resultsCount"></div>
                </div>
                <div class="rail-filters">
                    <input type="text" id="orderSearchInput" class="sa-inp" placeholder="Search order, customer, variety…">
                    <div class="rf-row">
                        <select id="priorityFilter" class="sa-inp">
                            <option value="">All priorities</option>
                            <option value="High">High</option>
                            <option value="Medium">Medium</option>
                            <option value="Low">Low</option>
                        </select>
                        <select id="boxTypeFilter" class="sa-inp">
                            <option value="">All box types</option>
                            <option value="mixed">Mixed only</option>
                            <option value="straight">Straight only</option>
                        </select>
                        <button class="rf-clear" id="clearFilters" title="Clear filters">&times;</button>
                    </div>
                </div>
                <div class="sales-order-list" id="salesOrderList">
                    <div class="empty-state"><p>Select a location to begin.</p></div>
                </div>
            </aside>
            <section class="sa-detail" id="salesAllocationDetail">
                <header class="sa-detail-head" id="detailHead"></header>
                <div class="sa-detail-toolbar" id="detailToolbar" style="display:none;"></div>
                <div class="sa-detail-body">
                    <nav class="sa-lines" id="linesRail"></nav>
                    <div class="sa-work" id="allocationWorkspace"></div>
                </div>
                <footer class="sa-actionbar" id="actionBar"></footer>
            </section>
        </div>
    `);
    // State
    const P = frappe.pages['sales-allocation'];
    P.current_sales_orders = [];
    P.selected_order = null;
    P.order_items = [];
    P.allocations = [];
    P.selected_location = null;
    P.location_config = null;
    P.selected_farms = [];
    // Which order line is showing its bucket table in the right pane
    P.selected_item = null;
    P._preserve_item = null;
    // Mix group filter ("" = show all, otherwise custom_mix_group number as string)
    P.selected_mix_group = '';
    // Per-item batch filter state: keyed by sales_order_item
    // Each entry: { cut_stage_min: '', cut_stage_max: '' }
    P.item_filters = {};
    P.item_teams = {};
    const tomorrow = frappe.datetime.add_days(frappe.datetime.get_today(), 1);
    P.filters = { search: '', priority: '', box_type: '', order_start: '', order_end: '', delivery_start: tomorrow, delivery_end: tomorrow };
    $('#deliveryStartDate').val(tomorrow);
    $('#deliveryEndDate').val(tomorrow);
    P.render_allocation_grid();
    P.load_location_config();
    $('#orderSearchInput').on('input', function () { P.filters.search = $(this).val(); P.apply_filters(); });
    $('#priorityFilter').on('change', function () { P.filters.priority = $(this).val(); P.apply_filters(); });
    $('#boxTypeFilter').on('change', function () { P.filters.box_type = $(this).val(); P.apply_filters(); });
    $('#orderStartDate, #orderEndDate').on('change', function () {
        P.filters.order_start = $('#orderStartDate').val();
        P.filters.order_end = $('#orderEndDate').val();
        if (P.selected_location) P.load_sales_orders();
    });
    $('#deliveryStartDate, #deliveryEndDate').on('change', function () {
        P.filters.delivery_start = $('#deliveryStartDate').val();
        P.filters.delivery_end = $('#deliveryEndDate').val();
        if (P.selected_location) P.load_sales_orders();
    });
    // Posting date is a secondary filter — hidden until asked for.
    $('#togglePosting').on('click', function () {
        const $w = $('#postingWrap');
        const was_open = $w.is(':visible');
        if (was_open) {
            $w.hide();
            $('#orderStartDate, #orderEndDate').val('');
            const had_value = !!(P.filters.order_start || P.filters.order_end);
            P.filters.order_start = '';
            P.filters.order_end = '';
            $(this).text('Add posting date');
            if (had_value && P.selected_location) P.load_sales_orders();
        } else {
            $w.css('display', 'flex');
            $(this).text('Remove posting date');
        }
    });
    $('#clearFilters').on('click', function () {
        $('#orderSearchInput, #priorityFilter, #boxTypeFilter, #orderStartDate, #orderEndDate, #deliveryStartDate, #deliveryEndDate').val('');
        $('#postingWrap').hide();
        $('#togglePosting').text('Add posting date');
        P.filters = { search: '', priority: '', box_type: '', order_start: '', order_end: '', delivery_start: '', delivery_end: '' };
        if (P.selected_location) P.load_sales_orders();
    });
};

// jQuery scope for everything inside the detail pane (toolbar + lines + workspace).
frappe.pages['sales-allocation']._scope = function () {
    return $('#salesAllocationDetail');
};

// ─── LOCATION CONFIG ───
frappe.pages['sales-allocation'].load_location_config = function () {
    frappe.call({
        method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.get_location_config',
        callback: function (r) {
            if (r.message && r.message.locations) {
                frappe.pages['sales-allocation'].location_config = r.message;
                frappe.pages['sales-allocation'].render_location_selector();
            } else {
                $('#locationButtons').html('<span style="padding:5px 12px;font-size:11px;color:var(--bad);">No locations configured</span>');
            }
        },
        error: function () {
            $('#locationButtons').html('<span style="padding:5px 12px;font-size:11px;color:var(--bad);">Failed to load</span>');
        }
    });
};
frappe.pages['sales-allocation'].render_location_selector = function () {
    const config = frappe.pages['sales-allocation'].location_config;
    if (!config || !config.locations) return;
    const html = config.locations.map(loc => {
        const is_active = frappe.pages['sales-allocation'].selected_location === loc.name ? 'active' : '';
        return `<button class="location-btn ${is_active}" data-location="${loc.name}" title="${loc.farms.length} farm(s)">${loc.name}</button>`;
    }).join('');
    $('#locationButtons').html(html);
    $('.location-btn').on('click', function () {
        frappe.pages['sales-allocation'].select_location($(this).data('location'));
    });
};
frappe.pages['sales-allocation'].select_location = function (location) {
    const P = frappe.pages['sales-allocation'];
    P.selected_location = location;
    const loc_config = P.location_config.locations.find(l => l.name === location);
    P.selected_farms = loc_config ? [...loc_config.farms] : [];
    $('.location-btn').removeClass('active');
    $(`.location-btn[data-location="${location}"]`).addClass('active');
    if (loc_config) {
        const farm_labels = loc_config.farm_details
            .map(f => `${f.farm} (${f.sales_shelf ? 'Sales' : 'Remote'})`)
            .join(', ');
        $('#locationInfo').show().attr('title', farm_labels).html(`Farms: ${farm_labels}`);
    }
    P.selected_order = null;
    P.allocations = [];
    P.order_items = [];
    P.selected_item = null;
    P.render_allocation_grid();
    P.load_sales_orders();
};
// ─── SALES ORDERS ───
frappe.pages['sales-allocation'].load_sales_orders = function () {
    const P = frappe.pages['sales-allocation'];
    if (!P.selected_location) {
        $('#salesOrderList').html('<div class="empty-state"><p>Select a location to begin.</p></div>');
        return;
    }
    $('#salesOrderList').html('<div class="loading-state">Loading orders…</div>');
    frappe.call({
        method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.get_pending_sales_orders',
        args: {
            start_date: P.filters.order_start || null,
            end_date: P.filters.order_end || null,
            delivery_start: P.filters.delivery_start || null,
            delivery_end: P.filters.delivery_end || null
        },
        callback: function (r) {
            if (r.message && r.message.length) {
                P.current_sales_orders = r.message;
                P.apply_filters();
            } else {
                P.current_sales_orders = [];
                $('#resultsCount').text('0 orders');
                $('#salesOrderList').html('<div class="empty-state"><p>No pending orders for these dates.</p></div>');
            }
        }
    });
};
frappe.pages['sales-allocation'].apply_filters = function () {
    const P = frappe.pages['sales-allocation'];
    let orders = P.current_sales_orders;
    if (P.filters.search) {
        const term = P.filters.search.toLowerCase();
        orders = orders.filter(o =>
            (o.name || '').toLowerCase().includes(term) ||
            (o.customer || '').toLowerCase().includes(term) ||
            (o.custom_order_name || '').toLowerCase().includes(term) ||
            (o.item_codes || '').toLowerCase().includes(term)
        );
    }
    if (P.filters.priority) orders = orders.filter(o => (o.custom_priority || 'Low') === P.filters.priority);
    if (P.filters.box_type === 'mixed') orders = orders.filter(o => (o.has_mixed || 0) === 1);
    else if (P.filters.box_type === 'straight') orders = orders.filter(o => (o.has_straight || 0) === 1);
    const total = (P.current_sales_orders || []).length;
    $('#resultsCount').text(orders.length === total ? `${total} orders` : `${orders.length} of ${total}`);
    P.render_orders(orders);
};
frappe.pages['sales-allocation'].render_orders = function (orders) {
    const $list = $('#salesOrderList');
    const selected = frappe.pages['sales-allocation'].selected_order;
    if (!orders.length) { $list.html('<div class="empty-state"><p>No orders match these filters.</p></div>'); return; }
    const html = orders.map(order => {
        const priority = order.custom_priority || 'Low';
        const pri_class = 'pri-' + priority.toLowerCase();
        const is_selected = selected === order.name ? 'selected' : '';
        const pct = order.allocation_percentage || 0;
        const bar_color = pct >= 75 ? 'var(--good)' : pct >= 50 ? 'var(--warn-2)' : 'var(--bad)';
        const sub = [order.customer, order.custom_order_name].filter(Boolean).join(' · ');
        return `
            <div class="sales-order-card ${is_selected}" data-order="${order.name}">
                <div class="soc-top">
                    <span class="soc-id">${order.name}</span>
                    <span class="soc-pri ${pri_class}">${priority}</span>
                </div>
                <div class="soc-line" title="${sub}">${sub || '—'}</div>
                <div class="soc-line muted" title="${order.item_codes || ''}">${order.item_codes || '—'}</div>
                <div class="soc-foot">
                    <span>${frappe.datetime.str_to_user(order.delivery_date)}</span>
                    <span style="font-weight:600;color:${bar_color};">${pct}%</span>
                </div>
                <div class="soc-bar"><div style="width:${pct}%;height:100%;background:${bar_color};"></div></div>
            </div>`;
    }).join('');
    $list.html(html);
    $list.find('.sales-order-card').on('click', function () {
        frappe.pages['sales-allocation'].select_order($(this).data('order'));
    });
};
// ─── SELECT ORDER & LOAD ITEMS ───
// opts.keep  — keep the currently selected order line after a reload
// opts.force — skip the "unconfirmed session allocations" guard
frappe.pages['sales-allocation'].select_order = function (order_name, opts) {
    const P = frappe.pages['sales-allocation'];
    opts = opts || {};
    if (!P.selected_location) { frappe.msgprint('Please select a location first'); return; }
    const switching = P.selected_order && P.selected_order !== order_name;
    if (!opts.force && switching && (P.allocations || []).length) {
        frappe.confirm(
            __('You have {0} unconfirmed stems allocated on <b>{1}</b>. Switching orders discards them. Continue?',
               [P._session_total(), P.selected_order]),
            () => P.select_order(order_name, { keep: opts.keep, force: 1 })
        );
        return;
    }
    P._preserve_item = opts.keep ? P.selected_item : null;
    P.allocations = [];
    P.selected_order = order_name;
    P.selected_item = null;
    // Reset filters
    P.item_filters = {};
    P.selected_mix_group = '';
    P.apply_filters();
    P._load_available_filters_and_open_dialog();
};
frappe.pages['sales-allocation'].close_detail = function () {
    const P = frappe.pages['sales-allocation'];
    const finish = () => {
        P.allocations = [];
        P.selected_order = null;
        P.order_items = [];
        P.selected_item = null;
        P.item_teams = {};
        P.apply_filters();
        P.render_allocation_grid();
    };
    if ((P.allocations || []).length) {
        frappe.confirm(
            __('Discard {0} unconfirmed stems allocated on <b>{1}</b>?', [P._session_total(), P.selected_order]),
            finish
        );
        return;
    }
    finish();
};
// ─── Load items and render the detail pane ───
frappe.pages['sales-allocation']._load_available_filters_and_open_dialog = function () {
    const P = frappe.pages['sales-allocation'];
    // Filters are per-item and derived from batch data client-side
    P._fetch_items_and_open_dialog();
};
frappe.pages['sales-allocation']._fetch_items_and_open_dialog = function () {
    const P = frappe.pages['sales-allocation'];
    $('#allocationWorkspace').html('<div class="loading-state">Loading buckets…</div>');
    frappe.call({
        method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.get_sales_order_items_with_buckets',
        args: {
            sales_order: P.selected_order,
            location: P.selected_location,
            selected_farms: JSON.stringify(P.selected_farms)
        },
        callback: function (r) {
            if (r.message && r.message.length) {
                P.order_items = r.message.map(item => ({
                    ...item,
                    batches: (item.batches || []).map(b => ({
                        ...b,
                        original_available_qty: b.available_qty || 0
                    }))
                }));
                P.show_allocation_panel();
            } else {
                P.order_items = [];
                P.selected_item = null;
                P.render_allocation_grid();
                frappe.msgprint('No items with confirmed stems for this location.');
            }
        },
        error: function () {
            P.order_items = [];
            P.render_allocation_grid();
            frappe.msgprint('Failed to load items for allocation.');
        }
    });
};
// ─── ALLOCATION PANEL ───
frappe.pages['sales-allocation'].show_allocation_panel = function () {
    const P = frappe.pages['sales-allocation'];
    // Per-line team selections (sales_order_item -> team); reset on each load
    P.item_teams = {};
    P.selected_item = P._preserve_item || null;
    P._preserve_item = null;
    P.render_allocation_grid();
};
// Back-compat alias in case anything else calls the old name.
frappe.pages['sales-allocation'].show_allocation_dialog = frappe.pages['sales-allocation'].show_allocation_panel;

// ─── FARM FILTER ───
frappe.pages['sales-allocation']._render_farm_filter = function () {
    const P = frappe.pages['sales-allocation'];
    if (!P.location_config) return '';
    const loc = P.location_config.locations.find(l => l.name === P.selected_location);
    if (!loc || loc.farms.length <= 1) return '';
    const checks = loc.farm_details.map(f => {
        const checked = P.selected_farms.includes(f.farm);
        const badge_class = f.sales_shelf ? 'sales' : 'remote';
        const badge_text = f.sales_shelf ? 'Sales' : 'Remote';
        return `
            <div class="farm-checkbox-item ${checked ? 'checked' : ''}" data-farm="${f.farm}">
                <span>${f.farm}</span>
                <span class="farm-badge ${badge_class}">${badge_text}</span>
            </div>`;
    }).join('');
    return `
        <div class="farm-filter-bar">
            <label class="title" title="Changing farms reloads bucket data and clears session allocations">Farms</label>
            <div class="farm-checkbox-group" id="farmFilterGroup">${checks}</div>
        </div>`;
};
frappe.pages['sales-allocation']._bind_farm_filter = function () {
    const P = frappe.pages['sales-allocation'];
    P._scope().find('.farm-checkbox-item').on('click', function () {
        const farm = $(this).data('farm');
        const idx = P.selected_farms.indexOf(farm);
        if (idx > -1) {
            if (P.selected_farms.length === 1) { frappe.msgprint('At least one farm must be selected.'); return; }
            P.selected_farms.splice(idx, 1);
        } else {
            P.selected_farms.push(farm);
        }
        P.clear_allocations(true);
        P.item_filters = {};
        frappe.call({
            method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.get_sales_order_items_with_buckets',
            args: {
                sales_order: P.selected_order,
                location: P.selected_location,
                selected_farms: JSON.stringify(P.selected_farms)
            },
            freeze: false,
            callback: function (r) {
                if (r.message) {
                    P.order_items = r.message.map(item => ({
                        ...item,
                        batches: (item.batches || []).map(b => ({ ...b, original_available_qty: b.available_qty || 0 }))
                    }));
                }
                P.render_allocation_grid();
            }
        });
    });
};
// ─── MIX GROUP FILTER ───
frappe.pages['sales-allocation']._render_mix_filter = function () {
    const P = frappe.pages['sales-allocation'];
    const items = P.order_items || [];
    // Collect unique mix groups present in this order
    const seen = new Map();
    items.forEach(it => {
        if (!it.custom_mixed_box) return;
        const key = String(it.custom_mix_group || '');
        if (!key) return;
        if (!seen.has(key)) {
            seen.set(key, it.custom_mix_name || `Group ${key}`);
        }
    });
    if (seen.size === 0) return '';
    const options = [...seen.entries()].map(([key, label]) => {
        const selected = String(P.selected_mix_group || '') === key ? 'selected' : '';
        const safe_label = frappe.utils.escape_html(label);
        return `<option value="${key}" ${selected}>${safe_label}</option>`;
    }).join('');
    const all_selected = !P.selected_mix_group ? 'selected' : '';
    return `
        <div class="mix-filter-bar">
            <label class="title">Mix</label>
            <select id="mixGroupFilter">
                <option value="" ${all_selected}>All mixes and straight boxes</option>
                ${options}
            </select>
            <span class="mix-count">${seen.size} mix${seen.size === 1 ? '' : 'es'}</span>
        </div>`;
};
frappe.pages['sales-allocation']._bind_mix_filter = function () {
    const P = frappe.pages['sales-allocation'];
    P._scope().find('#mixGroupFilter').on('change', function () {
        P.selected_mix_group = $(this).val() || '';
        P.selected_item = null;
        P.render_allocation_grid();
    });
};
// ─── PER-ITEM CUT STAGE FILTER ───
// Fixed range steps from 1.5 to 4, always visible
frappe.pages['sales-allocation'].CUT_STAGE_STEPS = [1.5, 2, 2.5, 3, 3.5, 4];
frappe.pages['sales-allocation']._get_item_filter = function (so_item) {
    const P = frappe.pages['sales-allocation'];
    if (!P.item_filters[so_item]) {
        P.item_filters[so_item] = { cut_stage_min: '', cut_stage_max: '' };
    }
    return P.item_filters[so_item];
};
frappe.pages['sales-allocation']._render_per_item_filter = function (item) {
    const P = frappe.pages['sales-allocation'];
    const so_item = item.sales_order_item;
    const steps = P.CUT_STAGE_STEPS;
    const f = P._get_item_filter(so_item);
    const batches = item.batches || [];
    const shown = P._has_active_filter(so_item)
        ? batches.filter(b => P._batch_passes_filter(b, item)).length
        : batches.length;
    const min_opts = steps.map(s => {
        const v = s.toString();
        return `<option value="${v}" ${f.cut_stage_min === v ? 'selected' : ''}>${v}</option>`;
    }).join('');
    const max_opts = steps.map(s => {
        const v = s.toString();
        return `<option value="${v}" ${f.cut_stage_max === v ? 'selected' : ''}>${v}</option>`;
    }).join('');
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
frappe.pages['sales-allocation']._bind_per_item_filters = function () {
    const P = frappe.pages['sales-allocation'];
    const $w = P._scope();
    // Cut stage range dropdowns
    $w.find('select.ibf-cs-select').on('change', function () {
        const so_item = $(this).data('so-item');
        const field = $(this).data('filter');
        const val = $(this).val();
        const f = P._get_item_filter(so_item);
        f[field] = val;
        P._apply_item_batch_visibility(so_item);
    });
    // Per-item clear button
    $w.find('.ibf-clear[data-so-item]').on('click', function () {
        const so_item = $(this).data('so-item');
        if (!so_item) return;
        P.item_filters[so_item] = { cut_stage_min: '', cut_stage_max: '' };
        const $bar = $w.find(`.item-batch-filter[data-so-item="${so_item}"]`);
        $bar.find('select.ibf-cs-select').val('');
        P._apply_item_batch_visibility(so_item);
    });
    // Substitute variety buttons
    $w.find('.substitute-btn').on('click', function () {
        const so_item = $(this).data('so-item');
        P._show_substitute_dialog(so_item);
    });
};
// ─── BATCH VISIBILITY ───
frappe.pages['sales-allocation']._batch_passes_filter = function (batch, item) {
    const P = frappe.pages['sales-allocation'];
    const f = P._get_item_filter(item.sales_order_item);
    // Cut stage range — compares against shelf cut_stage only
    if (f.cut_stage_min !== '' || f.cut_stage_max !== '') {
        const raw = batch.cut_stage;
        // If the shelf has no cut_stage, hide this batch when filter is active
        if (raw == null || raw === '') return false;
        const cs = parseFloat(raw);
        if (isNaN(cs)) return false;
        if (f.cut_stage_min !== '' && cs < parseFloat(f.cut_stage_min)) return false;
        if (f.cut_stage_max !== '' && cs > parseFloat(f.cut_stage_max)) return false;
    }
    return true;
};
frappe.pages['sales-allocation']._has_active_filter = function (so_item) {
    const P = frappe.pages['sales-allocation'];
    const f = P._get_item_filter(so_item);
    return f.cut_stage_min !== '' || f.cut_stage_max !== '';
};
frappe.pages['sales-allocation']._apply_item_batch_visibility = function (so_item) {
    const P = frappe.pages['sales-allocation'];
    const $w = P._scope();
    const item = (P.order_items || []).find(i => i.sales_order_item === so_item);
    if (!item) return;
    const has_filter = P._has_active_filter(so_item);
    const $table = $w.find(`table.allocation-grid-table[data-so-item="${so_item}"]`);
    let shown = 0, total = 0;
    $table.find('tbody tr').each(function () {
        const bucket_id = $(this).data('bucket-id');
        if (!bucket_id) return;
        const batch = (item.batches || []).find(b => b.bucket_id === bucket_id);
        if (!batch) return;
        total += 1;
        if (!has_filter || P._batch_passes_filter(batch, item)) {
            $(this).show();
            shown += 1;
        } else {
            $(this).hide();
        }
    });
    $w.find(`.item-batch-filter[data-so-item="${so_item}"] .ibf-count`)
        .text(`${shown} of ${total} buckets`);
};
// ─── SUBSTITUTE VARIETY ───
frappe.pages['sales-allocation']._show_substitute_dialog = function (so_item) {
    const P = frappe.pages['sales-allocation'];
    const item = (P.order_items || []).find(i => i.sales_order_item === so_item);
    if (!item) return;
    const original_color = item.color || '';
    const original_headsize = item.headsize || '';
    // Start with recommended filter (same color + headsize)
    let filter_active = !!(original_color || original_headsize);
    const sub_dialog = new frappe.ui.Dialog({
        title: `Substitute variety: ${item.item_name || item.item_code}`,
        size: 'large',
        fields: [
            { fieldtype: 'HTML', fieldname: 'sub_content' }
        ]
    });
    sub_dialog.$wrapper.addClass('ufd-sa');
    sub_dialog.$wrapper.find('.modal-dialog').addClass('ufd-sa-modal');
    const render_sub_content = function (varieties, show_filter) {
        let filter_html = '';
        if (original_color || original_headsize) {
            const filter_label = [
                original_color ? `Color: ${original_color}` : '',
                original_headsize ? `Headsize: ${original_headsize}` : ''
            ].filter(Boolean).join(', ');
            filter_html = `
                <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;padding:9px 14px;background:${show_filter ? 'var(--good-soft)' : 'var(--surface)'};border-radius:10px;font-size:12px;">
                    <div>
                        <strong>Recommended:</strong> ${filter_label}
                        ${show_filter ? '<span style="color:var(--good);margin-left:6px;">active</span>' : '<span style="color:var(--ink-mute);margin-left:6px;">cleared</span>'}
                    </div>
                    <button class="sa-mini-btn" id="toggleSubFilter">
                        ${show_filter ? 'Show all varieties' : 'Show recommended only'}
                    </button>
                </div>`;
        }
        const current_html = `
            <div style="background:var(--surface);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:12px;color:var(--ink-3);">
                <strong>Current:</strong> ${item.item_name} (${item.item_code})
                ${original_color ? ` · Color: ${original_color}` : ''}
                ${original_headsize ? ` · Headsize: ${original_headsize}` : ''}
                · Qty: ${item.pending_stock_qty || 0} ${item.stock_uom || ''}
            </div>`;
        let list_html = '';
        if (!varieties || !varieties.length) {
            list_html = '<div class="empty-state"><p>No varieties found.</p></div>';
        } else {
            list_html = varieties.map(v => {
                const is_same = v.item_code === item.item_code;
                const is_recommended = (original_color && v.color === original_color) || (original_headsize && v.headsize === original_headsize);
                const rec_class = is_recommended && !is_same ? 'recommended' : '';
                const badges = [];
                if (is_same) badges.push('<span class="sub-badge" style="background:rgba(10,10,10,0.06);color:var(--ink-mute);">Current</span>');
                if (is_recommended && !is_same) badges.push('<span class="sub-badge" style="background:var(--signal-soft);color:var(--signal);">Recommended</span>');
                const meta = [
                    v.color ? `Color: ${v.color}` : '',
                    v.headsize ? `Headsize: ${v.headsize}` : '',
                    v.available_qty != null ? `Available: ${v.available_qty}` : ''
                ].filter(Boolean).join(' · ');
                return `
                    <div class="sub-variety-card ${rec_class} ${is_same ? '' : 'selectable'}" data-item-code="${v.item_code}" data-item-name="${v.item_name || v.item_code}" ${is_same ? 'style="opacity:0.55;cursor:default;"' : ''}>
                        <div>
                            <div class="sub-name">${v.item_name || v.item_code}</div>
                            <div class="sub-meta">${meta || '—'}</div>
                        </div>
                        <div>${badges.join(' ')}</div>
                    </div>`;
            }).join('');
        }
        sub_dialog.fields_dict.sub_content.$wrapper.html(current_html + filter_html + list_html);
        // Bind toggle
        sub_dialog.$wrapper.find('#toggleSubFilter').on('click', function () {
            filter_active = !filter_active;
            load_varieties();
        });
        // Bind selection
        sub_dialog.$wrapper.find('.sub-variety-card.selectable').on('click', function () {
            const new_code = $(this).data('item-code');
            const new_name = $(this).data('item-name');
            if (!new_code) return;
            frappe.confirm(
                `Substitute <strong>${item.item_name}</strong> with <strong>${new_name}</strong> on this sales order?`,
                () => {
                    sub_dialog.hide();
                    P._execute_substitute(so_item, new_code, new_name);
                }
            );
        });
    };
    const load_varieties = function () {
        sub_dialog.fields_dict.sub_content.$wrapper.html('<div class="loading-state">Loading varieties…</div>');
        const args = {
            sales_order: P.selected_order,
            sales_order_item: so_item,
            item_code: item.item_code,
            location: P.selected_location
        };
        if (filter_active) {
            if (original_color) args.color = original_color;
            if (original_headsize) args.headsize = original_headsize;
        }
        frappe.call({
            method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.get_substitute_varieties',
            args: args,
            callback: function (r) {
                render_sub_content(r.message || [], filter_active);
            },
            error: function () {
                render_sub_content([], filter_active);
            }
        });
    };
    sub_dialog.show();
    load_varieties();
};
frappe.pages['sales-allocation']._execute_substitute = function (so_item, new_item_code, new_item_name) {
    const P = frappe.pages['sales-allocation'];
    frappe.call({
        method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.substitute_variety',
        args: {
            sales_order: P.selected_order,
            sales_order_item: so_item,
            new_item_code: new_item_code
        },
        freeze: true,
        freeze_message: `Substituting with ${new_item_name}…`,
        callback: function (r) {
            if (r.message && r.message.success) {
                frappe.show_alert({ message: `Substituted to ${new_item_name}`, indicator: 'green' });
                // Reload the order items
                P.allocations = [];
                P.item_filters = {};
                P.select_order(P.selected_order, { keep: 1, force: 1 });
            } else {
                frappe.msgprint({
                    title: 'Substitution failed',
                    message: r.message?.message || 'Could not substitute variety.',
                    indicator: 'red'
                });
            }
        },
        error: function () {
            frappe.msgprint({ title: 'Error', message: 'Substitution request failed.', indicator: 'red' });
        }
    });
};
// ─── RENDER: DETAIL HEAD ───
frappe.pages['sales-allocation']._render_detail_head = function (items) {
    const P = frappe.pages['sales-allocation'];
    const so = (P.current_sales_orders || []).find(o => o.name === P.selected_order) || {};
    let required = 0, done = 0;
    (items || []).forEach(it => {
        required += it.pending_stock_qty || 0;
        done += (it.total_allocated_qty || 0) + P._session_qty(it.sales_order_item);
    });
    const pct = required > 0 ? Math.min(100, Math.round((done / required) * 100)) : 0;
    const bar_color = pct >= 75 ? 'var(--good)' : pct >= 50 ? 'var(--warn-2)' : 'var(--bad)';
    const meta = [
        so.customer,
        so.custom_order_name,
        so.delivery_date ? 'delivery ' + frappe.datetime.str_to_user(so.delivery_date) : '',
        `${(items || []).length} line(s)`
    ].filter(Boolean).join(' · ');
    return `
        <div style="flex:1;min-width:0;">
            <div style="font:600 15px var(--sans);color:var(--ink);letter-spacing:-.2px;">
                ${P.selected_order}
                <span class="pill pill-ink" style="margin-left:8px;">${P.selected_location || ''}</span>
            </div>
            <div style="font-size:11px;color:var(--ink-mute);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${meta}</div>
        </div>
        <div style="width:132px;flex-shrink:0;">
            <div style="display:flex;justify-content:space-between;font-size:9.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--ink-faint);margin-bottom:5px;">
                <span>Allocated</span><span style="font-weight:600;color:${bar_color};">${pct}%</span>
            </div>
            <div style="width:100%;height:4px;background:rgba(10,10,10,0.06);border-radius:999px;overflow:hidden;">
                <div style="width:${pct}%;height:100%;background:${bar_color};"></div>
            </div>
        </div>
        <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation'].close_detail()">Close</button>`;
};
// ─── RENDER: ORDER LINES RAIL ───
frappe.pages['sales-allocation']._render_lines_rail = function (items) {
    const P = frappe.pages['sales-allocation'];
    if (!items.length) {
        return `<div class="sa-lines-title">Order lines</div>
                <div style="font-size:11px;color:var(--ink-mute);padding:4px 2px;">Nothing to show.</div>`;
    }
    const cards = items.map(item => {
        const required = item.pending_stock_qty || 0;
        const grand = (item.total_allocated_qty || 0) + P._session_qty(item.sales_order_item);
        const pct = required > 0 ? Math.min(100, Math.round((grand / required) * 100)) : 0;
        const bar_color = pct >= 100 ? 'var(--good)' : pct > 0 ? 'var(--warn-2)' : 'var(--bad)';
        const is_active = item.sales_order_item === P.selected_item ? 'active' : '';
        let tag;
        if (item.custom_mixed_bunch) {
            tag = `<span class="lc-tag" style="background:var(--signal-soft);color:var(--signal);">Mixed bunch</span>`;
        } else if (item.custom_mixed_box) {
            tag = `<span class="lc-tag" style="background:var(--signal-soft);color:var(--signal);">Mixed box</span>`;
        } else {
            tag = `<span class="lc-tag" style="background:rgba(10,10,10,0.05);color:var(--ink-mute);">Straight</span>`;
        }
        const needs_team = P._session_qty(item.sales_order_item) > 0
            && !(P.item_teams || {})[item.sales_order_item];
        const team_flag = needs_team
            ? `<span class="lc-tag" style="background:var(--bad-soft);color:var(--bad);margin-left:4px;">No team</span>`
            : '';
        return `
            <div class="line-card ${is_active}" data-so-item="${item.sales_order_item}">
                <div class="lc-name">${item.item_name || item.item_code || '?'}</div>
                <div class="lc-meta">${item.required_length || 'No length'} · ${grand}/${required} ${item.stock_uom || ''}</div>
                <div>${tag}${team_flag}</div>
                <div class="line-bar"><div style="width:${pct}%;height:100%;background:${bar_color};"></div></div>
            </div>`;
    }).join('');
    return `<div class="sa-lines-title">Order lines</div>${cards}`;
};
frappe.pages['sales-allocation']._bind_lines_rail = function () {
    const P = frappe.pages['sales-allocation'];
    P._scope().find('.line-card[data-so-item]').on('click', function () {
        const so_item = $(this).data('so-item');
        if (!so_item || so_item === P.selected_item) return;
        P.selected_item = so_item;
        P.render_allocation_grid();
    });
};
// ─── RENDER: ACTION BAR ───
frappe.pages['sales-allocation']._render_action_bar = function () {
    const P = frappe.pages['sales-allocation'];
    if (!P.selected_order) {
        return `<div style="font-size:12px;color:var(--ink-mute);">Nothing to confirm yet.</div>`;
    }
    const total = P._session_total();
    const lines = new Set((P.allocations || []).map(a => a.sales_order_item)).size;
    return `
        <div style="font-size:12px;color:var(--ink-3);">
            Session
            <strong style="color:${total > 0 ? 'var(--good)' : 'var(--ink-mute)'};margin:0 3px;">${total.toLocaleString()}</strong>
            stems · <strong>${lines}</strong> line(s)
        </div>
        <div class="ab-actions">
            <button class="sa-mini-btn" onclick="frappe.pages['sales-allocation'].clear_all_and_render()">Clear all</button>
            <button class="sa-mini-btn primary" onclick="frappe.pages['sales-allocation'].confirm_allocation()">Confirm allocation</button>
        </div>`;
};
// ─── RENDER: ONE ORDER LINE (right pane) ───
frappe.pages['sales-allocation']._render_item_block = function (item) {
    const P = frappe.pages['sales-allocation'];
    const allocated_session = P._session_qty(item.sales_order_item);
    const total_allocated = item.total_allocated_qty || 0;
    const grand_allocated = total_allocated + allocated_session;
    const required = item.pending_stock_qty || 0;
    const remaining = required - grand_allocated;
    const batches = item.batches || [];
    const preferred_farm = item.preferred_farm || '';
    const uom = item.stock_uom || '';
    // ── Pills: length, colour, box type, mix name, so-item id
    const pills = [];
    pills.push(`<span class="pill pill-ink">${item.required_length || 'No length'}</span>`);
    if (item.color) pills.push(`<span class="pill pill-signal">${item.color}</span>`);
    if (item.custom_mixed_bunch) {
        pills.push(`<span class="pill pill-signal">Mixed bunch</span>`);
        pills.push(`<span class="pill pill-line" title="Internal group: ${item.custom_bunch_group || '?'}">${item.custom_mix_name ? frappe.utils.escape_html(item.custom_mix_name) : 'Bunch ' + (item.custom_bunch_group || '?')}</span>`);
    } else if (item.custom_mixed_box) {
        pills.push(`<span class="pill pill-signal">Mixed box</span>`);
        pills.push(`<span class="pill pill-line" title="Internal group: ${item.custom_mix_group || '?'}">${item.custom_mix_name ? frappe.utils.escape_html(item.custom_mix_name) : 'Group ' + (item.custom_mix_group || '?')}</span>`);
    } else {
        pills.push(`<span class="pill pill-line">Straight box</span>`);
    }
    if (preferred_farm) pills.push(`<span class="pill pill-signal">Preferred: ${preferred_farm}</span>`);
    if (remaining <= 0) pills.push(`<span class="pill pill-good">Fully allocated</span>`);
    pills.push(`<span class="pill pill-mono">${item.sales_order_item || '?'}</span>`);
    // ── Actions: auto-allocate, substitute, team
    const can_fifo = remaining > 0 && batches.some(b => (b.available_qty || 0) > 0);
    const _curTeam = (P.item_teams && P.item_teams[item.sales_order_item]) || '';
    const teams = ['', 'Team A', 'Team B', 'Jamafa', 'Eldama', 'Bravo'];
    const actions = `
        ${can_fifo ? `<button class="sa-mini-btn primary" onclick="frappe.pages['sales-allocation'].auto_allocate_fifo('${item.sales_order_item}')">Auto-allocate ${remaining}</button>` : ''}
        <button class="substitute-btn" data-so-item="${item.sales_order_item}">Substitute</button>
        <select class="item-team-select ${_curTeam ? 'is-set' : 'is-unset'}" data-so-item="${item.sales_order_item}"
            onchange="frappe.pages['sales-allocation'].set_item_team('${item.sales_order_item}', this.value)"
            title="Packing team for this line">
            ${teams.map(t => `<option value="${t}" ${t === _curTeam ? 'selected' : ''}>${t || 'Select team…'}</option>`).join('')}
        </select>`;
    // ── Confirmed stems band
    const confirmed = item.confirmed_stems || 0;
    let confirmedBanner = '';
    if (confirmed > 0) {
        const chips = (item.confirmed_detail || []).map(d =>
            `<span class="confirmed-chip-alloc">${d.farm}: ${d.stems.toLocaleString()}</span>`
        ).join('');
        const originalQty = item.original_ordered_qty || item.original_stock_qty || 0;
        const othersConfirmed = item.others_confirmed || 0;
        const totalAllConfirmed = item.total_all_confirmed || 0;
        confirmedBanner = `
            <div class="note-band note-good">
                <strong>Your confirmation: ${confirmed.toLocaleString()} stems</strong>
                ${chips}
                <span class="nb-right">
                    Ordered ${originalQty.toLocaleString()}${othersConfirmed > 0 ? ` · others ${othersConfirmed.toLocaleString()}` : ''} · total confirmed ${totalAllConfirmed.toLocaleString()}
                </span>
            </div>`;
    }
    const incomingBanner = (item.incoming_exact_stems || 0) > 0
        ? `<div class="note-band note-good">
               <strong>${item.incoming_exact_stems} exact-length stems</strong>
               <span>(${item.required_length || '?'}) received but not yet shelved.</span>
           </div>`
        : '';
    let html = `
        <div class="item-head">
            <div style="min-width:0;">
                <div class="ih-title">${item.item_name || 'Item'}<span class="ih-code">${item.item_code || '?'}</span></div>
                <div class="ih-pills">${pills.join('')}</div>
            </div>
            <div class="ih-actions">${actions}</div>
        </div>
        <div class="stat-strip">
            <div class="stat"><div class="st-l">To allocate</div><div class="st-v">${required.toLocaleString()}<small>${uom}</small></div></div>
            <div class="stat"><div class="st-l">Previously</div><div class="st-v">${total_allocated.toLocaleString()}</div></div>
            <div class="stat"><div class="st-l">This session</div><div class="st-v" style="color:${allocated_session > 0 ? 'var(--good)' : 'var(--ink-faint)'};">${allocated_session.toLocaleString()}</div></div>
            <div class="stat"><div class="st-l">Remaining</div><div class="st-v" style="color:${remaining > 0 ? 'var(--warn)' : 'var(--good)'};">${remaining.toLocaleString()}</div></div>
            <div class="stat"><div class="st-l">Available</div><div class="st-v" style="color:${(item.total_available_qty || 0) >= remaining ? 'var(--good)' : 'var(--bad)'};">${(item.total_available_qty || 0).toLocaleString()}</div></div>
        </div>
        ${confirmedBanner}${incomingBanner}`;
    // ── Cut stage filter + bucket table
    html += P._render_per_item_filter(item);
    html += `
        <table class="allocation-grid-table" data-so-item="${item.sales_order_item}">
            <thead><tr>
                <th>Age</th><th>Bucket</th><th>Farm</th><th>Shelf</th><th>Length</th>
                <th>Available</th><th>Allocated here</th><th>Session</th><th>Actions</th>
            </tr></thead>
            <tbody>`;
    if (!batches.length) {
        html += `<tr><td colspan="9" style="text-align:center;padding:30px;color:var(--ink-mute);">No compatible buckets found</td></tr>`;
    } else {
        batches.forEach(batch => {
            const is_preferred = batch.shelf_farm === preferred_farm;
            const is_awaiting = batch.awaiting_transfer === 1;
            const is_zero = (batch.available_qty || 0) <= 0;
            const is_downgrade = batch.length_status === 'downgrade';
            let row_class = '';
            if (is_awaiting) row_class = 'awaiting-transfer-row';
            else if (is_downgrade) row_class = 'downgrade-bucket';
            else if (is_preferred) row_class = 'preferred-farm-row';
            if (is_zero && !batch.allocated_to_this_item) row_class += ' previously-allocated-bucket';
            const session_alloc = P._session_bucket_qty(item.sales_order_item, batch.bucket_id);
            const allocated_here = batch.allocated_to_this_item || 0;
            let length_badge = is_downgrade
                ? `<span class="grid-badge badge-downgrade">Downgrade</span>`
                : `<span class="grid-badge badge-exact">Exact</span>`;
            if (is_downgrade && batch.downgrade_approval === 'amber_expired')
                length_badge += ` <span class="grid-badge badge-ok">Amber OK</span>`;
            else if (is_downgrade)
                length_badge += ` <span class="grid-badge badge-need">Needs approval</span>`;
            const farm_badge = is_preferred ? `<span class="grid-badge badge-preferred">Preferred</span>` : '';
            const await_badge = is_awaiting ? `<span class="grid-badge badge-awaiting">Remote shelf</span>` : '';
            let actions_html = '';
            if (remaining > 0 && (batch.available_qty || 0) > 0 && !allocated_here && !session_alloc) {
                const esc_bucket = (batch.bucket_id || '').replace(/'/g, "\\'");
                const esc_length = (batch.length_status || 'exact').replace(/'/g, "\\'");
                const esc_stem = (batch.stem_length || '').replace(/'/g, "\\'");
                actions_html += `
                    <button class="btn btn-xs btn-primary" onclick="frappe.pages['sales-allocation'].allocate_from_bucket(
                        '${item.sales_order_item}','${esc_bucket}',${batch.available_qty},
                        '${uom}',${remaining},'${esc_length}','${esc_stem}'
                    )">Allocate all</button>`;
            }
            if (allocated_here > 0 || session_alloc > 0) {
                actions_html += `
                    <button class="btn btn-xs btn-danger" onclick="frappe.pages['sales-allocation'].unallocate_from_bucket(
                        '${item.sales_order_item}','${batch.bucket_id}',${allocated_here + session_alloc}
                    )">Unallocate (${allocated_here + session_alloc})</button>`;
            }
            if (is_zero && !allocated_here && !session_alloc) {
                actions_html = '<span style="color:var(--ink-faint);font-size:11px;">No stock</span>';
            }
            html += `
                <tr class="${row_class}" data-bucket-id="${batch.bucket_id || ''}">
                    <td>${batch.age_days != null ? batch.age_days : '?'}d</td>
                    <td><strong>${batch.bucket_id || '-'}</strong></td>
                    <td>${batch.shelf_farm || '-'} ${farm_badge}</td>
                    <td>${batch.shelf_location || '-'} ${await_badge}</td>
                    <td>${batch.stem_length || 'N/A'} ${length_badge}</td>
                    <td style="color:${(batch.available_qty || 0) > 0 ? 'var(--good)' : 'var(--bad)'}">${batch.available_qty || 0}</td>
                    <td>${allocated_here > 0 ? `<strong>${allocated_here}</strong>` : '-'}</td>
                    <td>${session_alloc > 0 ? `<span style="color:var(--good)">${session_alloc}</span>` : '-'}</td>
                    <td>${actions_html || '-'}</td>
                </tr>`;
        });
    }
    html += `</tbody></table>`;
    return html;
};
// ─── RENDER THE WHOLE DETAIL PANE ───
// Kept under the old name so every existing call site still works.
frappe.pages['sales-allocation'].render_allocation_grid = function () {
    const P = frappe.pages['sales-allocation'];
    if (!$('#salesAllocationDetail').length) return;
    if (!P.selected_order) {
        $('#detailHead').html('<div style="font:600 14px var(--sans);color:var(--ink-mute);">No order selected</div>');
        $('#detailToolbar').empty().hide();
        $('#linesRail').empty();
        $('#allocationWorkspace').html('<div class="empty-state"><p>Pick a location, then choose an order on the left to start allocating.</p></div>');
        $('#actionBar').html(P._render_action_bar());
        return;
    }
    const all_items = P.order_items || [];
    // Apply mix-group filter (empty string = no filter, show everything)
    const items = P.selected_mix_group
        ? all_items.filter(it => String(it.custom_mix_group || '') === String(P.selected_mix_group))
        : all_items;
    // Resolve which line is showing
    if (!items.some(i => i.sales_order_item === P.selected_item)) {
        P.selected_item = items.length ? items[0].sales_order_item : null;
    }
    const item = items.find(i => i.sales_order_item === P.selected_item);
    $('#detailHead').html(P._render_detail_head(items));
    const toolbar = P._render_farm_filter() + P._render_mix_filter();
    if (toolbar.trim()) $('#detailToolbar').html(toolbar).css('display', 'flex');
    else $('#detailToolbar').empty().hide();
    $('#linesRail').html(P._render_lines_rail(items));
    if (item) {
        $('#allocationWorkspace').html(P._render_item_block(item));
    } else if (P.selected_mix_group) {
        $('#allocationWorkspace').html('<div class="empty-state"><p>No items in the selected mix.</p></div>');
    } else {
        $('#allocationWorkspace').html('<div class="empty-state"><p>No items with confirmed stems for this location.</p></div>');
    }
    $('#actionBar').html(P._render_action_bar());
    P._bind_farm_filter();
    P._bind_mix_filter();
    P._bind_lines_rail();
    P._bind_per_item_filters();
    if (item) P._apply_item_batch_visibility(item.sales_order_item);
};
// ─── ALLOCATE FROM BUCKET ───
frappe.pages['sales-allocation'].allocate_from_bucket = function (so_item, bucket_id, max_from_bucket, uom, remaining, length_status, stem_length) {
    const P = frappe.pages['sales-allocation'];
    const qty = Math.min(max_from_bucket, remaining);
    if (qty <= 0) { frappe.msgprint('Nothing to allocate.'); return; }
    const item = P.order_items.find(i => i.sales_order_item === so_item);
    if (!item) return;
    if (length_status === 'downgrade') {
        const batch = (item.batches || []).find(b => b.bucket_id === bucket_id);
        const approval = batch ? batch.downgrade_approval : 'requires_approval';
        if (approval === 'amber_expired') {
            P._do_allocate(so_item, bucket_id, qty, uom, length_status, 'Amber time expired');
            return;
        }
        const age_days = batch ? batch.age_days : 0;
        const amber_time = item.amber_time || 3;
        const incoming = item.incoming_exact_stems || 0;
        let incoming_html = '';
        if (incoming > 0) {
            incoming_html = `<div style="background:var(--good-soft);border-radius:10px;padding:9px 12px;margin-top:10px;font-size:12px;color:var(--good);">
                <strong>${incoming} exact-length stems</strong> (${item.required_length || '?'}) received but not yet shelved.
            </div>`;
        }
        const d = new frappe.ui.Dialog({
            title: 'Downgrade — reason required',
            fields: [
                { fieldtype: 'HTML', fieldname: 'info', options: `
                    <div style="background:var(--warn-soft);border-radius:10px;padding:12px 14px;margin-bottom:10px;color:var(--ink-3);font-size:12px;">
                        <strong>Bucket:</strong> ${bucket_id} (${stem_length}) — order requires ${item.required_length || 'N/A'}<br>
                        <strong>Qty:</strong> ${qty} ${uom} · age ${age_days}d (amber at ${amber_time}d)
                    </div>
                    <div style="background:var(--bad-soft);border-radius:10px;padding:9px 12px;margin-bottom:6px;font-size:12px;color:var(--bad);">
                        Requires approval — bucket is only ${age_days} days old.
                    </div>${incoming_html}` },
                { fieldtype: 'Small Text', fieldname: 'reason', label: 'Downgrade reason', reqd: 1 }
            ],
            primary_action_label: 'Confirm',
            primary_action: function (vals) {
                if (!vals.reason || !vals.reason.trim()) { frappe.msgprint('Reason required.'); return; }
                d.hide();
                P._do_allocate(so_item, bucket_id, qty, uom, length_status, vals.reason.trim());
            }
        });
        d.$wrapper.addClass('ufd-sa');
        d.$wrapper.find('.modal-dialog').addClass('ufd-sa-modal');
        d.show();
        setTimeout(() => d.fields_dict.reason.$input.focus(), 200);
    } else {
        P._do_allocate(so_item, bucket_id, qty, uom, length_status, '');
    }
};
frappe.pages['sales-allocation']._do_allocate = function (so_item, bucket_id, qty, uom, length_status, downgrade_reason) {
    const P = frappe.pages['sales-allocation'];
    const item = P.order_items.find(i => i.sales_order_item === so_item);
    if (!item) return;
    const existing = P.allocations.find(a => a.sales_order_item === so_item && a.bucket_id === bucket_id);
    if (existing) {
        existing.qty += qty;
        if (downgrade_reason) existing.downgrade_reason = downgrade_reason;
    } else {
        const batch = (item.batches || []).find(b => b.bucket_id === bucket_id);
        P.allocations.push({
            item_code: item.item_code || '',
            bucket_id, qty,
            sales_order_item: so_item,
            stem_length: batch ? batch.stem_length : '',
            warehouse: batch ? batch.warehouse : '',
            uom: item.uom || '', stock_uom: item.stock_uom || '',
            conversion_factor: item.conversion_factor || 1,
            length_status: length_status || 'exact',
            downgrade_reason: downgrade_reason || '',
            available_exact_stems: length_status === 'downgrade' ? (item.incoming_exact_stems || 0) : 0
        });
    }
    const batch = (item.batches || []).find(b => b.bucket_id === bucket_id);
    if (batch) batch.available_qty = Math.max(0, (batch.available_qty || 0) - qty);
    P.render_allocation_grid();
};
// ─── UNALLOCATE ───
frappe.pages['sales-allocation'].unallocate_from_bucket = function (so_item, bucket_id, allocated_qty) {
    const P = frappe.pages['sales-allocation'];
    frappe.confirm(`Unallocate ${allocated_qty} stems from bucket ${bucket_id}?`, () => {
        P.allocations = P.allocations.filter(a => !(a.sales_order_item === so_item && a.bucket_id === bucket_id));
        frappe.call({
            method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.unallocate_bucket_from_opl',
            args: { sales_order_item: so_item, bucket_id },
            freeze: true, freeze_message: 'Unallocating…',
            callback: function (r) {
                if (r.message && r.message.success) frappe.show_alert({ message: r.message.message, indicator: 'green' });
                else frappe.msgprint({ title: 'Note', message: r.message?.message || 'Partial success.', indicator: 'orange' });
                P.select_order(P.selected_order, { keep: 1, force: 1 });
            },
            error: function () { frappe.msgprint({ title: 'Failed', message: 'Unallocation failed. Please refresh.', indicator: 'red' }); }
        });
    });
};
// ─── AUTO-ALLOCATE FIFO ───
frappe.pages['sales-allocation'].auto_allocate_fifo = function (so_item) {
    const P = frappe.pages['sales-allocation'];
    const item = P.order_items.find(i => i.sales_order_item === so_item);
    if (!item) return;
    const allocated_session = P._session_qty(so_item);
    const grand_allocated = (item.total_allocated_qty || 0) + allocated_session;
    const required = item.pending_stock_qty || 0;
    let remaining = required - grand_allocated;
    if (remaining <= 0) { frappe.msgprint('Item already fully allocated.'); return; }
    const available_batches = (item.batches || []).filter(b => (b.available_qty || 0) > 0);
    if (!available_batches.length) { frappe.msgprint('No compatible buckets available.'); return; }
    let to_check = remaining;
    const needs_reason_downgrades = [];
    for (const batch of available_batches) {
        if (to_check <= 0) break;
        const qty = Math.min(batch.available_qty, to_check);
        if (batch.length_status === 'downgrade' && batch.downgrade_approval === 'requires_approval') {
            needs_reason_downgrades.push({ ...batch, planned_qty: qty });
        }
        to_check -= qty;
    }
    if (needs_reason_downgrades.length) {
        const summary = needs_reason_downgrades.map(d =>
            `<li><strong>${d.bucket_id}</strong> (${d.stem_length}, ${d.shelf_farm}) — ${d.planned_qty} stems</li>`
        ).join('');
        const incoming = item.incoming_exact_stems || 0;
        const d = new frappe.ui.Dialog({
            title: 'Auto-allocate — downgrade reason required',
            fields: [
                { fieldtype: 'HTML', fieldname: 'info', options: `
                    <div style="background:var(--warn-soft);border-radius:10px;padding:12px 14px;margin-bottom:10px;color:var(--ink-3);font-size:12px;">
                        <strong>Downgrade buckets that will be used:</strong>
                        <ul style="margin:8px 0 0;padding-left:20px;">${summary}</ul>
                    </div>
                    ${incoming > 0 ? `<div style="background:var(--good-soft);border-radius:10px;padding:9px 12px;margin-bottom:6px;font-size:12px;color:var(--good);">
                        <strong>${incoming} exact-length stems</strong> received but not yet shelved.
                    </div>` : ''}` },
                { fieldtype: 'Small Text', fieldname: 'reason', label: 'Downgrade reason (applies to all downgraded buckets)', reqd: 1 }
            ],
            primary_action_label: 'Confirm auto-allocate',
            primary_action: function (vals) {
                if (!vals.reason || !vals.reason.trim()) { frappe.msgprint('Reason required.'); return; }
                d.hide();
                P._execute_fifo(so_item, vals.reason.trim());
            }
        });
        d.$wrapper.addClass('ufd-sa');
        d.$wrapper.find('.modal-dialog').addClass('ufd-sa-modal');
        d.show();
        setTimeout(() => d.fields_dict.reason.$input.focus(), 200);
    } else {
        P._execute_fifo(so_item, '');
    }
};
frappe.pages['sales-allocation']._execute_fifo = function (so_item, downgrade_reason) {
    const P = frappe.pages['sales-allocation'];
    const item = P.order_items.find(i => i.sales_order_item === so_item);
    if (!item) return;
    const allocated_session = P._session_qty(so_item);
    const grand_allocated = (item.total_allocated_qty || 0) + allocated_session;
    const required = item.pending_stock_qty || 0;
    let remaining = required - grand_allocated;
    let count = 0;
    for (const batch of (item.batches || [])) {
        if (remaining <= 0) break;
        const available = batch.available_qty || 0;
        if (available <= 0) continue;
        const qty = Math.min(available, remaining);
        const is_downgrade = batch.length_status === 'downgrade';
        const reason = is_downgrade
            ? (batch.downgrade_approval === 'amber_expired' ? 'Amber time expired' : downgrade_reason)
            : '';
        const existing = P.allocations.find(a => a.sales_order_item === so_item && a.bucket_id === batch.bucket_id);
        if (existing) { existing.qty += qty; if (reason) existing.downgrade_reason = reason; }
        else {
            P.allocations.push({
                item_code: item.item_code, bucket_id: batch.bucket_id, qty,
                sales_order_item: so_item,
                stem_length: batch.stem_length || '', warehouse: batch.warehouse || '',
                uom: item.uom || '', stock_uom: item.stock_uom || '',
                conversion_factor: item.conversion_factor || 1,
                length_status: batch.length_status || 'exact',
                downgrade_reason: reason,
                available_exact_stems: is_downgrade ? (item.incoming_exact_stems || 0) : 0
            });
        }
        batch.available_qty = Math.max(0, available - qty);
        remaining -= qty;
        count += qty;
    }
    if (count > 0) {
        frappe.show_alert({ message: `Auto-allocated ${count} ${item.stock_uom || ''}.`, indicator: 'green' });
        P.render_allocation_grid();
    }
};
// ─── CONFIRM ALLOCATION ───
frappe.pages['sales-allocation'].confirm_allocation = function () {
    const P = frappe.pages['sales-allocation'];
    const allocations = P.allocations || [];
    if (!allocations.length) { frappe.msgprint('No allocations to confirm.'); return; }
    if (!P.selected_location) { frappe.msgprint('Location not selected.'); return; }
    const valid_so_items = new Set((P.order_items || []).map(i => i.sales_order_item));
    const valid_allocations = allocations.filter(a => valid_so_items.has(a.sales_order_item));
    if (!valid_allocations.length) { frappe.msgprint('No valid allocations.'); return; }
    // Team is per line (per Sales Order Item). Every item being allocated needs one.
    const teams = P.item_teams || {};
    const missing = [...new Set(valid_allocations.map(a => a.sales_order_item))]
        .filter(soi => !teams[soi]);
    if (missing.length) {
        const names = missing.map(soi => {
            const it = (P.order_items || []).find(i => i.sales_order_item === soi);
            return it ? (it.item_name || it.item_code || soi) : soi;
        });
        // Jump to the first offending line so its team dropdown is on screen.
        P.selected_item = missing[0];
        P.render_allocation_grid();
        frappe.msgprint('Select a packing team for: ' + names.join(', '));
        return;
    }
    frappe.call({
        method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.allocate_stock_with_buckets',
        args: { sales_order: P.selected_order, allocations: valid_allocations, location: P.selected_location, teams: JSON.stringify(teams) },
        freeze: true, freeze_message: 'Allocating stock…',
        callback: function (r) {
            if (r.message && r.message.success) {
                const results = r.message.pick_list_results || [];
                const messages = results.map(res => {
                    const link = `<a href="/app/order-pick-list/${res.name || '?'}" target="_blank"><strong>${res.name || '?'}</strong></a>`;
                    if (res.status === 'submitted') return `Pick list ${link} created and submitted`;
                    if (res.status === 'draft') return `Pick list ${link} created as draft`;
                    if (res.status === 'updated_existing') return `Pick list ${link} updated`;
                    return '';
                }).filter(Boolean);
                frappe.msgprint({
                    title: 'Allocation complete',
                    message: messages.length ? messages.join('<br>') : 'Allocation completed successfully.',
                    indicator: 'green'
                });
                P.allocations = [];
                P.selected_order = null;
                P.order_items = [];
                P.selected_item = null;
                P.item_teams = {};
                P.render_allocation_grid();
                P.load_sales_orders();
            } else {
                frappe.msgprint({ title: 'Allocation failed', message: r.message?.message || 'Allocation failed.', indicator: 'red' });
            }
        }
    });
};
// ─── HELPERS ───
frappe.pages['sales-allocation'].set_item_team = function (so_item, team) {
    const P = frappe.pages['sales-allocation'];
    P.item_teams = P.item_teams || {};
    if (team) P.item_teams[so_item] = team; else delete P.item_teams[so_item];
    // Refresh the select's state colour and the line rail's "no team" flag
    // without rebuilding the bucket table.
    const $w = P._scope();
    $w.find(`.item-team-select[data-so-item="${so_item}"]`)
        .toggleClass('is-set', !!team)
        .toggleClass('is-unset', !team);
    const all_items = P.order_items || [];
    const items = P.selected_mix_group
        ? all_items.filter(it => String(it.custom_mix_group || '') === String(P.selected_mix_group))
        : all_items;
    $('#linesRail').html(P._render_lines_rail(items));
    P._bind_lines_rail();
};
frappe.pages['sales-allocation'].clear_all_and_render = function () {
    const P = frappe.pages['sales-allocation'];
    P.clear_allocations();
    P.render_allocation_grid();
};
frappe.pages['sales-allocation'].clear_allocations = function (silent) {
    const P = frappe.pages['sales-allocation'];
    (P.order_items || []).forEach(item => {
        (item.batches || []).forEach(b => { b.available_qty = b.original_available_qty || 0; });
    });
    P.allocations = [];
    if (!silent) frappe.show_alert({ message: 'Session allocations cleared.', indicator: 'blue' });
};
frappe.pages['sales-allocation']._session_qty = function (so_item) {
    return (frappe.pages['sales-allocation'].allocations || [])
        .filter(a => a.sales_order_item === so_item)
        .reduce((s, a) => s + (parseFloat(a.qty) || 0), 0);
};
frappe.pages['sales-allocation']._session_total = function () {
    return (frappe.pages['sales-allocation'].allocations || [])
        .reduce((s, a) => s + (parseFloat(a.qty) || 0), 0);
};
frappe.pages['sales-allocation']._session_bucket_qty = function (so_item, bucket_id) {
    const match = (frappe.pages['sales-allocation'].allocations || [])
        .find(a => a.sales_order_item === so_item && a.bucket_id === bucket_id);
    return match ? (parseFloat(match.qty) || 0) : 0;
};