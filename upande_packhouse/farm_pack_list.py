"""Farm Pack List: header-field carry-forward + the completion check that
decides when it should submit itself.

Several of Farm Pack List's own fields (s_number, consignee, delivery_point,
shipping_agent, currency, total_stems, picked_total_stems,
sale_order_number_of_boxes) existed but were never populated by anything --
confirmed by grepping the whole codebase for each fieldname. The data they
were clearly meant to carry (createOrUpdateDispatch already reads the
equivalent Sales Order fields directly when it later builds the Delivery
Note, so the INTENT of these FPL fields was to mirror that data onto the
pack list itself, e.g. for print/audit) is populated here instead of being
left permanently blank.

A Farm Pack List is "complete" once every (box_number, variety) its Order
Pick List's own Packing Guide (table_nade -- see packing_guide.py) calls for
has been matched by real packed stems here. At that point it should submit
itself, and the boxes it describes are ready to move to staging -- so this
is also where their Box Labels get generated (nothing else in the codebase
ever creates one; see box_label.py's own docstring).

There used to be no completion check at all: createOrUpdateFarmPackList
just kept appending pack_list_item rows to a permanently-draft Farm Pack
List forever, with no signal anywhere that packing for an order was done.
"""

import frappe

from upande_packhouse import roses_warehouse_map
from upande_packhouse.box_label import sync_box_labels_for_fpl

# Fields NOT populated here, deliberately, rather than guessed:
#   customer_address       -- Data field; Sales Order's own customer_address
#                              is a Link to Address, so this would need
#                              resolving to display text, and nothing else
#                              in the app has ever needed it. Flag, don't fill.
#   customers_purchase_order -- ambiguous: Sales Order's own po_no vs.
#                              Box Label's customer_purchase_order actually
#                              stores the SALES ORDER name (see box_label.py).
#                              Guessing which convention this field meant
#                              would just add a third, inconsistent one.
#   sale_order_packrate    -- a single scalar can't represent a mixed-box/
#                              mixed-bunch OPL's several different packrates;
#                              this field predates that reality.
#   remote_truck_details    -- reads as transfer-truck detail (remote farm ->
#                              sales farm), a separate concern from Box
#                              Label's own local-delivery truck_details.


def sync_header_fields(fpl_doc, opl_doc, so_doc):
	"""Keep the FPL's own header fields in step with its Sales Order / Order
	Pick List. Safe to call on every pack scan; only writes what changed."""
	changed = False

	def _set(field, value):
		nonlocal changed
		if value not in (None, "") and fpl_doc.get(field) != value:
			fpl_doc.set(field, value)
			changed = True

	_set("s_number", so_doc.get("custom_s_number"))
	_set("consignee", so_doc.get("custom_consignee"))
	_set("delivery_point", so_doc.get("custom_delivery_point"))
	_set("shipping_agent", so_doc.get("custom_shipping_agent"))
	_set("currency", so_doc.get("currency"))
	_set("total_stems", opl_doc.get("custom_total_stems"))

	guide_rows = opl_doc.get("table_nade") or []
	if guide_rows:
		_set("sale_order_number_of_boxes", len({r.box_number for r in guide_rows}))

	picked_total = sum(int(r.stock_qty or 0) for r in fpl_doc.pack_list_item)
	_set("picked_total_stems", str(picked_total))

	if changed:
		fpl_doc.flags.ignore_permissions = True
		fpl_doc.save(ignore_permissions=True)

	return changed


def _packed_stems_by_box_variety(fpl_doc):
	packed = {}
	for row in fpl_doc.pack_list_item:
		box_no = int(row.box_id or 0) or 1
		key = (box_no, row.item_code)
		packed[key] = packed.get(key, 0) + (row.stock_qty or 0)
	return packed


