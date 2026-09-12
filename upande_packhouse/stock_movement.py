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

**One Stock Entry per variety per stem length.** Moving three buckets of
Snowflake 62cm for one order is one entry with three lines, not three entries.
The bucket lives on the line (`Stock Entry Detail.custom_bucket_id`); on a site
where that field has not been created yet the module falls back to one entry
per bucket, so nothing breaks before the patch runs.

Quantities are tracked **per bucket**, never off the shared `Bin`: a cold store
holds thousands of buckets, so "has this bucket already moved?" can only be
answered from the bucket's own Stock Entry history.
"""

from collections import OrderedDict

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
# LEDGER HELPERS — always per bucket
# ============================================================
def line_has_bucket():
    """Is `Stock Entry Detail.custom_bucket_id` available on this site?

    Grouping several buckets into one entry is only safe once it is: without
    it, a grouped entry cannot say which bucket moved which stems.
    """
    return frappe.db.has_column("Stock Entry Detail", "custom_bucket_id")


def _bucket_expr():
    """Line bucket where present, else the parent's — old entries stamp only the
    parent, grouped entries stamp only the line."""
    if line_has_bucket():
        return "COALESCE(sed.custom_bucket_id, se.custom_bucket_id)"
    return "se.custom_bucket_id"


def bucket_balance(bucket_id, item_code, warehouse):
    """How many of THIS bucket's stems the ledger still has in `warehouse`.

    Bin is useless here: a receiving cold store holds thousands of buckets, so
    a non-zero Bin says nothing about whether this bucket has already moved on.
    """
    if not (bucket_id and item_code and warehouse):
        return 0.0
    return flt(
        frappe.db.sql(
            f"""
            SELECT COALESCE(
                       SUM(CASE WHEN sed.t_warehouse = %(wh)s THEN sed.qty ELSE 0 END)
                     - SUM(CASE WHEN sed.s_warehouse = %(wh)s THEN sed.qty ELSE 0 END),
                   0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE {_bucket_expr()} = %(bucket)s
              AND sed.item_code = %(item)s
              AND se.docstatus = 1
            """,
            {"wh": warehouse, "bucket": bucket_id, "item": item_code},
        )[0][0]
    )


def sold_qty(bucket_id, item_code, so_item, target):
    """Stems of this bucket **net** sold to this SO line and sitting in `target`.

    Net, because an unallocation posts a reversing transfer rather than
    cancelling a grouped entry that other buckets still depend on. Drives
    top-ups too: allocating 50 more from a bucket that already moved 200 for
    the same line moves exactly the extra 50.
    """
    if not (bucket_id and so_item and target):
        return 0.0
    return flt(
        frappe.db.sql(
            f"""
            SELECT COALESCE(
                       SUM(CASE WHEN sed.t_warehouse = %(wh)s THEN sed.qty ELSE 0 END)
                     - SUM(CASE WHEN sed.s_warehouse = %(wh)s THEN sed.qty ELSE 0 END),
                   0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.custom_issued_to = %(so_item)s
              AND {_bucket_expr()} = %(bucket)s
              AND sed.item_code = %(item)s
              AND se.docstatus = 1
            """,
            {"wh": target, "bucket": bucket_id, "item": item_code, "so_item": so_item},
        )[0][0]
    )


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
    lines,
    farm=None,
    business_unit=None,
    stem_length=None,
    so_item=None,
    opl=None,
    remarks=None,
):
    """Submit ONE Material Transfer for one variety at one stem length.

    `lines` is [{"bucket_id": ..., "qty": ...}, ...] — one row per contributing
    bucket. The parent carries the bucket only when the entry is single-bucket,
    so existing per-bucket reports keep working.
    """
    lines = [dict(line) for line in lines if flt(line.get("qty")) > 0]
    if not lines or not source or not target or source == target:
        return None

    _require_entry_types()

    total = sum(flt(line["qty"]) for line in lines)
    available = on_hand(item_code, source)
    if available < total - QTY_TOLERANCE:
        buckets = ", ".join(str(line.get("bucket_id")) for line in lines)
        frappe.throw(
            f"{item_code} {stem_length or ''}: {total} needed in {source}, "
            f"{available} on hand (buckets {buckets})"
        )

    company = frappe.db.get_value("Warehouse", source, "company")
    purpose = (
        frappe.db.get_value("Stock Entry Type", entry_type, "purpose")
        or "Material Transfer"
    )
    buckets = {line.get("bucket_id") for line in lines}

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
            "custom_bucket_id": lines[0]["bucket_id"] if len(buckets) == 1 else None,
            "farm": farm,
            "business_unit": business_unit,
            "custom_stem_length": stem_length,
            "custom_issued_to": so_item,
            "custom_opl_scanned": opl,
            "remarks": remarks,
        }
    )
    for line in lines:
        se.append(
            "items",
            {
                "item_code": item_code,
                "qty": flt(line["qty"]),
                "s_warehouse": source,
                "t_warehouse": target,
                "farm": line.get("farm") or farm,
                "business_unit": business_unit,
                "custom_stem_length": stem_length,
                "custom_bucket_id": line.get("bucket_id"),
                "allow_zero_valuation_rate": 1,  # harvest stock is valued at 0
            },
        )
    se.insert(ignore_permissions=True)
    se.submit()
    return se.name


# ============================================================
# PLAN / POST
# ============================================================
def plan_moves(rows, business_unit, stop_before_terminal=False):
    """Work out every leg each row needs, without posting anything.

    A row is {bucket_id, item_code, qty, source, stem_length, farm, so_item,
    opl, graded, remarks}. Returns (plans, skipped); `plans` are posted in hop
    order by `post_plans`.
    """
    plans, skipped = [], []

    for row in rows:
        qty = flt(row.get("qty"))
        bucket_id = row.get("bucket_id")
        item_code = row.get("item_code")
        if qty <= 0 or not bucket_id or not item_code:
            continue

        for depth, hop in enumerate(
            resolve_route(row["source"], business_unit, graded=row.get("graded", True))
        ):
            if stop_before_terminal and hop["terminal"]:
                break

            if hop["terminal"]:
                already = sold_qty(bucket_id, item_code, row.get("so_item"), hop["to"])
                move = qty - already
                if move <= QTY_TOLERANCE:
                    skipped.append({**hop, "bucket_id": bucket_id, "reason": "already sold"})
                    continue
            else:
                here = bucket_balance(bucket_id, item_code, hop["from"])
                if here <= QTY_TOLERANCE:
                    skipped.append(
                        {**hop, "bucket_id": bucket_id, "reason": "bucket already moved"}
                    )
                    continue
                move = min(qty, here)

            plans.append(
                {
                    **hop,
                    "depth": depth,
                    "bucket_id": bucket_id,
                    "item_code": item_code,
                    "qty": move,
                    "stem_length": row.get("stem_length"),
                    "farm": row.get("farm"),
                    "so_item": row.get("so_item") if hop["terminal"] else None,
                    "opl": row.get("opl"),
                    "remarks": row.get("remarks"),
                }
            )

    return plans, skipped


def post_plans(plans, business_unit):
    """Post the planned legs, one Stock Entry per variety per stem length.

    Legs are grouped on (hop, variety, stem length, SO item) and posted
    shallowest-hop-first, so the consolidation leg lands before the sale leg
    that depends on it.
    """
    group_buckets = line_has_bucket()
    groups = OrderedDict()

    for plan in plans:
        key = (
            plan["depth"],
            plan["from"],
            plan["to"],
            plan["type"],
            plan["item_code"],
            plan["stem_length"],
            plan["so_item"],
            plan["opl"],
            plan["remarks"],
            # Without a bucket field on the line, keep entries per bucket so
            # traceability survives.
            None if group_buckets else plan["bucket_id"],
        )
        groups.setdefault(key, []).append(plan)

    posted = []
    for key in sorted(groups, key=lambda k: k[0]):
        members = groups[key]
        head = members[0]
        entry = post_transfer(
            entry_type=head["type"],
            source=head["from"],
            target=head["to"],
            item_code=head["item_code"],
            lines=[
                {"bucket_id": m["bucket_id"], "qty": m["qty"], "farm": m["farm"]}
                for m in members
            ],
            farm=head["farm"],
            business_unit=business_unit,
            stem_length=head["stem_length"],
            so_item=head["so_item"],
            opl=head["opl"],
            remarks=head["remarks"],
        )
        posted.append(
            {
                "entry": entry,
                "from": head["from"],
                "to": head["to"],
                "type": head["type"],
                "item_code": head["item_code"],
                "stem_length": head["stem_length"],
                "so_item": head["so_item"],
                "qty": sum(m["qty"] for m in members),
                "buckets": [m["bucket_id"] for m in members],
            }
        )
    return posted


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

    route = resolve_route(source_warehouse, business_unit)
    row = {
        "bucket_id": bucket_id,
        "item_code": item_code,
        "qty": flt(qty),
        "source": source_warehouse,
        "stem_length": stem_length,
        "farm": farm,
        "remarks": f"Arrived {farm} packhouse" if farm else "Arrived packhouse",
    }
    plans, skipped = plan_moves([row], business_unit, stop_before_terminal=True)
    posted = post_plans(plans, business_unit)

    landing = [hop for hop in route if not hop["terminal"]]
    return {
        "warehouse": landing[-1]["to"] if landing else source_warehouse,
        "posted": posted,
        "skipped": skipped,
        "moved": bool(posted),
    }


# ============================================================
# EVENT 2 — ALLOCATION (TERMINAL HOP: THE SALE)
# ============================================================
def move_allocation_to_sold(allocations, business_unit, sales_order=None, opl=None):
    """Land allocated stems in a *Sold warehouse, grouped per variety per length.

    `allocations` rows carry: bucket_id, item_code, qty, sales_order_item and
    (set by the allocation page) `_shelf_farm`. Raises on the first failure —
    the caller allocates inside a transaction, so a sale we cannot back with
    stock must roll the whole allocation back.
    """
    rows = []
    for a in allocations:
        bucket_id = a.get("bucket_id")
        item_code = a.get("item_code")
        qty = flt(a.get("qty"))
        if not (bucket_id and item_code and qty > 0):
            continue

        source = a.get("warehouse") or receiving_warehouse(bucket_id, item_code)
        if not source:
            frappe.throw(
                f"Bucket {bucket_id} ({item_code}) has no receiving entry — "
                "its stems were never booked into a cold store."
            )

        rows.append(
            {
                "bucket_id": bucket_id,
                "item_code": item_code,
                "qty": qty,
                "source": source,
                "stem_length": a.get("stem_length"),
                "farm": a.get("_shelf_farm") or a.get("shelf_farm"),
                "so_item": a.get("sales_order_item"),
                "opl": a.get("_opl") or opl,
                "graded": bool(a.get("graded", True)),
                "remarks": f"Allocated to {sales_order}" if sales_order else "Allocated",
            }
        )

    plans, skipped = plan_moves(rows, business_unit)
    posted = post_plans(plans, business_unit)
    return {"posted": posted, "skipped": skipped}


def reverse_allocation_movement(sales_order_item, bucket_id=None, item_code=None,
                                business_unit=None):
    """Send unallocated stems back out of the Sold warehouse.

    A grouped entry carries several buckets, so cancelling it would reverse
    other buckets' sales too. Instead post a reversing transfer for exactly the
    stems this line still holds; `sold_qty()` is net, so the pair cancels out.
    """
    filters = {"custom_issued_to": sales_order_item, "stock_entry_type": TYPE_TO_SOLD,
               "docstatus": 1}
    entries = frappe.get_all(
        "Stock Entry", filters=filters,
        fields=["name", "from_warehouse", "to_warehouse", "business_unit",
                "custom_stem_length", "farm"],
    )
    if not entries:
        return []

    reversed_moves = []
    for entry in entries:
        doc = frappe.get_doc("Stock Entry", entry.name)
        for line in doc.items:
            line_bucket = line.get("custom_bucket_id") or doc.get("custom_bucket_id")
            if bucket_id and line_bucket != bucket_id:
                continue
            if item_code and line.item_code != item_code:
                continue

            outstanding = sold_qty(
                line_bucket, line.item_code, sales_order_item, entry.to_warehouse
            )
            if outstanding <= QTY_TOLERANCE:
                continue  # already sent back

            reversed_moves.append(
                {
                    "entry": post_transfer(
                        entry_type=TYPE_HOP,
                        source=entry.to_warehouse,
                        target=entry.from_warehouse,
                        item_code=line.item_code,
                        lines=[{"bucket_id": line_bucket, "qty": outstanding}],
                        farm=entry.farm,
                        business_unit=entry.business_unit or business_unit,
                        stem_length=entry.custom_stem_length,
                        so_item=sales_order_item,
                        remarks=f"Unallocated from {sales_order_item}",
                    ),
                    "bucket": line_bucket,
                    "item_code": line.item_code,
                    "qty": outstanding,
                    "reverses": entry.name,
                }
            )
    return reversed_moves


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
            posted.append({**plan, "entries": result["posted"]})
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
