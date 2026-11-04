// Opens on tomorrow's deliveries; a Delivery Date the user picked is kept.
const opl_list = (frappe.listview_settings["Order Pick List"] = frappe.listview_settings["Order Pick List"] || {});
const opl_prev_onload = opl_list.onload;
opl_list.onload = function (listview) {
	if (opl_prev_onload) opl_prev_onload(listview);
	if ((listview.filters || []).some((f) => f[1] === "delivery_date")) return;
	listview.filter_area.add(
		"Order Pick List",
		"delivery_date",
		"=",
		frappe.datetime.add_days(frappe.datetime.get_today(), 1)
	);
};
