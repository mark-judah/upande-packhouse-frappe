# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Quality issues found while PACKING — the packhouse app's Packing screen.
#
# A packer opens a line, finds bad stems in a bucket that was issued to it (the
# wrong stem length, a disease such as Botrytis, a pest such as Thrips) and
# replaces them: the whole bucket, or only some stems.
#
#   1. The bad stems leave the line: a "Packhouse Rejects" Stock Entry moves them
#      from the Packhouse store to the Rejects warehouse, and the old bucket's
#      allocation and pick rows shrink by that many stems.
#   2. A replacement is allocated to the same line through the allocation page's
#      own core (sales_allocation._allocate_stock_with_buckets_impl), so the
#      Bucket Allocation Status, the pick row and the Sold-leg stock follow the
#      usual rules:
#        - from the SALES farm (recommended, offered first): it is issued to the
#          packhouse straight away, through the issuing scan's own endpoint, and
#          can be packed at once;
#        - from a REMOTE farm (only when the sales farm has nothing): the row
#          waits for a truck like any remote allocation, flagged ASAP — the
#          automatic transfer planner puts it on the next trip from that farm
#          before any other order (auto_transfer._place_first).
#   3. A Bucket Replacement (reason "Quality issue") records the defect, what was
#      replaced, where the replacement came from and the stock entries.
#
# Candidates follow the replacement rules everywhere else (remote transfers,
# issuing): same variety, the line's stem length or longer, enough stems,
# unallocated, not too old — sales_allocation._replacement_candidates.

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

from upande_packhouse import stock_movement
from upande_packhouse.upande_packhouse.page.sales_allocation import sales_allocation as sa

REJECTS_ENTRY_TYPE = "Packhouse Rejects"
SCOPES = {"bucket": "Whole bucket", "stems": "Some stems"}
ASAP = "ASAP"

# QC Parameter -> category the packer picks from. Matched on words in its name;
# anything unmatched is "Other".
CATEGORY_WORDS = (
	("Wrong stem length", ("length", "short stem", "long stem")),
	(
		"Pest",
		(
			"thrip",
			"aphid",
			"mite",
			"white fl",
			"whitefl",
			"fcm",
			"helicoverpa",
			"spodoptera",
			"caterpillar",
			"worm",
			"mealy",
			"leaf miner",
			"leafminer",
			"pest",
			"insect",
			"spider",
			"beetle",
			"scale",
		),
	),
	(
		"Disease",
		(
			"botrytis",
			"mildew",
			"rust",
			"downy",
			"powdery",
			"fung",
			"disease",
			"rot",
			"mould",
			"mold",
			"canker",
			"black spot",
			"blackspot",
			"bacteri",
			"virus",
			"blight",
		),
	),
)
CATEGORIES = ("Wrong stem length", "Disease", "Pest", "Other")


def _category(defect):
	text = (defect or "").lower()
	for category, words in CATEGORY_WORDS:
		if any(w in text for w in words):
			return category
	return "Other"


def _defects():
	"""The QC Parameters (the quality app's defect master), each with its
	category. "Wrong Length" is always offered, master or not."""
	names = []
	if frappe.db.exists("DocType", "QC Parameters"):
		names = frappe.get_all("QC Parameters", pluck="name", order_by="name asc")
	if not any(_category(n) == "Wrong stem length" for n in names):
		names.insert(0, "Wrong Length")
	out = [{"defect": n, "category": _category(n)} for n in names]
	out.sort(key=lambda d: (CATEGORIES.index(d["category"]), d["defect"]))
	return out


def _fail(message, **extra):
	return {"success": False, "message": str(message), **extra}


def _opl(opl_name):
	opl = frappe.db.get_value(
		"Order Pick List",
		opl_name,
		["name", "docstatus", "farm", "sales_order", "order_name", "team"],
		as_dict=True,
	)
	if not opl or opl.docstatus == 2:
		frappe.throw(_("Order Pick List {0} not found.").format(opl_name))
	if not frappe.has_permission("Order Pick List", "read", opl_name):
		frappe.throw(_("You are not permitted to read {0}.").format(opl_name), frappe.PermissionError)
	return opl