def fpl_pack_blockers(fpl_doc, opl_doc):
	"""Human-readable list of what's still short; empty = fully packed."""
	guide_rows = opl_doc.get("table_nade") or []
	if not guide_rows:
		return ["Order Pick List has no Packing Guide rows"]

	packed = _packed_stems_by_box_variety(fpl_doc)
	short = []
	for row in guide_rows:
		key = (int(row.box_number or 0), row.variety)
		have = packed.get(key, 0)
		need = row.stems or 0
		if have < need - 0.001:
			short.append(
				"Box {0} {1}: {2:g}/{3:g} stems".format(row.box_number, row.variety, have, need)
			)
	return short


def _move_to_graded_sold(fpl_doc):
	"""The second (and final) real stock move in the chain: once a Farm Pack
	List is fully packed and submits, its stems move from the farm's
	Ungraded Sold warehouse into its Graded Sold warehouse -- resolved via
	Roses-MAP, keyed by the OPL's own `farm` field (not a specific bucket's
	source_warehouse: by packing time every bucket for this OPL has already
	been issued -- individually, per bucket, in issueBucketToSaleOrderItem
	-- so what's left to move is one farm-wide aggregate per variety, not
	anything bucket-specific). Graded Sold is what the Delivery Note later
	deducts from, so this is what actually makes a Delivery Note
	submittable at all.

	One Stock Entry, one row per distinct variety packed on this FPL. Real
	quantities (aggregated pack_list_item.stock_qty), not planned ones.
	"""
	map_row = roses_warehouse_map.mapping_row_for_farm(fpl_doc.farm)
	ungraded = map_row.ungraded_sold_warehouse if map_row else None
	graded = map_row.delivery_warehouse if map_row else None
	if not (ungraded and graded):
		frappe.log_error(
			title="FPL submit: no Roses-MAP row for Ungraded/Graded Sold",
			message=f"FPL={fpl_doc.name} farm={fpl_doc.farm} -- "
					f"add/complete a Roses-MAP row for this farm's coldstore.",
		)
		return None

	by_variety = {}
	for row in fpl_doc.pack_list_item:
		by_variety[row.item_code] = by_variety.get(row.item_code, 0) + (row.stock_qty or 0)
	by_variety = {k: v for k, v in by_variety.items() if k and v}
	if not by_variety:
		return None

	transfer = frappe.new_doc("Stock Entry")
	transfer.stock_entry_type = "Move To Graded Sold"
	transfer.company = frappe.db.get_value("Warehouse", ungraded, "company")
	transfer.business_unit = "Roses"
	transfer.farm = fpl_doc.farm
	transfer.remarks = f"Farm Pack List {fpl_doc.name} fully packed -- {fpl_doc.order_pick_list}"
	for item_code, qty in by_variety.items():
		transfer.append("items", {
			"item_code": item_code,
			"qty": qty,
			"uom": "Stems",
			"conversion_factor": 1,
			"s_warehouse": ungraded,
			"t_warehouse": graded,
			"allow_zero_valuation_rate": 1,
			"basic_rate": 0,
		})
	transfer.insert(ignore_permissions=True)
	transfer.submit()
	return transfer.name


def sync_and_maybe_submit_fpl(fpl_name):
	"""Called after every pack scan lands on a Farm Pack List: refreshes its
	header fields, then submits it (and generates Box Labels) once its Order
	Pick List's whole Packing Guide is satisfied. Idempotent throughout.

	Returns (submitted: bool, box_labels: dict | None).
	"""
	fpl = frappe.get_doc("Farm Pack List", fpl_name)
	if fpl.docstatus == 1:
		return True, None
	if fpl.docstatus == 2 or not fpl.order_pick_list or not fpl.sales_order:
		return False, None

	opl = frappe.get_doc("Order Pick List", fpl.order_pick_list)
	so = frappe.get_doc("Sales Order", fpl.sales_order)

	sync_header_fields(fpl, opl, so)

	if fpl_pack_blockers(fpl, opl):
		return False, None

	fpl.flags.ignore_permissions = True
	fpl.submit()

	_move_to_graded_sold(fpl)

	labels = sync_box_labels_for_fpl(fpl, opl, so)
	return True, labels
