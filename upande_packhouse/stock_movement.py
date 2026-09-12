"""Physical stock movement for the packhouse flow.

Everything between Receiving and Packing used to be metadata only: shelving,
allocation, picking and issuing wrote to `Shelf Item` / `Pick List Item` /
`Bucket Allocation Status` and never touched the ledger, so stems piled up in
the farm receiving cold stores forever.

This module is the single place where flowers actually move. Routing is driven
by the `SO Warehouse Mapping` doctype, keyed on business unit, e.g. Roses:

    source_warehouse -> transfer_to -> delivery_warehouse -> packhouse
                        -> dispatch_cold_store -> delivery_truck
       (Arrival)           (Sold)                 (Packing)
                        (Dispatch)              (Loading)

Each mapping row spells out the whole pipeline for one source warehouse; a blank
column simply has no leg for that stage. For Roses that reads:

    Simotwo/Torongo/Chepsito/Kaptumbo Receiving --Arrival--> Kapkolia Receiving
    Kapkolia Receiving --Sold--> Kapkolia Graded Sold (ungraded: Kapkolia Ungraded Sold)
                       --Packing--> Kapkolia Packhouse Store
                       --Dispatch--> Kapkolia Dispatch Coldroom
                       --Loading--> Delivery Truck
    Karen Receiving --Sold--> Karen Graded Sold -> Karen Packhouse Store -> ...

The **Sold** leg is the sale: it is stamped with the Sales Order Item, allocation
stops there, and every later leg belongs to that same order. The Arrival leg is
order-agnostic — the truck brings the whole bucket before anyone has bought it.

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

# Every leg is a plain warehouse-to-warehouse Material Transfer. What a leg
# MEANS is carried by its stage, its warehouses and (from the sale onwards) the
# Sales Order Item on `custom_issued_to` — not by a bespoke Stock Entry Type.
TYPE_HOP = "Material Transfer"

#: Legacy: the sale leg used to be posted under its own type. Still recognised
#: when reading history back, never used for new entries.
TYPE_TO_SOLD = "Move To Graded Sold"

RECEIVING_TYPES = ("Receiving", "Late Receipt")

MAX_HOPS = 5  # cycle / runaway guard
QTY_TOLERANCE = 0.001


# ============================================================
# MAPPING
# ============================================================
#: The pipeline a bucket walks, in order. Each entry is
#: (SO Warehouse Mapping Item fieldname, stage name, Stock Entry Type).
#: A row that leaves a column blank simply has no leg for that stage.
STAGES = (
    ("transfer_to", "Arrival", TYPE_HOP),
    ("delivery_warehouse", "Sold", TYPE_HOP),
    ("packhouse", "Packing", TYPE_HOP),
    ("dispatch_cold_store", "Dispatch", TYPE_HOP),
    ("delivery_truck", "Loading", TYPE_HOP),
)

STAGE_NAMES = tuple(stage for _f, stage, _t in STAGES)
ARRIVAL_STAGE = "Arrival"
SALE_STAGE = "Sold"  # allocation stops here — this leg is the sale
LAST_STAGE = STAGE_NAMES[-1]


def load_mapping(business_unit):
    """{source_warehouse: <SO Warehouse Mapping Item row as a dict>}.

    Duplicate rows for the same source are tolerated: the first row wins, and
    any column it leaves blank is filled from a later row for the same source.
    """
    if not business_unit:
        frappe.throw("Business Unit is required to resolve warehouse routing")

    name = frappe.db.get_value(MAPPING_DT, {"business_unit": business_unit})
    if not name:
        frappe.throw(f"No {MAPPING_DT} configured for business unit {business_unit}")

    columns = [field for field, _stage, _type in STAGES] + ["ungraded_sold_warehouse"]
    table = {}
    for row in frappe.get_doc(MAPPING_DT, name).items:
        if not row.source_warehouse:
            continue
        entry = table.setdefault(row.source_warehouse, {})
        for column in columns:
            # Several of these are newer fields; `.get()` keeps the module
            # working against a mapping that predates them.
            if not entry.get(column) and row.get(column):
                entry[column] = row.get(column)
    return table


def resolve_route(source, business_unit, graded=True, upto=SALE_STAGE):
    """The legs from `source` up to and including stage `upto`.

    Returns [{"from", "to", "stage", "type", "terminal"}, ...] — empty when the
    mapping has no row for `source`. `terminal` marks the sale leg, the one
    stamped with the Sales Order Item.
    """
    if upto not in STAGE_NAMES:
        frappe.throw(f"Unknown stage '{upto}' — expected one of {', '.join(STAGE_NAMES)}")

    table = load_mapping(business_unit)
    row = table.get(source)
    if not row:
        return []

    # Legacy shape: before `transfer_to` existed, an outlying farm's row pointed
    # its delivery_warehouse at the packhouse cold store, which was itself a
    # source row. Follow that chain so old mappings keep working.
    hops, seen = [], {source}
    while not row.get("transfer_to") and row.get("delivery_warehouse") in table:
        nxt = row["delivery_warehouse"]
        if nxt in seen or len(hops) >= MAX_HOPS:
            frappe.throw(f"Warehouse mapping loops at {nxt} ({business_unit})")
        seen.add(nxt)
        hops.append(
            {"from": source, "to": nxt, "stage": ARRIVAL_STAGE, "type": TYPE_HOP,
             "terminal": False}
        )
        source, row = nxt, table[nxt]

    if hops and upto == ARRIVAL_STAGE:
        return hops

    current = source
    for field, stage, entry_type in STAGES:
        if hops and stage == ARRIVAL_STAGE:
            continue  # already walked above by the legacy chain
        target = row.get(field)
        if stage == SALE_STAGE and not graded and row.get("ungraded_sold_warehouse"):
            target = row["ungraded_sold_warehouse"]
        if target and target != current:
            hops.append(
                {
                    "from": current,
                    "to": target,
                    "stage": stage,
                    "type": entry_type,
                    "terminal": stage == SALE_STAGE,
                }
            )
            current = target
        if stage == upto:
            break

    return hops


def sale_targets(business_unit):
    """Every warehouse the sale leg can land in for this business unit."""
    targets = set()
    for row in load_mapping(business_unit).values():
        for column in ("delivery_warehouse", "ungraded_sold_warehouse"):
            if row.get(column):
                targets.add(row[column])
    return targets


def stage_warehouse(source, business_unit, stage=SALE_STAGE, graded=True):
    """Where `source` is supposed to have landed by the end of `stage`."""
    route = resolve_route(source, business_unit, graded=graded, upto=stage)
    return route[-1]["to"] if route else source


def terminal_warehouse(source, business_unit, graded=True):
    """Where the sale leg puts the stems — the Sold warehouse."""
    return stage_warehouse(source, business_unit, stage=SALE_STAGE, graded=graded)


# ============================================================
# LEDGER HELPERS — per bucket before packing, per box after
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
            WHERE {_bucket_expr()} = %(bucket)s
              AND sed.item_code = %(item)s
              AND se.docstatus = 1
            """,
            params,
        )[0][0]
    )