def _sales_farm(opl):
	from upande_packhouse.api.transfer_control import transfer_hub

	return opl.farm or transfer_hub(required=False)


def _line_rows(opl_name, bucket, sale_order_item):
	"""The issued pick rows of `bucket` on this line (sales order item) of `opl_name`."""
	rows = frappe.get_all(
		"Pick List Item",
		filters={"parent": opl_name, "parenttype": "Order Pick List", "bucket": bucket, "issued": 1},
		fields=[
			"name",
			"idx",
			"item_code",
			"stem_length",
			"uom",
			"conversion_factor",
			"qty",
			"stock_qty",
			"shelf",
			"farm",
			"source_warehouse",
			"warehouse",
			"sales_order_item",
			"custom_sale_order_item",
			"custom_box_id",
			"custom_box_label",
		],
		order_by="idx desc",
	)
	rows = [r for r in rows if sale_order_item in (r.sales_order_item, r.custom_sale_order_item)]
	if not rows:
		frappe.throw(_("Bucket {0} is not issued to this line of {1}.").format(bucket, opl_name))
	if len({(r.item_code, r.stem_length or "") for r in rows}) > 1:
		frappe.throw(_("Bucket {0} carries more than one variety or length on this line.").format(bucket))
	return rows


def _anchor(opl_name, rows, bucket):
	r = rows[-1]  # rows are idx desc: the first row of the bucket
	return frappe._dict(
		parent=opl_name,
		bucket=bucket,
		item_code=r.item_code,
		stem_length=r.stem_length,
		shelf=r.shelf,
		farm=r.farm,
	)


def _describe(c, farm, recommended=False):
	return {
		"new_bucket": c.bucket_id,
		"farm": farm,
		"shelf": c.shelf,
		"variety": c.variety,
		"stem_length": c.stem_length,
		"available_qty": flt(c.available_qty),
		"harvest_date": str(c.harvest_date)[:10] if c.harvest_date else None,
		"recommended": recommended,
	}


def _with_stock(candidates, item_code, needed):
	"""Only buckets the stock ledger can back: a shelf record can say more stems
	than the ledger holds, and allocating such a bucket fails on its stock move.
	Stems already sold, in a packhouse or in rejects are not available."""
	if not candidates:
		return []
	held, _sold = sa._bucket_ledger([c.bucket_id for c in candidates], item_code)
	gone = ("sold", "packhouse", "reject", "dispatch", "truck")
	on_hand = {}
	free = {}
	for (bucket, warehouse), n in held.items():
		if n <= 0 or any(g in (warehouse or "").lower() for g in gone):
			continue
		# The warehouse itself must hold them too: the move is checked against it.
		if warehouse not in on_hand:
			on_hand[warehouse] = flt(stock_movement.on_hand(item_code, warehouse))
		free[bucket] = max(free.get(bucket, 0), min(n, on_hand[warehouse]))
	return [c for c in candidates if free.get(c.bucket_id, 0) + stock_movement.QTY_TOLERANCE >= needed]


def _delivers_today(sales_order):
	dd = frappe.db.get_value("Sales Order", sales_order, "delivery_date") if sales_order else None
	return bool(dd) and getdate(dd) <= getdate(today())


# ── Endpoints ─────────────────────────────────────────────────────────────────


@frappe.whitelist()
def quality_issue_lines(opl_name: str):
	"""The buckets issued to each line of `opl_name` — what a packer can report a
	quality issue on — and the defects to choose from."""
	opl = _opl(opl_name)
	rows = frappe.get_all(
		"Pick List Item",
		filters={"parent": opl_name, "parenttype": "Order Pick List", "issued": 1, "bucket": ["is", "set"]},
		fields=[
			"bucket",
			"item_code",
			"stem_length",
			"uom",
			"stock_qty",
			"farm",
			"sales_order_item",
			"custom_sale_order_item",
		],
		order_by="idx asc",
	)
	lines = {}
	for r in rows:
		so_item = r.custom_sale_order_item or r.sales_order_item
		line = lines.setdefault(
			so_item,
			{
				"sales_order_item": so_item,
				"item_code": r.item_code,
				"stem_length": r.stem_length,
				"uom": r.uom,
				"buckets": {},
			},
		)
		b = line["buckets"].setdefault(r.bucket, {"bucket": r.bucket, "stems": 0.0, "farm": r.farm})
		b["stems"] += flt(r.stock_qty)
	return {
		"opl_name": opl_name,
		"order_name": opl.order_name,
		"sales_farm": _sales_farm(opl),
		"defects": _defects(),
		"categories": list(CATEGORIES),
		"lines": [{**line, "buckets": list(line["buckets"].values())} for line in lines.values()],
	}


