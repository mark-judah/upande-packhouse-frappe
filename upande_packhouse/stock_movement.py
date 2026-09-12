"""Physical stock movement for the packhouse flow.

Everything between Receiving and Packing used to be metadata only: shelving,
allocation, picking and issuing wrote to `Shelf Item` / `Pick List Item` /
`Bucket Allocation Status` and never touched the ledger, so stems piled up in
the farm receiving cold stores forever.

This module is the single place where flowers actually move. Routing is driven
by the `SO Warehouse Mapping` doctype, keyed on business unit, e.g. Roses:

    Simotwo  Receiving ─┐
    Torongo  Receiving ─┼─→ Kapkolia Receiving ─→ Kapkolia Graded Sold
    Chepsito Receiving ─┤                         (ungraded: Kapkolia Ungraded Sold)
    Kaptumbo Receiving ─┘
    Karen    Receiving ──────────────────────────→ Karen Graded Sold

The mapping is a graph, not a flat lookup, so a bucket on an outlying farm takes
two hops. The last hop — the one that lands in a *Sold* warehouse — is the sale
and is stamped with the Sales Order Item it was sold against; earlier hops are
the truck arriving at the packhouse.

Quantities are tracked **per bucket**, never off the shared `Bin`: a cold store
holds thousands of buckets, so "is this leg already paid for?" can only be
answered from this bucket's own Stock Entry history (`custom_bucket_id`).
"""

import frappe
from frappe.utils import flt, nowdate, nowtime

MAPPING_DT = "SO Warehouse Mapping"

# Stock Entry Types (both are Material Transfer purpose)
TYPE_TO_SOLD = "Move To Graded Sold"  # terminal hop, into a *Sold warehouse
TYPE_HOP = "Material Transfer"  # consolidation hop, cold store -> cold store

RECEIVING_TYPES = ("Receiving", "Late Receipt")

MAX_HOPS = 5  # cycle / runaway guard
QTY_TOLERANCE = 0.001


# ============================================================
# MAPPING
# ============================================================
def load_mapping(business_unit):
    """{source_warehouse: {"to": delivery, "ungraded": ungraded_sold}}.

    Duplicate rows for the same source are tolerated: the first delivery wins,
    and an ungraded target is picked up from whichever row carries one.
    """
    if not business_unit:
        frappe.throw("Business Unit is required to resolve warehouse routing")

    name = frappe.db.get_value(MAPPING_DT, {"business_unit": business_unit})
    if not name:
        frappe.throw(f"No {MAPPING_DT} configured for business unit {business_unit}")

    table = {}
    for row in frappe.get_doc(MAPPING_DT, name).items:
        if not row.source_warehouse or not row.delivery_warehouse:
            continue
        entry = table.setdefault(
            row.source_warehouse,
            {"to": row.delivery_warehouse, "ungraded": None},
        )
        # `ungraded_sold_warehouse` is a site-level custom field on
        # SO Warehouse Mapping Item — absent on a stock install, hence .get().
        if row.get("ungraded_sold_warehouse"):
            entry["ungraded"] = row.get("ungraded_sold_warehouse")
    return table


def resolve_route(source, business_unit, graded=True):
    """Every hop from `source` to its terminal warehouse, in order.

    Returns [{"from", "to", "type", "terminal"}, ...]. Empty when `source` is
    already terminal (it is not a source in the mapping).
    """
    table = load_mapping(business_unit)
    route, seen, current = [], {source}, source

    while current in table and len(route) < MAX_HOPS:
        row = table[current]
        target = row["to"]
        terminal = target not in table  # nothing routes onward from there

        if terminal and not graded and row["ungraded"]:
            target = row["ungraded"]

        if target in seen:
            frappe.throw(f"Warehouse mapping loops at {target} ({business_unit})")

        seen.add(target)
        route.append(
            {
                "from": current,
                "to": target,
                "type": TYPE_TO_SOLD if terminal else TYPE_HOP,
                "terminal": terminal,
            }
        )
        if terminal:
            return route
        current = target

    if current in table:
        frappe.throw(f"Warehouse mapping is deeper than {MAX_HOPS} hops from {source}")
    return route


def terminal_warehouse(source, business_unit, graded=True):
    """Where this source is ultimately supposed to land. `source` if nowhere."""
    route = resolve_route(source, business_unit, graded=graded)
    return route[-1]["to"] if route else source


