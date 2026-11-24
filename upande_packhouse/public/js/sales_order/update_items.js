// Update Items on a submitted Sales Order, with the order's own item fields and its Specifications.
//
// ERPNext's Update Items dialog knows the standard fields only (item, qty, rate, UOM, delivery date): a line added
// there has no Specification, stem length, boxes, pack rate or mix, and nothing to pick a spec with. For a Sales
// Order this dialog takes its place:
//   * a Specification column, limited to the order's customer's active specs; picking one on an empty row (or
//     "Add from Specifications") opens the spec picker, and the lines it builds land here with every field filled,
//   * every item field of the order (the main ones as columns, the rest in the row's form),
//   * Update saves through upande_packhouse.so_update_items.update_items: ERPNext's own update, then the other
//     fields and the order engine on the new and changed lines.
// Other doctypes (Quotation, Purchase Order ...) keep ERPNext's dialog.

(function () {
	if (!window.erpnext || !erpnext.utils || erpnext.utils.__upande_update_items) return;
	const original = erpnext.utils.update_child_items;
	erpnext.utils.update_child_items = function (opts) {
		if (opts && opts.frm && opts.frm.doc.doctype === "Sales Order")
			return upande_so_update_items(opts);
		return original.apply(this, arguments);
	};
	erpnext.utils.__upande_update_items = true;
})();

// the columns of the table, in order: [field, width in grid units (Frappe 16), width in px (later Frappe)]. More
// than a row holds: the table scrolls sideways (SOUI_CSS).
const SOUI_COLUMNS = [
	["custom_line", 2, 170],
	["item_code", 2, 140],
	["custom_length", 1, 90],
	["custom_box_type", 2, 130],
	["custom_number_of_boxes", 1, 80],
	["custom_packrate", 1, 90],
	["custom_packrate_mixed_box", 1, 100],
	["uom", 2, 110],
	["qty", 1, 80],
	["rate", 1, 90],
	["delivery_date", 2, 120],
	["warehouse", 2, 190],
	["custom_cut_stage", 1, 90],
	["custom_mix_name", 2, 150],
	["custom_mixed_box", 1, 70],
	["custom_mixed_bunch", 1, 80],
];
// standard fields kept in the row's form
const SOUI_STANDARD = ["conversion_factor", "description"];
// Frappe 16 lays a grid row out in 12 units and wraps the rest onto a second line: in this dialog the row stays one
// line and the table scrolls sideways (later Frappe sizes columns in px and scrolls already)
const SOUI_CSS = `
.soui-wide .form-grid{overflow-x:auto}
.soui-wide .form-grid .data-row.row{flex-wrap:nowrap;min-width:max-content}
.soui-wide .form-grid .grid-static-col[class*="col-xs-"]{flex:0 0 auto;max-width:none}
.soui-wide .form-grid .grid-static-col.col-xs-1{width:90px}
.soui-wide .form-grid .grid-static-col.col-xs-2{width:150px}
.soui-wide .form-grid .grid-static-col.col-xs-3{width:190px}
`;
// never edited here: the system and the order engine own them
const SOUI_SKIP = new Set([
	"custom_opl",
	"custom_fully_allocated",
	"custom_total_stems",
	"custom_pack_rate",
	// an old field labelled like the real one (warehouse, "Source Warehouse")
	"custom_source_warehouse",
]);
const SOUI_LAYOUT = new Set([
	"Section Break",
	"Column Break",
	"Tab Break",
	"HTML",
	"Button",
	"Fold",
	"Table",
	"Table MultiSelect",
]);

