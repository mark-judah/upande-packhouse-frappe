// Copyright (c) 2026, Upande and contributors
// For license information, please see license.txt

frappe.ui.form.on("Packing Teams", {
	setup(frm) {
		// Locations are the sales locations (a farm's Farm Location), not the farms.
		frm.set_query("location", () => ({
			query: "upande_packhouse.api.v2.allocation_teams.sales_location_query",
		}));
	},
});