# ============================================================
# LEDGER HELPERS — per bucket before packing, per box after
# ============================================================
def _ledger_balance(item_code, warehouse, bucket_id=None, box_label=None):
    """How many of THIS bucket's (or, once packed, THIS box's) stems the
    ledger still has in `warehouse`.

    Bin is useless here: a receiving cold store holds thousands of buckets, so
    a non-zero Bin says nothing about whether this specific bucket/box has
    already moved on. Every entry in the flow carries `custom_bucket_id`
    (pre-pack) or `custom_box_label` (post-pack, once several buckets'
    stems have been combined into one box and a bucket_id no longer
    identifies anything), so its own in/out balance is exact.
    """
    if not (item_code and warehouse and (bucket_id or box_label)):
        return 0.0
    conditions = ["sed.item_code = %(item)s", "se.docstatus = 1"]
    params = {"wh": warehouse, "item": item_code}
    if bucket_id:
        conditions.append("se.custom_bucket_id = %(bucket)s")
        params["bucket"] = bucket_id
    if box_label:
        conditions.append("se.custom_box_label = %(box)s")
        params["box"] = box_label
    return flt(
        frappe.db.sql(
            f"""
            SELECT COALESCE(
                       SUM(CASE WHEN sed.t_warehouse = %(wh)s THEN sed.qty ELSE 0 END)
                     - SUM(CASE WHEN sed.s_warehouse = %(wh)s THEN sed.qty ELSE 0 END),
                   0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE {" AND ".join(conditions)}
            """,
            params,
        )[0][0]
    )


def bucket_balance(bucket_id, item_code, warehouse):
    """Back-compat name for the bucket-tracked case -- see _ledger_balance."""
    return _ledger_balance(item_code, warehouse, bucket_id=bucket_id)


def on_hand(item_code, warehouse):
    """Warehouse-wide balance — the backstop against posting negative stock."""
    return flt(
        frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
        )
    )


def receiving_warehouse(bucket_id, item_code):
    """Where the Receiving entry put this bucket's stems."""
    row = frappe.db.sql(
        """
        SELECT sed.t_warehouse
        FROM `tabStock Entry` se
        JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
        WHERE se.custom_bucket_id = %s
          AND sed.item_code = %s
          AND se.stock_entry_type IN %s
          AND se.docstatus = 1
        ORDER BY se.posting_date DESC, se.posting_time DESC
        LIMIT 1
        """,
        (bucket_id, item_code, RECEIVING_TYPES),
        as_dict=True,
    )
    return row[0].t_warehouse if row else None


def sold_qty(bucket_id, item_code, so_item, target):
    """Stems of this bucket already sold to this SO line and sitting in `target`.

    Drives top-ups: allocating 50 more from a bucket that already moved 200 for
    the same line must move exactly the extra 50, not nothing and not 250.
    """
    if not so_item:
        return 0.0
    return flt(
        frappe.db.sql(
            """
            SELECT COALESCE(SUM(sed.qty), 0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.custom_issued_to = %s
              AND se.custom_bucket_id = %s
              AND sed.item_code = %s
              AND sed.t_warehouse = %s
              AND se.docstatus = 1
            """,
            (so_item, bucket_id, item_code, target),
        )[0][0]
    )


def _require_entry_types():
    """Fail with a readable message instead of a mandatory-Link validation error."""
    for entry_type in (TYPE_TO_SOLD, TYPE_HOP):
        if not frappe.db.exists("Stock Entry Type", entry_type):
            frappe.throw(
                f"Stock Entry Type '{entry_type}' is missing — packhouse stock "
                "movement cannot post without it."
            )


