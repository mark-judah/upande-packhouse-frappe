# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse Analytics ("Insights") page API. One shared from_date/to_date
# range, five independent whitelisted panels:
#   getQualityChain       -- scouting pressure -> intake quarantine -> packhouse
#                            rejects -> corrective actions, the "field to vase"
#                            quality chain, tied together by real shared
#                            greenhouse codes and defect-name vocabulary.
#   getConsumablesStock   -- packaging/consumables stock value, consumption
#                            trend, and an estimated days-of-stock-remaining
#                            per item (no reorder points exist in this system,
#                            so it's derived from real usage rate instead).
#   getSalesOverview      -- currency mix, customer concentration risk,
#                            consignee/country geography, freight-agent volume.
#   getConversionData     -- ordered -> allocated -> packed stem funnel per order.
#   getDowngradeAnalysis  -- which stem lengths get downgraded most, and the trend.
#   getOrdersAtRisk       -- which open orders are at risk of being shorted, given
#                            what's really on the shelf right now (not date-ranged --
#                            a live snapshot, recomputed fresh on every call, so it
#                            updates the moment more of a variety/length gets shelved).
#   getAgeToAllocation    -- how long a bucket sits on the shelf before it actually
#                            gets picked into an order. Joins Order Pick List (the
#                            real allocation/pick event, via Pick List Item.bucket)
#                            back to Shelf Item.date_added (when it was shelved) --
#                            NOT the "Shelving Log" doctype, which exists but has
#                            every shelved_on field NULL across all 29 of its rows
#                            (unpopulated in practice; Shelf Item is the real,
#                            populated shelving record already trusted everywhere
#                            else in this app, e.g. sales_allocation.py).

import frappe
from frappe.utils import getdate, add_days, flt, cint

# Scouting Entry + its Pests/Diseases child tables are genuinely huge on this
# bench (2.8M / 1.5M / 600K rows) -- a 30-day window alone matches hundreds of
# thousands of child rows, and the GROUP BY/ORDER BY needed for "top pests" and
# "top diseases" cost several real seconds of MySQL time each (confirmed via
# EXPLAIN -- the plan already uses every relevant index; the cost is genuine
# data volume, not a bad query). None of this needs per-second freshness, so
# the whole Quality Chain response is cached for a few minutes per date range.
QUALITY_CHAIN_CACHE_TTL = 600


def _cache_key(prefix, *parts):
    return "upande_packhouse:analytics:" + prefix + ":" + ":".join(str(p) for p in parts)


def _date_range():
    fd = frappe.form_dict
    from_date = fd.get("from_date")
    to_date = fd.get("to_date")
    if not from_date or not to_date:
        to_date = frappe.utils.today()
        from_date = add_days(to_date, -30)
    return str(getdate(from_date)), str(getdate(to_date))


# ============================================================
# 1. FIELD-TO-VASE QUALITY CHAIN
# ============================================================
@frappe.whitelist()
def getQualityChain():
    from_date, to_date = _date_range()
    cache_key = _cache_key("quality_chain", from_date, to_date)

    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        frappe.response["message"] = cached
        return

    result = _compute_quality_chain(from_date, to_date)
    frappe.cache().set_value(cache_key, result, expires_in_sec=QUALITY_CHAIN_CACHE_TTL)
    frappe.response["message"] = result


