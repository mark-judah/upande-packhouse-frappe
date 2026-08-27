import frappe
from frappe import _
import json

# Buckets locked by an open (non-Rejected) Discard Request must never count as
# available or be allocated. Injected into the shelf-availability read paths so
# the allocation page agrees with the SO spec-autofill popup (single rule).
DISCARD_EXCLUSION = """
          AND si.bucket_id NOT IN (
              SELECT drb.bucket_id
              FROM `tabDiscard Request Bucket` drb
              INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
              WHERE COALESCE(dr.workflow_state, '') != 'Rejected'
                AND COALESCE(drb.bucket_id, '') != ''
          )"""

# ============================================================
# HELPER: Load Production Settings config once
# Returns: { discard_age, amber_time, farms_by_location, farm_config }
# farm_config: { farm_name: { sales_shelf, max_allocation_age } }
# farms_by_location: { location_name: [farm_name, ...] }
# ============================================================
def _get_production_config():
    ps = frappe.get_cached_doc("Production Settings")

    discard_age = ps.discard_age or 5
    amber_time = ps.amber_time or 3

    farm_config = {}       # farm -> { sales_shelf, max_allocation_age }
    farms_by_location = {} # location -> [farm, ...]

    if not ps.shelf_locations:
        return {
            "discard_age": discard_age,
            "amber_time": amber_time,
            "farm_config": {},
            "farms_by_location": {}
        }

    enabled_farms = [row.farm for row in ps.shelf_locations if row.enabled]

    if not enabled_farms:
        return {
            "discard_age": discard_age,
            "amber_time": amber_time,
            "farm_config": {},
            "farms_by_location": {}
        }

    # Build farm_config from shelf_locations
    for row in ps.shelf_locations:
        if row.enabled:
            farm_config[row.farm] = {
                "sales_shelf": int(row.sales_shelf or 0),
                "max_allocation_age": int(row.max_allocation_age or 5)
            }

    # Single query to get location for all enabled farms
    placeholders = ", ".join(["%s"] * len(enabled_farms))
    farm_rows = frappe.db.sql(f"""
        SELECT name AS farm, location AS location
        FROM `tabFarm`
        WHERE name IN ({placeholders})
          AND location IS NOT NULL
          AND location != ''
    """, enabled_farms, as_dict=True)

    for f in farm_rows:
        loc = f.location
        farms_by_location.setdefault(loc, []).append(f.farm)

    return {
        "discard_age": discard_age,
        "amber_time": amber_time,
        "farm_config": farm_config,
        "farms_by_location": farms_by_location
    }


# ============================================================
# HELPER: Location Configuration (for UI location buttons)
# ============================================================
@frappe.whitelist()
def get_location_config():
    config = _get_production_config()
    farms_by_location = config["farms_by_location"]
    farm_config = config["farm_config"]

    locations = []
    for loc_name, farms in farms_by_location.items():
        # The sales shelf farm for this location
        sales_farms = [f for f in farms if farm_config.get(f, {}).get("sales_shelf")]
        remote_farms = [f for f in farms if not farm_config.get(f, {}).get("sales_shelf")]

        locations.append({
            "name": loc_name,
            "farms": farms,
            "sales_farms": sales_farms,
            "remote_farms": remote_farms,
            # farm details for the frontend filter checkboxes
            "farm_details": [
                {
                    "farm": f,
                    "sales_shelf": farm_config.get(f, {}).get("sales_shelf", 0),
                    "max_allocation_age": farm_config.get(f, {}).get("max_allocation_age", 5)
                }
                for f in farms
            ]
        })

    return {"locations": locations}


# ============================================================
# HELPER: Get confirmed stems for SO items scoped to specific farms
# Returns: { so_item_name: confirmed_stems_total }
# ============================================================
def _get_confirmed_stems_for_farms(sales_order, farm_names):
    """
    Fetch confirmed stems from the Sales Order's custom_confirmed_stems_table
    and return totals per SO item, filtered to only the specified farms.
    
    Returns: dict { sales_order_item_name: total_confirmed_stems }
    """
    if not farm_names:
        return {}
    
    farm_placeholders = ", ".join(["%s"] * len(farm_names))
    
    rows = frappe.db.sql(f"""
        SELECT 
            cs.sales_order_item,
            cs.farm,
            cs.stems
        FROM `tabConfirmed Stems` cs
        WHERE cs.parent = %s
          AND cs.parenttype = 'Sales Order'
          AND cs.parentfield = 'custom_confirmed_stems_table'
          AND cs.farm IN ({farm_placeholders})
          AND cs.stems > 0
    """, [sales_order] + list(farm_names), as_dict=True)
    
    # Sum stems per SO item (multiple farms at the same location may have confirmed)
    confirmed_by_item = {}
    confirmed_detail = {}  # item -> [{farm, stems}]
    for r in rows:
        item_name = r["sales_order_item"]
        confirmed_by_item[item_name] = confirmed_by_item.get(item_name, 0) + (r["stems"] or 0)
        confirmed_detail.setdefault(item_name, []).append({
            "farm": r["farm"],
            "stems": r["stems"]
        })
    
    return confirmed_by_item, confirmed_detail


def _get_all_confirmed_stems(sales_order):
    """
    Get ALL confirmed stems for a Sales Order (across all farms).
    Returns: { so_item_name: total_confirmed_stems_all_farms }
    """
    rows = frappe.db.sql("""
        SELECT 
            cs.sales_order_item,
            SUM(cs.stems) AS total_stems
        FROM `tabConfirmed Stems` cs
        WHERE cs.parent = %s
          AND cs.parenttype = 'Sales Order'
          AND cs.parentfield = 'custom_confirmed_stems_table'
          AND cs.stems > 0
        GROUP BY cs.sales_order_item
    """, [sales_order], as_dict=True)
    
    return {r["sales_order_item"]: r["total_stems"] or 0 for r in rows}


# ============================================================
# PENDING SALES ORDERS
# ============================================================
@frappe.whitelist()
def get_pending_sales_orders(start_date=None, end_date=None, delivery_start=None, delivery_end=None):
    date_conditions = [
        "so.docstatus = 1",
        "so.status NOT IN ('Completed', 'Closed', 'Cancelled')"
    ]

    if start_date and end_date:
        date_conditions.append(f"so.transaction_date BETWEEN '{start_date}' AND '{end_date}'")
    elif start_date:
        date_conditions.append(f"so.transaction_date >= '{start_date}'")
    elif end_date:
        date_conditions.append(f"so.transaction_date <= '{end_date}'")
    else:
        date_conditions.append("so.transaction_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)")

    if delivery_start and delivery_end:
        date_conditions.append(f"so.delivery_date BETWEEN '{delivery_start}' AND '{delivery_end}'")
    elif delivery_start:
        date_conditions.append(f"so.delivery_date >= '{delivery_start}'")
    elif delivery_end:
        date_conditions.append(f"so.delivery_date <= '{delivery_end}'")

    where_clause = " AND ".join(date_conditions)

    sql = f"""
        WITH so_stats AS (
            SELECT
                so.name AS so_name,
                COUNT(soi.name) AS total_items,
                COUNT(CASE WHEN opl.docstatus = 1 THEN 1 END) AS submitted_items,
                SUM(soi.stock_qty) AS ordered_stems,
                -- How far the order is allocated, by STEMS (allocated / ordered).
                -- Stem-based (not submitted-OPL-count) so remote-farm allocations,
                -- which sit in-transit before their OPL is submitted, still register.
                ROUND(
                    LEAST(100, COALESCE(MAX(alloc.allocated), 0) * 100.0
                        / NULLIF(SUM(soi.stock_qty), 0)),
                    1
                ) AS allocation_percentage,
                GROUP_CONCAT(soi.item_code ORDER BY soi.idx SEPARATOR ', ') AS item_codes,
                GROUP_CONCAT(DISTINCT NULLIF(TRIM(soi.custom_length), '') ORDER BY soi.custom_length SEPARATOR ', ') AS lengths,
                GROUP_CONCAT(DISTINCT NULLIF(TRIM(it.item_group), '') SEPARATOR ', ') AS item_groups,
                MAX(CASE WHEN soi.custom_mixed_box = 1 THEN 1 ELSE 0 END) AS has_mixed,
                MAX(CASE WHEN (soi.custom_mixed_box = 0 OR soi.custom_mixed_box IS NULL) THEN 1 ELSE 0 END) AS has_straight
            FROM `tabSales Order` so
            INNER JOIN `tabSales Order Item` soi ON so.name = soi.parent
            LEFT JOIN `tabOrder Pick List` opl ON soi.custom_opl = opl.name
            LEFT JOIN `tabItem` it ON it.name = soi.item_code
            LEFT JOIN (
                SELECT soi2.parent AS so_name, SUM(ba.quantity_allocated) AS allocated
                FROM `tabBucket Allocations` ba
                INNER JOIN `tabSales Order Item` soi2 ON soi2.name = ba.sales_order_item
                WHERE ba.cancelled = 0
                GROUP BY soi2.parent
            ) alloc ON alloc.so_name = so.name
            WHERE {where_clause}
            GROUP BY so.name
        )
        SELECT
            so.name, so.customer, so.transaction_date, so.delivery_date,
            so.custom_order_name, so.grand_total AS total, so.currency,
            so.status, so.custom_priority,
            ss.total_items, ss.submitted_items, ss.allocation_percentage, ss.item_codes,
            ss.lengths, ss.item_groups, ss.has_mixed, ss.has_straight
        FROM `tabSales Order` so
        INNER JOIN so_stats ss ON so.name = ss.so_name
        ORDER BY so.delivery_date ASC, so.transaction_date DESC
    """

    try:
        return frappe.db.sql(sql, as_dict=True)
    except Exception as e:
        frappe.log_error("Pending SOs Error", frappe.get_traceback())
        frappe.throw(_("Error loading sales orders: {0}").format(str(e)))