# ============================================================
# THE ONE PRIMITIVE EVERY CALLER USES
# ============================================================
def post_transfer(
    *,
    entry_type,
    source,
    target,
    item_code,
    qty,
    bucket_id=None,
    box_label=None,
    farm=None,
    business_unit=None,
    stem_length=None,
    so_item=None,
    opl=None,
    receiving_entry=None,
    remarks=None,
    allow_partial=False,
):
    """Build and submit one Material Transfer. Returns the Stock Entry name."""
    qty = flt(qty)
    if qty <= 0 or not source or not target or source == target:
        return None

    _require_entry_types()

    available = on_hand(item_code, source)
    if available < qty:
        if not allow_partial or available <= 0:
            frappe.throw(
                f"{item_code}: {qty} needed in {source}, {available} on hand"
                + (f" (bucket {bucket_id})" if bucket_id else "")
            )
        qty = available  # move what exists; the caller reports the shortfall

    company = frappe.db.get_value("Warehouse", source, "company")
    purpose = (
        frappe.db.get_value("Stock Entry Type", entry_type, "purpose")
        or "Material Transfer"
    )

    # Post-harvest stock entries have no greenhouse to derive a cost centre
    # from (see stock_entry_cost_center.py) -- the SOURCE warehouse carries
    # its own custom_cost_center instead. Set it directly here rather than
    # adding TYPE_HOP ("Material Transfer") to that module's doctype-wide
    # hook: "Material Transfer" is ERPNext's own generic type, reused all
    # over the system for unrelated transfers, so forcing every such entry
    # everywhere to require a warehouse-level cost centre would be a much
    # bigger, unrelated blast radius than this one route walker.
    cost_center = frappe.db.get_value("Warehouse", source, "custom_cost_center")
    if not cost_center:
        frappe.throw(
            f"Please contact your IT administrator to add the cost center for warehouse {source}"
        )

    se = frappe.new_doc("Stock Entry")
    se.update(
        {
            "stock_entry_type": entry_type,
            "purpose": purpose,
            "company": company,
            "posting_date": nowdate(),
            "posting_time": nowtime(),
            "set_posting_time": 1,
            "from_warehouse": source,
            "to_warehouse": target,
            "custom_bucket_id": bucket_id,
            "custom_box_label": box_label,
            "farm": farm,
            "business_unit": business_unit,
            "custom_stem_length": stem_length,
            "custom_issued_to": so_item,
            "custom_opl_scanned": opl,
            "custom_receiving_entry": receiving_entry,
            "remarks": remarks,
            "cost_center": cost_center,
        }
    )
    se.append(
        "items",
        {
            "item_code": item_code,
            "qty": qty,
            "s_warehouse": source,
            "t_warehouse": target,
            "farm": farm,
            "business_unit": business_unit,
            "custom_stem_length": stem_length,
            "allow_zero_valuation_rate": 1,  # harvest stock is valued at 0
            "cost_center": cost_center,
        },
    )
    se.insert(ignore_permissions=True)
    se.submit()
    return se.name


# ============================================================
# ROUTE WALKER
# ============================================================
def move_along_route(
    *,
    bucket_id,
    item_code,
    qty,
    source,
    business_unit,
    farm=None,
    stem_length=None,
    so_item=None,
    opl=None,
    graded=True,
    stop_before_terminal=False,
    remarks=None,
):
    """Walk the mapping from `source`, posting one Stock Entry per hop.

    Re-entrant by construction, because every decision is made from this
    bucket's own ledger balance:

    * a leg whose source holds none of this bucket's stems is skipped — that is
      how a bucket whose arrival was posted at shelving time pays only for the
      terminal hop, and how an unallocate/re-allocate cycle does not re-post
      the farm → packhouse leg a second time;
    * the terminal hop moves only the stems this SO line has not been sold yet,
      so a top-up allocation moves exactly the increment.
    """
    qty = flt(qty)
    if qty <= 0:
        return []

    results = []
    for hop in resolve_route(source, business_unit, graded=graded):
        if stop_before_terminal and hop["terminal"]:
            break

        if hop["terminal"]:
            already = sold_qty(bucket_id, item_code, so_item, hop["to"])
            move = qty - already
            if move <= QTY_TOLERANCE:
                results.append(
                    {**hop, "entry": None, "qty": 0, "skipped": "already sold"}
                )
                continue
        else:
            here = bucket_balance(bucket_id, item_code, hop["from"])
            if here <= QTY_TOLERANCE:
                results.append(
                    {**hop, "entry": None, "qty": 0, "skipped": "bucket already moved"}
                )
                continue
            move = min(qty, here)

        results.append(
            {
                **hop,
                "qty": move,
                "entry": post_transfer(
                    entry_type=hop["type"],
                    source=hop["from"],
                    target=hop["to"],
                    item_code=item_code,
                    qty=move,
                    bucket_id=bucket_id,
                    farm=farm,
                    business_unit=business_unit,
                    stem_length=stem_length,
                    so_item=so_item if hop["terminal"] else None,
                    opl=opl,
                    remarks=remarks,
                ),
            }
        )
    return results