@frappe.whitelist()
def quality_replacement_options(
	opl_name: str, bucket: str, sale_order_item: str, stems: float | None = None, limit: int = 20
):
	"""Buckets that can replace `stems` bad stems of `bucket` on this line: the
	sales farm's first (the best one recommended), then the remote farms that truck
	to it — a remote one comes on the next truck, ASAP."""
	try:
		opl = _opl(opl_name)
		rows = _line_rows(opl_name, bucket, sale_order_item)
	except frappe.ValidationError as e:
		return _fail(e)
	on_line = sum(flt(r.stock_qty) for r in rows)
	needed = flt(stems) or on_line
	if needed <= 0 or needed > on_line + stock_movement.QTY_TOLERANCE:
		return _fail(_("Choose between 1 and {0} stems.").format(int(on_line)))
	anchor = _anchor(opl_name, rows, bucket)
	limit = max(1, min(cint(limit) or 20, 100))
	sales_farm = _sales_farm(opl)

	local = (
		_with_stock(
			sa._replacement_candidates(anchor, sales_farm, needed, limit=limit), anchor.item_code, needed
		)
		if sales_farm
		else []
	)
	remote = []
	for farm in sa._remote_farms_of(sales_farm) if sales_farm else []:
		found = sa._replacement_candidates(anchor, farm, needed, limit=limit)
		for c in _with_stock(found, anchor.item_code, needed):
			remote.append(_describe(c, farm))
	return {
		"success": True,
		"bucket": bucket,
		"variety": anchor.item_code,
		"stem_length": anchor.stem_length,
		"on_line": on_line,
		"needed": needed,
		"sales_farm": sales_farm,
		"sales_farm_candidates": [
			_describe(c, sales_farm, recommended=(i == 0)) for i, c in enumerate(local)
		],
		"remote_candidates": remote[:limit],
		"warning": _(
			"This order is delivered today. A bucket from a remote farm comes on the next truck and may not arrive in time."
		)
		if not local and remote and _delivers_today(opl.sales_order)
		else None,
		"message": None if local or remote else sa._no_replacement_message(anchor, sales_farm or "", needed),
	}