@frappe.whitelist()
def get_order_filter_options():
    """Complete option lists for the order-list Length and Item-group filters.

    Fetched from the masters (Stem Length, and the item groups ever ordered) rather
    than from whatever orders are on screen — so every value is selectable even when
    no currently-loaded order uses it.
    """
    # All stem lengths from the master, sorted by their numeric (cm) value.
    lengths = [r["name"] for r in frappe.db.sql("""
        SELECT name FROM `tabStem Length`
        ORDER BY CAST(REGEXP_REPLACE(name, '[^0-9]', '') AS UNSIGNED), name
    """, as_dict=True)]

    # Item groups of every item that has ever been ordered (keeps the list complete
    # but relevant — no Consumable / Chemical-Mix noise from unrelated groups).
    item_groups = [r["item_group"] for r in frappe.db.sql("""
        SELECT DISTINCT it.item_group
        FROM `tabItem` it
        INNER JOIN `tabSales Order Item` soi ON soi.item_code = it.name
        WHERE it.item_group IS NOT NULL AND TRIM(it.item_group) <> ''
        ORDER BY it.item_group
    """, as_dict=True)]

    return {"lengths": lengths, "item_groups": item_groups}


# ============================================================
# GET SO ITEMS + AVAILABLE BUCKETS
# UPDATED: Exclude in_transit buckets from availability
# ============================================================
@frappe.whitelist()
def get_sales_order_items_with_buckets(sales_order, location=None, selected_farms=None,
                                       filter_headsize=None, filter_color=None,
                                       filter_cut_stage=None):
    if not sales_order:
        frappe.throw(_("Sales Order is required"))

    if isinstance(selected_farms, str):
        selected_farms = json.loads(selected_farms)

    headsize_list = []
    if filter_headsize:
        if isinstance(filter_headsize, str):
            headsize_list = [h.strip() for h in filter_headsize.split(',') if h.strip()]
        elif isinstance(filter_headsize, list):
            headsize_list = filter_headsize

    color_list = []
    if filter_color:
        if isinstance(filter_color, str):
            color_list = [c.strip() for c in filter_color.split(',') if c.strip()]
        elif isinstance(filter_color, list):
            color_list = filter_color

    cut_stage_list = []
    if filter_cut_stage:
        if isinstance(filter_cut_stage, str):
            cut_stage_list = [s.strip() for s in filter_cut_stage.split(',') if s.strip()]
        elif isinstance(filter_cut_stage, list):
            cut_stage_list = filter_cut_stage

    config = _get_production_config()
    discard_age = config["discard_age"]
    amber_time = config["amber_time"]
    farm_config = config["farm_config"]
    farms_by_location = config["farms_by_location"]

    location_farms = farms_by_location.get(location, []) if location else []

    if not location_farms:
        frappe.throw(_("No enabled farms found for location: {0}").format(location))

    if selected_farms:
        active_farms = [f for f in selected_farms if f in location_farms]
    else:
        active_farms = [f for f in location_farms if farm_config.get(f, {}).get("sales_shelf")]

    if not active_farms:
        active_farms = location_farms

    farm_max_age = {f: farm_config.get(f, {}).get("max_allocation_age", 5) for f in active_farms}

    confirmed_by_item, confirmed_detail = _get_confirmed_stems_for_farms(
        sales_order, location_farms
    )

    items = frappe.db.sql("""
        SELECT
            soi.name AS sales_order_item,
            soi.item_code,
            soi.item_name,
            soi.qty,
            soi.conversion_factor,
            soi.uom,
            soi.stock_uom,
            soi.custom_length AS required_length,
            soi.stock_qty AS original_stock_qty,
            soi.custom_ordered_quantity AS original_ordered_qty,
            soi.custom_mixed_box,
            soi.custom_mix_group,
            soi.custom_mix_name,
            soi.custom_mixed_bunch,
            soi.custom_bunch_group
        FROM `tabSales Order Item` soi
        WHERE soi.parent = %s
        ORDER BY soi.idx
    """, [sales_order], as_dict=True)

    if not items:
        return []

    all_confirmed = _get_all_confirmed_stems(sales_order)

    filtered_items = []
    for item in items:
        so_item_name = item["sales_order_item"]
        my_confirmed = confirmed_by_item.get(so_item_name, 0)
        total_all_confirmed = all_confirmed.get(so_item_name, 0)
        others_confirmed = total_all_confirmed - my_confirmed
        ordered_qty = item["original_ordered_qty"] or item["original_stock_qty"] or 0

        if my_confirmed > 0:
            item["pending_stock_qty"] = my_confirmed
            item["confirmed_stems"] = my_confirmed
            item["confirmed_detail"] = confirmed_detail.get(so_item_name, [])
            item["total_all_confirmed"] = total_all_confirmed
            item["others_confirmed"] = others_confirmed
            item["balance_available"] = max(0, ordered_qty - total_all_confirmed)
            filtered_items.append(item)
        elif not all_confirmed:
            item["pending_stock_qty"] = ordered_qty
            item["confirmed_stems"] = 0
            item["confirmed_detail"] = []
            item["total_all_confirmed"] = 0
            item["others_confirmed"] = 0
            item["balance_available"] = ordered_qty
            filtered_items.append(item)

    items = filtered_items

    if not items:
        return []

    item_codes = list({i["item_code"] for i in items})

    ic_placeholders = ", ".join(["%s"] * len(item_codes))
    item_metadata = frappe.db.sql(f"""
        SELECT
            name AS item_code,
            custom_headsize_cm AS headsize,
            custom_color AS color
        FROM `tabItem`
        WHERE name IN ({ic_placeholders})
    """, item_codes, as_dict=True)

    metadata_map = {row["item_code"]: row for row in item_metadata}

    if headsize_list or color_list:
        filtered = []
        for item in items:
            meta = metadata_map.get(item["item_code"], {})
            item_headsize = str(meta.get("headsize", "")).strip()
            item_color = str(meta.get("color", "")).strip()

            headsize_match = not headsize_list or item_headsize in headsize_list
            color_match = not color_list or item_color in color_list

            if headsize_match and color_match:
                filtered.append(item)

        items = filtered

        if not items:
            return []

        item_codes = list({i["item_code"] for i in items})

    ic_placeholders = ", ".join(["%s"] * len(item_codes))
    farm_placeholders = ", ".join(["%s"] * len(active_farms))

    # ── UPDATED: Exclude in_transit buckets from availability ──
    buckets = frappe.db.sql(f"""
        SELECT
            si.bucket_id,
            si.variety AS item_code,
            si.stem_length,
            COALESCE(si.stem_qty, 0) AS total_qty,
            si.warehouse,
            COALESCE(si.harvest_date, si.date_added) AS harvest_date,
            s.name AS shelf_location,
            s.farm AS shelf_farm,
            DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added)) AS age_days,
            COALESCE(bas.allocated_quantity, 0) AS allocated_qty,
            COALESCE(si.stem_qty, 0) - COALESCE(bas.allocated_quantity, 0) AS available_qty,
            COALESCE(si.cut_stage, '') AS cut_stage,
            COALESCE(bas.in_transit, 0) AS in_transit
        FROM `tabShelf Item` si
        INNER JOIN `tabShelf` s ON s.name = si.parent
        LEFT JOIN `tabBucket Allocation Status` bas
            ON bas.bucket_id = si.bucket_id
            AND bas.item_code = si.variety
        WHERE s.farm IN ({farm_placeholders})
          AND si.variety IN ({ic_placeholders})
          AND DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added)) < %s
          AND (bas.in_transit = 0 OR bas.in_transit IS NULL)
          {DISCARD_EXCLUSION}
    """, active_farms + item_codes + [discard_age], as_dict=True)

    buckets = [
        b for b in buckets
        if b["age_days"] <= farm_max_age.get(b["shelf_farm"], 5)
    ]

    # ── Apply cut_stage filter to buckets ──
    if cut_stage_list:
        buckets = [
            b for b in buckets
            if str(b.get("cut_stage", "")).strip() in cut_stage_list
        ]

    so_item_names = [i["sales_order_item"] for i in items]
    si_placeholders = ", ".join(["%s"] * len(so_item_names))

    allocated_per_item = frappe.db.sql(f"""
        SELECT
            ba.sales_order_item,
            SUM(ba.quantity_allocated) AS total_allocated
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item IN ({si_placeholders})
          AND ba.cancelled = 0
        GROUP BY ba.sales_order_item
    """, so_item_names, as_dict=True)

    allocated_dict = {row["sales_order_item"]: row["total_allocated"] or 0 for row in allocated_per_item}

    per_bucket_per_item = {}
    if buckets:
        bucket_ids = list({b["bucket_id"] for b in buckets})
        bid_placeholders = ", ".join(["%s"] * len(bucket_ids))

        result = frappe.db.sql(f"""
            SELECT
                bas.bucket_id,
                ba.sales_order_item,
                SUM(ba.quantity_allocated) AS allocated_to_this_item
            FROM `tabBucket Allocations` ba
            INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
            WHERE ba.sales_order_item IN ({si_placeholders})
              AND bas.bucket_id IN ({bid_placeholders})
              AND ba.cancelled = 0
            GROUP BY bas.bucket_id, ba.sales_order_item
        """, so_item_names + bucket_ids, as_dict=True)

        for row in result:
            per_bucket_per_item[(row["bucket_id"], row["sales_order_item"])] = row["allocated_to_this_item"] or 0

    buckets_by_item = {}
    for b in buckets:
        buckets_by_item.setdefault(b["item_code"], []).append(b)

    for item in items:
        meta = metadata_map.get(item["item_code"], {})
        item["headsize"] = meta.get("headsize", "")
        item["color"] = meta.get("color", "")

        item_buckets = buckets_by_item.get(item["item_code"], [])
        req_cm = _parse_cm(item["required_length"])

        exact = []
        downgrade = []

        for b in item_buckets:
            b_cm = _parse_cm(b["stem_length"])
            is_sales_shelf = farm_config.get(b["shelf_farm"], {}).get("sales_shelf", 0)

            if b_cm == req_cm and b_cm > 0:
                status = "exact"
            elif b_cm > req_cm and b_cm > 0:
                status = "downgrade"
            else:
                continue

            allocated_here = per_bucket_per_item.get((b["bucket_id"], item["sales_order_item"]), 0)

            entry = {
                "bucket_id": b["bucket_id"],
                "stem_length": b["stem_length"],
                "total_qty": b["total_qty"],
                "allocated_qty": b["allocated_qty"],
                "available_qty": b["available_qty"],
                "allocated_to_this_item": allocated_here,
                "age_days": b["age_days"],
                "shelf_location": b["shelf_location"],
                "shelf_farm": b["shelf_farm"],
                "harvest_date": b["harvest_date"],
                "warehouse": b["warehouse"],
                "cut_stage": b.get("cut_stage", ""),
                "length_status": status,
                "is_sales_shelf": is_sales_shelf,
                "awaiting_transfer": 0 if is_sales_shelf else 1,
                "downgrade_approval": (
                    "amber_expired" if (b["age_days"] or 0) >= amber_time
                    else "requires_approval"
                ) if status == "downgrade" else ""
            }

            if status == "exact":
                exact.append(entry)
            else:
                downgrade.append(entry)

        farm_exact_qty = {}
        for b in exact:
            farm_exact_qty[b["shelf_farm"]] = farm_exact_qty.get(b["shelf_farm"], 0) + (b["available_qty"] or 0)

        preferred_farm = max(farm_exact_qty, key=farm_exact_qty.get) if farm_exact_qty else None

        def sort_key(b):
            is_preferred = 0 if b["shelf_farm"] == preferred_farm else 1
            return (is_preferred, b["age_days"] * -1)

        exact.sort(key=sort_key)
        downgrade.sort(key=sort_key)

        compatible = exact + downgrade

        item["batches"] = compatible
        item["total_available_qty"] = sum(b["available_qty"] for b in compatible)
        item["total_allocated_qty"] = allocated_dict.get(item["sales_order_item"], 0)
        item["has_sufficient_stock"] = item["total_available_qty"] >= item["pending_stock_qty"]
        item["preferred_farm"] = preferred_farm
        item["amber_time"] = amber_time

    if items and location and active_farms:
        _attach_incoming_stems(items, location, active_farms)
    else:
        for item in items:
            item["incoming_exact_stems"] = 0

    return items

