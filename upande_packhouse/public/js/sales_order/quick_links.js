// Sales Order — Actions shortcuts to the two pages that pick up from here:
// the Order Pick List(s) generated for this order, and the Sales Allocation
// dashboard, pre-filtered to this order so the allocator doesn't have to hunt
// for it in the list. The allocation button's own label reflects how far the
// order is already allocated, so it reads as a next-step verb rather than a
// static page name.

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		frm.add_custom_button(
			__("Order Pick List"),
			() => {
				frappe.set_route("list", "Order Pick List", { sales_order: frm.doc.name });
			},
			__("Actions")
		);

		// every step of the order's allocation, from planning to its pick lists
		if (!frm.is_new()) {
			frm.add_custom_button(__("Allocation Log"), () => show_allocation_log(frm), __("Actions"));
		}

		// Allocation only makes sense once the order is submitted — that's
		// also what the allocation page's own order list requires.
		if (frm.doc.docstatus === 1) {
			frappe.call({
				method: "upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2.get_order_allocation_status",
				args: { sales_order: frm.doc.name },
				callback: (r) => {
					const status = (r.message && r.message.status) || "none";
					const label =
						status === "full"
							? __("View Allocation")
							: status === "partial"
							? __("Continue Allocating")
							: __("Allocate");
					// The label depends on server-side state that can change
					// between refreshes (someone allocates, or re-runs
					// allocation) without a fresh add_custom_button knowing to
					// replace an earlier, differently-labeled one -- so drop
					// every label this button could have had before adding
					// the current one. Scoped to just this button (not
					// clear_custom_buttons, which would also wipe the Mixed
					// Box wizard's own Actions buttons from another script).
					[__("Allocate"), __("Continue Allocating"), __("View Allocation")].forEach(
						(l) => {
							frm.remove_custom_button(l, __("Actions"));
						}
					);
					frm.add_custom_button(
						label,
						() => {
							const q = new URLSearchParams({
								sales_order: frm.doc.name,
								farm: frm.doc.farm || frm.doc.custom_farm || "",
								delivery_date: frm.doc.delivery_date || "",
							});
							window.location.href =
								"/packhouse-dashboard-v2/sales-allocation-v2?" + q.toString();
						},
						__("Actions")
					);
				},
			});
		}
	},
});

// the pick list a step names opens from the log: its name in the text is the link
function opl_links(text, opl) {
	if (!opl) return text;
	const link = `<a href="/app/order-pick-list/${encodeURIComponent(opl)}">${frappe.utils.escape_html(opl)}</a>`;
	return text.includes(opl) ? text.split(opl).join(link) : `${text} ${link}`;
}

function show_allocation_log(frm) {
	frappe.call({
		method: "upande_packhouse.allocation_log.get_log",
		args: { sales_order: frm.doc.name },
		callback: (r) => {
			const rows = r.message || [];
			const esc = frappe.utils.escape_html;
			const n = (v) => (v === null || v === undefined || v === "" ? "" : format_number(v, null, 0));
			const body = rows.length
				? `<div style="max-height:65vh;overflow:auto"><table class="table table-sm table-bordered" style="font-size:12px">
					<thead><tr><th>${__("When")}</th><th>${__("Stage")}</th><th>${__("Variety")}</th><th>${__("Team")}</th>
					<th class="text-right">${__("Before")}</th><th class="text-right">${__("After")}</th><th>${__("What happened")}</th><th>${__("By")}</th></tr></thead>
					<tbody>${rows
						.map(
							(x) => `<tr><td style="white-space:nowrap">${frappe.datetime.str_to_user(x.creation)}</td>
							<td><b>${esc(x.stage || "")}</b></td>
							<td>${esc(x.item_code || "")} ${esc(x.stem_length || "")}</td>
							<td>${esc(x.team || "")}</td>
							<td class="text-right">${n(x.stems_before)}</td><td class="text-right">${n(x.stems_after)}</td>
							<td>${opl_links(esc(x.message || ""), x.order_pick_list)}</td>
							<td>${esc(x.user || "")}</td></tr>`
						)
						.join("")}</tbody></table></div>`
				: `<p class="text-muted">${__("Nothing has been logged for this order yet.")}</p>`;
			const d = new frappe.ui.Dialog({ title: __("Allocation Log: {0}", [frm.doc.name]), size: "extra-large" });
			d.$body.html(body);
			d.show();
		},
	});
}