def _compute_quality_chain(from_date, to_date):
    # ── Intake quarantine rate by greenhouse (Quality Reporting has no
    # transaction-date field of its own; `creation` is the effective date). ──
    intake_rows = frappe.db.sql("""
        SELECT ghouse, SUM(stems_checked) AS checked, SUM(quarantined_stems) AS quarantined
        FROM `tabQuality Reporting`
        WHERE control_point = 'Intake'
          AND DATE(creation) BETWEEN %(from_date)s AND %(to_date)s
          AND ghouse IS NOT NULL AND ghouse != ''
        GROUP BY ghouse
        HAVING checked > 0
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    # Intake's ghouse carries the " - KR" farm suffix (and sometimes extra
    # internal whitespace, e.g. "Chepsito GH 15   - KR"); Packhouse's ghouse
    # is bare. Normalize both to the same bare, single-spaced key so the two
    # control points actually line up on the same greenhouse.
    def _bare_gh(name):
        import re
        return re.sub(r"\s+", " ", re.sub(r"\s*-\s*KR\s*$", "", name or "")).strip()

    intake_by_gh_map = {}
    for r in intake_rows:
        key = _bare_gh(r.ghouse)
        cur = intake_by_gh_map.setdefault(key, {"checked": 0, "quarantined": 0})
        cur["checked"] += cint(r.checked)
        cur["quarantined"] += cint(r.quarantined)
    for key, v in intake_by_gh_map.items():
        v["rate_pct"] = round((v["quarantined"] / v["checked"]) * 100, 1) if v["checked"] else 0

    intake_total = frappe.db.sql("""
        SELECT SUM(stems_checked) AS checked, SUM(quarantined_stems) AS quarantined
        FROM `tabQuality Reporting`
        WHERE control_point = 'Intake' AND DATE(creation) BETWEEN %(from_date)s AND %(to_date)s
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    intake_checked = flt(intake_total[0].checked) if intake_total else 0
    intake_quarantined = flt(intake_total[0].quarantined) if intake_total else 0

    # ── Packhouse-stage rejects BY GREENHOUSE (not just by customer) --
    # ghouse is populated on every Packhouse-control-point row, and it uses
    # the same greenhouse code as Scouting Entry once " - KR" is appended
    # (confirmed empirically against real data), so this is the actual join
    # key that ties scouting pressure through to what customers received. ──
    packhouse_rows = frappe.db.sql("""
        SELECT ghouse, SUM(stems_checked) AS checked, SUM(custom_stems_rejected) AS rejected
        FROM `tabQuality Reporting`
        WHERE control_point = 'Packhouse'
          AND DATE(creation) BETWEEN %(from_date)s AND %(to_date)s
          AND ghouse IS NOT NULL AND ghouse != ''
        GROUP BY ghouse
        HAVING checked > 0
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    # Full per-customer breakdown (not squeezed into a comma string) -- every
    # customer that had a reject traced to this greenhouse, with their own
    # checked/rejected counts, so the worst-affected customer per greenhouse
    # is visible, not just a name in a list.
    customer_rows = frappe.db.sql("""
        SELECT ghouse, custom_customer AS customer, SUM(stems_checked) AS checked,
               SUM(custom_stems_rejected) AS rejected
        FROM `tabQuality Reporting`
        WHERE control_point = 'Packhouse'
          AND DATE(creation) BETWEEN %(from_date)s AND %(to_date)s
          AND ghouse IS NOT NULL AND ghouse != ''
          AND custom_customer IS NOT NULL AND custom_customer != ''
        GROUP BY ghouse, custom_customer
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    customers_by_gh = {}
    for r in customer_rows:
        gh = _bare_gh(r.ghouse)
        customers_by_gh.setdefault(gh, []).append({
            "customer": r.customer, "checked": cint(r.checked), "rejected": cint(r.rejected),
        })
    for gh in customers_by_gh:
        customers_by_gh[gh].sort(key=lambda c: c["rejected"], reverse=True)

    # ── Scouting pressure per greenhouse in the SAME range, keyed the same
    # way, so it lines up in the same row as that greenhouse's downstream
    # intake/packhouse numbers -- this is the actual field-to-vase join. ──
    scout_pest_by_gh = frappe.db.sql("""
        SELECT se.greenhouse AS gh, SUM(pse.count) AS n
        FROM `tabPests Scouting Entry` pse
        INNER JOIN `tabScouting Entry` se ON se.name = pse.parent
        WHERE se.date_of_capture BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY se.greenhouse
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    scout_disease_by_gh = frappe.db.sql("""
        SELECT se.greenhouse AS gh, COUNT(*) AS n
        FROM `tabDiseases Scouting Entry` dse
        INNER JOIN `tabScouting Entry` se ON se.name = dse.parent
        WHERE se.date_of_capture BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY se.greenhouse
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    scout_pressure_by_gh = {}
    for r in scout_pest_by_gh:
        scout_pressure_by_gh[r.gh] = scout_pressure_by_gh.get(r.gh, 0) + flt(r.n)
    for r in scout_disease_by_gh:
        scout_pressure_by_gh[r.gh] = scout_pressure_by_gh.get(r.gh, 0) + flt(r.n)

    greenhouse_chain = []
    for r in packhouse_rows:
        gh = _bare_gh(r.ghouse)
        checked = cint(r.checked)
        rejected = cint(r.rejected)
        greenhouse_chain.append({
            "greenhouse": gh, "gh_kr": gh + " - KR",  # Quality Reporting.ghouse has no farm suffix; Scouting Entry.greenhouse does
            "ph_checked": checked, "ph_rejected": rejected,
            "ph_reject_pct": round((rejected / checked) * 100, 1) if checked else 0,
        })
    # Worst business outcome first (stems actually rejected at the packhouse) --
    # that's the number that costs money; pressure/intake ride along on the
    # same row so the cause is visible next to the effect, not on another tab.
    greenhouse_chain.sort(key=lambda r: r["ph_rejected"], reverse=True)
    greenhouse_chain = greenhouse_chain[:15]

    # ── Real sales orders whose ALLOCATED buckets were harvested from these
    # greenhouses -- not the Quality Reporting custom_order_pick_list text
    # field (confirmed it doesn't match any real Order Pick List name), but
    # the actual physical chain: harvest (Stock Entry.custom_greenhouse) ->
    # bucket_id -> Bucket Allocation Status -> Bucket Allocations.sales_order.
    # Scoped to just this range's worst greenhouses, so it stays fast. ──
    orders_by_gh = {}
    if greenhouse_chain:
        gh_kr_list = [r["gh_kr"] for r in greenhouse_chain]
        # Literal-quoted IN list (server-computed greenhouse names, not raw
        # user input) so from_date/to_date can stay named params -- frappe.db.sql
        # can't mix positional %s and named %(x)s placeholders in one call.
        ph_gh_literal = ", ".join(frappe.db.escape(g) for g in gh_kr_list)
        order_rows = frappe.db.sql(f"""
            SELECT se.custom_greenhouse AS gh_kr, ba.sales_order AS sales_order,
                   soi.custom_length AS length, SUM(ba.quantity_allocated) AS qty
            FROM `tabStock Entry` se
            INNER JOIN `tabBucket Allocation Status` bas ON bas.bucket_id = se.custom_bucket_id
            INNER JOIN `tabBucket Allocations` ba ON ba.parent = bas.name AND ba.cancelled = 0
            LEFT JOIN `tabSales Order Item` soi ON soi.name = ba.sales_order_item
            WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus = 1
              AND se.custom_greenhouse IN ({ph_gh_literal})
              AND se.posting_date BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY se.custom_greenhouse, ba.sales_order, soi.custom_length
        """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
        for r in order_rows:
            orders_by_gh.setdefault(r.gh_kr, []).append({
                "sales_order": r.sales_order, "length": r.length, "qty": round(flt(r.qty)),
            })
        for gh_kr in orders_by_gh:
            orders_by_gh[gh_kr].sort(key=lambda o: o["qty"], reverse=True)

    for row in greenhouse_chain:
        gh = row["greenhouse"]
        intake = intake_by_gh_map.get(gh, {})
        row["scouting_pressure"] = round(scout_pressure_by_gh.get(row["gh_kr"], 0))
        row["intake_checked"] = intake.get("checked", 0)
        row["intake_quarantine_pct"] = intake.get("rate_pct", 0)
        row["customers"] = customers_by_gh.get(gh, [])
        row["orders"] = orders_by_gh.get(row["gh_kr"], [])[:10]
        del row["gh_kr"]

    return {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "intake": {
            "checked": round(intake_checked),
            "quarantined": round(intake_quarantined),
            "rate_pct": round((intake_quarantined / intake_checked) * 100, 1) if intake_checked else 0,
        },
        "greenhouse_chain": greenhouse_chain,
    }


# ============================================================
# 2. CONSUMABLES & PACKAGING STOCK
# ============================================================
CONSUMABLE_ITEM_GROUPS = [
    "Packaging Materials", "Customer Consumables", "Consumables Purchased",
]


@frappe.whitelist()
def getConsumablesStock():
    from_date, to_date = _date_range()
    ph = ", ".join(["%s"] * len(CONSUMABLE_ITEM_GROUPS))
    # Same item-group list literal-quoted for the two queries below that also
    # need from_date/to_date as named params (can't mix positional %s IN(...)
    # with named %(from_date)s in one frappe.db.sql call).
    ph_literal = ", ".join(f"'{g}'" for g in CONSUMABLE_ITEM_GROUPS)

    by_group = frappe.db.sql(f"""
        SELECT i.item_group AS item_group, SUM(b.stock_value) AS value, SUM(b.actual_qty) AS qty,
               COUNT(DISTINCT b.item_code) AS item_count
        FROM `tabBin` b
        INNER JOIN `tabItem` i ON i.name = b.item_code
        WHERE i.item_group IN ({ph}) AND b.actual_qty > 0
        GROUP BY i.item_group
    """, CONSUMABLE_ITEM_GROUPS, as_dict=True)

    top_items = frappe.db.sql(f"""
        SELECT b.item_code AS item_code, i.item_name AS item_name, i.item_group AS item_group,
               SUM(b.actual_qty) AS qty, SUM(b.stock_value) AS value
        FROM `tabBin` b
        INNER JOIN `tabItem` i ON i.name = b.item_code
        WHERE i.item_group IN ({ph}) AND b.actual_qty > 0
        GROUP BY b.item_code, i.item_name, i.item_group
        ORDER BY value DESC
        LIMIT 15
    """, CONSUMABLE_ITEM_GROUPS, as_dict=True)

    # Daily net outflow rate over the selected range, per item, to estimate
    # "days remaining" -- there are no Item Reorder rows in this system, so
    # this is derived from real consumption instead of a configured minimum.
    days = max(1, (getdate(to_date) - getdate(from_date)).days)
    usage = frappe.db.sql(f"""
        SELECT sle.item_code AS item_code, SUM(CASE WHEN sle.actual_qty < 0 THEN -sle.actual_qty ELSE 0 END) AS consumed
        FROM `tabStock Ledger Entry` sle
        INNER JOIN `tabItem` i ON i.name = sle.item_code
        WHERE i.item_group IN ({ph_literal})
          AND sle.posting_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY sle.item_code
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    usage_by_item = {r.item_code: flt(r.consumed) for r in usage}
    for row in top_items:
        consumed = usage_by_item.get(row.item_code, 0)
        daily_rate = consumed / days if days else 0
        row["daily_usage"] = round(daily_rate, 2)
        row["days_remaining"] = round(row.qty / daily_rate) if daily_rate > 0 else None

    # Consumption trend: total qty issued vs received per day, across all
    # consumable groups combined -- the volume signal over time.
    trend = frappe.db.sql(f"""
        SELECT sle.posting_date AS date,
               SUM(CASE WHEN sle.actual_qty < 0 THEN -sle.actual_qty ELSE 0 END) AS issued,
               SUM(CASE WHEN sle.actual_qty > 0 THEN sle.actual_qty ELSE 0 END) AS received
        FROM `tabStock Ledger Entry` sle
        INNER JOIN `tabItem` i ON i.name = sle.item_code
        WHERE i.item_group IN ({ph_literal})
          AND sle.posting_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY sle.posting_date
        ORDER BY sle.posting_date
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    total_value = sum(flt(r.value) for r in by_group)

    # ── Pending Material Requests for these consumable groups -- procurement
    # already in motion, so a low days-remaining item that already has an MR
    # on the way reads differently from one with nothing coming. ──
    mr_rows = frappe.db.sql(f"""
        SELECT mr.name AS name, mr.status AS status, mr.transaction_date AS date,
               mr.per_ordered AS per_ordered, mr.per_received AS per_received,
               mri.item_code AS item_code, i.item_name AS item_name, mri.qty AS qty
        FROM `tabMaterial Request` mr
        INNER JOIN `tabMaterial Request Item` mri ON mri.parent = mr.name
        INNER JOIN `tabItem` i ON i.name = mri.item_code
        WHERE mr.docstatus = 1 AND mr.status NOT IN ('Stopped', 'Cancelled')
          AND i.item_group IN ({ph_literal})
        ORDER BY mr.transaction_date DESC
        LIMIT 30
    """, as_dict=True)

    # ── Demand vs stock: expected consumable draw from currently OPEN orders
    # (via each customer's own Specification -> Spec Consumable box-rate),
    # against what's actually on hand right now. Not date-ranged like the
    # rest of this tab -- "open orders" is inherently forward-looking, same
    # reasoning as Orders at Risk. A customer can have several Specification
    # versions on file; the most recently created one is treated as active. ──
    open_items = frappe.db.sql("""
        SELECT so.customer AS customer, soi.item_code AS variety,
               COALESCE(soi.custom_number_of_boxes, 0) AS boxes
        FROM `tabSales Order Item` soi
        INNER JOIN `tabSales Order` so ON so.name = soi.parent
        WHERE so.docstatus = 1 AND so.status NOT IN ('Cancelled', 'Closed', 'Completed')
          AND so.customer IS NOT NULL AND so.customer != ''
    """, as_dict=True)
    customers_needed = list({r.customer for r in open_items})

    demand_by_item = {}
    if customers_needed:
        cust_literal = ", ".join(frappe.db.escape(c) for c in customers_needed)
        latest_spec_by_customer = {}
        for r in frappe.db.sql(f"""
            SELECT name, customer, creation
            FROM `tabSpecifications`
            WHERE customer IN ({cust_literal})
            ORDER BY creation DESC
        """, as_dict=True):
            latest_spec_by_customer.setdefault(r.customer, r.name)  # first hit per customer = latest (DESC order)

        spec_names = list(set(latest_spec_by_customer.values()))
        if spec_names:
            spec_literal = ", ".join(frappe.db.escape(s) for s in spec_names)
            consumables_by_spec = {}
            for r in frappe.db.sql(f"""
                SELECT sc.parent AS spec, sc.item AS item_code, i.item_name AS item_name, sc.qty_per_box AS qty_per_box
                FROM `tabSpec Consumable` sc
                LEFT JOIN `tabItem` i ON i.name = sc.item
                WHERE sc.parent IN ({spec_literal}) AND sc.qty_per_box > 0
            """, as_dict=True):
                consumables_by_spec.setdefault(r.spec, []).append(r)

            for r in open_items:
                spec = latest_spec_by_customer.get(r.customer)
                if not spec:
                    continue
                for c in consumables_by_spec.get(spec, []):
                    need = flt(c.qty_per_box) * flt(r.boxes)
                    if c.item_code not in demand_by_item:
                        demand_by_item[c.item_code] = {"item_name": c.item_name, "needed": 0}
                    demand_by_item[c.item_code]["needed"] += need

    if demand_by_item:
        item_codes = list(demand_by_item.keys())
        item_literal = ", ".join(frappe.db.escape(i) for i in item_codes)
        stock_rows = frappe.db.sql(f"""
            SELECT item_code, SUM(actual_qty) AS qty FROM `tabBin`
            WHERE item_code IN ({item_literal}) GROUP BY item_code
        """, as_dict=True)
        stock_by_item = {r.item_code: flt(r.qty) for r in stock_rows}
    else:
        stock_by_item = {}

    demand_vs_stock = []
    for item_code, d in demand_by_item.items():
        on_hand = stock_by_item.get(item_code, 0)
        needed = round(d["needed"])
        demand_vs_stock.append({
            "item_code": item_code, "item_name": d["item_name"] or item_code,
            "needed_for_open_orders": needed, "on_hand": round(on_hand),
            "shortfall": round(max(0, needed - on_hand)),
        })
    demand_vs_stock.sort(key=lambda r: r["shortfall"], reverse=True)

    frappe.response["message"] = {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "total_value": round(total_value),
        "by_group": [{
            "item_group": r.item_group, "value": round(flt(r.value)), "qty": round(flt(r.qty)),
            "item_count": cint(r.item_count),
        } for r in by_group],
        "top_items": top_items,
        "trend": [{"date": str(r.date), "issued": round(flt(r.issued)), "received": round(flt(r.received))} for r in trend],
        "material_requests": [{
            "name": r.name, "status": r.status, "date": str(r.date), "item_code": r.item_code,
            "item_name": r.item_name, "qty": round(flt(r.qty)),
            "per_ordered": round(flt(r.per_ordered), 1), "per_received": round(flt(r.per_received), 1),
        } for r in mr_rows],
        "demand_vs_stock": demand_vs_stock[:15],
    }


# ============================================================
# 3. SALES & CUSTOMERS
# ============================================================
@frappe.whitelist()
def getSalesOverview():
    from_date, to_date = _date_range()

    orders = frappe.db.sql("""
        SELECT so.name, so.customer, so.territory, so.grand_total, so.currency,
               IFNULL((SELECT SUM(soi.stock_qty) FROM `tabSales Order Item` soi WHERE soi.parent = so.name), 0) AS stems
        FROM `tabSales Order` so
        WHERE so.docstatus = 1
          AND so.transaction_date BETWEEN %(from_date)s AND %(to_date)s
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    currency_totals = {}
    customer_orders = {}
    customer_revenue = {}
    customer_stems = {}
    for o in orders:
        currency_totals[o.currency] = currency_totals.get(o.currency, 0) + flt(o.grand_total)
        customer_orders[o.customer] = customer_orders.get(o.customer, 0) + 1
        customer_revenue[o.customer] = customer_revenue.get(o.customer, 0) + flt(o.grand_total)
        customer_stems[o.customer] = customer_stems.get(o.customer, 0) + flt(o.stems)

    total_orders = len(orders)
    top_customer_rows = sorted(customer_orders.items(), key=lambda kv: kv[1], reverse=True)[:8]
    top_customer_share_pct = round((top_customer_rows[0][1] / total_orders) * 100, 1) if total_orders and top_customer_rows else 0
    concentration_rows = [{
        "customer": c, "orders": n,
        "share_pct": round((n / total_orders) * 100, 1) if total_orders else 0,
        "revenue": round(customer_revenue.get(c, 0), 2),
    } for c, n in top_customer_rows]

    top_by_volume = sorted(customer_stems.items(), key=lambda kv: kv[1], reverse=True)[:8]
    volume_rows = [{"customer": c, "stems": round(v)} for c, v in top_by_volume]

    # ── Consignee/country geography: real shipped-box destinations, sharper
    # than the coarse Sales Order territory field. ──
    geo_rows = frappe.db.sql("""
        SELECT c.country AS country, COUNT(*) AS boxes
        FROM `tabBox Label` bl
        INNER JOIN `tabConsignee` c ON c.consignee = bl.consignee
        WHERE bl.date BETWEEN %(from_date)s AND %(to_date)s
          AND c.country IS NOT NULL AND c.country != ''
        GROUP BY c.country
        ORDER BY boxes DESC
        LIMIT 12
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    # ── Freight/shipping agent volume (real per-shipment usage, not the
    # Delivery Point config table of merely-allowed agents). ──
    agent_rows = frappe.db.sql("""
        SELECT bl.freight_agent AS agent, COUNT(*) AS boxes
        FROM `tabBox Label` bl
        WHERE bl.date BETWEEN %(from_date)s AND %(to_date)s
          AND bl.freight_agent IS NOT NULL AND bl.freight_agent != ''
        GROUP BY bl.freight_agent
        ORDER BY boxes DESC
        LIMIT 10
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    frappe.response["message"] = {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "total_orders": total_orders,
        "active_customers": len(customer_orders),
        "currency_mix": [{"currency": k, "revenue": round(v, 2)} for k, v in
                         sorted(currency_totals.items(), key=lambda kv: kv[1], reverse=True)],
        "top_customer_share_pct": top_customer_share_pct,
        "customer_concentration": concentration_rows,
        "top_by_volume": volume_rows,
        "by_country": [{"country": r.country, "boxes": cint(r.boxes)} for r in geo_rows],
        "by_freight_agent": [{"agent": r.agent, "boxes": cint(r.boxes)} for r in agent_rows],
    }


# ============================================================
# 3b. CUSTOMER ORDER CADENCE
# ============================================================
# Not scoped to the tab's date range -- cadence needs real history to
# establish what "normal" looks like for a customer before flagging a gap
# as unusual, so this always looks back a fixed 120 days regardless of the
# range picker, then reports against "today".
@frappe.whitelist()
def getCustomerCadence():
    lookback_days = 120
    today = getdate(frappe.utils.today())
    floor_date = add_days(today, -lookback_days)

    rows = frappe.db.sql("""
        SELECT customer, transaction_date
        FROM `tabSales Order`
        WHERE docstatus = 1 AND status NOT IN ('Cancelled')
          AND transaction_date >= %(floor)s
          AND customer IS NOT NULL AND customer != ''
        ORDER BY customer, transaction_date
    """, {"floor": floor_date}, as_dict=True)

    by_customer = {}
    for r in rows:
        by_customer.setdefault(r.customer, []).append(getdate(r.transaction_date))

    weekly = []
    monthly = []
    quiet_this_week = []  # ordered last week (or the week before), not this week
    dormant = []          # last order well beyond their own normal interval, or just old
    anomalies = []         # regular cadence that abruptly stopped

    for customer, dates in by_customer.items():
        dates = sorted(dates)
        n = len(dates)
        # Normalized against the FIXED lookback window, not the customer's own
        # observed order span -- a customer whose few orders happen to cluster
        # within a couple of days would otherwise extrapolate to an absurd
        # "84 orders/week" from a tiny, non-representative span.
        span_days = (dates[-1] - dates[0]).days
        orders_per_week = round(n / (lookback_days / 7), 2)
        orders_per_month = round(n / (lookback_days / 30), 2)
        last_order = dates[-1]
        days_since_last = (today - last_order).days

        weekly.append({"customer": customer, "orders_per_week": orders_per_week, "total_orders": n})
        monthly.append({"customer": customer, "orders_per_month": orders_per_month, "total_orders": n})

        if n < 2:
            continue  # not enough history to judge a gap as normal or not

        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, n)]
        avg_gap = sum(gaps) / len(gaps)

        if days_since_last > 60:
            dormant.append({
                "customer": customer, "last_order_date": str(last_order),
                "days_since": days_since_last, "total_orders": n,
            })
        elif 7 <= days_since_last <= 14 and avg_gap <= 7:
            # A customer whose normal rhythm is weekly-or-tighter, but who
            # hasn't shown up this week -- worth a look, not yet "dormant".
            quiet_this_week.append({
                "customer": customer, "last_order_date": str(last_order),
                "days_since": days_since_last, "avg_gap_days": round(avg_gap, 1), "total_orders": n,
            })

        # Anomaly: a genuinely regular customer (tight, consistent gaps) whose
        # latest silence is much longer than their own established rhythm --
        # "ordered daily/weekly then suddenly stopped".
        if avg_gap <= 10 and n >= 4 and days_since_last > max(14, avg_gap * 3):
            anomalies.append({
                "customer": customer, "avg_gap_days": round(avg_gap, 1),
                "days_since_last": days_since_last, "last_order_date": str(last_order),
                "total_orders": n,
            })

    weekly.sort(key=lambda r: r["orders_per_week"], reverse=True)
    monthly.sort(key=lambda r: r["orders_per_month"], reverse=True)
    dormant.sort(key=lambda r: r["days_since"], reverse=True)
    anomalies.sort(key=lambda r: r["days_since_last"], reverse=True)
    quiet_this_week.sort(key=lambda r: r["days_since"], reverse=True)

    frappe.response["message"] = {
        "success": True,
        "lookback_days": lookback_days,
        "most_frequent": weekly[:10],
        "quiet_this_week": quiet_this_week[:10],
        "dormant": dormant[:10],
        "anomalies": anomalies[:10],
    }


# ============================================================
# 4. ORDER -> PACKING CONVERSION
# ============================================================
@frappe.whitelist()
def getConversionData():
    from_date, to_date = _date_range()

    orders = frappe.db.sql("""
        SELECT so.name AS sales_order, so.customer, so.currency AS currency,
               SUM(soi.stock_qty) AS ordered_stems,
               SUM(soi.amount) AS order_value
        FROM `tabSales Order` so
        INNER JOIN `tabSales Order Item` soi ON soi.parent = so.name
        WHERE so.docstatus = 1
          AND so.transaction_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY so.name, so.customer, so.currency
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    if not orders:
        frappe.response["message"] = {
            "success": True, "from_date": from_date, "to_date": to_date,
            "totals": {"ordered": 0, "allocated": 0, "packed": 0},
            "overall_conversion_pct": 0, "worst_orders": [],
        }
        return

    so_names = [o.sales_order for o in orders]
    ph = ", ".join(["%s"] * len(so_names))

    allocated_rows = frappe.db.sql(f"""
        SELECT ba.sales_order AS sales_order, SUM(ba.quantity_allocated) AS allocated_stems
        FROM `tabBucket Allocations` ba
        WHERE ba.cancelled = 0 AND ba.sales_order IN ({ph})
        GROUP BY ba.sales_order
    """, so_names, as_dict=True)
    allocated_by_so = {r.sales_order: flt(r.allocated_stems) for r in allocated_rows}

    packed_rows = frappe.db.sql(f"""
        SELECT opl.sales_order AS sales_order, SUM(bli.qty) AS packed_stems
        FROM `tabBox Label` bl
        INNER JOIN `tabBox Label Item` bli ON bli.parent = bl.name
        INNER JOIN `tabOrder Pick List` opl ON opl.name = bl.order_pick_list
        WHERE opl.sales_order IN ({ph})
        GROUP BY opl.sales_order
    """, so_names, as_dict=True)
    packed_by_so = {r.sales_order: flt(r.packed_stems) for r in packed_rows}

    total_ordered = total_allocated = total_packed = 0.0
    per_order = []
    for o in orders:
        ordered = flt(o.ordered_stems)
        allocated = allocated_by_so.get(o.sales_order, 0)
        packed = packed_by_so.get(o.sales_order, 0)
        total_ordered += ordered
        total_allocated += allocated
        total_packed += packed
        pct = round((packed / ordered) * 100, 1) if ordered else 0
        # Value still at stake on this line = the unpacked portion, priced at
        # the order's own average per-stem rate -- not the whole order value,
        # which would overstate what's actually still exposed.
        rate_per_stem = (flt(o.order_value) / ordered) if ordered else 0
        value_outstanding = round(rate_per_stem * max(0, ordered - packed), 2)
        per_order.append({
            "sales_order": o.sales_order, "customer": o.customer,
            "ordered": ordered, "allocated": allocated, "packed": packed,
            "conversion_pct": pct, "value_outstanding": value_outstanding, "currency": o.currency,
        })

    per_order.sort(key=lambda r: r["conversion_pct"])
    overall_pct = round((total_packed / total_ordered) * 100, 1) if total_ordered else 0

    # ── Boxes packed per day (Packing Guide -- real per-box detail, child of
    # Order Pick List, richer real volume than Box Label on this bench). ──
    boxes_per_day = frappe.db.sql("""
        SELECT opl.date_created AS date, COUNT(DISTINCT pg.box_number) AS boxes
        FROM `tabPacking Guide` pg
        INNER JOIN `tabOrder Pick List` opl ON opl.name = pg.parent
        WHERE opl.date_created BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY opl.date_created
        ORDER BY opl.date_created
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    # ── Dispatched (Delivery Note) / invoiced (Sales Invoice) in the same
    # window -- shown honestly even where this bench barely has real data
    # for these two (unlike the pick/pack side, which is populated). ──
    dispatched = frappe.db.sql("""
        SELECT COUNT(*) AS n FROM `tabDelivery Note`
        WHERE docstatus = 1 AND posting_date BETWEEN %(from_date)s AND %(to_date)s
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    invoiced = frappe.db.sql("""
        SELECT COUNT(*) AS n FROM `tabSales Invoice`
        WHERE docstatus = 1 AND posting_date BETWEEN %(from_date)s AND %(to_date)s
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    frappe.response["message"] = {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "totals": {
            "ordered": round(total_ordered), "allocated": round(total_allocated),
            "packed": round(total_packed),
        },
        "overall_conversion_pct": overall_pct,
        "worst_orders": [r for r in per_order if r["ordered"] > 0][:15],
        "boxes_per_day": [{"date": str(r.date), "boxes": cint(r.boxes)} for r in boxes_per_day],
        "orders_vs_dispatched_invoiced": {
            "orders": len(orders),
            "dispatched": cint(dispatched[0].n) if dispatched else 0,
            "invoiced": cint(invoiced[0].n) if invoiced else 0,
        },
    }


# ============================================================
# 5. DOWNGRADE ANALYSIS
# ============================================================
@frappe.whitelist()
def getDowngradeAnalysis():
    from_date, to_date = _date_range()

    rows = frappe.db.sql("""
        SELECT pli.stem_length AS picklist_length, pli.item_code AS variety,
               (SELECT se.custom_stem_length FROM `tabStock Entry` se
                WHERE se.custom_bucket_id = pli.bucket
                  AND se.stock_entry_type = 'Harvesting' AND se.docstatus = 1
                ORDER BY se.creation DESC LIMIT 1) AS original_length,
               COALESCE(NULLIF(pli.stock_qty, 0), pli.qty * COALESCE(pli.conversion_factor, 1), 0) AS stems,
               pli.available_stems_of_exact_length AS available_exact_length_raw,
               opl.date_created AS date
        FROM `tabPick List Item` pli
        INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
        WHERE pli.parenttype = 'Order Pick List'
          AND pli.downgrade_reason IS NOT NULL AND pli.downgrade_reason != ''
          AND opl.date_created BETWEEN %(from_date)s AND %(to_date)s
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    by_length = {}
    by_variety = {}
    by_day = {}
    total_downgraded_stems = 0
    total_available_unreceived = 0
    downgrade_events = 0
    for r in rows:
        ol, pl_len = r.get("original_length"), r.get("picklist_length")
        if ol and pl_len and str(ol) == str(pl_len):
            continue
        stems = flt(r.get("stems"))
        length_key = ol or pl_len or "Unknown"
        variety_key = r.get("variety") or "Unknown"
        by_length[length_key] = by_length.get(length_key, 0) + stems
        by_variety[variety_key] = by_variety.get(variety_key, 0) + stems
        total_downgraded_stems += stems
        d = str(r["date"])[:10] if r.get("date") else "Unknown"
        by_day[d] = by_day.get(d, 0) + stems
        # available_stems_of_exact_length: stock the exact requested length
        # that technically existed (per Pick List Item's own snapshot at pick
        # time) but wasn't in the picker's hands yet -- still on its way
        # through receiving/shelving, not actually absent.
        try:
            avail = float(r.get("available_exact_length_raw") or 0)
        except (TypeError, ValueError):
            avail = 0
        if avail > 0:
            total_available_unreceived += avail
            downgrade_events += 1

    total_picked = frappe.db.sql("""
        SELECT SUM(COALESCE(NULLIF(pli.stock_qty, 0), pli.qty * COALESCE(pli.conversion_factor, 1), 0)) AS s
        FROM `tabPick List Item` pli
        INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
        WHERE pli.parenttype = 'Order Pick List'
          AND opl.date_created BETWEEN %(from_date)s AND %(to_date)s
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)
    total_picked_stems = flt(total_picked[0]["s"]) if total_picked and total_picked[0]["s"] else 0

    length_rows = [{"length": k, "stems": round(v)} for k, v in by_length.items()]
    length_rows.sort(key=lambda r: r["stems"], reverse=True)

    variety_rows = [{"variety": k, "stems": round(v)} for k, v in by_variety.items()]
    variety_rows.sort(key=lambda r: r["stems"], reverse=True)

    trend_rows = [{"date": k, "stems": round(v)} for k, v in by_day.items()]
    trend_rows.sort(key=lambda r: r["date"])

    frappe.response["message"] = {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "total_downgraded_stems": round(total_downgraded_stems),
        "total_picked_stems": round(total_picked_stems),
        "downgrade_rate_pct": round((total_downgraded_stems / total_picked_stems) * 100, 1) if total_picked_stems else 0,
        "by_length": length_rows[:12],
        "by_variety": variety_rows[:12],
        "trend": trend_rows,
        # Stock of the exact requested length that technically existed
        # (Pick List Item's own snapshot at pick time) but was still working
        # its way through receiving/shelving -- "unreceived at the time of
        # downgrading", not genuinely absent.
        "unreceived_at_downgrade": {
            "events_with_available_stock": downgrade_events,
            "total_available_stems": round(total_available_unreceived),
        },
    }


# ============================================================
# 6. ORDERS AT RISK OF SHORTING
# ============================================================
# Same "Available = Shelf - Allocated - Discard" formula the Avails page uses
# (upande_packhouse/api/avails.py -- getAvailsData), aggregated to variety+length
# (not per-farm here, since an order can be served from any farm). Not date-ranged:
# always a live snapshot of the shelf right now, so risk % moves the instant more
# of that variety/length gets shelved -- no caching, recomputed on every call.
@frappe.whitelist()
def getOrdersAtRisk():
    horizon_days = cint(frappe.form_dict.get("horizon_days") or 30)
    today = frappe.utils.today()
    to_date = add_days(today, horizon_days)
    from_floor = add_days(today, -60)  # safety net against ancient stale-open orders

    shelf_rows = frappe.db.sql("""
        SELECT si.variety AS variety, si.stem_length AS length, SUM(si.stem_qty) AS stems
        FROM `tabShelf` s INNER JOIN `tabShelf Item` si ON s.name = si.parent
        WHERE si.stem_qty > 0 AND si.variety IS NOT NULL AND TRIM(si.variety) != ''
        GROUP BY si.variety, si.stem_length
    """, as_dict=True)
    alloc_rows = frappe.db.sql("""
        SELECT bas.item_code AS variety, bas.stem_length AS length, SUM(bas.allocated_quantity) AS stems
        FROM `tabBucket Allocation Status` bas
        WHERE bas.item_code IS NOT NULL AND TRIM(bas.item_code) != ''
          AND EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = bas.bucket_id AND si.stem_qty > 0)
        GROUP BY bas.item_code, bas.stem_length
    """, as_dict=True)
    discard_rows = frappe.db.sql("""
        SELECT drb.variety AS variety, drb.stem_length AS length, SUM(drb.stem_qty) AS stems
        FROM `tabDiscard Request Bucket` drb INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
        WHERE dr.docstatus < 2 AND IFNULL(drb.is_shelved, 0) = 1 AND IFNULL(drb.discarded, 0) = 0
          AND EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = drb.bucket_id AND si.stem_qty > 0)
        GROUP BY drb.variety, drb.stem_length
    """, as_dict=True)

    pool = {}
    for r in shelf_rows:
        key = (r.variety, r.length)
        pool[key] = pool.get(key, 0) + flt(r.stems)
    for r in alloc_rows:
        key = (r.variety, r.length)
        pool[key] = pool.get(key, 0) - flt(r.stems)
    for r in discard_rows:
        key = (r.variety, r.length)
        pool[key] = pool.get(key, 0) - flt(r.stems)
    for k in pool:
        pool[k] = max(0, pool[k])

    # Open order-items still needing stock, earliest delivery date first --
    # whoever's due soonest gets first claim on the shared shelf pool.
    items = frappe.db.sql("""
        SELECT soi.name AS sales_order_item, soi.parent AS sales_order, so.customer AS customer,
               so.delivery_date AS delivery_date, soi.item_code AS variety, soi.custom_length AS length,
               soi.stock_qty AS ordered_stems, soi.rate AS rate, soi.conversion_factor AS conversion_factor,
               so.currency AS currency
        FROM `tabSales Order Item` soi
        INNER JOIN `tabSales Order` so ON so.name = soi.parent
        WHERE so.docstatus = 1 AND so.status NOT IN ('Cancelled', 'Closed', 'Completed')
          AND so.delivery_date BETWEEN %(from_floor)s AND %(to_date)s
          AND soi.item_code IS NOT NULL AND soi.custom_length IS NOT NULL AND soi.custom_length != ''
        ORDER BY so.delivery_date ASC, so.creation ASC
    """, {"from_floor": from_floor, "to_date": to_date}, as_dict=True)

    if not items:
        frappe.response["message"] = {
            "success": True, "horizon_days": horizon_days,
            "summary": {"orders_at_risk": 0, "lines_at_risk": 0, "total_shortfall_stems": 0},
            "at_risk": [],
        }
        return

    so_item_names = [i.sales_order_item for i in items]
    ph = ", ".join(["%s"] * len(so_item_names))
    alloc_by_item = {r.sales_order_item: flt(r.qty) for r in frappe.db.sql(f"""
        SELECT sales_order_item, SUM(quantity_allocated) AS qty
        FROM `tabBucket Allocations` WHERE cancelled = 0 AND sales_order_item IN ({ph})
        GROUP BY sales_order_item
    """, so_item_names, as_dict=True)}

    results = []
    for it in items:
        key = (it.variety, it.length)
        ordered = flt(it.ordered_stems)
        allocated = alloc_by_item.get(it.sales_order_item, 0)
        remaining_need = max(0, ordered - allocated)
        if remaining_need <= 0:
            continue  # already fully allocated -- not at risk

        available_here = pool.get(key, 0)
        claim = min(remaining_need, available_here)
        pool[key] = available_here - claim  # this order's claim reduces the shared pool
        shortfall = remaining_need - claim
        if shortfall <= 0:
            continue  # fully covered by what's on the shelf right now

        risk_pct = round((shortfall / remaining_need) * 100, 1)
        # Per-stem rate: Sales Order Item.rate is priced per the line's own UOM
        # (bunch), conversion_factor turns that into stems -- same derivation
        # downgrades.py already uses for "foregone revenue".
        conv = flt(it.conversion_factor) or 1.0
        rate_per_stem = flt(it.rate) / conv if conv else 0
        value_at_risk = round(rate_per_stem * shortfall, 2)
        results.append({
            "sales_order": it.sales_order, "customer": it.customer,
            "variety": it.variety, "length": it.length,
            "delivery_date": str(it.delivery_date),
            "remaining_need": round(remaining_need), "available_now": round(available_here),
            "shortfall": round(shortfall), "risk_pct": risk_pct,
            "value_at_risk": value_at_risk, "currency": it.currency,
        })

    results.sort(key=lambda r: (-r["risk_pct"], r["delivery_date"]))
    distinct_orders = {r["sales_order"] for r in results}
    value_by_currency = {}
    for r in results:
        value_by_currency[r["currency"]] = value_by_currency.get(r["currency"], 0) + r["value_at_risk"]

    frappe.response["message"] = {
        "success": True,
        "horizon_days": horizon_days,
        "summary": {
            "orders_at_risk": len(distinct_orders),
            "lines_at_risk": len(results),
            "total_shortfall_stems": round(sum(r["shortfall"] for r in results)),
            "value_at_risk_by_currency": {k: round(v, 2) for k, v in value_by_currency.items()},
        },
        "at_risk": results[:50],
    }


# ============================================================
# 6b. UNRECEIVED BUCKETS BY FARM
# ============================================================
# Buckets currently harvested but not yet received (Bucket QR Code.status
# flips In Use -> Available the moment createReceivingStockEntry processes
# them -- see upande_quality/mobile/api.py). Not date-ranged, like Orders at
# Risk: this is "what's stuck right now", so it's a live snapshot too --
# explains why shelf stock can look short even when the farm has plenty
# sitting in the receiving queue (space at the cold room, not supply).
@frappe.whitelist()
def getUnreceivedBuckets():
    rows = frappe.db.sql("""
        SELECT bq.name AS bucket_id, se.farm AS farm,
               TIMESTAMPDIFF(HOUR, se.creation, %(now)s) AS hours_waiting
        FROM `tabBucket QR Code` bq
        INNER JOIN `tabStock Entry` se ON se.name = bq.last_stock_entry
        WHERE bq.status = 'In Use'
    """, {"now": frappe.utils.now_datetime()}, as_dict=True)

    by_farm = {}
    for r in rows:
        f = r.farm or "Unknown"
        cur = by_farm.setdefault(f, {"buckets": 0, "max_wait_hours": 0, "total_wait_hours": 0})
        cur["buckets"] += 1
        cur["max_wait_hours"] = max(cur["max_wait_hours"], flt(r.hours_waiting))
        cur["total_wait_hours"] += flt(r.hours_waiting)

    farm_rows = [{
        "farm": f, "buckets": v["buckets"],
        "avg_wait_hours": round(v["total_wait_hours"] / v["buckets"], 1) if v["buckets"] else 0,
        "max_wait_hours": round(v["max_wait_hours"], 1),
    } for f, v in by_farm.items()]
    farm_rows.sort(key=lambda r: r["buckets"], reverse=True)

    frappe.response["message"] = {
        "success": True,
        "total_unreceived": len(rows),
        "by_farm": farm_rows,
    }


# ============================================================
# 7. AGE-TO-ALLOCATION
# ============================================================
@frappe.whitelist()
def getAgeToAllocation():
    from_date, to_date = _date_range()

    rows = frappe.db.sql("""
        SELECT pli.parent AS opl, opl.date_created AS pick_date, pli.bucket AS bucket,
               pli.item_code AS variety, opl.farm AS farm,
               DATEDIFF(opl.date_created, DATE(si.date_added)) AS age_days
        FROM `tabPick List Item` pli
        INNER JOIN `tabShelf Item` si ON si.bucket_id = pli.bucket
        INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
        WHERE pli.parenttype = 'Order Pick List'
          AND pli.bucket IS NOT NULL AND pli.bucket != ''
          AND opl.date_created BETWEEN %(from_date)s AND %(to_date)s
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    # A bucket can appear on more than one pick-list line (partial picks) --
    # keep the earliest pick event per bucket, the moment it actually left
    # the "still waiting on the shelf" pool.
    seen = {}
    for r in rows:
        b = r["bucket"]
        if b not in seen or r["age_days"] < seen[b]["age_days"]:
            seen[b] = r
    clean_rows = [r for r in seen.values() if r["age_days"] is not None and r["age_days"] >= 0]

    if not clean_rows:
        frappe.response["message"] = {
            "success": True, "from_date": from_date, "to_date": to_date,
            "summary": {"count": 0, "avg_days": 0, "median_days": 0, "p90_days": 0},
            "distribution": [], "trend": [], "slowest_varieties": [],
        }
        return

    ages = sorted(r["age_days"] for r in clean_rows)
    n = len(ages)

    def pct(p):
        idx = min(n - 1, int(round(p * (n - 1))))
        return ages[idx]

    bands = [("0d (same day)", 0, 0), ("1-2d", 1, 2), ("3-6d", 3, 6), ("7-13d", 7, 13), ("14d+", 14, 99999)]
    dist = [{"band": label, "count": sum(1 for a in ages if lo <= a <= hi)} for label, lo, hi in bands]

    by_date = {}
    for r in clean_rows:
        by_date.setdefault(str(r["pick_date"]), []).append(r["age_days"])
    trend = [{"date": d, "avg_age": round(sum(v) / len(v), 1)} for d, v in sorted(by_date.items())]

    by_variety = {}
    for r in clean_rows:
        by_variety.setdefault(r["variety"] or "Unknown", []).append(r["age_days"])
    variety_rows = [{"variety": v, "avg_age": round(sum(a) / len(a), 1), "count": len(a)}
                    for v, a in by_variety.items() if len(a) >= 2]
    variety_rows.sort(key=lambda x: x["avg_age"], reverse=True)

    frappe.response["message"] = {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "summary": {
            "count": n,
            "avg_days": round(sum(ages) / n, 1),
            "median_days": pct(0.5),
            "p90_days": pct(0.9),
        },
        "distribution": dist,
        "trend": trend,
        "slowest_varieties": variety_rows[:10],
    }