def _parse_cm(length_str):
    """Parse stem length string like '60cm' -> 60. Returns 0 on failure."""
    if not length_str:
        return 0
    try:
        return int(str(length_str).lower().replace("cm", "").strip())
    except:
        return 0


def _attach_incoming_stems(items, location, active_farms):
    """Attach incoming (received but not yet shelved) stem counts to each item."""
    item_length_map = {}
    for item in items:
        if item.get("required_length") and item.get("item_code"):
            key = (item["item_code"], item["required_length"])
            item_length_map.setdefault(key, []).append(item)

    if not item_length_map:
        for item in items:
            item["incoming_exact_stems"] = 0
        return

    item_codes_for_incoming = list({k[0] for k in item_length_map})
    lengths_for_incoming = list({k[1] for k in item_length_map})

    ic_placeholders = ", ".join(["%s"] * len(item_codes_for_incoming))
    ln_placeholders = ", ".join(["%s"] * len(lengths_for_incoming))
    farm_ph = ", ".join(["%s"] * len(active_farms))

    unshelved = frappe.db.sql(f"""
        SELECT
            se.custom_bucket_id AS bucket_id,
            sei.item_code,
            se.custom_stem_length AS stem_length,
            sei.qty
        FROM `tabStock Entry` se
        INNER JOIN `tabStock Entry Detail` sei ON se.name = sei.parent
        WHERE se.stock_entry_type IN ('Receiving', 'Late Receipt')
          AND se.docstatus = 1
          AND se.custom_farm IN ({farm_ph})
          AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 5 DAY)
          AND sei.item_code IN ({ic_placeholders})
          AND se.custom_stem_length IN ({ln_placeholders})
          AND se.custom_bucket_id IS NOT NULL
          AND se.custom_bucket_id != ''
          AND NOT EXISTS (
              SELECT 1 FROM `tabShelf Item` si
              WHERE si.bucket_id = se.custom_bucket_id
          )
    """, active_farms + item_codes_for_incoming + lengths_for_incoming, as_dict=True)

    incoming_map = {}
    if unshelved:
        bucket_ids = list({r["bucket_id"] for r in unshelved})
        bid_placeholders = ", ".join(["%s"] * len(bucket_ids))

        issued_rows = frappe.db.sql(f"""
            SELECT DISTINCT custom_bucket_id
            FROM `tabStock Entry`
            WHERE stock_entry_type = 'Harvesting'
              AND docstatus = 1
              AND custom_bucket_id IN ({bid_placeholders})
              AND custom_issued_to IS NOT NULL
              AND custom_issued_to != ''
        """, bucket_ids)

        issued_set = {r[0] for r in issued_rows}

        for r in unshelved:
            if r["bucket_id"] not in issued_set:
                key = (r["item_code"], r["stem_length"])
                incoming_map[key] = incoming_map.get(key, 0) + (r["qty"] or 0)

    for item in items:
        key = (item.get("item_code"), item.get("required_length"))
        item["incoming_exact_stems"] = incoming_map.get(key, 0)


