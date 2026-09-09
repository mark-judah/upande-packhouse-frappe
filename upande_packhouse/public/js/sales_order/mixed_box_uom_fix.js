// Mixed Box Wizard UOM Fix -- surgical patch, not part of mixed_box_wizard.js.
// The wizard copies Item.sales_uom onto rows it creates; when an Item's Sales
// UOM master is set to "Nos" instead of "Stems", the row inherits "Nos" and
// server-side box math (which expects a Stems-based UOM) is thrown off.
// Rows the wizard creates are reliably flagged with custom_mixed_box = 1, so
// this only ever touches wizard-created rows -- normal manually-entered lines
// (custom_mixed_box unset) are never touched.
//
// Shipped live first as a DB-only "Mixed Box Wizard UOM Fix" Client Script
// (2026-09-04, same pattern the rest of this file's siblings started as per
// the note in hooks.py) -- mirrored here so it ships with the app on the
// next real deploy instead of living only on production's database. If you
// deploy this file, disable/delete the live Client Script of the same name
// to avoid running the fix twice.
frappe.ui.form.on('Sales Order', {
	validate(frm) {
		(frm.doc.items || []).forEach(row => {
			if (row.custom_mixed_box && row.uom !== 'Stems') {
				frappe.model.set_value(row.doctype, row.name, 'uom', 'Stems');
			}
		});
	}
});