@frappe.whitelist(methods=["POST"])
def report_packing_quality_issue(
	opl_name: str,
	bucket: str,
	sale_order_item: str,
	defect: str,
	scope: str = "bucket",
	stems: float | None = None,
	new_bucket_id: str | None = None,
	notes: str | None = None,
):
	"""Reject the bad stems of `bucket` on this line and replace them with
	`new_bucket_id` (one of quality_replacement_options), see the module header.
	Without `new_bucket_id` the stems are only rejected (nothing to replace with)."""
	if scope not in SCOPES:
		return _fail(_("Choose whether the whole bucket or some stems are replaced."))
	if not (defect or "").strip():
		return _fail(_("Choose the quality issue found."))
	defect = defect.strip()
	try:
		opl = _opl(opl_name)
		rows = _line_rows(opl_name, bucket, sale_order_item)
	except frappe.ValidationError as e:
		return _fail(e)

	on_line = sum(flt(r.stock_qty) for r in rows)
	bad = on_line if scope == "bucket" else flt(stems)
	if bad <= 0 or bad > on_line + stock_movement.QTY_TOLERANCE:
		return _fail(_("Choose between 1 and {0} stems.").format(int(on_line)))
	anchor = _anchor(opl_name, rows, bucket)
	sales_farm = _sales_farm(opl)

	# Where the replacement sits is the server's to decide, never the app's.
	new = new_farm = None
	if new_bucket_id:
		for farm in [sales_farm, *sa._remote_farms_of(sales_farm)]:
			found = _with_stock(
				[
					c
					for c in sa._replacement_candidates(anchor, farm, bad, limit=500)
					if c.bucket_id == new_bucket_id
				],
				anchor.item_code,
				bad,
			)
			if found:
				new, new_farm = found[0], farm
				break
		if not new:
			return _fail(
				_(
					"Bucket {0} can no longer replace these stems (allocated, moved or too short). Pick another."
				).format(new_bucket_id)
			)
	remote = bool(new) and new_farm != sales_farm

	try:
		reject_entry = _post_rejects(opl, rows, anchor, bad, defect)
		_shrink_old_allocation(opl, rows, anchor, bad, sale_order_item)
		new_rows, stock_moves = [], []
		if new:
			new_rows, stock_moves = _allocate_replacement(
				opl, rows, anchor, new, new_farm, bad, sale_order_item, remote
			)
		replacement = _record(
			opl, rows, anchor, new, new_farm, bad, scope, defect, notes, remote, reject_entry, stock_moves
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one transaction for the whole report
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title="Packing quality issue failed", message=frappe.get_traceback())
		return _fail(_("Could not report the quality issue: {0}").format(e))

	result = {
		"success": True,
		"replacement": replacement,
		"reject_entry": reject_entry,
		"rejected_stems": bad,
		"new_bucket": new.bucket_id if new else None,
		"source": ("remote" if remote else "sales_farm") if new else None,
		"priority": ASAP if remote else None,
	}
	if not new:
		result["message"] = _("{0} stems of {1} rejected ({2}). No replacement was chosen.").format(
			int(bad), bucket, defect
		)
		return result

	if remote:
		from upande_packhouse.api import auto_transfer

		auto_transfer.replan_soon()
		result["message"] = _(
			"{0} stems of {1} rejected ({2}). {3} requested from {4} as ASAP — it goes on the next truck to {5}."
		).format(int(bad), bucket, defect, new.bucket_id, new_farm, sales_farm)
		return result

	# From the sales farm: issue it to the packhouse now, exactly as the issuing scan does.
	from upande_packhouse.api import offline_issue

	issued = offline_issue._issue(new.bucket_id, opl_name)
	result["issued"] = bool(issued) and all(r["ok"] for r in issued)
	if result["issued"]:
		result["message"] = _(
			"{0} stems of {1} rejected ({2}). {3} from shelf {4} is issued to this line — pack it now."
		).format(int(bad), bucket, defect, new.bucket_id, new.shelf)
	else:
		result["message"] = _(
			"{0} stems of {1} rejected ({2}). {3} is allocated from shelf {4}; scan it at Issuing to bring it to packing."
		).format(int(bad), bucket, defect, new.bucket_id, new.shelf)
	return result


# ── Steps (one transaction, committed by report_packing_quality_issue) ──────


def _packhouse_holding(opl, rows, anchor, qty):
	"""The packhouse store holding the bucket's issued stems: the farm's mapped
	Packhouse (where the issuing scan sent them), else any packhouse the ledger has
	at least `qty` of them in."""
	business_unit = stock_movement.opl_business_unit(opl) or "Roses"
	held, _sold = sa._bucket_ledger([anchor.bucket], anchor.item_code)
	farms = [r.farm for r in rows if r.farm] + [opl.farm]
	for farm in farms:
		row = stock_movement.mapping_row_for_farm(farm, business_unit)
		if (
			row
			and row.packhouse
			and held.get((anchor.bucket, row.packhouse), 0) + stock_movement.QTY_TOLERANCE >= qty
		):
			return row.packhouse, business_unit
	for (bucket, warehouse), n in sorted(held.items(), key=lambda kv: -kv[1]):
		if (
			bucket == anchor.bucket
			and "packhouse" in (warehouse or "").lower()
			and n + stock_movement.QTY_TOLERANCE >= qty
		):
			return warehouse, business_unit
	in_packhouse = max(
		[n for (b, w), n in held.items() if b == anchor.bucket and "packhouse" in (w or "").lower()] or [0]
	)
	frappe.throw(
		_("Only {0} stems of bucket {1} are in the packhouse store; {2} cannot be rejected.").format(
			int(in_packhouse), anchor.bucket, int(qty)
		)
	)