# ============================================================
# NEW: Get available headsize and color options for current SO
# ============================================================
@frappe.whitelist()
def get_available_filters(sales_order, location=None, selected_farms=None):
    """
    Returns available headsize and color values for items in the sales order
    that have confirmed stems at the selected location.
    """
    if not sales_order or not location:
        return {"headsizes": [], "colors": []}
    
    # Parse selected_farms
    if isinstance(selected_farms, str):
        selected_farms = json.loads(selected_farms)
    
    config = _get_production_config()
    farms_by_location = config["farms_by_location"]
    location_farms = farms_by_location.get(location, [])
    
    if not location_farms:
        return {"headsizes": [], "colors": []}
    
    # Get confirmed items for this location
    confirmed_by_item, _ = _get_confirmed_stems_for_farms(sales_order, location_farms)
    
    # Fetch SO items
    items = frappe.db.sql("""
        SELECT DISTINCT soi.item_code
        FROM `tabSales Order Item` soi
        WHERE soi.parent = %s
    """, [sales_order], as_dict=True)
    
    # Filter to only confirmed items
    all_confirmed = _get_all_confirmed_stems(sales_order)
    
    # Get item codes with confirmed stems or no confirmations exist
    confirmed_item_codes = []
    for item in items:
        # Check if this specific item has confirmed stems at this location
        # or if no confirmations exist at all
        has_local_confirmed = any(
            so_item for so_item in confirmed_by_item.keys()
            if frappe.db.get_value("Sales Order Item", so_item, "item_code") == item["item_code"]
        )
        
        if has_local_confirmed or not all_confirmed:
            confirmed_item_codes.append(item["item_code"])
    
    if not confirmed_item_codes:
        return {"headsizes": [], "colors": []}
    
    # Fetch unique headsize and color values
    ic_placeholders = ", ".join(["%s"] * len(confirmed_item_codes))
    
    metadata = frappe.db.sql(f"""
        SELECT DISTINCT
            custom_headsize_cm AS headsize,
            custom_color AS color
        FROM `tabItem`
        WHERE name IN ({ic_placeholders})
          AND (custom_headsize_cm IS NOT NULL OR custom_color IS NOT NULL)
    """, confirmed_item_codes, as_dict=True)
    
    headsizes = sorted(list({str(row["headsize"]).strip() 
                            for row in metadata 
                            if row["headsize"]}))
    colors = sorted(list({str(row["color"]).strip() 
                         for row in metadata 
                         if row["color"]}))
    
    return {
        "headsizes": headsizes,
        "colors": colors
    }


# ============================================================
# ALLOCATE STOCK
# UPDATED: Set in_transit = 1 for remote farm allocations
# ============================================================
# Teams allowed to be stamped onto an OPL's custom_team during allocation.
ALLOWED_ALLOCATION_TEAMS = {"Team A", "Team B", "Jamafa", "Eldama", "Bravo"}


@frappe.whitelist()
def allocate_stock_with_buckets(sales_order, allocations, location=None, teams=None):
    if isinstance(allocations, str):
        allocations = json.loads(allocations)

    if isinstance(teams, str):
        teams = json.loads(teams)
    teams = teams or {}

    if not sales_order or not allocations:
        frappe.throw(_("Sales Order and allocations required"))

    if not location:
        frappe.throw(_("Location parameter is required"))

    # ── Team is mandatory PER LINE: every Sales Order Item being allocated must have a
    #    valid team. It is written to custom_team on that item's OPL (see impl). ──
    for so_item in {a["sales_order_item"] for a in allocations}:
        t = teams.get(so_item)
        if not t:
            frappe.throw(_("A team is required for every item being allocated."))
        if not frappe.db.exists("Packing Teams", t):
            frappe.throw(_("Invalid team: {0}").format(t))

    try:
        return _allocate_stock_with_buckets_impl(sales_order, allocations, location, teams)
    except Exception:
        frappe.db.rollback()
        raise


def _allocate_stock_with_buckets_impl(sales_order, allocations, location, teams=None):
    config = _get_production_config()
    farm_config = config["farm_config"]
    farms_by_location = config["farms_by_location"]

    location_farms = set(farms_by_location.get(location, []))
    if not location_farms:
        frappe.throw(_("No farms configured for location: {0}").format(location))

    # Determine the sales shelf farm for this location
    sales_shelf_farm = None
    for farm in location_farms:
        if farm_config.get(farm, {}).get("sales_shelf"):
            sales_shelf_farm = farm
            break

    frappe.log_error(
        title="Incoming Allocation Request",
        message=f"SO: {sales_order}, Location: {location}\nSales Shelf Farm: {sales_shelf_farm}\nPayload: {json.dumps(allocations, indent=2)}"
    )

    so_doc = frappe.get_doc("Sales Order", sales_order)

    # ── Validate against confirmed stems ──
    confirmed_by_item, _ = _get_confirmed_stems_for_farms(sales_order, list(location_farms))
    
    alloc_totals_by_item = {}
    for a in allocations:
        so_item = a["sales_order_item"]
        alloc_totals_by_item[so_item] = alloc_totals_by_item.get(so_item, 0) + float(a.get("qty", 0))
    
    for so_item, new_qty in alloc_totals_by_item.items():
        confirmed = confirmed_by_item.get(so_item, 0)
        if confirmed > 0:
            existing_allocated = frappe.db.sql("""
                SELECT COALESCE(SUM(ba.quantity_allocated), 0)
                FROM `tabBucket Allocations` ba
                INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
                WHERE ba.sales_order_item = %s AND ba.cancelled = 0
            """, so_item)[0][0] or 0
            
            total_after = existing_allocated + new_qty
            if total_after > confirmed + 0.001:
                frappe.throw(
                    f"Cannot allocate {int(new_qty)} stems for item. "
                    f"This location confirmed {int(confirmed)} stems, "
                    f"already allocated {int(existing_allocated)}. "
                    f"Maximum remaining: {int(confirmed - existing_allocated)}"
                )

    # ── PRE-CHECK: Cross-location conflict ──
    so_item_ids = list({a["sales_order_item"] for a in allocations})
    si_placeholders = ", ".join(["%s"] * len(so_item_ids))

    existing_farm_rows = frappe.db.sql(f"""
        SELECT DISTINCT ba.sales_order_item, bas.shelf_farm
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item IN ({si_placeholders})
          AND ba.cancelled = 0
    """, so_item_ids, as_dict=True)

    for row in existing_farm_rows:
        if row["shelf_farm"] not in location_farms:
            frappe.throw(
                f"Cannot allocate: item {row['sales_order_item']} already has allocations "
                f"from farm '{row['shelf_farm']}' which is in a different location. "
                f"Please refresh and try again."
            )

    # ── Fetch all shelf items for allocated buckets in ONE query ──
    bucket_ids = list({a.get("bucket_id") for a in allocations if a.get("bucket_id")})
    bid_placeholders = ", ".join(["%s"] * len(bucket_ids))
    farm_placeholders = ", ".join(["%s"] * len(location_farms))

    shelf_rows = frappe.db.sql(f"""
        SELECT
            si.bucket_id,
            si.variety AS item_code,
            si.stem_qty,
            si.stem_length,
            si.warehouse,
            si.date_added,
            COALESCE(si.harvest_date, si.date_added) AS harvest_date,
            si.parent AS shelf_location,
            s.farm
        FROM `tabShelf Item` si
        INNER JOIN `tabShelf` s ON s.name = si.parent
        WHERE si.bucket_id IN ({bid_placeholders})
          AND s.farm IN ({farm_placeholders})
    """, bucket_ids + list(location_farms), as_dict=True)

    shelf_map = {(r["bucket_id"], r["item_code"]): r for r in shelf_rows}

    for a in allocations:
        key = (a.get("bucket_id"), a.get("item_code"))
        if key not in shelf_map:
            any_shelf = frappe.db.sql("""
                SELECT si.bucket_id, si.variety, s.farm
                FROM `tabShelf Item` si
                INNER JOIN `tabShelf` s ON s.name = si.parent
                WHERE si.bucket_id = %s
                LIMIT 1
            """, a.get("bucket_id"), as_dict=True)

            if any_shelf:
                actual = any_shelf[0]
                frappe.throw(
                    f"Bucket '{a['bucket_id']}' is on farm '{actual['farm']}' which is not in "
                    f"location '{location}'. Please refresh and try again."
                )
            else:
                frappe.throw(
                    f"Bucket '{a['bucket_id']}' not found on any shelf. "
                    f"It may have been moved. Please refresh and try again."
                )

    # ── Fetch all BAS records for these buckets in ONE query ──
    variety_list = list({a["item_code"] for a in allocations})
    var_placeholders = ", ".join(["%s"] * len(variety_list))

    bas_rows = frappe.db.sql(f"""
        SELECT name, bucket_id, item_code, total_quantity,
               allocated_quantity, available_quantity, in_transit
        FROM `tabBucket Allocation Status`
        WHERE bucket_id IN ({bid_placeholders})
          AND item_code IN ({var_placeholders})
    """, bucket_ids + variety_list, as_dict=True)

    bas_map = {(r["bucket_id"], r["item_code"]): r for r in bas_rows}

    # ── Group allocations by bucket ──
    alloc_by_bucket = {}
    for a in allocations:
        alloc_by_bucket.setdefault((a["bucket_id"], a["item_code"]), []).append(a)

    for (bucket_id, item_code), group in alloc_by_bucket.items():
        shelf = shelf_map[(bucket_id, item_code)]
        is_sales_shelf = farm_config.get(shelf["farm"], {}).get("sales_shelf", 0)
        
        # ── TRANSIT CHECK: Is this bucket on a remote farm? ──
        needs_transfer = (sales_shelf_farm and shelf["farm"] != sales_shelf_farm)

        bas_key = (bucket_id, item_code)

        if bas_key in bas_map:
            bas = frappe.get_doc("Bucket Allocation Status", bas_map[bas_key]["name"], for_update=True)
        else:
            bas = frappe.new_doc("Bucket Allocation Status")
            bas.bucket_id = bucket_id
            bas.item_code = item_code
            bas.total_quantity = float(shelf["stem_qty"] or 0)
            bas.stem_length = shelf["stem_length"] or ""
            bas.warehouse = shelf["warehouse"] or ""
            bas.harvest_date = shelf.get("harvest_date") or shelf["date_added"]
            bas.shelf_location = shelf["shelf_location"]
            bas.shelf_farm = shelf["farm"]
            bas.in_transit = 0
            bas.insert(ignore_permissions=True)
            bas.allocated_quantity = 0
            bas.available_quantity = bas.total_quantity

        current_available = float(bas.available_quantity or 0)
        requested = sum(float(a.get("qty", 0)) for a in group)

        if requested > current_available:
            frappe.throw(
                f"Over-allocation on bucket {bucket_id}: "
                f"{current_available} available, {requested} requested. "
                "Please refresh and try again."
            )

        existing_so_items = {row.sales_order_item for row in bas.bucket_allocations if not row.cancelled}
        for a in group:
            if a["sales_order_item"] in existing_so_items:
                continue
            bas.append("bucket_allocations", {
                "sales_order": sales_order,
                "sales_order_item": a["sales_order_item"],
                "quantity_allocated": float(a["qty"]),
                "cancelled": 0
            })

        non_cancelled = [r for r in bas.bucket_allocations if not r.cancelled]
        bas.allocated_quantity = sum(r.quantity_allocated for r in non_cancelled)
        bas.available_quantity = bas.total_quantity - bas.allocated_quantity
        
        # ── MARK IN TRANSIT if needs transfer ──
        if needs_transfer:
            bas.in_transit = 1
            bas.available_quantity = 0  # Lock entire bucket
            frappe.log_error(
                title="Bucket Marked In Transit",
                message=f"Bucket: {bucket_id}, Farm: {shelf['farm']}, "
                        f"Destination: {sales_shelf_farm}, Allocated: {bas.allocated_quantity}"
            )
        
        bas.save(ignore_permissions=True)

    # ── Update SO item flags based on actual cumulative coverage ──
    affected_so_items = {a["sales_order_item"] for a in allocations}
    for so_item in affected_so_items:
        frappe.db.set_value("Sales Order Item", so_item, {
            "custom_fully_allocated": 1 if _so_item_is_fully_allocated(so_item) else 0,
            "custom_stock_available": 1
        }, update_modified=False)

    # Capture which lines already had an OPL BEFORE this allocation (custom_opl is set
    # during creation below). The team stamp uses this to tell a freshly-created OPL
    # from a pre-existing one for first-allocation-wins.
    prior_opl_by_soi = {
        a["sales_order_item"]: frappe.db.get_value(
            "Sales Order Item", a["sales_order_item"], "custom_opl"
        )
        for a in allocations
    }

    # ── Create/update pick lists ──
    for a in allocations:
        shelf = shelf_map.get((a["bucket_id"], a["item_code"]), {})
        a["_shelf_farm"] = shelf.get("farm", "")
        a["_is_sales_shelf"] = farm_config.get(shelf.get("farm", ""), {}).get("sales_shelf", 0)

    pick_results = _create_pick_list(sales_order, allocations, so_doc, location, confirmed_by_item)

    # ── Stamp the per-line team onto each OPL this allocation created/updated.
    #    The team is taken from the Sales Order Item the OPL covers (straight boxes are
    #    one OPL per item, so each line keeps its own team). First allocation wins:
    #    only set custom_team when not already set, so a later allocation to an existing
    #    OPL never overwrites the team chosen on the first allocation. ──
    teams = teams or {}
    for r in pick_results:
        opl_name = r.get("name")
        if not opl_name:
            continue
        opl_soi = frappe.db.get_value(
            "Pick List Item", {"parent": opl_name}, "sales_order_item"
        )
        opl_team = teams.get(opl_soi)
        if not opl_team:
            continue
        # First-allocation-wins: if this line already had THIS OPL before the current
        # allocation, keep its team. Otherwise it is a freshly-created OPL, so write the
        # chosen team (overwriting the Select field's auto first-option value "Team A").
        if opl_name == prior_opl_by_soi.get(opl_soi):
            continue
        frappe.db.set_value(
            "Order Pick List", opl_name, "team", opl_team,
            update_modified=False
        )

    return {
        "success": True,
        "message": "Allocation completed successfully",
        "pick_list_results": pick_results
    }