# ============================================================
# EVENT 1 — BUCKET ARRIVES AT THE PACKHOUSE (SHELVING)
# ============================================================
@frappe.whitelist()
def post_arrival(bucket_id, item_code, qty, source_warehouse, business_unit, farm=None,
                 stem_length=None):
    """Move a freshly shelved bucket onto its packhouse cold store.

    Called from the `Shelving Entry` server script once per received item row.
    Returns the warehouse the stems now sit in, which is what belongs on the
    `Shelf Item` row — so `Shelf.farm` and `Shelf Item.warehouse` stop drifting.
    """
    if not frappe.has_permission("Stock Entry", "submit"):
        frappe.throw(
            "Not permitted to move stock between warehouses", frappe.PermissionError
        )

    hops = move_along_route(
        bucket_id=bucket_id,
        item_code=item_code,
        qty=flt(qty),
        source=source_warehouse,
        business_unit=business_unit,
        farm=farm,
        stem_length=stem_length,
        stop_before_terminal=True,
        remarks=f"Arrived {farm} packhouse" if farm else "Arrived packhouse",
    )
    landed = [h for h in hops if h.get("entry")]
    return {
        "warehouse": hops[-1]["to"] if hops else source_warehouse,
        "hops": hops,
        "moved": bool(landed),
    }


# ============================================================
# EVENT 2 — ALLOCATION (TERMINAL HOP: THE SALE)
# ============================================================
def move_allocation_to_sold(allocations, business_unit, sales_order=None, opl=None):
    """Post every hop needed to land allocated stems in a *Sold warehouse.

    `allocations` rows carry: bucket_id, item_code, qty, sales_order_item and
    (set by the allocation page) `_shelf_farm`. Raises on the first failure —
    the caller allocates inside a transaction, so a sale we cannot back with
    stock must roll the whole allocation back.
    """
    moved = []
    for a in allocations:
        bucket_id = a.get("bucket_id")
        item_code = a.get("item_code")
        qty = flt(a.get("qty"))
        so_item = a.get("sales_order_item")
        if not (bucket_id and item_code and qty > 0):
            continue

        source = a.get("warehouse") or receiving_warehouse(bucket_id, item_code)
        if not source:
            frappe.throw(
                f"Bucket {bucket_id} ({item_code}) has no receiving entry — "
                "its stems were never booked into a cold store."
            )

        moved += move_along_route(
            bucket_id=bucket_id,
            item_code=item_code,
            qty=qty,
            source=source,
            business_unit=business_unit,
            farm=a.get("_shelf_farm") or a.get("shelf_farm"),
            stem_length=a.get("stem_length"),
            so_item=so_item,
            opl=a.get("_opl") or opl,
            graded=bool(a.get("graded", True)),
            remarks=f"Allocated to {sales_order}" if sales_order else "Allocated",
        )
    return moved


def reverse_allocation_movement(sales_order_item, bucket_id=None, item_code=None):
    """Cancel the sale transfers behind an allocation being undone.

    Only the terminal hop is reversed: the stems physically are at the
    packhouse, so the arrival hop stays posted and the bucket simply becomes
    sellable again from the packhouse cold store.
    """
    filters = {
        "custom_issued_to": sales_order_item,
        "stock_entry_type": TYPE_TO_SOLD,
        "docstatus": 1,
    }
    if bucket_id:
        filters["custom_bucket_id"] = bucket_id

    cancelled = []
    for name in frappe.get_all("Stock Entry", filters=filters, pluck="name"):
        se = frappe.get_doc("Stock Entry", name)
        if item_code and not any(i.item_code == item_code for i in se.items):
            continue
        se.cancel()
        cancelled.append(name)
    return cancelled


# ============================================================
# EVENTS 3-5 — ISSUE, STAGE, LOAD (downstream of the sale)
# ============================================================
# Graded Sold isn't the end of the chain: `SO Warehouse Mapping` also carries
# `packhouse`, `dispatch_cold_store` and `delivery_truck` per farm, but until
# now nothing ever read them -- Issue/Stage/Load only ever flipped metadata
# flags (Shelf Item removed, Box Label.staged/loaded) with no ledger move
# behind any of it. Unlike the arrival/terminal hops above, each of these is
# a single, already-known leg driven by one real scan event, not a route to
# walk -- so they share one small primitive (post_single_hop) instead of
# move_along_route's multi-hop logic.
#
# Issue is still per BUCKET (custom_bucket_id) -- packing hasn't happened
# yet. Stage and Load are per BOX (custom_box_label): by then several
# buckets' stems have been combined into one box and a bucket_id no longer
# identifies anything on its own.
def mapping_row_for_farm(farm, business_unit):
    """Full Roses-MAP row for a FARM directly (matches farm_pack_list.py's
    own established convention for "packing is already past individual
    bucket provenance") -- used here because by Issue/Stage/Load time we
    already know which farm's chain a bucket/box belongs to, and only the
    downstream fields (packhouse, dispatch_cold_store, delivery_truck) are
    needed, not a route walked from a specific source warehouse.
    """
    if not farm:
        return None
    name = frappe.db.get_value(MAPPING_DT, {"business_unit": business_unit})
    if not name:
        return None
    for row in frappe.get_doc(MAPPING_DT, name).items:
        if row.source_warehouse and frappe.db.get_value("Warehouse", row.source_warehouse, "custom_farm") == farm:
            return row
    return None


