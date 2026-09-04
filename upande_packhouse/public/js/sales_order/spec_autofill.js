// Autofill Sales Order By Specification — thin client over upande_packhouse.spec_autofill.
// One unified picker for EVERY spec (straight box, mixed box, mixed bunch): a clean
// table of the spec's colour lines, each with a variety chooser annotated by LIVE
// shelf availability, a boxes input, and a running stem tally. The server decides
// row shaping (mix/bunch groups, packrate field, warehouse routing).

frappe.ui.form.on('Sales Order', {
    onload(frm)  { set_spec_query(frm); },
    refresh(frm) { set_spec_query(frm); },
    customer(frm) { set_spec_query(frm); },
    validate(frm) {
        const missing = [];
        (frm.doc.items || []).forEach((it, i) => { if (it.item_code && !it.custom_line) missing.push(i + 1); });
        if (missing.length) {
            // Non-modal toast so it never competes with a save/submit error modal.
            frappe.show_alert({ message: __('Row(s) {0} have no Specification.', [missing.join(', ')]), indicator: 'orange' }, 5);
        }
    }
});

// This customer's Active specs only (expired Temporary specs are set Inactive by the daily job).
function set_spec_query(frm) {
    frm.set_query('custom_line', 'items', () => {
        const filters = { status: 'Active' };
        if (frm.doc.customer) filters.customer = frm.doc.customer;
        return { filters };
    });
}

frappe.ui.form.on('Sales Order Item', {
    custom_line(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (!row.custom_line) return;
        frappe.call({
            method: 'upande_packhouse.spec_autofill.get_spec_fill_data',
            args: { spec: row.custom_line }
        }).then(r => { if (r.message) open_spec_dialog(frm, cdn, r.message); });
    }
});

function esc(s) {
    return (frappe.utils && frappe.utils.escape_html) ? frappe.utils.escape_html(s || '') : (s || '');
}
function nfmt(n) { return format_number(n || 0, null, 0); }

