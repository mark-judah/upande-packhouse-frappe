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
    const styleEl = document.createElement('style');
    styleEl.id = 'sales-allocation-styles';
    styleEl.textContent = `
        .sales-allocation-container {
            padding: 15px;
            background: #f5f7fa;
            min-height: calc(100vh - 120px);
        }
        .allocation-panel {
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 15px;
            border-bottom: 2px solid #ecf0f1;
        }
        .panel-header h3 { color: #2c3e50; font-size: 16px; margin: 0; }
        .sales-order-card {
            border: 2px solid #ecf0f1;
            border-radius: 6px;
            padding: 15px;
            margin-bottom: 10px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .sales-order-card:hover { border-color: #5e64ff; box-shadow: 0 2px 5px rgba(94,100,255,0.2); }
        .sales-order-card.selected { border-color: #5e64ff; background: #f0f1ff; }
        .loading-state, .empty-state { text-align: center; padding: 40px 20px; color: #95a5a6; }
        .allocation-grid-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            margin-bottom: 20px;
        }
        .allocation-grid-table thead { background: #f5f7fa; position: sticky; top: 0; z-index: 10; }
        .allocation-grid-table th {
            padding: 10px 8px;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid #d1d8dd;
            white-space: nowrap;
        }
        .allocation-grid-table td {
            padding: 10px 8px;
            border-bottom: 1px solid #ebeff2;
            vertical-align: middle;
        }
        .allocation-grid-table tbody tr:hover { background: #f9fafb; }
        .allocation-grid-table tbody tr.downgrade-bucket { background: #fffbeb; border-left: 3px solid #d97706; }
        .allocation-grid-table tbody tr.previously-allocated-bucket { background: #e3f2fd; opacity: 0.85; border-left: 4px solid #2196f3; }
        .allocation-grid-table tbody tr.preferred-farm-row { border-left: 3px solid #10b981; }
        .allocation-grid-table tbody tr.awaiting-transfer-row { background: #fef3c7; border-left: 3px solid #f59e0b; }
        .grid-badge {
            display: inline-block;
            padding: 3px 7px;
            border-radius: 10px;
            font-size: 10px;
            font-weight: bold;
            text-transform: uppercase;
            margin: 1px;
        }
        .badge-exact { background: #c6f6d5; color: #276749; }
        .badge-downgrade { background: #feebc8; color: #d69e2e; }
        .badge-preferred { background: #d1fae5; color: #065f46; }
        .badge-awaiting { background: #fef3c7; color: #92400e; }
        .badge-allocated { background: #2196f3; color: white; }
        .badge-confirmed { background: #e4ffc1; color: #4a7c1f; }
        .so-item-id {
            background: #e0f2f1;
            color: #00796b;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-family: monospace;
            margin-left: 8px;
        }

        /* ─── Location selector ─── */
        .location-selector {
            background: #f8f9fa;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 15px;
        }
        .location-selector h4 { margin: 0 0 10px 0; color: #2c3e50; font-size: 14px; font-weight: 600; }
        .location-buttons { display: flex; gap: 10px; }
        .location-btn {
            flex: 1;
            padding: 12px;
            border: 2px solid #d1d8dd;
            border-radius: 6px;
            background: white;
            cursor: pointer;
            text-align: center;
            font-weight: 600;
            transition: all 0.2s;
        }
        .location-btn:hover { border-color: #5e64ff; background: #f0f1ff; }
        .location-btn.active { border-color: #5e64ff; background: #5e64ff; color: white; }
        .location-info {
            margin-top: 10px;
            padding: 8px 12px;
            background: #e3f2fd;
            border-left: 4px solid #2196f3;
            border-radius: 4px;
            font-size: 12px;
            color: #1565c0;
        }

        /* ─── Farm filter in dialog ─── */
        .farm-filter-bar {
            background: #f8f9fa;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            padding: 12px 16px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }
        .farm-filter-bar label.title {
            font-weight: 600;
            color: #2c3e50;
            font-size: 13px;
            white-space: nowrap;
        }
        .farm-checkbox-group { display: flex; gap: 12px; flex-wrap: wrap; }
        .farm-checkbox-item {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 6px 10px;
            border: 1px solid #d1d8dd;
            border-radius: 20px;
            background: white;
            cursor: pointer;
            font-size: 12px;
            transition: all 0.15s;
            user-select: none;
        }
        .farm-checkbox-item:hover { border-color: #5e64ff; background: #f0f1ff; }
        .farm-checkbox-item.checked { border-color: #5e64ff; background: #5e64ff; color: white; }
        .farm-checkbox-item.checked .farm-badge { background: rgba(255,255,255,0.25); color: white; }
        .farm-badge {
            font-size: 9px;
            padding: 2px 5px;
            border-radius: 8px;
            background: #e0e0e0;
            color: #555;
            text-transform: uppercase;
        }
        .farm-badge.sales { background: #d1fae5; color: #065f46; }
        .farm-badge.remote { background: #fef3c7; color: #92400e; }
        .current-so-item {
            background: #f0f9ff;
            border: 2px solid #1e88e5;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 16px;
        }

        /* ─── Mix filter in dialog ─── */
        .mix-filter-bar {
            background: #faf5ff;
            border: 1px solid #d8b4fe;
            border-radius: 6px;
            padding: 10px 16px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }
        .mix-filter-bar label.title {
            font-weight: 600;
            color: #6b21a8;
            font-size: 13px;
            white-space: nowrap;
        }
        .mix-filter-bar select {
            padding: 6px 10px;
            border: 1px solid #d8b4fe;
            border-radius: 4px;
            font-size: 12px;
            background: white;
            color: #4c1d95;
            min-width: 200px;
        }
        .mix-filter-bar .mix-count {
            font-size: 11px;
            color: #6b21a8;
            margin-left: auto;
        }

        /* ─── Confirmed stems banner ─── */
        .confirmed-stems-banner {
            background: #e4ffc1;
            border: 1px solid #8bd346;
            border-radius: 6px;
            padding: 8px 12px;
            margin-bottom: 10px;
            font-size: 12px;
            color: #4a7c1f;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .confirmed-stems-banner strong { font-size: 13px; }
        .confirmed-chip-alloc {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 10px;
            background: #d1fae5;
            color: #065f46;
            font-size: 11px;
            font-weight: 600;
            margin: 0 3px;
        }

        /* ─── Per-item inline filter bar ─── */
        .item-batch-filter {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
            background: #f8f9fa;
            border: 1px solid #e0e0e0;
            border-radius: 5px;
            padding: 6px 10px;
            margin-bottom: 10px;
            font-size: 11px;
        }
        .item-batch-filter .ibf-label {
            font-weight: 600;
            color: #2c3e50;
            font-size: 11px;
            white-space: nowrap;
        }
        .item-batch-filter .ibf-sep {
            width: 1px;
            height: 18px;
            background: #d1d8dd;
            flex-shrink: 0;
        }
        .item-batch-filter .ibf-group {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .item-batch-filter .ibf-group-label {
            font-size: 10px;
            color: #6c757d;
            font-weight: 600;
            white-space: nowrap;
        }
        .item-batch-filter select.ibf-select {
            padding: 2px 6px;
            border: 1px solid #d1d8dd;
            border-radius: 4px;
            font-size: 11px;
            background: white;
            cursor: pointer;
            max-width: 80px;
        }
        .item-batch-filter .ibf-range-sep {
            font-size: 10px;
            color: #999;
        }
        .item-batch-filter .ibf-clear {
            padding: 2px 8px;
            border: 1px solid #d1d8dd;
            border-radius: 4px;
            background: white;
            cursor: pointer;
            font-size: 10px;
            margin-left: auto;
            transition: all 0.15s;
        }
        .item-batch-filter .ibf-clear:hover { border-color: #e74c3c; color: #e74c3c; }
        /* ─── Substitute variety button ─── */
        .substitute-btn {
            padding: 3px 10px;
            border: 1px solid #e67e22;
            border-radius: 4px;
            background: #fff8f0;
            color: #e67e22;
            cursor: pointer;
            font-size: 11px;
            font-weight: 600;
            transition: all 0.15s;
        }
        .substitute-btn:hover { background: #e67e22; color: white; }
        .sub-variety-card {
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            padding: 10px 12px;
            margin-bottom: 6px;
            cursor: pointer;
            transition: all 0.15s;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .sub-variety-card:hover { border-color: #5e64ff; background: #f0f1ff; }
        .sub-variety-card.recommended { border-left: 3px solid #10b981; background: #f0fdf4; }
        .sub-variety-card .sub-name { font-weight: 600; font-size: 13px; color: #2c3e50; }
        .sub-variety-card .sub-meta { font-size: 11px; color: #6c757d; }
        .sub-variety-card .sub-badge {
            font-size: 9px;
            padding: 2px 6px;
            border-radius: 8px;
            font-weight: 600;
            text-transform: uppercase;
        }
    `;
    document.head.appendChild(styleEl);
};