def post_single_hop(*, item_code, qty, source, target, business_unit, bucket_id=None,
                     box_label=None, farm=None, stem_length=None, so_item=None, remarks=None):
    """One deliberate, already-known leg -- not a route walk. Idempotent the
    same way every other hop in this module is: skipped (returns None) once
    this bucket's/box's own ledger balance at `source` is already zero, so a
    re-scan or a retried request never double-moves stock.
    """
    qty = flt(qty)
    if qty <= 0 or not source or not target or source == target:
        return None
    have = _ledger_balance(item_code, source, bucket_id=bucket_id, box_label=box_label)
    if have <= QTY_TOLERANCE:
        return None
    return post_transfer(
        entry_type=TYPE_HOP,
        source=source,
        target=target,
        item_code=item_code,
        qty=min(qty, have),
        bucket_id=bucket_id,
        box_label=box_label,
        farm=farm,
        business_unit=business_unit,
        stem_length=stem_length,
        so_item=so_item,
        remarks=remarks,
    )


@frappe.whitelist()
def post_issue_to_packhouse(bucket_id, item_code, qty, business_unit, farm, stem_length=None, so_item=None):
    """EVENT 3 — a packer scans the bucket off the shelf to start packing it.
    Graded Sold -> Packhouse. Still per-bucket."""
    row = mapping_row_for_farm(farm, business_unit)
    if not row or not row.packhouse:
        return {"moved": False, "reason": f"no packhouse mapped for farm {farm}"}
    source = row.delivery_warehouse
    entry = post_single_hop(
        item_code=item_code, qty=qty, source=source, target=row.packhouse,
        business_unit=business_unit, bucket_id=bucket_id, farm=farm,
        stem_length=stem_length, so_item=so_item,
        remarks=f"Issued to {so_item}" if so_item else "Issued",
    )
    return {"moved": bool(entry), "entry": entry, "warehouse": row.packhouse if entry else source}


@frappe.whitelist()
def post_stage_to_dispatch(box_label, item_code, qty, business_unit, farm, remarks=None):
    """EVENT 4 — a Box Label is scanned staged in the dispatch coldroom.
    Packhouse -> Dispatch Cold Store. Per-box from here on."""
    row = mapping_row_for_farm(farm, business_unit)
    if not row or not row.dispatch_cold_store:
        return {"moved": False, "reason": f"no dispatch cold store mapped for farm {farm}"}
    source = row.packhouse
    entry = post_single_hop(
        item_code=item_code, qty=qty, source=source, target=row.dispatch_cold_store,
        business_unit=business_unit, box_label=box_label, farm=farm,
        remarks=remarks or f"Staged {box_label}",
    )
    return {"moved": bool(entry), "entry": entry, "warehouse": row.dispatch_cold_store if entry else source}


@frappe.whitelist()
def post_load_to_truck(box_label, item_code, qty, business_unit, farm, remarks=None):
    """EVENT 5 — a Box Label is scanned loaded onto the delivery truck.
    Dispatch Cold Store -> Delivery Truck."""
    row = mapping_row_for_farm(farm, business_unit)
    if not row or not row.delivery_truck:
        return {"moved": False, "reason": f"no delivery truck mapped for farm {farm}"}
    source = row.dispatch_cold_store
    entry = post_single_hop(
        item_code=item_code, qty=qty, source=source, target=row.delivery_truck,
        business_unit=business_unit, box_label=box_label, farm=farm,
        remarks=remarks or f"Loaded {box_label}",
    )
    return {"moved": bool(entry), "entry": entry, "warehouse": row.delivery_truck if entry else source}