function open_spec_dialog(frm, trigger_cdn, data) {
    const lines = data.lines || [];
    if (!lines.length) { frappe.msgprint(__('Specification {0} has no box items.', [data.spec])); return; }

    // box-type badge helps the salesperson see what they're building
    const kind = data.is_mixed_box ? 'Mixed Box' : 'Straight Box';

    const fields = [{ fieldtype: 'HTML', fieldname: 'grid' }];

    const d = new frappe.ui.Dialog({
        title: __('Fill Order from {0}', [data.spec_name || data.spec]),
        size: 'extra-large',
        fields: fields,
        primary_action_label: __('Add to Order'),
        primary_action() {
            const $rows = d.$wrapper.find('tr.spa-row');
            const selections = [];
            $rows.each(function () {
                const $r = $(this);
                if (!$r.find('.spa-inc').prop('checked')) return;
                const variety = $r.find('.spa-var').val();
                if (!variety) return;
                selections.push({
                    line_idx: parseInt($r.attr('data-idx'), 10),
                    variety: variety,
                    stems: cint($r.find('.spa-stems').val()),
                    boxes: cint($r.find('.spa-boxes').val())
                });
            });
            if (!selections.length) { frappe.msgprint(__('Tick at least one line.')); return; }
            d.hide();
            append_rows(frm, trigger_cdn, data.spec, selections, null);
        }
    });

    // ---- build the interactive table ----
    const rowsHtml = lines.map(line => {
        const opts = (line.approved || []).map(a => {
            const label = `${a.item_name || a.variety} — ${nfmt(a.available)} stems`;
            return `<option value="${esc(a.variety)}" data-avail="${a.available || 0}">${esc(label)}</option>`;
        }).join('');
        // default: most-available approved variety
        let best = '', bestQ = -1, bestFarms = '';
        (line.approved || []).forEach(a => {
            if ((a.available || 0) > bestQ) {
                bestQ = a.available || 0; best = a.variety;
                bestFarms = Object.keys(a.by_farm || {}).sort((x, y) => a.by_farm[y] - a.by_farm[x])
                    .map(f => `${f}: ${nfmt(a.by_farm[f])}`).join(' · ');
            }
        });
        const bunch = line.is_mixed_bunch ? '<span class="spa-chip spa-chip-mix">Mixed Bunch</span>'
                                          : '<span class="spa-chip">Mono Bunch</span>';
        const meta = [bunch, line.length ? esc(line.length) : '', line.stems_per_bunch ? `${line.stems_per_bunch}/bunch` : '']
            .filter(Boolean).join(' · ');
        const colour = line.colour || __('Line {0}', [line.idx + 1]);
        const packrate = line.pack_rate || line.stems_per_bunch || 0;
        return `
        <tr class="spa-row" data-idx="${line.idx}">
          <td class="spa-c"><input type="checkbox" class="spa-inc" ${bestQ > 0 ? 'checked' : ''}></td>
          <td><div class="spa-colour">${esc(colour)}</div><div class="spa-meta">${meta}</div></td>
          <td>
            <select class="spa-var form-control input-sm">${opts}</select>
            <div class="spa-avail">${bestFarms || '<span class="spa-nostock">no shelf stock</span>'}</div>
          </td>
          <td><input type="number" class="spa-stems form-control input-sm" min="0" value="${packrate}"></td>
          <td><input type="number" class="spa-boxes form-control input-sm" min="0" value="1"></td>
          <td class="spa-c spa-line-stems" style="text-align:right">0</td>
        </tr>`;
    }).join('');

    const html = `
    <style>
      .spa-tbl{width:100%;border-collapse:collapse;font-size:13px}
      .spa-tbl th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#8a8780;text-align:left;padding:6px 8px;border-bottom:1px solid var(--border-color,#e2e4e9)}
      .spa-tbl td{padding:8px;border-bottom:1px solid var(--border-color,#f0f1f3);vertical-align:top}
      .spa-c{text-align:center}
      .spa-colour{font-weight:600}
      .spa-meta{font-size:11px;color:#8a8780;margin-top:2px}
      .spa-avail{font-size:11px;color:#16a34a;margin-top:3px}
      .spa-nostock{color:#b45309}
      .spa-chip{display:inline-block;font-size:10px;padding:1px 6px;border-radius:9px;background:#f4f3ef;color:#5a5a52}
      .spa-chip-mix{background:rgba(10,10,10,.08);color:#0a0a0a}
      .spa-var,.spa-stems,.spa-boxes{height:28px}
      .spa-stems,.spa-boxes{width:74px}
      .spa-foot{margin-top:12px;padding-top:10px;border-top:1px solid var(--border-color,#e2e4e9);font-size:13px;text-align:right;color:#3a3a34}
      .spa-badge{float:left;font-size:11px;color:#8a8780;text-transform:uppercase;letter-spacing:.04em}
    </style>
    <div class="spa">
      <table class="spa-tbl">
        <thead><tr>
          <th class="spa-c">Use</th><th>Colour / Line</th><th>Variety &amp; availability</th>
          <th>Stems/Box</th><th>Boxes</th><th style="text-align:right">Stems</th>
        </tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
      <div class="spa-foot"><span class="spa-badge">${esc(kind)}</span>
        Total: <b class="spa-tot-boxes">0</b> boxes &middot; <b class="spa-tot-stems">0</b> stems</div>
    </div>`;

    d.fields_dict.grid.$wrapper.html(html);

    // by-farm lookup for the availability hint on variety change
    const farmMap = {};
    lines.forEach(line => {
        farmMap[line.idx] = {};
        (line.approved || []).forEach(a => { farmMap[line.idx][a.variety] = a.by_farm || {}; });
    });

    const $w = d.$wrapper;
    function recompute() {
        let tb = 0, ts = 0;
        $w.find('tr.spa-row').each(function () {
            const $r = $(this);
            const inc = $r.find('.spa-inc').prop('checked');
            const stems = cint($r.find('.spa-stems').val());
            const boxes = cint($r.find('.spa-boxes').val());
            const line = stems * boxes;
            $r.find('.spa-line-stems').text(nfmt(inc ? line : 0));
            if (inc) { tb += boxes; ts += line; }
        });
        $w.find('.spa-tot-boxes').text(nfmt(tb));
        $w.find('.spa-tot-stems').text(nfmt(ts));
    }
    $w.on('input change', '.spa-stems,.spa-boxes,.spa-inc', recompute);
    $w.on('change', '.spa-var', function () {
        const $r = $(this).closest('tr.spa-row');
        const idx = parseInt($r.attr('data-idx'), 10);
        const farms = (farmMap[idx] || {})[$(this).val()] || {};
        const txt = Object.keys(farms).sort((a, b) => farms[b] - farms[a]).map(f => `${f}: ${nfmt(farms[f])}`).join(' · ');
        $r.find('.spa-avail').html(txt || '<span class="spa-nostock">no shelf stock</span>');
    });

    d.show();
    recompute();
}

function next_group(frm, field) {
    let max = 0;
    (frm.doc.items || []).forEach(r => { max = Math.max(max, cint(r[field])); });
    return max + 1;
}

function append_rows(frm, trigger_cdn, spec, selections, source_warehouse) {
    frappe.call({
        method: 'upande_packhouse.spec_autofill.build_spec_rows',
        args: {
            spec: spec,
            selections: JSON.stringify(selections),
            next_mix_group: next_group(frm, 'custom_mix_group'),
            next_bunch_group: next_group(frm, 'custom_bunch_group'),
            source_warehouse: source_warehouse
        }
    }).then(r => {
        const rows = (r.message && r.message.rows) || [];
        if (!rows.length) { frappe.msgprint(__('Nothing to add.')); return; }
        frm.doc.items = (frm.doc.items || []).filter(row => row.name !== trigger_cdn);  // drop scratch row
        rows.forEach(data => { Object.assign(frm.add_child('items'), data); });          // direct assign — no re-fire
        frm.refresh_field('items');
        frm.script_manager.trigger('calculate_taxes_and_totals');
        recompute_order_summary(frm);   // box_math.js — add_child doesn't reliably fire items_add
        frappe.show_alert({ message: __('Added {0} line(s) from {1}', [rows.length, spec]), indicator: 'green' }, 3);
    });
}