frappe.pages['sales-allocation'].make = function (page) {
    frappe.pages['sales-allocation'].page = page;
    let $container = $('<div class="sales-allocation-container"></div>').appendTo(page.main);

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

            <div class="filter-section" style="padding:12px;border-bottom:1px solid #e0e0e0;background:#f8f9fa;">
                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px;">
                    <div style="display:flex;gap:5px;align-items:center;">
                        <label style="font-size:11px;color:#6c757d;white-space:nowrap;">Posting Date:</label>
                        <input type="date" id="orderStartDate" style="flex:1;padding:6px;border:1px solid #d1d8dd;border-radius:4px;font-size:12px;">
                        <span style="color:#6c757d;">to</span>
                        <input type="date" id="orderEndDate" style="flex:1;padding:6px;border:1px solid #d1d8dd;border-radius:4px;font-size:12px;">
                    </div>
                    <div style="display:flex;gap:5px;align-items:center;">
                        <label style="font-size:11px;color:#6c757d;white-space:nowrap;">Delivery:</label>
                        <input type="date" id="deliveryStartDate" style="flex:1;padding:6px;border:1px solid #d1d8dd;border-radius:4px;font-size:12px;">
                        <span style="color:#6c757d;">to</span>
                        <input type="date" id="deliveryEndDate" style="flex:1;padding:6px;border:1px solid #d1d8dd;border-radius:4px;font-size:12px;">
                    </div>
                    <div style="display:flex;gap:5px;">
                        <input type="text" id="orderSearchInput" placeholder="Search order, customer, order name, variety..." style="flex:1;padding:6px;border:1px solid #d1d8dd;border-radius:4px;font-size:12px;">
                        <select id="priorityFilter" style="padding:6px;border:1px solid #d1d8dd;border-radius:4px;font-size:12px;">
                            <option value="">All Priorities</option>
                            <option value="High">High</option>
                            <option value="Medium">Medium</option>
                            <option value="Low">Low</option>
                        </select>
                        <select id="boxTypeFilter" style="padding:6px;border:1px solid #d1d8dd;border-radius:4px;font-size:12px;">
                            <option value="">All Box Types</option>
                            <option value="mixed">Mixed Only</option>
                            <option value="straight">Straight Only</option>
                        </select>
                    </div>
                </div>
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div id="resultsCount" style="font-size:12px;color:#6c757d;"></div>
                    <button id="clearFilters" style="padding:6px 12px;background:#fff;border:1px solid #d1d8dd;border-radius:4px;cursor:pointer;font-size:12px;">Clear Filters</button>
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
        method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.get_location_config',
        callback: function (r) {
            if (r.message && r.message.locations) {
                frappe.pages['sales-allocation'].location_config = r.message;
                frappe.pages['sales-allocation'].render_location_selector();
            } else {
                $('#locationButtons').html('<div style="color:#e74c3c;padding:10px;">No locations configured</div>');
            }
        },
        error: function () {
            $('#locationButtons').html('<div style="color:#e74c3c;padding:10px;">Error loading locations</div>');
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
            `${f.farm} <span style="font-size:10px;opacity:0.8;">(${f.sales_shelf ? 'Sales' : 'Remote'})</span>`
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
        method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.get_pending_sales_orders',
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
        const bar_color = pct >= 75 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';

        return `
            <div class="sales-order-card ${is_selected}" data-order="${order.name}">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div style="font-weight:bold;color:#34495e;">${order.name}</div>
                    <div style="font-size:10px;padding:3px 8px;border-radius:4px;background:#e0f2f1;color:#00796b;">${priority}</div>
                </div>
                <div style="margin-top:5px;font-size:12px;color:#7f8c8d;">
                    <div>Customer: ${order.customer}</div>
                    <div>Order: ${order.custom_order_name || '-'}</div>
                    <div>Varieties: ${order.item_codes || '-'}</div>
                    <div>Delivery: ${frappe.datetime.str_to_user(order.delivery_date)}</div>
                </div>
                <div style="margin-top:8px;">
                    <div style="display:flex;justify-content:space-between;font-size:11px;color:#6c757d;margin-bottom:3px;">
                        <span>Allocation</span>
                        <span style="font-weight:600;color:${bar_color};">${pct}%</span>
                    </div>
                    <div style="width:100%;height:6px;background:#e0e0e0;border-radius:3px;overflow:hidden;">
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
        method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.get_sales_order_items_with_buckets',
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
            <div style="font-size:11px;color:#6c757d;margin-left:auto;">
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
            method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.get_sales_order_items_with_buckets',
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

    const render_sub_content = function (varieties, show_filter) {
        let filter_html = '';
        if (original_color || original_headsize) {
            const filter_label = [
                original_color ? `Color: ${original_color}` : '',
                original_headsize ? `Headsize: ${original_headsize}` : ''
            ].filter(Boolean).join(', ');

            filter_html = `
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding:8px 12px;background:${show_filter ? '#f0fdf4' : '#f8f9fa'};border:1px solid ${show_filter ? '#86efac' : '#e0e0e0'};border-radius:6px;font-size:12px;">
                    <div>
                        <strong>Recommendation:</strong> ${filter_label}
                        ${show_filter ? '<span style="color:#16a085;margin-left:8px;">(active)</span>' : '<span style="color:#6c757d;margin-left:8px;">(cleared)</span>'}
                    </div>
                    <button class="btn btn-xs btn-default" id="toggleSubFilter">
                        ${show_filter ? 'Show All Varieties' : 'Show Recommended Only'}
                    </button>
                </div>`;
        }

        const current_html = `
            <div style="background:#e3f2fd;border:1px solid #90caf9;border-radius:6px;padding:10px 12px;margin-bottom:12px;font-size:12px;">
                <strong>Current:</strong> ${item.item_name} (${item.item_code})
                ${original_color ? ` | Color: ${original_color}` : ''}
                ${original_headsize ? ` | Headsize: ${original_headsize}` : ''}
                | Qty: ${item.pending_stock_qty || 0} ${item.stock_uom || ''}
            </div>`;

        let list_html = '';
        if (!varieties || !varieties.length) {
            list_html = '<div style="text-align:center;padding:20px;color:#95a5a6;">No varieties found</div>';
        } else {
            list_html = varieties.map(v => {
                const is_same = v.item_code === item.item_code;
                const is_recommended = (original_color && v.color === original_color) || (original_headsize && v.headsize === original_headsize);
                const rec_class = is_recommended && !is_same ? 'recommended' : '';
                const badges = [];
                if (is_same) badges.push('<span class="sub-badge" style="background:#e3f2fd;color:#1565c0;">Current</span>');
                if (is_recommended && !is_same) badges.push('<span class="sub-badge" style="background:#d1fae5;color:#065f46;">Recommended</span>');

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
            '<div style="text-align:center;padding:20px;color:#95a5a6;">Loading varieties...</div>'
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
            method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.get_substitute_varieties',
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
        method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.substitute_variety',
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
        html += `<div style="padding:20px;text-align:center;color:#6b21a8;background:#faf5ff;border:1px dashed #d8b4fe;border-radius:6px;">
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
            ? `<span style="background:#fce4ec;color:#c2185b;padding:4px 10px;border-radius:12px;font-size:12px;margin-left:6px;">${item.color}</span>`
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
            style="margin-left:10px;font-size:12px;padding:3px 8px;border-radius:8px;border:1px solid ${_curTeam ? '#27ae60' : '#e74c3c'};">
            ${['', 'Team A', 'Team B', 'Jamafa', 'Eldama', 'Bravo'].map(t => `<option value="${t}" ${t === _curTeam ? 'selected' : ''}>${t || 'Select team…'}</option>`).join('')}
        </select>`;

        html += `
            <div class="current-so-item">
                <h4 style="margin:0 0 10px 0;color:#2c3e50;">
                    ${item.item_name || 'Item'} (${item.item_code || '?'})
                    <span style="background:#3498db;color:white;padding:4px 12px;border-radius:12px;font-size:12px;margin-left:10px;">
                        ${item.required_length || 'No length'}
                    </span>
                    ${color_badge}
                    ${item.custom_mixed_bunch ? `
                        <span style="background:#c0392b;color:white;padding:4px 10px;border-radius:12px;font-size:12px;margin-left:6px;">Mixed Bunch</span>
                        <span style="background:#922b21;color:white;padding:4px 10px;border-radius:12px;font-size:12px;margin-left:4px;" title="Internal group: ${item.custom_bunch_group || '?'}">${item.custom_mix_name ? frappe.utils.escape_html(item.custom_mix_name) : 'Bunch ' + (item.custom_bunch_group || '?')}</span>
                    ` : item.custom_mixed_box ? `
                        <span style="background:#8e44ad;color:white;padding:4px 10px;border-radius:12px;font-size:12px;margin-left:6px;">Mixed Box</span>
                        <span style="background:#6c3483;color:white;padding:4px 10px;border-radius:12px;font-size:12px;margin-left:4px;" title="Internal group: ${item.custom_mix_group || '?'}">${item.custom_mix_name ? frappe.utils.escape_html(item.custom_mix_name) : 'Group ' + (item.custom_mix_group || '?')}</span>
                    ` : `
                        <span style="background:#1a7a4a;color:white;padding:4px 10px;border-radius:12px;font-size:12px;margin-left:6px;">Straight Box</span>
                    `}
                    <span class="so-item-id">${item.sales_order_item || '?'}</span>
                    <button class="substitute-btn" data-so-item="${item.sales_order_item}">Substitute Variety</button>
                    ${_teamSelect}
                    ${remaining <= 0 ? '<span style="color:#16a085;margin-left:10px;font-size:12px;">Fully Allocated</span>' : ''}
                </h4>
                ${confirmedBanner}
                <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:15px;font-size:13px;margin-bottom:8px;">
                    <div>To Allocate: <strong>${required} ${item.stock_uom || ''}</strong></div>
                    <div>Previously: <strong>${total_allocated}</strong></div>
                    <div>Session: <strong style="color:#27ae60">${allocated_session}</strong></div>
                    <div>Remaining: <strong style="color:${remaining > 0 ? '#e67e22' : '#27ae60'}">${remaining}</strong></div>
                    <div>Available: <strong style="color:${item.total_available_qty >= remaining ? '#27ae60' : '#e74c3c'}">${item.total_available_qty || 0}</strong></div>
                </div>
                ${preferred_farm ? `<div style="font-size:11px;color:#065f46;margin-bottom:8px;">
                    <span class="grid-badge badge-preferred">Preferred farm: ${preferred_farm}</span>
                </div>` : ''}
                ${(item.incoming_exact_stems || 0) > 0 ? `
                <div style="margin-bottom:8px;padding:8px 12px;background:#e8f5e9;border:1px solid #a5d6a7;border-radius:6px;font-size:12px;color:#2e7d32;">
                    <strong>${item.incoming_exact_stems} exact-length stems</strong> (${item.required_length || '?'}) received but not yet shelved.
                </div>` : ''}
            </div>`;

        // Quick FIFO button
        if (remaining > 0 && batches.some(b => (b.available_qty || 0) > 0)) {
            html += `
                <div style="background:#f8f9fa;padding:10px 15px;border-radius:6px;border:1px solid #e0e0e0;margin-bottom:12px;">
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
            html += `<tr><td colspan="9" style="text-align:center;padding:30px;color:#95a5a6;">No compatible buckets found</td></tr>`;
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
                    length_badge += ` <span class="grid-badge" style="background:#e8f5e9;color:#2e7d32;font-size:9px;">Amber OK</span>`;
                else if (is_downgrade)
                    length_badge += ` <span class="grid-badge" style="background:#ffcdd2;color:#c62828;font-size:9px;">Needs Approval</span>`;

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
                    actions_html = '<span style="color:#9ca3af;font-size:11px;">No stock</span>';
                }

                html += `
                    <tr class="${row_class}" data-bucket-id="${batch.bucket_id || ''}">
                        <td>${batch.age_days != null ? batch.age_days : '?'}d</td>
                        <td><strong>${batch.bucket_id || '-'}</strong></td>
                        <td>${batch.shelf_farm || '-'} ${farm_badge}</td>
                        <td>${batch.shelf_location || '-'} ${await_badge}</td>
                        <td>${batch.stem_length || 'N/A'} ${length_badge}</td>
                        <td style="color:${(batch.available_qty || 0) > 0 ? '#27ae60' : '#e74c3c'}">${batch.available_qty || 0}</td>
                        <td>${allocated_here > 0 ? `<strong>${allocated_here}</strong>` : '-'}</td>
                        <td>${session_alloc > 0 ? `<span style="color:#27ae60">${session_alloc}</span>` : '-'}</td>
                        <td>${actions_html || '-'}</td>
                    </tr>`;
            });
        }

        html += `</tbody></table></div>`;
        if (item_idx < items.length - 1) html += `<hr style="margin:20px 0;border:none;border-top:2px solid #ecf0f1;">`;
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
            incoming_html = `<div style="background:#e3f2fd;border:1px solid #90caf9;border-radius:4px;padding:8px;margin-top:10px;font-size:12px;color:#1565c0;">
                <strong>${incoming} exact-length stems</strong> (${item.required_length || '?'}) received but not yet shelved.
            </div>`;
        }
        const d = new frappe.ui.Dialog({
            title: 'Downgrade — Reason Required',
            fields: [
                { fieldtype: 'HTML', fieldname: 'info', options: `
                    <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:12px;margin-bottom:12px;">
                        <strong>Bucket:</strong> ${bucket_id} (${stem_length}) — Order requires: ${item.required_length || 'N/A'}<br>
                        <strong>Qty:</strong> ${qty} ${uom} | Age: ${age_days}d (amber at ${amber_time}d)
                    </div>
                    <div style="background:#ffebee;border:1px solid #ef9a9a;border-radius:4px;padding:8px;margin-bottom:10px;font-size:12px;color:#c62828;">
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
            method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.unallocate_bucket_from_opl',
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
                    <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:12px;margin-bottom:12px;">
                        <strong>Downgrade buckets that will be used:</strong>
                        <ul style="margin:8px 0;padding-left:20px;">${summary}</ul>
                    </div>
                    ${incoming > 0 ? `<div style="background:#e3f2fd;border:1px solid #90caf9;border-radius:4px;padding:8px;margin-bottom:10px;font-size:12px;color:#1565c0;">
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
        method: 'upande_kaitet.upande_kaitet.page.sales_allocation.sales_allocation.allocate_stock_with_buckets',
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