# ============================================================
# PICK LIST CREATION
# ============================================================
def _create_pick_list(sales_order, allocations, so_doc, location, confirmed_by_item=None):
    straight = []
    mixed = []
    mixed_bunch = []

    for a in allocations:
        item = next((i for i in so_doc.items if i.name == a["sales_order_item"]), None)
        if not item:
            continue
        if item.get("custom_mixed_bunch"):
            mixed_bunch.append(a)
        elif item.custom_mixed_box:
            mixed.append(a)
        else:
            straight.append(a)

    results = []

    if straight:
        # Rule: one Sales Order Item = exactly one OPL for straight boxes.
        # Route per-SOI based on the existing OPL's state:
        #   - no OPL (or cancelled) → create a new one
        #   - draft OPL              → ORM append via _update_existing_pick_list
        #   - submitted OPL          → raw-SQL append via _append_rows_to_existing_opls
        by_soi = {}
        for a in straight:
            by_soi.setdefault(a["sales_order_item"], []).append(a)

        for soi, allocs in by_soi.items():
            existing_opl = frappe.db.get_value("Sales Order Item", soi, "custom_opl")
            parent_docstatus = None
            if existing_opl and frappe.db.exists("Order Pick List", existing_opl):
                parent_docstatus = frappe.db.get_value(
                    "Order Pick List", existing_opl, "docstatus"
                )

            if parent_docstatus == 0:
                name = _update_existing_pick_list(
                    existing_opl, allocs, so_doc,
                    location=location,
                    confirmed_by_item=confirmed_by_item,
                )
                status = "submitted" if frappe.db.get_value(
                    "Order Pick List", name, "docstatus"
                ) == 1 else "draft"
                results.append({"type": "straight", "status": status, "name": name})

            elif parent_docstatus == 1:
                for a in allocs:
                    a["_existing_opl"] = existing_opl
                updated = _append_rows_to_existing_opls(allocs, so_doc, location)
                for opl_name in updated:
                    results.append({"type": "straight", "status": "updated_existing", "name": opl_name})

            else:
                from upande_packhouse.server_scripts.create_straight_box_pick_list import create_straight_box_pick_list_for_allocated_items
                names = create_straight_box_pick_list_for_allocated_items(
                    so_doc, allocs, submit=True, location=location
                ) or []
                for opl_name in names:
                    status = "submitted" if frappe.db.get_value(
                        "Order Pick List", opl_name, "docstatus"
                    ) == 1 else "draft"
                    results.append({"type": "straight", "status": status, "name": opl_name})

    if mixed:
        from upande_packhouse.server_scripts.create_mixed_box_picklist import create_mixed_box_pick_list_for_allocated_items
        name = create_mixed_box_pick_list_for_allocated_items(
            sales_order_doc=so_doc,
            allocations=mixed,
            submit=True,
            location=location
        )
        status = "submitted" if name and frappe.db.get_value("Order Pick List", name, "docstatus") == 1 else "draft"
        results.append({"type": "mixed", "status": status, "name": name})

    if mixed_bunch:
        from upande_packhouse.server_scripts.create_mixed_bunch_picklist import create_mixed_bunch_pick_list_for_allocated_items
        name = create_mixed_bunch_pick_list_for_allocated_items(
            sales_order_doc=so_doc,
            allocations=mixed_bunch,
            submit=True,
            location=location
        )
        status = "submitted" if name and frappe.db.get_value("Order Pick List", name, "docstatus") == 1 else "draft"
        results.append({"type": "mixed_bunch", "status": status, "name": name})

    return results