def _rejects_warehouse(company):
	abbr = frappe.db.get_value("Company", company, "abbr")
	name = f"Rejects - {abbr}" if abbr else None
	if name and frappe.db.exists("Warehouse", name):
		return name
	found = frappe.get_all(
		"Warehouse",
		filters={"company": company, "is_group": 0, "name": ["like", "%Reject%"]},
		pluck="name",
		limit=1,
	)
	if not found:
		frappe.throw(_("No Rejects warehouse is set up for {0}.").format(company))
	return found[0]


def _post_rejects(opl, rows, anchor, qty, defect):
	"""Packhouse store -> Rejects: the bad stems leave the line's stock."""
	source, business_unit = _packhouse_holding(opl, rows, anchor, qty)
	company = frappe.db.get_value("Warehouse", source, "company")
	target = _rejects_warehouse(company)
	cost_center = frappe.db.get_value(
		"Warehouse", source, "custom_cost_center"
	) or stock_movement.default_cost_center(company)
	so_item = rows[0].custom_sale_order_item or rows[0].sales_order_item
	farm = opl.farm if opl.farm and frappe.db.exists("Farm", opl.farm) else None
	remarks = _("Quality issue during packing ({0}): {1} stems of bucket {2} on {3}").format(
		defect, int(qty), anchor.bucket, opl.name
	)
	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": REJECTS_ENTRY_TYPE,
			"purpose": frappe.db.get_value("Stock Entry Type", REJECTS_ENTRY_TYPE, "purpose")
			or "Material Transfer",
			"company": company,
			"posting_date": frappe.utils.nowdate(),
			"posting_time": frappe.utils.nowtime(),
			"set_posting_time": 1,
			"from_warehouse": source,
			"to_warehouse": target,
			"farm": farm,
			"business_unit": business_unit,
			"custom_stem_length": anchor.stem_length,
			"custom_issued_to": so_item,
			"custom_opl_scanned": opl.name,
			"remarks": remarks,
			"cost_center": cost_center,
		}
	)
	se.append(
		"items",
		{
			"item_code": anchor.item_code,
			"qty": qty,
			"s_warehouse": source,
			"t_warehouse": target,
			"farm": farm,
			"business_unit": business_unit,
			"custom_stem_length": anchor.stem_length,
			"custom_bucket_id": anchor.bucket,
			"cost_center": cost_center,
			"allow_zero_valuation_rate": 1,
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()
	return se.name


def _shrink_old_allocation(opl, rows, anchor, qty, sale_order_item):
	"""The rejected stems are no longer the old bucket's to supply: its pick rows
	and its allocation shrink by `qty` (a row left with nothing goes), so the
	replacement fits the line's confirmed stems."""
	left = qty
	for r in rows:  # idx desc: the last box first
		if left <= stock_movement.QTY_TOLERANCE:
			break
		take = min(flt(r.stock_qty), left)
		remaining = flt(r.stock_qty) - take
		left -= take
		if remaining <= stock_movement.QTY_TOLERANCE:
			frappe.db.delete("Pick List Item", {"name": r.name})
		else:
			conv = flt(r.conversion_factor) or 1
			frappe.db.set_value(
				"Pick List Item",
				r.name,
				{"stock_qty": remaining, "qty": remaining / conv},
				update_modified=False,
			)

	bas_name = frappe.db.get_value(
		"Bucket Allocation Status",
		{"bucket_id": anchor.bucket, "item_code": anchor.item_code, "stem_length": anchor.stem_length or ""},
		"name",
	)
	if bas_name:
		bas = frappe.get_doc("Bucket Allocation Status", bas_name, for_update=True)
		left = qty
		for a in bas.bucket_allocations:
			if left <= stock_movement.QTY_TOLERANCE:
				break
			if a.cancelled or a.sales_order_item != sale_order_item:
				continue
			take = min(flt(a.quantity_allocated), left)
			a.quantity_allocated = flt(a.quantity_allocated) - take
			left -= take
			if a.quantity_allocated <= stock_movement.QTY_TOLERANCE:
				a.quantity_allocated = 0
				a.cancelled = 1
			a.db_update()
		sa.recompute_bas_quantities(bas)
		bas.flags.ignore_validate = True
		bas.flags.ignore_mandatory = True
		bas.save(ignore_permissions=True)

	total = frappe.db.sql(
		"SELECT COALESCE(SUM(stock_qty), 0) FROM `tabPick List Item` WHERE parent = %s", opl.name
	)[0][0]
	frappe.db.set_value("Order Pick List", opl.name, "custom_total_stems", total, update_modified=False)


def _location_of(farm):
	for location, farms in sa._get_production_config()["farms_by_location"].items():
		if farm in farms:
			return location
	frappe.throw(_("No allocation location includes {0}.").format(farm))


def _allocate_replacement(opl, rows, anchor, new, new_farm, qty, sale_order_item, remote):
	"""Allocate `qty` stems of `new` to the line, as rows of this OPL, with the
	allocation page's own core. Returns (new pick row names, stock moves)."""
	so_item = frappe.db.get_value(
		"Sales Order Item", sale_order_item, ["uom", "stock_uom", "conversion_factor"], as_dict=True
	)
	longer = (new.stem_length or "") != (anchor.stem_length or "")
	allocation = {
		"sales_order_item": sale_order_item,
		"bucket_id": new.bucket_id,
		"item_code": anchor.item_code,
		"qty": qty,
		"stem_length": new.stem_length,
		"warehouse": new.warehouse,
		"uom": so_item.uom if so_item else None,
		"stock_uom": so_item.stock_uom if so_item else None,
		"conversion_factor": flt(so_item.conversion_factor) if so_item else 1,
		"downgrade_reason": _("Quality issue replacement for {0} ({1})").format(
			anchor.bucket, anchor.stem_length or ""
		)
		if longer
		else "",
	}
	before = set(
		frappe.get_all(
			"Pick List Item", filters={"parent": opl.name, "parenttype": "Order Pick List"}, pluck="name"
		)
	)
	res = sa._allocate_stock_with_buckets_impl(
		opl.sales_order, [allocation], _location_of(opl.farm or new_farm), teams={}, target_opl=opl.name
	)
	new_rows = [
		n
		for n in frappe.get_all(
			"Pick List Item",
			filters={"parent": opl.name, "parenttype": "Order Pick List", "bucket": new.bucket_id},
			pluck="name",
		)
		if n not in before
	]
	if not new_rows:
		frappe.throw(_("Bucket {0} could not be added to {1}.").format(new.bucket_id, opl.name))

	# The replacement fills the boxes the bad stems came out of.
	box = rows[-1]
	values = {"custom_box_id": box.custom_box_id, "custom_box_label": box.custom_box_label}
	if remote:
		# Requested from the remote farm: waits for a truck, first in line.
		values.update(
			{
				"awaiting_transfer": 1,
				"transfer_priority": ASAP,
				"custom_ready_for_packing": 0,
				"loaded_in_trolley": 0,
				"in_transit": 0,
				"shelved": 0,
				"farm": new_farm,
			}
		)
	else:
		values.update({"awaiting_transfer": 0, "custom_ready_for_packing": 1})
	for name in new_rows:
		frappe.db.set_value("Pick List Item", name, {k: v for k, v in values.items() if v is not None})
	posted = (res.get("stock_moves") or {}).get("posted") or []
	return new_rows, [m.get("entry") for m in posted if m.get("entry")]


def _record(opl, rows, anchor, new, new_farm, qty, scope, defect, notes, remote, reject_entry, stock_moves):
	from upande_packhouse.api import bucket_replacement

	extra = {
		"defect": defect,
		"defect_category": _category(defect),
		"replace_scope": SCOPES[scope],
		"replacement_source": ("Remote farm" if remote else "Sales farm") if new else "",
		"transfer_priority": ASAP if remote else "",
		"reject_entry": reject_entry,
		# The bad stems are gone to Rejects: nothing to find again.
		"status": "Discarded",
		"resolution": _("Rejected during packing: {0}").format(defect),
		"resolved_by": frappe.session.user,
		"resolved_at": now_datetime(),
	}
	return bucket_replacement.record(
		anchor.bucket,
		new.bucket_id if new else "",
		anchor,
		rows,
		new_farm or opl.farm,
		opl.name,
		new.shelf if new else "",
		qty,
		[reject_entry, *stock_moves],
		reason="Quality issue",
		notes=notes,
		extra=extra,
	)
