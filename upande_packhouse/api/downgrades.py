# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `packhouse-downgrades` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getDowngradeData():
    # ---------------------------------------------------------
    # FETCH DOWNGRADE DATA  (api_method: getDowngradeData)
    # Downgrade = Pick List Item (Order Pick List) with custom_downgrade_reason set
    #   AND picklist length < (different from) the original received length.
    # Foregone revenue = (price-list rate @ original length  -  actual sold rate)
    #                     x available_stems_of_exact_length, in customer currency.
    #   - actual sold rate = Sales Order Item rate / conversion_factor (per stem;
    #     SO lines are priced per Bunch, price lists per Stem).
    #   - original length = custom_stem_length on the latest Receiving/Late Receipt
    #     Stock Entry for the bucket.
    #   - customer -> reference price list comes from Production Settings child
    #     table "Customer Price List".
    # Lines where picklist length == original length are NOT downgrades (excluded).
    # ---------------------------------------------------------
    fd = frappe.form_dict
    from_date = fd.get("from_date")
    to_date = fd.get("to_date")
    if not from_date or not to_date:
        today = frappe.utils.today()
        from_date = today
        to_date = today
    today = frappe.utils.today()

    rose = (fd.get("rose_type") or "all").lower()
    rose_cond = ""
    if rose == "standard":
        rose_cond = " AND it.item_group = 'Standard Roses'"
    elif rose == "spray":
        rose_cond = " AND it.item_group = 'Spray Roses'"

    params = {"from_date": from_date, "to_date": to_date}
    team_filter = fd.get("team_filter") or "all"
    team_sql = ""
    if team_filter and team_filter != "all":
        teams = []
        for t in team_filter.split(","):
            t = t.strip()
            if t:
                teams = teams + [t]
        if teams:
            ph = []
            i = 0
            while i < len(teams):
                key = "team_" + str(i)
                ph = ph + ["%(" + key + ")s"]
                params[key] = teams[i]
                i = i + 1
            team_sql = " AND opl.team IN (" + ", ".join(ph) + ")"

    raw = frappe.db.sql(
        "SELECT opl.name AS opl, opl.customer AS customer, opl.team AS team, "
        "opl.date_created AS date, opl.order_name AS order_name, opl.owner AS picked_by, "
        "pli.item_code AS variety, pli.stem_length AS picklist_length, "
        "pli.available_stems_of_exact_length AS avail_raw, "
        "COALESCE(NULLIF(pli.stock_qty,0), pli.qty * COALESCE(pli.conversion_factor,1), 0) AS line_stems, "
        "pli.downgrade_reason AS reason, pli.bucket AS bucket, "
        "it.item_group AS rose_group, "
        "soi.rate AS so_rate, soi.conversion_factor AS conv, so2.currency AS so_currency, "
        "(SELECT se.custom_stem_length FROM `tabStock Entry` se "
        " WHERE se.custom_bucket_id = pli.bucket "
        " AND se.stock_entry_type = 'Harvesting' AND se.docstatus = 1 "
        " ORDER BY se.creation DESC LIMIT 1) AS original_length "
        "FROM `tabPick List Item` pli "
        "INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent "
        "LEFT JOIN `tabItem` it ON it.name = pli.item_code "
        "LEFT JOIN `tabSales Order Item` soi ON soi.name = pli.sales_order_item "
        "LEFT JOIN `tabSales Order` so2 ON so2.name = pli.sales_order "
        "WHERE pli.parenttype = 'Order Pick List' "
        "AND pli.downgrade_reason IS NOT NULL AND pli.downgrade_reason != '' "
        "AND opl.date_created BETWEEN %(from_date)s AND %(to_date)s"
        + rose_cond + team_sql +
        " ORDER BY opl.date_created DESC, opl.name LIMIT 4000",
        params, as_dict=True)

    # Keep only genuine downgrades: picklist length must differ from original length.
    rows = []
    for r in raw:
        ol = r.get("original_length")
        pl_len = r.get("picklist_length")
        if ol and pl_len and str(ol) == str(pl_len):
            continue
        rows = rows + [r]
        if len(rows) >= 2000:
            break

    # Distinct reasons + owners (before applying the reason/owner filters) so the
    # dashboard dropdowns stay stable within the date/team/rose selection.
    reasons_set = {}
    owners_set = {}
    for r in rows:
        rs = (r.get("reason") or "").strip()
        if rs:
            reasons_set[rs] = 1
        ow = r.get("picked_by")
        if ow:
            owners_set[ow] = 1

    # Reason + "by who" (OPL owner) filters — "|~|"-joined exact-match lists.
    reason_sel = None
    rf = fd.get("reason_filter")
    if rf:
        reason_sel = {}
        for x in rf.split("|~|"):
            x = x.strip()
            if x:
                reason_sel[x] = 1
    owner_sel = None
    of = fd.get("owner_filter")
    if of:
        owner_sel = {}
        for x in of.split("|~|"):
            x = x.strip()
            if x:
                owner_sel[x] = 1
    if reason_sel is not None or owner_sel is not None:
        filtered = []
        for r in rows:
            if reason_sel is not None and (r.get("reason") or "").strip() not in reason_sel:
                continue
            if owner_sel is not None and (r.get("picked_by") or "") not in owner_sel:
                continue
            filtered = filtered + [r]
        rows = filtered

    # Currency -> reference Price List (Production Settings child table "Currency Price List")
    cur_pl = {}
    if frappe.db.exists("DocType", "Currency Price List"):
        for r in frappe.get_all("Currency Price List",
                                filters={"parenttype": "Production Settings"},
                                fields=["currency", "price_list"]):
            cur_pl[r["currency"]] = r["price_list"]

    pl_cur = {}
    for p in frappe.get_all("Price List", fields=["name", "currency"]):
        pl_cur[p["name"]] = p["currency"]

    # Fallback reference price list by the Sales Order currency when a customer is
    # not explicitly mapped in Production Settings. Keyed on currency (not the SO's
    # selling_price_list, which can disagree with the SO currency) so the original
    # rate stays in the same currency as the sold rate.
    default_pl_by_currency = {}
    for name in pl_cur:
        cur = pl_cur[name]
        if name == (cur + " Price List"):
            default_pl_by_currency[cur] = name

    # Collect (price_list, item, original_length) combos for the reference price lookup
    need_pl = {}
    need_item = {}
    need_len = {}
    for r in rows:
        soc = r.get("so_currency")
        pl = cur_pl.get(soc)
        if not pl:
            pl = default_pl_by_currency.get(soc)
        r["price_list"] = pl
        r["currency"] = pl_cur.get(pl)
        if pl and r.get("original_length"):
            need_pl[pl] = 1
            need_item[r.get("variety")] = 1
            need_len[r.get("original_length")] = 1

    rate_map = {}
    if need_pl and need_item and need_len:
        p2 = {}
        def inlist(prefix, values):
            ph = []
            i = 0
            for v in values:
                k = prefix + str(i)
                ph = ph + ["%(" + k + ")s"]
                p2[k] = v
                i = i + 1
            return "(" + ", ".join(ph) + ")"
        sql = ("SELECT price_list, item_code, custom_length, price_list_rate, "
               "valid_from, valid_upto FROM `tabItem Price` "
               "WHERE selling = 1 AND price_list IN " + inlist("pl_", list(need_pl.keys())) +
               " AND item_code IN " + inlist("it_", list(need_item.keys())) +
               " AND custom_length IN " + inlist("ln_", list(need_len.keys())))
        cand = {}
        for ip in frappe.db.sql(sql, p2, as_dict=True):
            key = (ip["price_list"], ip["item_code"], ip["custom_length"])
            cand[key] = cand.get(key, []) + [ip]
        for key in cand:
            lst = cand[key]
            valid = []
            for x in lst:
                vf = str(x["valid_from"]) if x["valid_from"] else ""
                vu = str(x["valid_upto"]) if x["valid_upto"] else ""
                if (vf == "" or vf <= today) and (vu == "" or vu >= today):
                    valid = valid + [x]
            pool = valid if valid else lst
            best = pool[0]
            for x in pool:
                bf = str(best["valid_from"]) if best["valid_from"] else ""
                xf = str(x["valid_from"]) if x["valid_from"] else ""
                if xf > bf:
                    best = x
            rate_map[key] = best["price_list_rate"]

    def to_int(s):
        try:
            return int(float(str(s).strip()))
        except:
            return 0

    totals = {}
    total_stems = 0
    total_downgraded = 0
    unmapped = {}
    out = []
    for r in rows:
        avail = to_int(r.get("avail_raw"))
        r["avail_stems"] = avail
        total_stems = total_stems + avail
        dg = 0
        try:
            dg = int(float(r.get("line_stems") or 0))
        except:
            dg = 0
        r["dg_stems"] = dg
        total_downgraded = total_downgraded + dg
        pl = r.get("price_list")
        if not pl and r.get("so_currency"):
            unmapped[r.get("so_currency")] = 1

        # actual sold rate per stem = SO line rate / conversion factor
        sold = None
        if r.get("so_rate") is not None:
            conv = r.get("conv")
            try:
                conv = float(conv)
            except:
                conv = 1.0
            if not conv:
                conv = 1.0
            sold = round(float(r.get("so_rate")) / conv, 4)
        r["rate_picklist"] = sold

        orig = rate_map.get((pl, r.get("variety"), r.get("original_length")))
        r["rate_original"] = orig

        if pl and orig is not None and sold is not None:
            fg = round((float(orig) - sold) * dg, 2)
            r["foregone"] = fg
            cur = r.get("currency") or "?"
            totals[cur] = round(totals.get(cur, 0.0) + fg, 2)
        else:
            r["foregone"] = None

        r.pop("avail_raw", None)
        r.pop("line_stems", None)
        r.pop("conv", None)
        r.pop("so_rate", None)
        out = out + [r]

    # Grand total in KES: convert each currency's foregone at the latest selling
    # exchange rate (as of today) from Currency Exchange.
    fx_to_kes = {}
    total_foregone_kes = 0.0
    for cur in totals:
        if cur == "KES":
            rate = 1.0
        else:
            er = frappe.db.sql(
                "SELECT exchange_rate FROM `tabCurrency Exchange` "
                "WHERE from_currency = %(c)s AND to_currency = 'KES' AND for_selling = 1 "
                "AND date <= %(t)s ORDER BY date DESC LIMIT 1",
                {"c": cur, "t": today}, as_dict=True)
            rate = float(er[0]["exchange_rate"]) if er else None
        if rate is not None:
            fx_to_kes[cur] = rate
            total_foregone_kes = total_foregone_kes + totals[cur] * rate
    total_foregone_kes = round(total_foregone_kes, 2)

    reasons_list = list(reasons_set.keys())
    reasons_list.sort()
    owners_list = list(owners_set.keys())
    owners_list.sort()

    frappe.response["message"] = {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "total_lines": len(out),
        "total_stems": total_stems,
        "total_stems_downgraded": total_downgraded,
        "foregone_by_currency": totals,
        "total_foregone_kes": total_foregone_kes,
        "fx_to_kes": fx_to_kes,
        "unmapped_currencies": list(unmapped.keys()),
        "reasons": reasons_list,
        "owners": owners_list,
        "truncated": len(out) >= 2000,
        "rows": out,
    }
