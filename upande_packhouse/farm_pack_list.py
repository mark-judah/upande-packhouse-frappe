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
			short.append("Box {0} {1}: {2:g}/{3:g} stems".format(row.box_number, row.variety, have, need))
	return short


def farm_pack_list_on_submit(doc, method=None):
	"""Runs on EVERY Farm Pack List submit -- via sync_and_maybe_submit_fpl's
	own auto-submit-when-complete path below, but just as much via a plain
	Desk "Submit" click, or anything else that flips this doc to docstatus 1.
	Deliberately has NO packing-completeness check of its own: whether an
	FPL was allowed to auto-submit is sync_and_maybe_submit_fpl's decision
	(via fpl_pack_blockers) to make BEFORE calling .submit() -- once a
	document is actually submitted, by any path, it should always get its
	Box Label(s) generated from whatever was actually packed, complete or
	not (see box_label.py's own docstring: a label always reflects reality,
	not the plan).

	No stock move happens here. The sale already landed every stem in the
	farm's Graded Sold warehouse at ALLOCATION time
	(stock_movement.move_allocation_to_sold); issuing already moved it on to
	the Packhouse (stock_movement.post_issue_to_packhouse). An FPL submit is
	a paperwork event -- box labels exist now -- not a stock event. The
	physical Packhouse -> Dispatch Cold Store hop happens later, when a box
	label is actually scanned staged (stock_movement.post_stage_to_dispatch).
	A previous version of this function re-moved stock from a since-removed
	"Ungraded Sold" warehouse into Graded Sold here, redundantly and with the
	wrong source (nothing ever deposited stock there) -- confirmed live: it
	silently posted only its outgoing leg, leaving a permanent hole in that
	warehouse's ledger and no matching credit anywhere.

	Before this hook existed, Box Label generation was called explicitly,
	only from inside sync_and_maybe_submit_fpl, right after its own
	fpl.submit() call -- so any FPL submitted any other way (e.g. directly
	from the Desk form) got no Box Label, silently, with no error to notice
	(confirmed in production: FPL-2026-00001, submitted via Desk while only
	60 of its 200 required stems were packed -- no Box Label, fixed by hand
	once; this hook closes the gap for good).

	Stashes its result on doc.flags so sync_and_maybe_submit_fpl (which
	calls .submit() on this SAME doc instance) can still return it to its
	own callers without this hook and that function running the step twice
	between them.
	"""
	opl = frappe.get_doc("Order Pick List", doc.order_pick_list) if doc.order_pick_list else None
	so = frappe.get_doc("Sales Order", doc.sales_order) if doc.sales_order else None

	if opl and so:
		doc.flags.box_labels_result = sync_box_labels_for_fpl(doc, opl, so)
	else:
		doc.flags.box_labels_result = None
		frappe.log_error(
			title="FPL submit: missing order_pick_list/sales_order",
			message="FPL={0} order_pick_list={1} sales_order={2} -- can't generate Box Labels without both.".format(
				doc.name, doc.order_pick_list, doc.sales_order
			),
		)


def sync_and_maybe_submit_fpl(fpl_name):
	"""Called after every pack scan lands on a Farm Pack List: refreshes its
	header fields, then submits it once its Order Pick List's whole Packing
	Guide is satisfied -- submitting triggers farm_pack_list_on_submit
	(doc_events, hooks.py), which generates the Box Label(s). Idempotent
	throughout.

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
	fpl.submit()  # farm_pack_list_on_submit runs here -- see hooks.py doc_events

	return True, fpl.flags.get("box_labels_result")