function upande_so_update_items(opts) {
	const frm = opts.frm;
	const meta = frappe.get_meta("Sales Order Item");
	const by_name = Object.fromEntries(meta.fields.map((f) => [f.fieldname, f]));
	const custom = meta.fields.filter(
		(f) =>
			f.is_custom_field &&
			!SOUI_LAYOUT.has(f.fieldtype) &&
			!f.read_only &&
			!f.hidden &&
			!SOUI_SKIP.has(f.fieldname),
	);
	const column_names = new Set(SOUI_COLUMNS.map((c) => c[0]));
	const field_names = [
		...SOUI_COLUMNS.map((c) => c[0]),
		...SOUI_STANDARD,
		...custom.map((f) => f.fieldname).filter((n) => !column_names.has(n)),
	].filter((n, i, all) => by_name[n] && all.indexOf(n) === i);

	let dialog;
	const rows = () => (dialog ? dialog.fields_dict.trans_items.df.data : []);
	const refresh = () => dialog && dialog.fields_dict.trans_items.grid.refresh();

	// the item's rate, UOM and name, as ERPNext's own dialog fetches them
	const fill_item = (row) =>
		frappe
			.call({
				method: "erpnext.stock.get_item_details.get_item_details",
				args: {
					doc: frm.doc,
					ctx: {
						item_code: row.item_code,
						set_warehouse: frm.doc.set_warehouse,
						customer: frm.doc.customer,
						currency: frm.doc.currency,
						conversion_rate: frm.doc.conversion_rate,
						price_list: frm.doc.selling_price_list,
						price_list_currency: frm.doc.price_list_currency,
						plc_conversion_rate: frm.doc.plc_conversion_rate,
						company: frm.doc.company,
						order_type: frm.doc.order_type,
						transaction_date: frm.doc.transaction_date,
						ignore_pricing_rule: frm.doc.ignore_pricing_rule,
						doctype: frm.doc.doctype,
						name: frm.doc.name,
						qty: row.qty || 1,
						uom: row.uom,
						child_doctype: "Sales Order Item",
					},
				},
			})
			.then((r) => {
				const m = r.message || {};
				row.item_name = m.item_name || row.item_name;
				row.uom = row.uom || m.uom;
				row.conversion_factor = row.conversion_factor || m.conversion_factor;
				if (!row.rate) row.rate = m.price_list_rate || m.rate || 0;
				if (!row.description) row.description = m.description;
				refresh();
			});

	// the spec picker hands its lines here (spec_autofill.js append_spec_batch)
	const sink = {
		rows,
		add(built) {
			const data = rows();
			// drop the row the Specification was picked on, and untouched blank rows
			for (let i = data.length - 1; i >= 0; i--) {
				if (!data[i].docname && !data[i].item_code) data.splice(i, 1);
			}
			built.forEach((r) => {
				const row = {
					name: frappe.utils.get_random(10),
					delivery_date: frm.doc.delivery_date,
				};
				field_names.forEach((f) => {
					if (r[f] !== undefined && r[f] !== null) row[f] = r[f];
				});
				row.item_name = r.item_name || row.item_code;
				// the order's own source warehouse, unless the spec gave one
				row.warehouse = row.warehouse || frm.doc.set_warehouse;
				data.push(row);
			});
			refresh();
			Promise.all(data.filter((r) => !r.docname && r.item_code && !r.rate).map(fill_item));
			frappe.show_alert(
				{ message: __("Added {0} line(s)", [built.length]), indicator: "green" },
				4,
			);
		},
	};
	const open_picker = (specs) => {
		if (!window.upande_open_spec_picker) {
			frappe.msgprint(__("The specification picker is not loaded."));
			return;
		}
		window.upande_open_spec_picker(frm, null, specs || []);
	};

	const field = (name, columns, width) => {
		const f = by_name[name];
		const df = {
			fieldtype: f.fieldtype,
			fieldname: name,
			label: __(f.label),
			options: f.options,
			precision: f.precision,
			in_list_view: columns ? 1 : 0,
			columns: columns || undefined,
			width: width || undefined,
			read_only: 0,
		};
		if (name === "item_code") {
			df.reqd = 1;
			df.get_query = () => ({
				query: "erpnext.controllers.queries.item_query",
				filters: { is_sales_item: 1 },
			});
			df.change = function () {
				const row = rows().find((r) => r.name === this.doc.name);
				if (row && row.item_code) fill_item(row);
			};
		}
		if (name === "custom_line") {
			// the order's customer's active specifications
			df.get_query = () => ({
				filters: Object.assign(
					{ status: "Active" },
					frm.doc.customer ? { customer: frm.doc.customer } : {},
				),
			});
			// picked on an empty row: the spec picker builds the lines
			df.change = function () {
				const row = rows().find((r) => r.name === this.doc.name);
				if (row && row.custom_line && !row.item_code && !row.docname)
					open_picker([row.custom_line]);
			};
		}
		if (name === "delivery_date") {
			df.reqd = 1;
			df.default = frm.doc.delivery_date;
		}
		return df;
	};

	const data = (frm.doc.items || []).map((d) => {
		const row = { docname: d.name, name: d.name, item_name: d.item_name };
		field_names.forEach((f) => (row[f] = d[f]));
		return row;
	});

	dialog = new frappe.ui.Dialog({
		title: __("Update Items"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "Button",
				fieldname: "add_from_specs",
				label: __("Add from Specifications"),
				click: () => open_picker([]),
			},
			{
				fieldname: "trans_items",
				fieldtype: "Table",
				label: __("Items"),
				cannot_add_rows: false,
				in_place_edit: false,
				reqd: 1,
				data,
				get_data: () => data,
				fields: [
					{ fieldtype: "Data", fieldname: "docname", read_only: 1, hidden: 1 },
					{
						fieldtype: "Data",
						fieldname: "item_name",
						label: __("Item Name"),
						read_only: 1,
					},
					...SOUI_COLUMNS.filter((c) => by_name[c[0]]).map((c) => field(c[0], c[1], c[2])),
					...field_names.filter((n) => !column_names.has(n)).map((n) => field(n)),
				],
			},
		],
		primary_action_label: __("Update"),
		primary_action() {
			const go = () => {
				const items = (this.get_values().trans_items || []).filter((r) => r.item_code);
				frappe
					.call({
						method: "upande_packhouse.so_update_items.update_items",
						args: { sales_order: frm.doc.name, trans_items: JSON.stringify(items) },
						freeze: true,
						freeze_message: __("Updating items…"),
					})
					.then(() => {
						this.hide();
						frm.reload_doc();
					});
			};
			if (opts.has_reserved_stock) {
				frappe.confirm(
					__(
						"The reserved stock will be released when you update items. Are you certain you wish to proceed?",
					),
					go,
				);
			} else {
				go();
			}
		},
	});

	frm.__spec_sink = sink;
	dialog.$wrapper.on("hidden.bs.modal", () => {
		if (frm.__spec_sink === sink) frm.__spec_sink = null;
	});
	if (!document.getElementById("soui-css")) {
		$("<style id='soui-css'>").text(SOUI_CSS).appendTo(document.head);
	}
	dialog.$wrapper.addClass("soui-wide");
	dialog.show();
	return dialog;
}