def _append_rows_to_existing_opls(allocations, so_doc, location):
    """Append rows to existing submitted OPLs via direct SQL."""
    by_opl = {}
    for a in allocations:
        by_opl.setdefault(a["_existing_opl"], []).append(a)

    updated_opls = []

    for opl_name, allocs in by_opl.items():
        max_idx = frappe.db.sql(
            "SELECT COALESCE(MAX(idx), 0) FROM `tabPick List Item` WHERE parent = %s", opl_name
        )[0][0] or 0

        max_box_id = frappe.db.sql(
            "SELECT COALESCE(MAX(custom_box_id), 0) FROM `tabPick List Item` WHERE parent = %s", opl_name
        )[0][0] or 0

        alloc_bucket_ids = [a.get("bucket_id") for a in allocs if a.get("bucket_id")]
        shelf_lookup = _fetch_shelf_for_buckets(alloc_bucket_ids, [a["item_code"] for a in allocs])

        for alloc in allocs:
            so_item_name = alloc["sales_order_item"]
            so_item = next((i for i in so_doc.items if i.name == so_item_name), None)
            if not so_item:
                continue

            existing = frappe.db.exists("Pick List Item", {
                "parent": opl_name,
                "sales_order_item": so_item_name,
                "bucket": alloc.get("bucket_id")
            })
            if existing:
                continue

            conv = so_item.conversion_factor or 1
            qty_uom = alloc["qty"] / conv if conv > 0 else alloc["qty"]
            max_idx += 1
            max_box_id += 1

            shelf_str = shelf_lookup.get((alloc.get("bucket_id"), alloc["item_code"]), "")
            is_sales_shelf = alloc.get("_is_sales_shelf", 1)
            awaiting_transfer = 0 if is_sales_shelf else 1

            row_name = frappe.generate_hash(length=10)

            frappe.db.sql("""
                INSERT INTO `tabPick List Item` (
                    name, parent, parenttype, parentfield, idx, docstatus,
                    item_code, item_name, shelf, bucket,
                    custom_sale_order_item, farm,
                    source_warehouse, stem_length, transit_truck,
                    qty, stock_qty, picked_qty, stock_reserved_qty,
                    packrate, uom, conversion_factor,
                    stock_uom, delivered_qty,
                    custom_box_id,
                    sales_order_item,
                    custom_ready_for_packing, issued,
                    downgrade_reason,
                    available_stems_of_exact_length,
                    awaiting_transfer
                ) VALUES (
                    %(name)s, %(parent)s, 'Order Pick List', 'table_ytkc', %(idx)s, 1,
                    %(item_code)s, %(item_name)s, %(shelf)s, %(bucket)s,
                    %(so_item)s, %(farm)s,
                    %(warehouse)s, %(stem_length)s, %(truck)s,
                    %(qty)s, %(stock_qty)s, 0, 0,
                    %(packrate)s, %(uom)s, %(conv)s,
                    %(stock_uom)s, 0,
                    %(box_id)s,
                    %(so_item)s,
                    1, 0,
                    %(downgrade_reason)s,
                    %(available_exact_stems)s,
                    %(awaiting_transfer)s
                )
            """, {
                "name": row_name,
                "parent": opl_name,
                "farm": alloc.get("_shelf_farm") or "",
                "idx": max_idx,
                "item_code": alloc["item_code"],
                "item_name": so_item.item_name,
                "shelf": shelf_str,
                "bucket": alloc.get("bucket_id"),
                "so_item": so_item_name,
                "item_group": so_item.item_group or "",
                "warehouse": alloc.get("warehouse") or "",
                "stem_length": alloc.get("stem_length") or so_item.custom_length or "",
                "truck": so_item.get("custom_truck") or "",
                "qty": qty_uom,
                "stock_qty": alloc["qty"],
                "packrate": so_item.get("custom_packrate") or "",
                "uom": so_item.uom,
                "conv": conv,
                "stock_uom": so_item.stock_uom,
                "box_id": max_box_id,
                "sales_order": so_doc.name,
                "downgrade_reason": alloc.get("downgrade_reason") or "",
                "available_exact_stems": alloc.get("available_exact_stems") or 0,
                "awaiting_transfer": awaiting_transfer
            })

        new_total = frappe.db.sql(
            "SELECT COALESCE(SUM(stock_qty), 0) FROM `tabPick List Item` WHERE parent = %s", opl_name
        )[0][0] or 0

        # Only update the running total. We no longer forcibly reset docstatus to 0
        # when an awaiting-transfer row is appended — un-submitting a previously
        # submitted OPL loses the audit trail and breaks downstream flows. If the
        # OPL was already submitted, we leave it submitted; the central helper
        # handles future state transitions.
        frappe.db.sql(
            "UPDATE `tabOrder Pick List` SET custom_total_stems = %s, modified = NOW() WHERE name = %s",
            [new_total, opl_name],
        )

        updated_opls.append(opl_name)

    # Ask the central helper to submit any drafts that are now fully covered.
    # Idempotent for already-submitted OPLs.
    for opl_name in updated_opls:
        try:
            _try_submit_opl_if_complete(opl_name)
        except Exception as e:
            frappe.log_error(
                title="OPL auto-submit (append path)",
                message=f"OPL: {opl_name}\n{e}"
            )

    return updated_opls


def _fetch_shelf_for_buckets(bucket_ids, item_codes):
    """Returns dict: (bucket_id, item_code) -> shelf_name string"""
    if not bucket_ids:
        return {}

    unique_buckets = list(set(bucket_ids))
    unique_items = list(set(item_codes))
    b_placeholders = ", ".join(["%s"] * len(unique_buckets))
    i_placeholders = ", ".join(["%s"] * len(unique_items))

    rows = frappe.db.sql(f"""
        SELECT si.bucket_id, si.variety AS item_code, si.parent AS shelf
        FROM `tabShelf Item` si
        WHERE si.bucket_id IN ({b_placeholders})
          AND si.variety IN ({i_placeholders})
    """, unique_buckets + unique_items, as_dict=True)

    return {(r["bucket_id"], r["item_code"]): r["shelf"] for r in rows}


# ============================================================
# CENTRALIZED OPL SUBMISSION HELPER
# Single source of truth for "is this OPL ready to submit?"
# Called from every code path that creates or modifies an OPL.
# ============================================================
def _required_stems_for_so_item(so_item_name, confirmed_qty=None):
    """Required stems = confirmed_qty if > 0, else custom_ordered_quantity,
    else qty × conversion_factor."""
    if confirmed_qty is not None and confirmed_qty > 0:
        return float(confirmed_qty)
    so_item = frappe.get_doc("Sales Order Item", so_item_name)
    if so_item.custom_ordered_quantity:
        return float(so_item.custom_ordered_quantity)
    return float((so_item.qty or 0) * (so_item.conversion_factor or 1))


def _cumulative_allocated_stems(so_item_name):
    """SUM of all non-cancelled Bucket Allocations across the system for this SO item."""
    return frappe.db.sql("""
        SELECT COALESCE(SUM(ba.quantity_allocated), 0)
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item = %s
          AND (ba.cancelled = 0 OR ba.cancelled IS NULL)
    """, so_item_name)[0][0] or 0


def _so_item_is_fully_allocated(so_item_name, confirmed_qty=None):
    """Cumulative coverage check. Used by the submission helper."""
    allocated = float(_cumulative_allocated_stems(so_item_name))
    required = _required_stems_for_so_item(so_item_name, confirmed_qty)
    return allocated >= required - 0.001


def _try_submit_opl_if_complete(opl_name, confirmed_by_item=None):
    """Promote a draft OPL to submitted iff every required SO item is cumulatively
    fully allocated AND no row is awaiting transfer. Idempotent — safe to call
    repeatedly.

    For mixed-box OPLs the "required" set is every SOI in the mix group on the
    parent SO — not just the SOIs already on the OPL — so the OPL stays in draft
    until the whole mix group is allocated.
    For straight-box OPLs the required set is the SOIs currently on the OPL.

    Returns: True if submitted (or already submitted), False if left as draft.
    """
    if not opl_name:
        return False

    opl = frappe.get_doc("Order Pick List", opl_name)

    if opl.docstatus == 1:
        return True
    if opl.docstatus == 2:
        return False
    if not opl.table_ytkc:
        return False

    has_awaiting = any((loc.get("awaiting_transfer") or 0) for loc in opl.table_ytkc)

    opl_mix_group = opl.get("custom_mix_group")
    opl_bunch_group = opl.get("custom_bunch_group")
    if opl_mix_group:
        required_so_items = set(frappe.get_all(
            "Sales Order Item",
            filters={
                "parent": opl.sales_order,
                "custom_mix_group": opl_mix_group,
                "custom_mixed_box": 1,
            },
            pluck="name",
        ))
    elif opl_bunch_group:
        # Mixed-bunch OPL: the whole bunch group (every colour-line) must be
        # cumulatively allocated before the bouquet OPL may submit.
        required_so_items = set(frappe.get_all(
            "Sales Order Item",
            filters={
                "parent": opl.sales_order,
                "custom_bunch_group": opl_bunch_group,
                "custom_mixed_bunch": 1,
            },
            pluck="name",
        ))
    else:
        required_so_items = {loc.sales_order_item for loc in opl.table_ytkc if loc.sales_order_item}

    all_covered = True
    for so_item in required_so_items:
        confirmed = (confirmed_by_item or {}).get(so_item)
        if not _so_item_is_fully_allocated(so_item, confirmed_qty=confirmed):
            all_covered = False
            break

    if all_covered and not has_awaiting:
        opl.flags.ignore_permissions = True
        opl.submit()
        return True

    return False


