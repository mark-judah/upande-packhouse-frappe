// Sales Order — Consignee / Shipping Agent / Delivery Point pickers.
//
// Consignee is filtered by the order's Customer (Consignee.customers multiselect
// -> resolved server-side by consignee_api.consignees_for_customer, since a
// client-side get_list can't read a child table's parent).
// Shipping Agent is filtered by the order's Delivery Point (Delivery Point's own
// shipping_agents child table); falls back to every Shipping Agent when that
// Delivery Point has no curated list yet, so the picker is never a dead end.
// Delivery Point itself is filtered to the Roses Business Unit — Roses orders
// should only ever be offered Roses delivery points.
//
// Both popups replace v15's plain non-searchable bordered-<div> dialogs with a
// searchable list styled to the packhouse-dashboard design tokens (so-dialogs.css).

frappe.require('/assets/upande_packhouse/css/so-dialogs.css');

frappe.ui.form.on('Sales Order', {
    onload(frm) { set_delivery_point_query(frm); },
    refresh(frm) {
        set_delivery_point_query(frm);
        bind_picker_intercept(frm, 'custom_shipping_agent', () => {
            if (!frm.doc.custom_delivery_point) {
                frappe.msgprint(__('Please select a Delivery Point first'));
                return false;
            }
            return true;
        }, () => open_shipping_agent_picker(frm));

        bind_picker_intercept(frm, 'custom_consignee', () => {
            if (!frm.doc.customer) {
                frappe.msgprint(__('Please select a Customer first'));
                return false;
            }
            return true;
        }, () => open_consignee_picker(frm));
    },

    // Roses delivery points only.
    custom_delivery_point(frm) {
        if (frm.doc.custom_shipping_agent) frm.set_value('custom_shipping_agent', '');
    },

    customer(frm) {
        if (frm.doc.custom_consignee) frm.set_value('custom_consignee', '');
    }
});

function set_delivery_point_query(frm) {
    frm.set_query('custom_delivery_point', () => ({ filters: { business_unit: 'Roses' } }));
}

// Intercept the plain click on a Link field's input and open our own picker
// instead of the standard awesomplete dropdown. `guard` may return false to
// block (showing its own message) before the dialog opens.
function bind_picker_intercept(frm, fieldname, guard, openFn) {
    const field = frm.fields_dict[fieldname];
    if (!field || !field.$input) return;
    const ns = 'click.sod_' + fieldname;
    field.$input.off(ns).on(ns, function (e) {
        if (guard && guard() === false) return;
        e.preventDefault();
        e.stopPropagation();
        openFn();
        $(this).blur();
    });
}

/* ============================ shared list-picker dialog ============================ */

// title: dialog title. options: [{value, title, sub}]. onPick(value) -> void.
// hint: optional small uppercase note shown above the search box.
function open_search_picker(title, options, onPick, hint) {
    if (!options.length) {
        frappe.msgprint(__('Nothing to choose from yet.'));
        return;
    }
    const d = new frappe.ui.Dialog({ title, fields: [{ fieldtype: 'HTML', fieldname: 'body' }] });

    const optHtml = (opt) => `
        <div class="sod-opt" data-value="${frappe.utils.escape_html(opt.value)}">
            <div class="sod-opt-title">${frappe.utils.escape_html(opt.title)}</div>
            ${opt.sub ? `<div class="sod-opt-sub">${frappe.utils.escape_html(opt.sub)}</div>` : ''}
        </div>`;

    d.fields_dict.body.$wrapper.html(`
        <div class="sod">
            ${hint ? `<div class="sod-hint">${frappe.utils.escape_html(hint)}</div>` : ''}
            <div class="sod-search"><input type="text" placeholder="${__('Search…')}" autocomplete="off"></div>
            <div class="sod-list">${options.map(optHtml).join('')}</div>
            <div class="sod-empty" style="display:none">${__('No matches.')}</div>
        </div>`);

    const $wrap = d.fields_dict.body.$wrapper;
    const $search = $wrap.find('.sod-search input');
    const $list = $wrap.find('.sod-list');
    const $empty = $wrap.find('.sod-empty');

    $search.on('input', function () {
        const q = $(this).val().trim().toLowerCase();
        let shown = 0;
        $list.find('.sod-opt').each(function () {
            const hit = !q || $(this).text().toLowerCase().includes(q);
            $(this).toggle(hit);
            if (hit) shown++;
        });
        $list.toggle(shown > 0);
        $empty.toggle(shown === 0);
    });

    $list.on('click', '.sod-opt', function () {
        const value = $(this).data('value');
        d.hide();
        onPick(String(value));
    });

    d.show();
    setTimeout(() => $search.trigger('focus'), 50);
}

/* ============================ consignee ============================ */

async function open_consignee_picker(frm) {
    let consignees = [];
    try {
        const r = await frappe.call({
            method: 'upande_packhouse.consignee_api.consignees_for_customer',
            args: { customer: frm.doc.customer }
        });
        consignees = (r.message || []).filter(Boolean);
    } catch (e) {
        frappe.msgprint(__('Error loading consignees: {0}', [e.message || e]));
        return;
    }
    const options = consignees.map(name => ({ value: name, title: name }));
    open_search_picker(__('Select Consignee'), options, (selected) => {
        frm.set_value('custom_consignee', selected);
        frappe.show_alert({ message: __('Consignee set to {0}', [selected]), indicator: 'green' }, 3);
    });
}

/* ============================ shipping agent ============================ */

async function open_shipping_agent_picker(frm) {
    let agents = [];
    let scoped = false;
    try {
        const dp = await frappe.db.get_doc('Delivery Point', frm.doc.custom_delivery_point);
        agents = (dp.shipping_agents || []).map(r => r.shipping_agent).filter(Boolean);
        scoped = agents.length > 0;
    } catch (e) { /* delivery point missing — fall through to every agent */ }

    if (!agents.length) {
        const all = await frappe.db.get_list('Shipping Agent', { fields: ['name'], limit: 0 });
        agents = (all || []).map(a => a.name);
    }

    const options = agents.map(name => ({ value: name, title: name }));
    open_search_picker(
        scoped
            ? __('Select Shipping Agent — {0}', [frm.doc.custom_delivery_point])
            : __('Select Shipping Agent'),
        options,
        (selected) => {
            frm.set_value('custom_shipping_agent', selected);
            frappe.show_alert({ message: __('Shipping agent set to {0}', [selected]), indicator: 'green' }, 3);
        },
        scoped ? null : __('No agents curated for this Delivery Point yet — showing every Shipping Agent')
    );
}
