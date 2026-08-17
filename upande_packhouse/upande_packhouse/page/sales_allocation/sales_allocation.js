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

        .ufd-sa.sales-allocation-container {
            padding: 24px;
            background: var(--bg);
            min-height: calc(100vh - 120px);
        }
        .ufd-sa .allocation-panel {
            background: var(--surface-2);
            border-radius: 20px;
            padding: 28px 30px;
            box-shadow: var(--shadow-card);
        }
        .ufd-sa .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 18px;
            border-bottom: 1px solid var(--hairline);
        }
        .ufd-sa .panel-header h3 { color: var(--ink); font: 600 18px var(--sans); letter-spacing:-.4px; margin: 0; }
        .ufd-sa .sales-order-card {
            border: 1px solid var(--hairline);
            border-radius: 14px;
            padding: 16px 18px;
            margin-bottom: 10px;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: var(--shadow-card);
        }
        .ufd-sa .sales-order-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
        .ufd-sa .sales-order-card.selected { box-shadow: inset 0 0 0 2px var(--ink), var(--shadow-hover); }
        .ufd-sa .loading-state, .ufd-sa .empty-state { text-align: center; padding: 40px 20px; color: var(--ink-mute); }
        .ufd-sa .allocation-grid-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            margin-bottom: 20px;
        }
        .ufd-sa .allocation-grid-table thead { background: var(--surface); position: sticky; top: 0; z-index: 10; }
        .ufd-sa .allocation-grid-table th {
            padding: 11px 10px;
            text-align: left;
            font: 600 11px var(--sans);
            text-transform: uppercase;
            letter-spacing: .6px;
            color: var(--ink-mute);
            border-bottom: 1px solid var(--hairline);
            white-space: nowrap;
        }
        .ufd-sa .allocation-grid-table td {
            padding: 11px 10px;
            border-bottom: 1px solid var(--hairline);
            vertical-align: middle;
            color: var(--ink-3);
        }
        .ufd-sa .allocation-grid-table tbody tr:hover { background: rgba(10,10,10,0.02); }
        .ufd-sa .allocation-grid-table tbody tr.downgrade-bucket { background: var(--warn-soft); box-shadow: inset 3px 0 0 var(--warn-2); }
        .ufd-sa .allocation-grid-table tbody tr.previously-allocated-bucket { background: rgba(10,10,10,0.03); opacity: 0.85; box-shadow: inset 3px 0 0 var(--ink-faint); }
        .ufd-sa .allocation-grid-table tbody tr.preferred-farm-row { box-shadow: inset 3px 0 0 var(--signal); }
        .ufd-sa .allocation-grid-table tbody tr.awaiting-transfer-row { background: var(--warn-soft); box-shadow: inset 3px 0 0 var(--warn-2); }
        .ufd-sa .grid-badge {
            display: inline-block;
            padding: 3px 9px;
            border-radius: 999px;
            font: 700 10px var(--sans);
            text-transform: uppercase;
            letter-spacing: .3px;
            margin: 1px;
        }
        .ufd-sa .badge-exact { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa .badge-downgrade { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa .badge-preferred { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa .badge-awaiting { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa .badge-allocated { background: var(--grad-ink); color: #fafaf6; }
        .ufd-sa .badge-confirmed { background: var(--good-soft); color: var(--good); }
        .ufd-sa .so-item-id {
            background: var(--signal-soft);
            color: var(--signal);
            padding: 2px 7px;
            border-radius: 999px;
            font-size: 10px;
            font-family: var(--mono);
            margin-left: 8px;
        }

        /* ─── Location selector ─── */
        .ufd-sa .location-selector {
            background: var(--surface);
            border: 1px solid var(--hairline);
            border-radius: 16px;
            padding: 18px;
            margin-bottom: 18px;
        }
        .ufd-sa .location-selector h4 { margin: 0 0 12px 0; color: var(--ink); font: 600 13px var(--sans); text-transform:uppercase; letter-spacing:1px; }
        .ufd-sa .location-buttons { display: flex; gap: 10px; }
        .ufd-sa .location-btn {
            flex: 1;
            padding: 14px;
            border: 1px solid var(--hairline);
            border-radius: 14px;
            background: var(--surface-2);
            cursor: pointer;
            text-align: center;
            font-weight: 600;
            color: var(--ink-3);
            box-shadow: var(--shadow-card);
            transition: all 0.2s;
        }
        .ufd-sa .location-btn:hover { transform: translateY(-2px); box-shadow: var(--shadow-hover); }
        .ufd-sa .location-btn.active { background: var(--grad-ink); color: #fafaf6; box-shadow: 0 4px 14px rgba(10,10,10,0.20); }
        .ufd-sa .location-info {
            margin-top: 12px;
            padding: 10px 14px;
            background: var(--signal-soft);
            border-radius: 12px;
            font-size: 12px;
            color: var(--ink-3);
        }

        /* ─── Farm filter in dialog ─── */
        .ufd-sa .farm-filter-bar {
            background: var(--surface);
            border: 1px solid var(--hairline);
            border-radius: 14px;
            padding: 14px 18px;
            margin-bottom: 18px;
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }
        .ufd-sa .farm-filter-bar label.title {
            font-weight: 600;
            color: var(--ink);
            font-size: 13px;
            white-space: nowrap;
        }
        .ufd-sa .farm-checkbox-group { display: flex; gap: 10px; flex-wrap: wrap; }
        .ufd-sa .farm-checkbox-item {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 7px 12px;
            border: 1px solid var(--hairline);
            border-radius: 999px;
            background: var(--surface-2);
            cursor: pointer;
            font-size: 12px;
            color: var(--ink-3);
            transition: all 0.15s;
            user-select: none;
        }
        .ufd-sa .farm-checkbox-item:hover { box-shadow: var(--shadow-card); }
        .ufd-sa .farm-checkbox-item.checked { background: var(--grad-ink); color: #fafaf6; border-color: transparent; }
        .ufd-sa .farm-checkbox-item.checked .farm-badge { background: rgba(255,255,255,0.2); color: #fafaf6; }
        .ufd-sa .farm-badge {
            font-size: 9px;
            padding: 2px 6px;
            border-radius: 999px;
            background: rgba(10,10,10,0.06);
            color: var(--ink-mute);
            text-transform: uppercase;
        }
        .ufd-sa .farm-badge.sales { background: var(--signal-soft); color: var(--signal); }
        .ufd-sa .farm-badge.remote { background: var(--warn-soft); color: var(--warn); }
        .ufd-sa .current-so-item {
            background: var(--surface-2);
            border-radius: 18px;
            padding: 18px;
            margin-bottom: 16px;
            box-shadow: var(--shadow-card);
        }

        /* ─── Mix filter in dialog ─── */
        .ufd-sa .mix-filter-bar {
            background: var(--surface);
            border: 1px solid var(--hairline);
            border-radius: 12px;
            padding: 10px 16px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }
        .ufd-sa .mix-filter-bar label.title {
            font-weight: 600;
            color: var(--ink);
            font-size: 13px;
            white-space: nowrap;
        }
        .ufd-sa .mix-filter-bar select {
            padding: 7px 12px;
            border: 1px solid var(--hairline);
            border-radius: 999px;
            font-size: 12px;
            background: var(--surface-2);
            color: var(--ink-3);
            min-width: 200px;
        }
        .ufd-sa .mix-filter-bar .mix-count {
            font-size: 11px;
            color: var(--ink-mute);
            margin-left: auto;
        }

        /* ─── Confirmed stems banner ─── */
        .ufd-sa .confirmed-stems-banner {
            background: var(--good-soft);
            border-radius: 12px;
            padding: 10px 14px;
            margin-bottom: 10px;
            font-size: 12px;
            color: var(--good);
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .ufd-sa .confirmed-stems-banner strong { font-size: 13px; }
        .ufd-sa .confirmed-chip-alloc {
            display: inline-block;
            padding: 2px 9px;
            border-radius: 999px;
            background: var(--signal-soft);
            color: var(--signal);
            font-size: 11px;
            font-weight: 600;
            margin: 0 3px;
        }

        /* ─── Per-item inline filter bar ─── */
        .ufd-sa .item-batch-filter {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
            background: var(--surface);
            border: 1px solid var(--hairline);
            border-radius: 10px;
            padding: 7px 12px;
            margin-bottom: 10px;
            font-size: 11px;
        }
        .ufd-sa .item-batch-filter .ibf-label {
            font-weight: 600;
            color: var(--ink);
            font-size: 11px;
            white-space: nowrap;
        }
        .ufd-sa .item-batch-filter .ibf-sep {
            width: 1px;
            height: 18px;
            background: var(--hairline);
            flex-shrink: 0;
        }
        .ufd-sa .item-batch-filter .ibf-group {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .ufd-sa .item-batch-filter .ibf-group-label {
            font-size: 10px;
            color: var(--ink-mute);
            font-weight: 600;
            white-space: nowrap;
        }
        .ufd-sa .item-batch-filter select.ibf-select {
            padding: 2px 7px;
            border: 1px solid var(--hairline);
            border-radius: 999px;
            font-size: 11px;
            background: var(--surface-2);
            color: var(--ink-3);
            cursor: pointer;
            max-width: 80px;
        }
        .ufd-sa .item-batch-filter .ibf-range-sep {
            font-size: 10px;
            color: var(--ink-mute);
        }
        .ufd-sa .item-batch-filter .ibf-clear {
            padding: 3px 10px;
            border: 1px solid var(--hairline);
            border-radius: 999px;
            background: var(--surface-2);
            cursor: pointer;
            font-size: 10px;
            color: var(--ink-4);
            margin-left: auto;
            transition: all 0.15s;
        }
        .ufd-sa .item-batch-filter .ibf-clear:hover { background: var(--bad); border-color: var(--bad); color: #fff; }
        /* ─── Substitute variety button ─── */
        .ufd-sa .substitute-btn {
            padding: 4px 12px;
            border: 1px solid var(--hairline);
            border-radius: 999px;
            background: var(--surface-2);
            color: var(--ink-4);
            cursor: pointer;
            font-size: 11px;
            font-weight: 600;
            transition: all 0.15s;
        }
        .ufd-sa .substitute-btn:hover { background: var(--ink); border-color: var(--ink); color: #fafaf6; }
        .ufd-sa .sub-variety-card {
            border: 1px solid var(--hairline);
            border-radius: 14px;
            padding: 12px 14px;
            margin-bottom: 8px;
            cursor: pointer;
            transition: all 0.15s;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .ufd-sa .sub-variety-card:hover { box-shadow: var(--shadow-hover); transform: translateY(-1px); }
        .ufd-sa .sub-variety-card.recommended { box-shadow: inset 3px 0 0 var(--signal); background: var(--signal-soft); }
        .ufd-sa .sub-variety-card .sub-name { font-weight: 600; font-size: 13px; color: var(--ink); }
        .ufd-sa .sub-variety-card .sub-meta { font-size: 11px; color: var(--ink-mute); }
        .ufd-sa .sub-variety-card .sub-badge {
            font-size: 9px;
            padding: 2px 8px;
            border-radius: 999px;
            font-weight: 600;
            text-transform: uppercase;
        }

        /* ─── Buttons & dialogs, scoped so the rest of Desk is untouched ─── */
        .ufd-sa .btn { font-family: var(--sans); border-radius: 999px !important; font-weight: 600; }
        .ufd-sa .btn-primary, .ufd-sa .btn-primary:focus { background: var(--grad-ink) !important; border-color: transparent !important; box-shadow: 0 2px 8px rgba(10,10,10,0.15); }
        .ufd-sa .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 14px rgba(10,10,10,0.22); }
        .ufd-sa .btn-default { background: var(--surface-2) !important; border-color: var(--hairline) !important; color: var(--ink-3) !important; }
        .ufd-sa .btn-default:hover { background: var(--ink) !important; color: #fafaf6 !important; }
        .ufd-sa .btn-danger, .ufd-sa .btn-danger:focus { background: var(--bad) !important; border-color: transparent !important; }
        .ufd-sa .btn-danger:hover { background: var(--bad-2) !important; }
        .modal-dialog.ufd-sa-modal .modal-content { border-radius: 20px; border: 0; box-shadow: var(--shadow-hover); font-family: var(--sans); }
        .modal-dialog.ufd-sa-modal .modal-header { border-bottom: 1px solid var(--hairline); }
        .modal-dialog.ufd-sa-modal .modal-title { font: 600 17px var(--sans); color: var(--ink); letter-spacing:-.3px; }
        .modal-dialog.ufd-sa-modal .modal-footer { border-top: 1px solid var(--hairline); }
        .modal-dialog.ufd-sa-modal, .modal-dialog.ufd-sa-modal input, .modal-dialog.ufd-sa-modal select, .modal-dialog.ufd-sa-modal textarea { font-family: var(--sans); }
        .modal-dialog.ufd-sa-modal .control-input, .modal-dialog.ufd-sa-modal textarea.form-control { border-radius: 10px; border-color: var(--hairline); }
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
        <div class="allocation-panel">
            <div class="panel-header"><h3>Pending Sales Orders</h3></div>

            <div class="location-selector">
                <h4>Select Location</h4>
                <div class="location-buttons" id="locationButtons">
                    <div class="loading-state" style="padding:20px;">Loading locations...</div>
                </div>
                <div class="location-info" id="locationInfo" style="display:none;"></div>
            </div>

            <div class="filter-section" style="padding:14px 16px;border-bottom:1px solid var(--hairline);background:var(--surface);border-radius:14px;margin-bottom:16px;">
                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px;">
                    <div style="display:flex;gap:5px;align-items:center;">
                        <label style="font-size:11px;color:var(--ink-mute);white-space:nowrap;">Posting Date:</label>
                        <input type="date" id="orderStartDate" style="flex:1;padding:7px 10px;border:1px solid var(--hairline);border-radius:8px;font-size:12px;background:var(--surface-2);color:var(--ink-3);">
                        <span style="color:var(--ink-mute);">to</span>
                        <input type="date" id="orderEndDate" style="flex:1;padding:7px 10px;border:1px solid var(--hairline);border-radius:8px;font-size:12px;background:var(--surface-2);color:var(--ink-3);">
                    </div>
                    <div style="display:flex;gap:5px;align-items:center;">
                        <label style="font-size:11px;color:var(--ink-mute);white-space:nowrap;">Delivery:</label>
                        <input type="date" id="deliveryStartDate" style="flex:1;padding:7px 10px;border:1px solid var(--hairline);border-radius:8px;font-size:12px;background:var(--surface-2);color:var(--ink-3);">
                        <span style="color:var(--ink-mute);">to</span>
                        <input type="date" id="deliveryEndDate" style="flex:1;padding:7px 10px;border:1px solid var(--hairline);border-radius:8px;font-size:12px;background:var(--surface-2);color:var(--ink-3);">
                    </div>
                    <div style="display:flex;gap:5px;">
                        <input type="text" id="orderSearchInput" placeholder="Search order, customer, order name, variety..." style="flex:1;padding:7px 10px;border:1px solid var(--hairline);border-radius:8px;font-size:12px;background:var(--surface-2);color:var(--ink-3);">
                        <select id="priorityFilter" style="padding:7px 10px;border:1px solid var(--hairline);border-radius:8px;font-size:12px;background:var(--surface-2);color:var(--ink-3);">
                            <option value="">All Priorities</option>
                            <option value="High">High</option>
                            <option value="Medium">Medium</option>
                            <option value="Low">Low</option>
                        </select>
                        <select id="boxTypeFilter" style="padding:7px 10px;border:1px solid var(--hairline);border-radius:8px;font-size:12px;background:var(--surface-2);color:var(--ink-3);">
                            <option value="">All Box Types</option>
                            <option value="mixed">Mixed Only</option>
                            <option value="straight">Straight Only</option>
                        </select>
                    </div>
                </div>
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div id="resultsCount" style="font-size:12px;color:var(--ink-mute);"></div>
                    <button id="clearFilters" style="padding:7px 14px;background:var(--surface-2);border:1px solid var(--hairline);border-radius:999px;cursor:pointer;font-size:12px;color:var(--ink-4);font-weight:600;">Clear Filters</button>
                </div>
            </div>

            <div class="sales-order-list" id="salesOrderList" style="max-height:600px;overflow-y:auto;">
                <div class="loading-state">Select a location to begin...</div>
            </div>
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

    // Mix group filter in allocation dialog ("" = show all, otherwise custom_mix_group number as string)
    P.selected_mix_group = '';

    // Per-item batch filter state: keyed by sales_order_item
    // Each entry: { cut_stage_min: '', cut_stage_max: '' }
    P.item_filters = {};

    const tomorrow = frappe.datetime.add_days(frappe.datetime.get_today(), 1);
    P.filters = { search: '', priority: '', box_type: '', order_start: '', order_end: '', delivery_start: tomorrow, delivery_end: tomorrow };
    setTimeout(() => {
        $('#deliveryStartDate').val(tomorrow);
        $('#deliveryEndDate').val(tomorrow);
        $('#resultsCount').html(
            `Showing orders with delivery date: <strong>${frappe.datetime.str_to_user(tomorrow)}</strong> — adjust filters above to change`
        );
    }, 100);
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
    $('#clearFilters').on('click', function () {
        $('#orderSearchInput, #priorityFilter, #boxTypeFilter, #orderStartDate, #orderEndDate, #deliveryStartDate, #deliveryEndDate').val('');
        P.filters = { search: '', priority: '', box_type: '', order_start: '', order_end: '', delivery_start: '', delivery_end: '' };
        if (P.selected_location) P.load_sales_orders();
    });
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
                $('#locationButtons').html('<div style="color:var(--bad);padding:10px;">No locations configured</div>');
            }
        },
        error: function () {
            $('#locationButtons').html('<div style="color:var(--bad);padding:10px;">Error loading locations</div>');
        }
    });
};

frappe.pages['sales-allocation'].render_location_selector = function () {
    const config = frappe.pages['sales-allocation'].location_config;
    if (!config || !config.locations) return;

    const html = config.locations.map(loc => {
        const is_active = frappe.pages['sales-allocation'].selected_location === loc.name ? 'active' : '';
        return `
            <div class="location-btn ${is_active}" data-location="${loc.name}">
                <div style="font-size:14px;">${loc.name}</div>
                <div style="font-size:11px;margin-top:4px;opacity:0.8;">${loc.farms.length} farm(s)</div>
            </div>`;
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
        const farm_labels = loc_config.farm_details.map(f =>
            `${f.farm} <span style="font-size:10px;opacity:0.75;">(${f.sales_shelf ? 'Sales' : 'Remote'})</span>`
        ).join(', ');
        $('#locationInfo').show().html(
            `<strong>Selected:</strong> ${location} | <strong>Farms:</strong> ${farm_labels}`
        );
    }

    P.selected_order = null;
    P.allocations = [];
    P.order_items = [];
    P.load_sales_orders();
};

// ─── SALES ORDERS ───
frappe.pages['sales-allocation'].load_sales_orders = function () {
    const P = frappe.pages['sales-allocation'];
    if (!P.selected_location) {
        $('#salesOrderList').html('<div class="empty-state"><p>Please select a location</p></div>');
        return;
    }
    $('#salesOrderList').html('<div class="loading-state">Loading...</div>');

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
                $('#salesOrderList').html('<div class="empty-state"><p>No pending orders</p></div>');
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

    $('#resultsCount').text(`Showing ${orders.length} of ${P.current_sales_orders.length} orders`);
    P.render_orders(orders);
};

frappe.pages['sales-allocation'].render_orders = function (orders) {
    const $list = $('#salesOrderList');
    const selected = frappe.pages['sales-allocation'].selected_order;

    if (!orders.length) { $list.html('<div class="empty-state"><p>No orders match filters</p></div>'); return; }

    const html = orders.map(order => {
        const priority = order.custom_priority || 'Low';
        const is_selected = selected === order.name ? 'selected' : '';
        const pct = order.allocation_percentage || 0;
        const bar_color = pct >= 75 ? 'var(--good)' : pct >= 50 ? 'var(--warn-2)' : 'var(--bad)';

        return `
            <div class="sales-order-card ${is_selected}" data-order="${order.name}">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div style="font-weight:600;color:var(--ink);">${order.name}</div>
                    <div style="font-size:10px;padding:3px 9px;border-radius:999px;background:var(--signal-soft);color:var(--signal);font-weight:600;">${priority}</div>
                </div>
                <div style="margin-top:6px;font-size:12px;color:var(--ink-mute);">
                    <div>Customer: ${order.customer}</div>
                    <div>Order: ${order.custom_order_name || '-'}</div>
                    <div>Varieties: ${order.item_codes || '-'}</div>
                    <div>Delivery: ${frappe.datetime.str_to_user(order.delivery_date)}</div>
                </div>
                <div style="margin-top:10px;">
                    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--ink-mute);margin-bottom:4px;">
                        <span>Allocation</span>
                        <span style="font-weight:600;color:${bar_color};">${pct}%</span>
                    </div>
                    <div style="width:100%;height:6px;background:rgba(10,10,10,0.06);border-radius:999px;overflow:hidden;">
                        <div style="width:${pct}%;height:100%;background:${bar_color};transition:width 0.3s;"></div>
                    </div>
                </div>
            </div>`;
    }).join('');

    $list.html(html);
    $list.find('.sales-order-card').on('click', function () {
        frappe.pages['sales-allocation'].select_order($(this).data('order'));
    });
};

// ─── SELECT ORDER & LOAD ITEMS ───
frappe.pages['sales-allocation'].select_order = function (order_name) {
    const P = frappe.pages['sales-allocation'];
    if (!P.selected_location) { frappe.msgprint('Please select a location first'); return; }
    P.allocations = [];
    P.selected_order = order_name;

    // Reset filters
    P.item_filters = {};
    P.selected_mix_group = '';

    P.apply_filters();
    P._load_available_filters_and_open_dialog();
};

// ─── Load items and open dialog ───
frappe.pages['sales-allocation']._load_available_filters_and_open_dialog = function () {
    const P = frappe.pages['sales-allocation'];
    // Filters are now per-item and derived from batch data client-side
    P._fetch_items_and_open_dialog();
};

frappe.pages['sales-allocation']._fetch_items_and_open_dialog = function () {
    const P = frappe.pages['sales-allocation'];

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
                P.show_allocation_dialog();
            } else {
                frappe.msgprint('No items with confirmed stems for this location.');
            }
        },
        error: function () { frappe.msgprint('Failed to load items for allocation.'); }
    });
};

// ─── ALLOCATION DIALOG ───
frappe.pages['sales-allocation'].show_allocation_dialog = function () {
    const P = frappe.pages['sales-allocation'];

    P._dialog = new frappe.ui.Dialog({
        title: `Allocate Stock: ${P.selected_order} (${P.selected_location})`,
        size: 'extra-large',
        fields: [{ fieldtype: 'HTML', fieldname: 'allocation_grid' }],
        primary_action_label: 'Confirm Allocation',
        primary_action: function () { P.confirm_allocation(); },
        secondary_action_label: 'Clear All Allocations',
        secondary_action: function () { P.clear_allocations(); P.render_allocation_grid(); }
    });

    // Per-line team selections (sales_order_item -> team); reset each time the dialog opens
    P.item_teams = {};

    // Scope the ufd-modern restyle to this dialog only (see add_styles) — never
    // touches any other Frappe Desk dialog on the page.
    P._dialog.$wrapper.addClass('ufd-sa');
    P._dialog.$wrapper.find('.modal-dialog').addClass('ufd-sa-modal');

    P._dialog.show();
    P.render_allocation_grid();
};

// ─── FARM FILTER BAR ───
frappe.pages['sales-allocation']._render_farm_filter = function () {
    const P = frappe.pages['sales-allocation'];
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
            <label class="title">Source Farms:</label>
            <div class="farm-checkbox-group" id="farmFilterGroup">${checks}</div>
            <div style="font-size:11px;color:var(--ink-mute);margin-left:auto;">
                Changes will reload bucket data and clear session allocations
            </div>
        </div>`;
};

frappe.pages['sales-allocation']._bind_farm_filter = function () {
    const P = frappe.pages['sales-allocation'];
    $(P._dialog.$wrapper).find('.farm-checkbox-item').on('click', function () {
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
            <label class="title">Mix:</label>
            <select id="mixGroupFilter">
                <option value="" ${all_selected}>All mixes (and straight boxes)</option>
                ${options}
            </select>
            <span class="mix-count">${seen.size} mix${seen.size === 1 ? '' : 'es'} in this order</span>
        </div>`;
};

frappe.pages['sales-allocation']._bind_mix_filter = function () {
    const P = frappe.pages['sales-allocation'];
    $(P._dialog.$wrapper).find('#mixGroupFilter').on('change', function () {
        P.selected_mix_group = $(this).val() || '';
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
            <span class="ibf-label">Cut Stage</span>
            <div class="ibf-sep"></div>
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
        </div>`;
};

frappe.pages['sales-allocation']._bind_per_item_filters = function () {
    const P = frappe.pages['sales-allocation'];
    const $w = $(P._dialog.$wrapper);

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
    const $w = $(P._dialog.$wrapper);
    const item = (P.order_items || []).find(i => i.sales_order_item === so_item);
    if (!item) return;

    const has_filter = P._has_active_filter(so_item);
    const $table = $w.find(`table.allocation-grid-table[data-so-item="${so_item}"]`);

    $table.find('tbody tr').each(function () {
        const bucket_id = $(this).data('bucket-id');
        if (!bucket_id) return;
        const batch = (item.batches || []).find(b => b.bucket_id === bucket_id);
        if (!batch) return;

        if (!has_filter || P._batch_passes_filter(batch, item)) {
            $(this).show();
        } else {
            $(this).hide();
        }
    });
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
        title: `Substitute Variety for: ${item.item_name || item.item_code}`,
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
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding:9px 14px;background:${show_filter ? 'var(--good-soft)' : 'var(--surface)'};border-radius:12px;font-size:12px;">
                    <div>
                        <strong>Recommendation:</strong> ${filter_label}
                        ${show_filter ? '<span style="color:var(--good);margin-left:8px;">(active)</span>' : '<span style="color:var(--ink-mute);margin-left:8px;">(cleared)</span>'}
                    </div>
                    <button class="btn btn-xs btn-default" id="toggleSubFilter">
                        ${show_filter ? 'Show All Varieties' : 'Show Recommended Only'}
                    </button>
                </div>`;
        }

        const current_html = `
            <div style="background:var(--surface);border-radius:12px;padding:11px 14px;margin-bottom:12px;font-size:12px;color:var(--ink-3);">
                <strong>Current:</strong> ${item.item_name} (${item.item_code})
                ${original_color ? ` | Color: ${original_color}` : ''}
                ${original_headsize ? ` | Headsize: ${original_headsize}` : ''}
                | Qty: ${item.pending_stock_qty || 0} ${item.stock_uom || ''}
            </div>`;

        let list_html = '';
        if (!varieties || !varieties.length) {
            list_html = '<div style="text-align:center;padding:20px;color:var(--ink-mute);">No varieties found</div>';
        } else {
            list_html = varieties.map(v => {
                const is_same = v.item_code === item.item_code;
                const is_recommended = (original_color && v.color === original_color) || (original_headsize && v.headsize === original_headsize);
                const rec_class = is_recommended && !is_same ? 'recommended' : '';
                const badges = [];
                if (is_same) badges.push('<span class="sub-badge" style="background:rgba(10,10,10,0.06);color:var(--ink-mute);">Current</span>');
                if (is_recommended && !is_same) badges.push('<span class="sub-badge" style="background:var(--signal-soft);color:var(--signal);">Recommended</span>');

                return `
                    <div class="sub-variety-card ${rec_class} ${is_same ? '' : 'selectable'}" data-item-code="${v.item_code}" data-item-name="${v.item_name || v.item_code}" ${is_same ? 'style="opacity:0.6;cursor:default;"' : ''}>
                        <div>
                            <div class="sub-name">${v.item_name || v.item_code}</div>
                            <div class="sub-meta">
                                ${v.color ? `Color: ${v.color}` : ''}
                                ${v.headsize ? ` | Headsize: ${v.headsize}` : ''}
                                ${v.available_qty != null ? ` | Available: ${v.available_qty}` : ''}
                            </div>
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
        sub_dialog.fields_dict.sub_content.$wrapper.html(
            '<div style="text-align:center;padding:20px;color:var(--ink-mute);">Loading varieties...</div>'
        );

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
        freeze_message: `Substituting with ${new_item_name}...`,
        callback: function (r) {
            if (r.message && r.message.success) {
                frappe.show_alert({ message: `Substituted to ${new_item_name}`, indicator: 'green' });
                // Reload the order items
                P.allocations = [];
                P.item_filters = {};
                P.select_order(P.selected_order);
            } else {
                frappe.msgprint({
                    title: 'Substitution Failed',
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

// ─── RENDER ALLOCATION GRID ───
frappe.pages['sales-allocation'].render_allocation_grid = function () {
    const P = frappe.pages['sales-allocation'];
    const all_items = P.order_items || [];

    // Apply mix-group filter (empty string = no filter, show everything)
    const items = P.selected_mix_group
        ? all_items.filter(it => String(it.custom_mix_group || '') === String(P.selected_mix_group))
        : all_items;

    let html = P._render_farm_filter() + P._render_mix_filter();

    if (P.selected_mix_group && items.length === 0) {
        html += `<div style="padding:20px;text-align:center;color:var(--ink-mute);background:var(--surface);border:1px dashed var(--hairline);border-radius:14px;">
            No items in the selected mix.
        </div>`;
    }

    items.forEach((item, item_idx) => {
        const allocated_session = P._session_qty(item.sales_order_item);
        const total_allocated = item.total_allocated_qty || 0;
        const grand_allocated = total_allocated + allocated_session;
        const required = item.pending_stock_qty || 0;
        const remaining = required - grand_allocated;
        const batches = item.batches || [];
        const preferred_farm = item.preferred_farm || '';

        // Item metadata badges (no emojis)
        const color_badge = item.color
            ? `<span style="background:var(--signal-soft);color:var(--signal);padding:4px 11px;border-radius:999px;font-size:12px;margin-left:6px;">${item.color}</span>`
            : '';

        // Confirmed stems banner with balance context
        const confirmed = item.confirmed_stems || 0;
        const confirmedDetail = item.confirmed_detail || [];
        const originalQty = item.original_ordered_qty || item.original_stock_qty || 0;
        const othersConfirmed = item.others_confirmed || 0;
        const totalAllConfirmed = item.total_all_confirmed || 0;
        let confirmedBanner = '';
        if (confirmed > 0) {
            const chips = confirmedDetail.map(d =>
                `<span class="confirmed-chip-alloc">${d.farm}: ${d.stems.toLocaleString()}</span>`
            ).join('');
            confirmedBanner = `
                <div class="confirmed-stems-banner">
                    <strong>Your confirmation: ${confirmed.toLocaleString()} stems</strong>
                    ${chips}
                    <span style="margin-left:auto;font-size:11px;opacity:0.8;">
                        Ordered: ${originalQty.toLocaleString()}
                        ${othersConfirmed > 0 ? ` | Others: ${othersConfirmed.toLocaleString()}` : ''}
                        | Total confirmed: ${totalAllConfirmed.toLocaleString()}
                    </span>
                </div>`;
        }

        const _curTeam = (P.item_teams && P.item_teams[item.sales_order_item]) || '';
        const _teamSelect = `<select class="item-team-select" data-so-item="${item.sales_order_item}"
            onchange="frappe.pages['sales-allocation'].set_item_team('${item.sales_order_item}', this.value)"
            title="Packing team for this line"
            style="margin-left:10px;font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid ${_curTeam ? 'var(--good)' : 'var(--bad)'};background:var(--surface-2);color:var(--ink-3);">
            ${['', 'Team A', 'Team B', 'Jamafa', 'Eldama', 'Bravo'].map(t => `<option value="${t}" ${t === _curTeam ? 'selected' : ''}>${t || 'Select team…'}</option>`).join('')}
        </select>`;

        html += `
            <div class="current-so-item">
                <h4 style="margin:0 0 10px 0;color:var(--ink);font-weight:600;">
                    ${item.item_name || 'Item'} (${item.item_code || '?'})
                    <span style="background:var(--grad-ink);color:#fafaf6;padding:4px 12px;border-radius:999px;font-size:12px;margin-left:10px;">
                        ${item.required_length || 'No length'}
                    </span>
                    ${color_badge}
                    ${item.custom_mixed_bunch ? `
                        <span style="background:var(--signal);color:#fff;padding:4px 11px;border-radius:999px;font-size:12px;margin-left:6px;">Mixed Bunch</span>
                        <span style="background:var(--ink-2);color:#fff;padding:4px 11px;border-radius:999px;font-size:12px;margin-left:4px;" title="Internal group: ${item.custom_bunch_group || '?'}">${item.custom_mix_name ? frappe.utils.escape_html(item.custom_mix_name) : 'Bunch ' + (item.custom_bunch_group || '?')}</span>
                    ` : item.custom_mixed_box ? `
                        <span style="background:var(--signal);color:#fff;padding:4px 11px;border-radius:999px;font-size:12px;margin-left:6px;">Mixed Box</span>
                        <span style="background:var(--ink-2);color:#fff;padding:4px 11px;border-radius:999px;font-size:12px;margin-left:4px;" title="Internal group: ${item.custom_mix_group || '?'}">${item.custom_mix_name ? frappe.utils.escape_html(item.custom_mix_name) : 'Group ' + (item.custom_mix_group || '?')}</span>
                    ` : `
                        <span style="background:transparent;color:var(--ink-3);border:1px solid var(--ink-faint);padding:3px 11px;border-radius:999px;font-size:12px;margin-left:6px;">Straight Box</span>
                    `}
                    <span class="so-item-id">${item.sales_order_item || '?'}</span>
                    <button class="substitute-btn" data-so-item="${item.sales_order_item}">Substitute Variety</button>
                    ${_teamSelect}
                    ${remaining <= 0 ? '<span style="color:var(--signal);margin-left:10px;font-size:12px;font-weight:600;">Fully Allocated</span>' : ''}
                </h4>
                ${confirmedBanner}
                <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:15px;font-size:13px;margin-bottom:8px;color:var(--ink-3);">
                    <div>To Allocate: <strong>${required} ${item.stock_uom || ''}</strong></div>
                    <div>Previously: <strong>${total_allocated}</strong></div>
                    <div>Session: <strong style="color:var(--good)">${allocated_session}</strong></div>
                    <div>Remaining: <strong style="color:${remaining > 0 ? 'var(--warn-2)' : 'var(--good)'}">${remaining}</strong></div>
                    <div>Available: <strong style="color:${item.total_available_qty >= remaining ? 'var(--good)' : 'var(--bad)'}">${item.total_available_qty || 0}</strong></div>
                </div>
                ${preferred_farm ? `<div style="font-size:11px;color:var(--signal);margin-bottom:8px;">
                    <span class="grid-badge badge-preferred">Preferred farm: ${preferred_farm}</span>
                </div>` : ''}
                ${(item.incoming_exact_stems || 0) > 0 ? `
                <div style="margin-bottom:8px;padding:9px 14px;background:var(--good-soft);border-radius:12px;font-size:12px;color:var(--good);">
                    <strong>${item.incoming_exact_stems} exact-length stems</strong> (${item.required_length || '?'}) received but not yet shelved.
                </div>` : ''}
            </div>`;

        // Quick FIFO button
        if (remaining > 0 && batches.some(b => (b.available_qty || 0) > 0)) {
            html += `
                <div style="background:var(--surface);padding:12px 16px;border-radius:14px;margin-bottom:12px;">
                    <button class="btn btn-sm btn-default" onclick="frappe.pages['sales-allocation'].auto_allocate_fifo('${item.sales_order_item}')">
                        Auto-Allocate (${remaining} ${item.stock_uom || ''})
                    </button>
                </div>`;
        }

        // Per-item filter bar
        html += P._render_per_item_filter(item);

        // Bucket table
        html += `
            <div style="max-height:400px;overflow-y:auto;margin-bottom:24px;">
                <table class="allocation-grid-table" data-so-item="${item.sales_order_item}">
                    <thead><tr>
                        <th>Age</th><th>Bucket</th><th>Farm</th><th>Shelf</th><th>Length</th>
                        <th>Available</th><th>Allocated Here</th><th>Session</th><th>Actions</th>
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
                    length_badge += ` <span class="grid-badge" style="background:var(--good-soft);color:var(--good);font-size:9px;">Amber OK</span>`;
                else if (is_downgrade)
                    length_badge += ` <span class="grid-badge" style="background:var(--bad-soft);color:var(--bad);font-size:9px;">Needs Approval</span>`;

                const farm_badge = is_preferred ? `<span class="grid-badge badge-preferred">Preferred</span>` : '';
                const await_badge = is_awaiting ? `<span class="grid-badge badge-awaiting">Remote Shelf</span>` : '';

                let actions_html = '';
                if (remaining > 0 && (batch.available_qty || 0) > 0 && !allocated_here && !session_alloc) {
                    const esc_bucket = (batch.bucket_id || '').replace(/'/g, "\\'");
                    const esc_length = (batch.length_status || 'exact').replace(/'/g, "\\'");
                    const esc_stem = (batch.stem_length || '').replace(/'/g, "\\'");
                    actions_html += `
                        <button class="btn btn-xs btn-primary" onclick="frappe.pages['sales-allocation'].allocate_from_bucket(
                            '${item.sales_order_item}','${esc_bucket}',${batch.available_qty},
                            '${item.stock_uom || ''}',${remaining},'${esc_length}','${esc_stem}'
                        )">Allocate All</button>`;
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

        html += `</tbody></table></div>`;
        if (item_idx < items.length - 1) html += `<hr style="margin:20px 0;border:none;border-top:1px solid var(--hairline);">`;
    });

    if (P._dialog && P._dialog.fields_dict && P._dialog.fields_dict.allocation_grid) {
        P._dialog.fields_dict.allocation_grid.$wrapper.html(html);
        P._bind_farm_filter();
        P._bind_mix_filter();
        P._bind_per_item_filters();
    }
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
            title: 'Downgrade — Reason Required',
            fields: [
                { fieldtype: 'HTML', fieldname: 'info', options: `
                    <div style="background:var(--warn-soft);border-radius:10px;padding:13px;margin-bottom:12px;color:var(--ink-3);">
                        <strong>Bucket:</strong> ${bucket_id} (${stem_length}) — Order requires: ${item.required_length || 'N/A'}<br>
                        <strong>Qty:</strong> ${qty} ${uom} | Age: ${age_days}d (amber at ${amber_time}d)
                    </div>
                    <div style="background:var(--bad-soft);border-radius:10px;padding:9px 12px;margin-bottom:10px;font-size:12px;color:var(--bad);">
                        Requires approval — bucket is only ${age_days} days old.
                    </div>${incoming_html}` },
                { fieldtype: 'Small Text', fieldname: 'reason', label: 'Downgrade Reason', reqd: 1 }
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
            freeze: true, freeze_message: 'Unallocating...',
            callback: function (r) {
                if (r.message && r.message.success) frappe.show_alert({ message: r.message.message, indicator: 'green' });
                else frappe.msgprint({ title: 'Note', message: r.message?.message || 'Partial success.', indicator: 'orange' });
                P.select_order(P.selected_order);
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
            title: 'Auto-Allocate — Downgrade Reason Required',
            fields: [
                { fieldtype: 'HTML', fieldname: 'info', options: `
                    <div style="background:var(--warn-soft);border-radius:10px;padding:13px;margin-bottom:12px;color:var(--ink-3);">
                        <strong>Downgrade buckets that will be used:</strong>
                        <ul style="margin:8px 0;padding-left:20px;">${summary}</ul>
                    </div>
                    ${incoming > 0 ? `<div style="background:var(--good-soft);border-radius:10px;padding:9px 12px;margin-bottom:10px;font-size:12px;color:var(--good);">
                        <strong>${incoming} exact-length stems</strong> received but not yet shelved.
                    </div>` : ''}` },
                { fieldtype: 'Small Text', fieldname: 'reason', label: 'Downgrade Reason (applies to all downgraded buckets)', reqd: 1 }
            ],
            primary_action_label: 'Confirm Auto-Allocate',
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
        frappe.msgprint('Select a Packing Team for: ' + names.join(', '));
        return;
    }

    frappe.call({
        method: 'upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.allocate_stock_with_buckets',
        args: { sales_order: P.selected_order, allocations: valid_allocations, location: P.selected_location, teams: JSON.stringify(teams) },
        freeze: true, freeze_message: 'Allocating stock...',
        callback: function (r) {
            if (r.message && r.message.success) {
                const results = r.message.pick_list_results || [];
                const messages = results.map(res => {
                    const link = `<a href="/app/order-pick-list/${res.name || '?'}" target="_blank"><strong>${res.name || '?'}</strong></a>`;
                    if (res.status === 'submitted') return `Pick List ${link} created and submitted`;
                    if (res.status === 'draft') return `Pick List ${link} created as draft`;
                    if (res.status === 'updated_existing') return `Pick List ${link} updated`;
                    return '';
                }).filter(Boolean);

                frappe.msgprint({
                    title: 'Allocation Complete',
                    message: messages.length ? messages.join('<br>') : 'Allocation completed successfully.',
                    indicator: 'green'
                });
                P.allocations = [];
                if (P._dialog) P._dialog.hide();
                P.load_sales_orders();
            } else {
                frappe.msgprint({ title: 'Allocation Failed', message: r.message?.message || 'Allocation failed.', indicator: 'red' });
            }
        }
    });
};

// ─── HELPERS ───
frappe.pages['sales-allocation'].set_item_team = function (so_item, team) {
    const P = frappe.pages['sales-allocation'];
    P.item_teams = P.item_teams || {};
    if (team) P.item_teams[so_item] = team; else delete P.item_teams[so_item];
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

frappe.pages['sales-allocation']._session_bucket_qty = function (so_item, bucket_id) {
    const match = (frappe.pages['sales-allocation'].allocations || [])
        .find(a => a.sales_order_item === so_item && a.bucket_id === bucket_id);
    return match ? (parseFloat(match.qty) || 0) : 0;
};