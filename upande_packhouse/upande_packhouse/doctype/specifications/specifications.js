// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

// Pack Rate is read-only and reqd -- it's always derived, never typed. The
// server (upande_packhouse.spec.ensure_spec_uoms_and_packrates, a
// before_validate hook) recomputes it authoritatively on every save; this is
// just so the grid shows the right number immediately as the operator types,
// instead of only after a save round-trip.
function recompute_pack_rate(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const pack_rate = flt(row.bunches_per_box || 0) * flt(row.stems_per_bunch || 0);
	frappe.model.set_value(cdt, cdn, "pack_rate", pack_rate);
}

frappe.ui.form.on("Spec Box Item", {
	bunches_per_box(frm, cdt, cdn) {
		recompute_pack_rate(frm, cdt, cdn);
	},
	stems_per_bunch(frm, cdt, cdn) {
		recompute_pack_rate(frm, cdt, cdn);
	},
});
