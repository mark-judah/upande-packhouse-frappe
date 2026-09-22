// Business Unit / Farm are filled manually on the Sales Order, but two fields
// exist for each: the plain, visible custom_business_unit / custom_farm (what
// the sales rep actually types into) and the real accounting-dimension fields
// business_unit / farm (what pricing, box math, warehouse routing and every
// other script in this app reads). roses_invoice.sync_sales_order_accounting_dimensions
// already keeps these in sync SERVER-SIDE on save — this mirrors the same
// direction live on the CLIENT the moment the rep fills the visible field, so
// business-unit-gated automation (box_math.js, warehouse_routing.js) reacts
// immediately instead of only after a save round-trip.
frappe.ui.form.on("Sales Order", {
	onload(frm) {
		set_farm_queries(frm);
	},
	refresh(frm) {
		set_farm_queries(frm);
	},
	company(frm) {
		set_farm_queries(frm);
		clear_foreign_farm(frm);
	},
	custom_business_unit(frm) {
		if (
			frm.doc.custom_business_unit &&
			frm.doc.business_unit !== frm.doc.custom_business_unit
		) {
			frm.set_value("business_unit", frm.doc.custom_business_unit);
		}
	},
	custom_farm(frm) {
		if (frm.doc.custom_farm && frm.doc.farm !== frm.doc.custom_farm) {
			frm.set_value("farm", frm.doc.custom_farm);
		}
	},
});

// A Farm belongs to exactly one Company (Farm.company) and an order is raised
// against exactly one company, so the rest of the group's farms have no
// business in the list. Unfiltered, a Karen Roses order offers Kaitet's and
// Westwood's farms too, and picking one puts a farm on the order whose
// warehouses, cost centres and Roses-MAP rows all belong to a different
// company -- which then surfaces much later, as a routing or dimension error
// nobody can trace back to the farm cell.
//
// Both farm fields are filtered, and the items table's own: they are the same
// fact in three places (see the header comment above).
function set_farm_queries(frm) {
	const by_company = () => ({
		filters: frm.doc.company ? { company: frm.doc.company } : {},
	});
	frm.set_query("farm", by_company);
	frm.set_query("custom_farm", by_company);
	frm.set_query("farm", "items", by_company);
}

// Changing the company invalidates a farm picked under the previous one.
// Clearing it is the honest outcome: leaving it would keep an out-of-company
// farm on the order that the link query can no longer even offer, so nobody
// would spot it until something downstream failed on it.
async function clear_foreign_farm(frm) {
	if (!frm.doc.company) return;
	for (const fieldname of ["farm", "custom_farm"]) {
		const farm = frm.doc[fieldname];
		if (!farm) continue;
		const r = await frappe.db.get_value("Farm", farm, "company");
		const company = r && r.message && r.message.company;
		if (company && company !== frm.doc.company) {
			frm.set_value(fieldname, null);
		}
	}
}