# ============================================================
# COMPLETENESS GATE
# ============================================================
def opl_rows(opl):
    """Pick rows of an Order Pick List.

    The table fieldname is `table_ytkc` on the doctype, but parts of this app
    still address the child table as `locations`, so accept either.
    """
    return opl.get("table_ytkc") or opl.get("locations") or []


def _row_bucket(row):
    return row.get("custom_bucket") or row.get("bucket")


def _row_so_item(row):
    return row.get("custom_sale_order_item") or row.get("sales_order_item")


def _row_warehouse(row):
    return row.get("warehouse") or row.get("source_warehouse")


def business_unit_of(doc):
    """`business_unit` is this app's field; `custom_business_unit` is the legacy
    mirror still populated on older Sales Orders."""
    return doc.get("business_unit") or doc.get("custom_business_unit")


def allocation_complete(opl_name):
    """An OPL is packable only once every row's stems sit in a Sold warehouse."""
    opl = frappe.get_doc("Order Pick List", opl_name)
    business_unit = business_unit_of(opl)
    pending = []

    for row in opl_rows(opl):
        bucket = _row_bucket(row)
        source = _row_warehouse(row) or receiving_warehouse(bucket, row.item_code)
        if not source:
            pending.append(
                {"bucket": bucket, "item": row.item_code,
                 "need": flt(row.stock_qty), "landed": 0, "terminal": None}
            )
            continue

        target = terminal_warehouse(source, business_unit)
        landed = sold_qty(bucket, row.item_code, _row_so_item(row), target)
        if landed < flt(row.stock_qty) - QTY_TOLERANCE:
            pending.append(
                {
                    "bucket": bucket,
                    "item": row.item_code,
                    "need": flt(row.stock_qty),
                    "landed": landed,
                    "terminal": target,
                }
            )

    return {"complete": not pending, "pending": pending}


# ============================================================
# BACKFILL — bench execute
# ============================================================
def backfill_shelf_arrivals(business_unit="Roses", dry_run=True, limit=None):
    """Post the arrival hop for every bucket currently sitting on a shelf.

    Squares the ledger with the shelves before the hooks go live:
        bench --site <site> execute \
            upande_packhouse.stock_movement.backfill_shelf_arrivals \
            --kwargs "{'business_unit': 'Roses', 'dry_run': False}"

    Each row is committed on its own so a mid-run failure leaves the rows
    already posted intact and reports the rest.
    """
    rows = frappe.db.sql(
        """
        SELECT si.name, si.bucket_id, si.variety AS item_code, si.stem_qty,
               si.warehouse, si.stem_length, s.farm
        FROM `tabShelf Item` si
        JOIN `tabShelf` s ON s.name = si.parent
        WHERE COALESCE(si.stem_qty, 0) > 0 AND si.warehouse IS NOT NULL
        ORDER BY si.date_added ASC
        """
        + (f" LIMIT {int(limit)}" if limit else ""),
        as_dict=True,
    )

    planned, posted, failed = [], [], []
    for r in rows:
        plan = {
            "shelf_item": r.name,
            "bucket": r.bucket_id,
            "item": r.item_code,
            "qty": flt(r.stem_qty),
            "from": r.warehouse,
        }
        try:
            route = [
                h for h in resolve_route(r.warehouse, business_unit) if not h["terminal"]
            ]
            if not route:
                continue  # already at a packhouse cold store
            plan["to"] = route[-1]["to"]
            planned.append(plan)
            if dry_run:
                continue

            result = post_arrival(
                bucket_id=r.bucket_id,
                item_code=r.item_code,
                qty=r.stem_qty,
                source_warehouse=r.warehouse,
                business_unit=business_unit,
                farm=r.farm,
                stem_length=r.stem_length,
            )
            # Stamp only the row we just moved, not every historical row for
            # this bucket.
            frappe.db.set_value(
                "Shelf Item", r.name, "warehouse", result["warehouse"],
                update_modified=False,
            )
            posted.append({**plan, "hops": result["hops"]})
            frappe.db.commit()
        except Exception as e:
            frappe.db.rollback()
            failed.append({**plan, "error": str(e)})

    summary = {
        "dry_run": dry_run,
        "candidates": len(planned),
        "posted": len(posted),
        "failed": len(failed),
        "sample": planned[:10],
        "errors": failed[:10],
    }
    print(frappe.as_json(summary))
    return summary