def _check_fully_allocated(so_item_name, added_qty, confirmed_qty=None):
    """Check if a SO item is fully allocated."""
    if confirmed_qty is not None:
        required = confirmed_qty
    else:
        so_item = frappe.get_doc("Sales Order Item", so_item_name)
        required = so_item.custom_ordered_quantity or (so_item.qty * (so_item.conversion_factor or 1))

    allocated = frappe.db.sql("""
        SELECT COALESCE(SUM(ba.quantity_allocated), 0)
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item = %s
          AND (ba.cancelled = 0 OR ba.cancelled IS NULL)
    """, so_item_name)[0][0] or 0

    return (allocated + added_qty) >= required - 0.001


def _update_existing_pick_list(opl_name, new_allocations, so_doc, submit_if_complete=None, location=None, confirmed_by_item=None):
    # submit_if_complete is retained for backward-compat but ignored.
    # The central helper now decides submission based on cumulative coverage.
    opl = frappe.get_doc("Order Pick List", opl_name)
    if opl.docstatus != 0:
        frappe.throw(f"Cannot update submitted Pick List {opl_name}")

    alloc_bucket_ids = [a.get("bucket_id") for a in new_allocations if a.get("bucket_id")]
    alloc_item_codes = [a["item_code"] for a in new_allocations]
    shelf_lookup = _fetch_shelf_for_buckets(alloc_bucket_ids, alloc_item_codes)

    box_id_counter = max([loc.custom_box_id or 0 for loc in opl.table_ytkc], default=0) + 1
    affected_items = set()
    has_awaiting_transfer = any(not a.get("_is_sales_shelf") for a in new_allocations)

    for alloc in new_allocations:
        so_item_name = alloc["sales_order_item"]
        affected_items.add(so_item_name)

        so_item = next((i for i in so_doc.items if i.name == so_item_name), None)
        if not so_item:
            continue

        conv = so_item.conversion_factor or 1
        qty_uom = alloc["qty"] / conv if conv > 0 else alloc["qty"]

        if any(loc.bucket == alloc.get("bucket_id") and loc.sales_order_item == so_item_name for loc in opl.table_ytkc):
            continue

        shelf_str = shelf_lookup.get((alloc.get("bucket_id"), alloc["item_code"]), "")
        is_sales_shelf = alloc.get("_is_sales_shelf", 1)

        opl.append("table_ytkc", {
            "item_code": alloc["item_code"],
            "bucket": alloc.get("bucket_id"),
            "custom_sale_order_item": so_item_name,
            "item_name": so_item.item_name,
            "stock_uom": so_item.stock_uom,
            "uom": so_item.uom,
            "qty": qty_uom,
            "stock_qty": alloc["qty"],
            "conversion_factor": conv,
            "source_warehouse": alloc.get("warehouse"),
            "sales_order_item": so_item.name,
            "stem_length": alloc.get("stem_length") or so_item.custom_length,
            "transit_truck": so_item.get("custom_truck"),
            "custom_box_id": box_id_counter,
            "shelf": shelf_str,
            "farm": alloc.get("_shelf_farm") or "",
            "downgrade_reason": alloc.get("downgrade_reason") or "",
            "available_stems_of_exact_length": alloc.get("available_exact_stems") or 0,
            "awaiting_transfer": 0 if is_sales_shelf else 1,
            "custom_ready_for_packing": 1,
        })
        box_id_counter += 1

    total_stems = sum(loc.stock_qty for loc in opl.table_ytkc)
    opl.custom_total_stems = total_stems
    opl.save(ignore_permissions=True)

    # Central helper checks cumulative coverage across all OPLs/sessions and
    # promotes to submitted if every represented SO item is fully allocated.
    try:
        _try_submit_opl_if_complete(opl.name, confirmed_by_item=confirmed_by_item)
    except Exception as e:
        frappe.log_error(
            title="OPL auto-submit (update path)",
            message=f"OPL: {opl.name}\n{e}"
        )

    return opl.name