def sold_qty(bucket_id, item_code, so_item, target):
    """This order's **net** balance of this bucket's stems in `target`.

    In minus out, restricted to entries stamped with `so_item`, so it answers
    "how many of this bucket's stems does this order have sitting here right
    now?" at any warehouse in the pipeline. Three things rely on that:

      * the sale leg moves `qty - sold_qty(at the Sold warehouse)`, so a top-up
        allocation moves only the increment;
      * Packing / Dispatch / Loading move at most `sold_qty(at their source)`,
        so one order can never carry off another order's stems from a shared
        warehouse, and re-running a stage moves nothing;
      * unallocation posts a reversing transfer (it cannot cancel a grouped
        entry other buckets depend on) and the pair nets to zero here.
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


def sold_in_qty(bucket_id, item_code, so_item, target):
    """Stems this line has EVER been sold into `target`, ignoring later outflow.

    `sold_qty` is net, which is what planning and unallocation need; but once
    packing moves stems out of the Sold warehouse the net drops to zero, and an
    OPL that has been packed must not read as un-allocated.
    """
    if not (bucket_id and so_item and target):
        return 0.0
    return flt(
        frappe.db.sql(
            f"""
            SELECT COALESCE(SUM(sed.qty), 0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.custom_issued_to = %(so_item)s
              AND {_bucket_expr()} = %(bucket)s
              AND sed.item_code = %(item)s
              AND sed.t_warehouse = %(wh)s
              AND se.docstatus = 1
            """,
            {"wh": target, "bucket": bucket_id, "item": item_code, "so_item": so_item},
        )[0][0]
    )


def default_cost_center(company):
    """A cost center for the stock lines.

    Sites with perpetual inventory reject a Stock Entry whose lines have none,
    and `Company.cost_center` is not always filled in. Where upande_packhouse
    is installed its own `apply_greenhouse_cost_center` hook overrides this on
    validate; this is only the floor.
    """
    cost_center = frappe.db.get_value("Company", company, "cost_center")
    if cost_center:
        return cost_center
    abbr = frappe.db.get_value("Company", company, "abbr")
    for candidate in (f"Main - {abbr}", f"{company} - {abbr}"):
        if frappe.db.exists("Cost Center", candidate):
            return candidate
    return frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")


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

    total = sum(flt(line["qty"]) for line in lines)
    available = on_hand(item_code, source)
    if available < total - QTY_TOLERANCE:
        buckets = ", ".join(str(line.get("bucket_id")) for line in lines)
        frappe.throw(
            f"{item_code} {stem_length or ''}: {total} needed in {source}, "
            f"{available} on hand (buckets {buckets})"
        )

    company = frappe.db.get_value("Warehouse", source, "company")
    cost_center = default_cost_center(company)
    purpose = (
        frappe.db.get_value("Stock Entry Type", entry_type, "purpose")
        or "Material Transfer"
    )
    buckets = {line.get("bucket_id") for line in lines}

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
            "custom_bucket_id": lines[0]["bucket_id"] if len(buckets) == 1 else None,
            "farm": farm,
            "business_unit": business_unit,
            "custom_stem_length": stem_length,
            "custom_issued_to": so_item,
            "custom_opl_scanned": opl,
            "remarks": remarks,
            "cost_center": cost_center,
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
                "cost_center": cost_center,
                "allow_zero_valuation_rate": 1,  # harvest stock is valued at 0
            },
        )
    se.insert(ignore_permissions=True)
    se.submit()
    return se.name


# ============================================================
# PLAN / POST
# ============================================================
def plan_moves(rows, business_unit, upto=SALE_STAGE, only_stage=None):
    """Work out every leg each row needs, without posting anything.

    A row is {bucket_id, item_code, qty, source, stem_length, farm, so_item,
    opl, graded, remarks}. `upto` is the last pipeline stage to walk;
    `only_stage` narrows it to that single leg. Returns (plans, skipped);
    `plans` are posted in hop order by `post_plans`.
    """
    plans, skipped = [], []

    for row in rows:
        qty = flt(row.get("qty"))
        bucket_id = row.get("bucket_id")
        item_code = row.get("item_code")
        if qty <= 0 or not bucket_id or not item_code:
            continue

        for depth, hop in enumerate(
            resolve_route(
                row["source"], business_unit, graded=row.get("graded", True), upto=upto
            )
        ):
            if only_stage and hop["stage"] != only_stage:
                continue

            so_item = row.get("so_item")

            if hop["terminal"]:
                # The sale: move what this line has not been sold yet.
                move = qty - sold_qty(bucket_id, item_code, so_item, hop["to"])
                if move <= QTY_TOLERANCE:
                    skipped.append({**hop, "bucket_id": bucket_id, "reason": "already sold"})
                    continue
            elif hop["stage"] == ARRIVAL_STAGE:
                # Pre-sale: the truck moves the bucket, not an order's share of
                # it, so the bucket's own balance is the right cap.
                here = bucket_balance(bucket_id, item_code, hop["from"])
                if here <= QTY_TOLERANCE:
                    skipped.append(
                        {**hop, "bucket_id": bucket_id, "reason": "bucket already moved"}
                    )
                    continue
                move = min(qty, here)
            else:
                # Packing / Dispatch / Loading: only this ORDER's stems may move
                # on. A Sold warehouse can hold the same bucket for two orders,
                # and the bucket-wide balance would let one order pack the
                # other's stems. It is also what makes these stages idempotent:
                # once moved, this order's balance at the source is zero.
                here = sold_qty(bucket_id, item_code, so_item, hop["from"])
                if here <= QTY_TOLERANCE:
                    skipped.append(
                        {**hop, "bucket_id": bucket_id,
                         "reason": f"nothing of this order at {hop['from']}"}
                    )
                    continue
                move = min(qty, here)

            plans.append(
                {
                    **hop,
                    "depth": depth,
                    "stage": hop["stage"],
                    "bucket_id": bucket_id,
                    "item_code": item_code,
                    "qty": move,
                    "stem_length": row.get("stem_length"),
                    "farm": row.get("farm"),
                    # Every leg from the sale onwards belongs to one order; the
                    # pre-sale Arrival leg is order-agnostic (the truck brings the
                    # whole bucket, nobody has bought it yet).
                    "so_item": None if hop["stage"] == ARRIVAL_STAGE else row.get("so_item"),
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

    route = resolve_route(source_warehouse, business_unit, upto=ARRIVAL_STAGE)
    row = {
        "bucket_id": bucket_id,
        "item_code": item_code,
        "qty": flt(qty),
        "source": source_warehouse,
        "stem_length": stem_length,
        "farm": farm,
        "remarks": f"Arrived {farm} packhouse" if farm else "Arrived packhouse",
    }
    plans, skipped = plan_moves([row], business_unit, upto=ARRIVAL_STAGE)
    posted = post_plans(plans, business_unit)

    return {
        "warehouse": route[-1]["to"] if route else source_warehouse,
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


# ============================================================
# EVENTS 3..5 — PACKING, DISPATCH, LOADING
# ============================================================
@frappe.whitelist()
def advance_opl(opl_name, stage):
    """Move an OPL's stems one stage further down the pipeline.

    Stage is "Packing" (Sold -> packhouse), "Dispatch" (packhouse -> dispatch
    cold store) or "Loading" (dispatch -> delivery truck); the targets come from
    the order's `SO Warehouse Mapping` row, so each business unit routes itself.

    A leg only moves stems the previous stage actually delivered — the planner
    reads each bucket's own balance in the source warehouse — so packing an OPL
    whose allocation never posted moves nothing instead of inventing stock.
    """
    if stage not in STAGE_NAMES or stage in (ARRIVAL_STAGE, SALE_STAGE):
        frappe.throw(
            f"'{stage}' is not a post-sale stage — expected one of "
            + ", ".join(s for s in STAGE_NAMES if s not in (ARRIVAL_STAGE, SALE_STAGE))
        )
    if not frappe.has_permission("Stock Entry", "submit"):
        frappe.throw(
            "Not permitted to move stock between warehouses", frappe.PermissionError
        )

    opl = frappe.get_doc("Order Pick List", opl_name)
    business_unit = opl_business_unit(opl)

    rows = []
    for row in opl_rows(opl):
        bucket = _row_bucket(row)
        source = _row_warehouse(row) or receiving_warehouse(bucket, row.item_code)
        if not (bucket and source):
            continue
        rows.append(
            {
                "bucket_id": bucket,
                "item_code": row.item_code,
                "qty": flt(row.stock_qty),
                "source": source,
                "stem_length": row.get("stem_length") or row.get("custom_stem_length"),
                "farm": row.get("farm") or opl.get("farm"),
                "so_item": _row_so_item(row),
                "opl": opl_name,
                "remarks": f"{stage} — {opl_name}",
            }
        )

    plans, skipped = plan_moves(rows, business_unit, upto=stage, only_stage=stage)
    return {"stage": stage, "posted": post_plans(plans, business_unit), "skipped": skipped}


def reverse_allocation_movement(sales_order_item, bucket_id=None, item_code=None,
                                business_unit=None):
    """Send unallocated stems back out of the Sold warehouse.

    A grouped entry carries several buckets, so cancelling it would reverse
    other buckets' sales too. Instead post a reversing transfer for exactly the
    stems this line still holds; `sold_qty()` is net, so the pair cancels out.
    """
    if not business_unit:
        parent = frappe.db.get_value("Sales Order Item", sales_order_item, "parent")
        business_unit = frappe.db.get_value("Sales Order", parent, "business_unit") if parent else None

    # Every leg is a Material Transfer and every leg from the sale onwards
    # carries the SO Item, so the sale leg is identified by where it LANDED —
    # a Sold warehouse — not by the entry type. TYPE_TO_SOLD is still honoured
    # for entries posted before that changed.
    targets = sale_targets(business_unit) if business_unit else set()
    entries = [
        e
        for e in frappe.get_all(
            "Stock Entry",
            filters={"custom_issued_to": sales_order_item, "docstatus": 1},
            fields=["name", "stock_entry_type", "from_warehouse", "to_warehouse",
                    "business_unit", "custom_stem_length", "farm"],
        )
        if e.to_warehouse in targets or e.stock_entry_type == TYPE_TO_SOLD
    ]
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


def opl_business_unit(opl):
    """An Order Pick List does not always carry the business unit — some sites
    have neither field on the doctype — so fall back to its Sales Order."""
    business_unit = business_unit_of(opl)
    if business_unit or not opl.get("sales_order"):
        return business_unit
    for field in ("business_unit", "custom_business_unit"):
        if frappe.db.has_column("Sales Order", field):
            business_unit = frappe.db.get_value("Sales Order", opl.sales_order, field)
            if business_unit:
                return business_unit
    return None


def allocation_complete(opl_name):
    """An OPL is packable only once every row's stems sit in a Sold warehouse."""
    opl = frappe.get_doc("Order Pick List", opl_name)
    business_unit = opl_business_unit(opl)
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
        landed = sold_in_qty(bucket, row.item_code, _row_so_item(row), target)
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
            route = resolve_route(r.warehouse, business_unit, upto=ARRIVAL_STAGE)
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