# ============================================================
# UNALLOCATE
# UPDATED: Clear in_transit when unallocating
# ============================================================
@frappe.whitelist()
def unallocate_bucket_from_opl(sales_order_item, bucket_id):
    frappe.db.begin()

    try:
        so_item = frappe.db.get_value(
            "Sales Order Item", sales_order_item,
            ["parent", "item_code", "qty", "conversion_factor"],
            as_dict=True
        )
        if not so_item:
            frappe.throw("Invalid sales order item")

        sales_order = so_item.parent
        item_code = so_item.item_code

        bas_name = frappe.db.get_value("Bucket Allocation Status", {
            "bucket_id": bucket_id,
            "item_code": item_code
        }, "name")

        bas_updated = False
        if bas_name:
            bas = frappe.get_doc("Bucket Allocation Status", bas_name, for_update=True)
            cancelled_any = False
            for row in bas.bucket_allocations:
                if row.sales_order_item == sales_order_item and not row.cancelled:
                    row.cancelled = 1
                    row.db_update()
                    cancelled_any = True

            if cancelled_any:
                non_cancelled = [r for r in bas.bucket_allocations if not r.cancelled]
                bas.allocated_quantity = sum(r.quantity_allocated for r in non_cancelled)
                bas.available_quantity = bas.total_quantity - bas.allocated_quantity
                
                # ── CLEAR IN_TRANSIT if no more allocations ──
                if not non_cancelled:
                    bas.in_transit = 0
                
                bas.flags.ignore_validate = True
                bas.flags.ignore_mandatory = True
                bas.flags.ignore_permissions = True
                bas.save(ignore_permissions=True)
                bas_updated = True

        opls = frappe.get_all("Order Pick List", filters={"sales_order": sales_order}, pluck="name")
        removed_from_opls = []
        opls_deleted = []

        for opl_name in opls:
            rows = frappe.db.sql("""
                SELECT name FROM `tabPick List Item`
                WHERE parent = %s AND sales_order_item = %s AND bucket = %s
            """, [opl_name, sales_order_item, bucket_id], as_dict=True)

            if not rows:
                continue

            for row in rows:
                frappe.db.sql("DELETE FROM `tabPick List Item` WHERE name = %s", row.name)

            removed_from_opls.append(opl_name)

        for opl_name in removed_from_opls:
            remaining_count = frappe.db.sql(
                "SELECT COUNT(*) FROM `tabPick List Item` WHERE parent = %s", opl_name
            )[0][0] or 0

            if remaining_count == 0:
                _force_delete_opl(opl_name)
                opls_deleted.append(opl_name)
            else:
                _reindex_opl_rows(opl_name)
                new_total = frappe.db.sql(
                    "SELECT COALESCE(SUM(stock_qty), 0) FROM `tabPick List Item` WHERE parent = %s", opl_name
                )[0][0] or 0
                frappe.db.sql(
                    "UPDATE `tabOrder Pick List` SET custom_total_stems = %s, modified = NOW() WHERE name = %s",
                    [new_total, opl_name]
                )

        remaining_allocated = frappe.db.sql("""
            SELECT COALESCE(SUM(ba.quantity_allocated), 0)
            FROM `tabBucket Allocations` ba
            INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
            WHERE ba.sales_order_item = %s AND ba.cancelled = 0
        """, sales_order_item)[0][0] or 0

        fully_unallocated = (remaining_allocated == 0)

        if fully_unallocated:
            frappe.db.set_value("Sales Order Item", sales_order_item, {
                "custom_fully_allocated": 0,
                "custom_stock_available": 0,
                "custom_opl": None
            }, update_modified=False)
        else:
            frappe.db.set_value("Sales Order Item", sales_order_item, {
                "custom_fully_allocated": 1 if _so_item_is_fully_allocated(sales_order_item) else 0
            }, update_modified=False)

        any_allocated = frappe.db.sql("""
            SELECT COUNT(*) FROM `tabSales Order Item`
            WHERE parent = %s AND custom_fully_allocated = 1
        """, sales_order)[0][0] or 0

        if any_allocated == 0:
            frappe.db.set_value("Sales Order", sales_order, "custom_stock_allocated", 0, update_modified=False)

        frappe.db.commit()

        return {
            "success": True,
            "message": "Unallocated successfully",
            "removed_from_opls": removed_from_opls,
            "opls_deleted": opls_deleted,
            "bas_updated": bas_updated,
            "remaining_allocated": remaining_allocated,
            "so_flags_reset": fully_unallocated
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error("Unallocation Failed", frappe.get_traceback())
        return {
            "success": False,
            "message": f"Unallocation failed and was rolled back: {str(e)}"
        }


def _force_delete_opl(opl_name):
    try:
        doc = frappe.get_doc("Order Pick List", opl_name)
        if doc.docstatus == 1:
            doc.flags.ignore_permissions = True
            doc.flags.ignore_validate = True
            doc.flags.ignore_links = True
            doc.cancel()
            frappe.db.commit()
        frappe.delete_doc(
            "Order Pick List", opl_name,
            force=True, ignore_permissions=True,
            ignore_on_trash=True, delete_permanently=True
        )
        frappe.db.commit()
    except Exception:
        frappe.log_error("OPL Deletion Failed", frappe.get_traceback())


def _reindex_opl_rows(opl_name):
    rows = frappe.db.sql(
        "SELECT name FROM `tabPick List Item` WHERE parent = %s ORDER BY idx", opl_name, as_dict=True
    )
    for i, row in enumerate(rows, 1):
        frappe.db.sql("UPDATE `tabPick List Item` SET idx = %s WHERE name = %s", [i, row.name])


# ============================================================
# HELPER: get farms for a location (used by frontend if needed)
# ============================================================
@frappe.whitelist()
def get_farms_for_location(location):
    config = _get_production_config()
    farm_config = config["farm_config"]
    farms = config["farms_by_location"].get(location, [])
    return [
        {
            "farm": f,
            "sales_shelf": farm_config.get(f, {}).get("sales_shelf", 0),
            "max_allocation_age": farm_config.get(f, {}).get("max_allocation_age", 5)
        }
        for f in farms
    ]


# ============================================================
# SUBSTITUTE VARIETY
# ============================================================
@frappe.whitelist()
def get_substitute_varieties(sales_order, sales_order_item, item_code, location=None,
                              color=None, headsize=None):
    """
    Returns available varieties that can substitute the current item.
    When color/headsize are provided, filters to matching varieties (recommended).
    When not provided, returns all varieties in the same item group with available stock.
    
    Returns: list of { item_code, item_name, color, headsize, available_qty }
    """
    if not sales_order or not sales_order_item:
        return []

    config = _get_production_config()
    farms_by_location = config["farms_by_location"]
    farm_config = config["farm_config"]
    discard_age = config["discard_age"]

    # Get the item group of the current item so we only suggest same-group varieties
    current_item = frappe.db.get_value("Item", item_code, 
        ["item_group", "custom_headsize_cm", "custom_color"], as_dict=True)
    
    if not current_item:
        return []

    item_group = current_item.get("item_group") or ""

    # Get active farms for this location
    location_farms = farms_by_location.get(location, []) if location else []
    if not location_farms:
        return []

    farm_placeholders = ", ".join(["%s"] * len(location_farms))

    # Build conditions for color/headsize filtering
    item_conditions = ["i.item_group = %s", "i.disabled = 0"]
    item_params = [item_group]

    if color:
        item_conditions.append("i.custom_color = %s")
        item_params.append(color)
    
    if headsize:
        item_conditions.append("i.custom_headsize_cm = %s")
        item_params.append(headsize)

    item_where = " AND ".join(item_conditions)

    # Find varieties that have stock on shelves at this location (exclude in_transit buckets)
    varieties = frappe.db.sql(f"""
        SELECT
            i.name AS item_code,
            i.item_name,
            i.custom_color AS color,
            i.custom_headsize_cm AS headsize,
            COALESCE(stock.available_qty, 0) AS available_qty
        FROM `tabItem` i
        LEFT JOIN (
            SELECT
                si.variety AS item_code,
                SUM(COALESCE(si.stem_qty, 0) - COALESCE(bas.allocated_quantity, 0)) AS available_qty
            FROM `tabShelf Item` si
            INNER JOIN `tabShelf` s ON s.name = si.parent
            LEFT JOIN `tabBucket Allocation Status` bas
                ON bas.bucket_id = si.bucket_id AND bas.item_code = si.variety
            WHERE s.farm IN ({farm_placeholders})
              AND DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added)) < %s
              AND (bas.in_transit = 0 OR bas.in_transit IS NULL)
              {DISCARD_EXCLUSION}
            GROUP BY si.variety
        ) stock ON stock.item_code = i.name
        WHERE {item_where}
        HAVING available_qty > 0 OR i.name = %s
        ORDER BY
            CASE WHEN i.name = %s THEN 0 ELSE 1 END,
            CASE WHEN i.custom_color = %s THEN 0 ELSE 1 END,
            CASE WHEN i.custom_headsize_cm = %s THEN 0 ELSE 1 END,
            available_qty DESC
        LIMIT 50
    """, location_farms + [discard_age] + item_params + [
        item_code, item_code,
        current_item.get("custom_color") or "",
        current_item.get("custom_headsize_cm") or ""
    ], as_dict=True)

    return varieties


@frappe.whitelist()
def substitute_variety(sales_order, sales_order_item, new_item_code):
    """
    Substitutes the variety on a Sales Order Item.
    Updates item_code and item_name on the SO item row.
    Only allowed if the item has no existing allocations.
    
    Returns: { success: True/False, message: str }
    """
    if not sales_order or not sales_order_item or not new_item_code:
        return {"success": False, "message": "Missing required parameters"}

    # Validate SO exists and is submitted
    so_doc = frappe.get_doc("Sales Order", sales_order)
    if so_doc.docstatus != 1:
        return {"success": False, "message": "Sales Order is not submitted"}

    # Find the SO item row
    so_item = None
    for item in so_doc.items:
        if item.name == sales_order_item:
            so_item = item
            break

    if not so_item:
        return {"success": False, "message": f"Sales Order Item {sales_order_item} not found"}

    old_item_code = so_item.item_code
    old_item_name = so_item.item_name

    if old_item_code == new_item_code:
        return {"success": False, "message": "New variety is the same as current variety"}

    # Check no existing allocations for this item
    existing_alloc = frappe.db.sql("""
        SELECT COALESCE(SUM(ba.quantity_allocated), 0) AS total
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item = %s AND ba.cancelled = 0
    """, sales_order_item)[0][0] or 0

    if existing_alloc > 0:
        return {
            "success": False,
            "message": f"Cannot substitute: {int(existing_alloc)} stems already allocated. "
                       f"Unallocate first before substituting."
        }

    # Check the item has an existing OPL — block if submitted
    existing_opl = frappe.db.get_value("Sales Order Item", sales_order_item, "custom_opl")
    if existing_opl:
        opl_status = frappe.db.get_value("Order Pick List", existing_opl, "docstatus")
        if opl_status == 1:
            return {
                "success": False,
                "message": f"Cannot substitute: item is on submitted pick list {existing_opl}. "
                           f"Cancel or remove from pick list first."
            }

    # Fetch new item details
    new_item = frappe.db.get_value("Item", new_item_code, 
        ["name", "item_name", "item_group", "stock_uom", "description"], as_dict=True)
    
    if not new_item:
        return {"success": False, "message": f"Item {new_item_code} not found"}

    # Update the Sales Order Item via direct SQL (since SO is submitted)
    try:
        frappe.db.sql("""
            UPDATE `tabSales Order Item`
            SET item_code = %s,
                item_name = %s,
                description = %s,
                modified = NOW()
            WHERE name = %s
        """, [new_item_code, new_item.item_name, new_item.description or new_item.item_name, sales_order_item])

        # Update SO modified timestamp
        frappe.db.sql("""
            UPDATE `tabSales Order`
            SET modified = NOW()
            WHERE name = %s
        """, [sales_order])

        frappe.db.commit()

        frappe.log_error(
            title="Variety Substitution",
            message=f"SO: {sales_order}, Item: {sales_order_item}\n"
                    f"Changed: {old_item_code} ({old_item_name}) → {new_item_code} ({new_item.item_name})"
        )

        return {
            "success": True,
            "message": f"Substituted {old_item_name} with {new_item.item_name}",
            "old_item_code": old_item_code,
            "new_item_code": new_item_code,
            "new_item_name": new_item.item_name
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error("Variety Substitution Failed", frappe.get_traceback())
        return {
            "success": False,
            "message": f"Substitution failed: {str(e)}"
        }