# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `packhouse-discards` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getDiscardData():
    # FETCH DISCARD DATA (api_method: getDiscardData) — v16
    # v16 discards live in Discard Request / Discard Request Bucket (not Stock Entry
    # "Discard"). Buckets carry stem_qty, age_days and harvest_date directly.
    fd = frappe.form_dict
    from_date = fd.get("from_date")
    to_date = fd.get("to_date")
    if not from_date or not to_date:
        today0 = frappe.utils.today()
        from_date = today0
        to_date = today0

    rose = (fd.get("rose_type") or "all").lower()
    rose_cond = ""
    if rose == "standard":
        rose_cond = " AND dr.item_group = 'Standard Roses'"
    elif rose == "spray":
        rose_cond = " AND dr.item_group = 'Spray Roses'"

    params = {"from_date": from_date, "to_date": to_date}
    DATE_COL = "COALESCE(dr.approval_date, dr.requested_date)"
    BASE = ("FROM `tabDiscard Request Bucket` drb "
            "INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent "
            "WHERE COALESCE(dr.workflow_state, '') = 'Approved' "
            "AND " + DATE_COL + " BETWEEN %(from_date)s AND %(to_date)s" + rose_cond)

    rows = frappe.db.sql(
        "SELECT " + DATE_COL + " AS date, drb.bucket_id AS bucket, drb.farm AS farm, "
        "dr.coldroom AS location, drb.stem_length AS length, drb.variety AS variety, "
        "dr.item_group AS rose_group, COALESCE(drb.stem_qty, 0) AS stems, drb.age_days AS age_days "
        + BASE + " ORDER BY " + DATE_COL + " DESC, drb.name DESC LIMIT 1000",
        params, as_dict=True)

    agg = frappe.db.sql(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(drb.stem_qty), 0) AS stems, AVG(drb.age_days) AS avg_age "
        + BASE, params, as_dict=True)[0]

    by_variety = frappe.db.sql(
        "SELECT drb.variety AS variety, COUNT(*) AS buckets, COALESCE(SUM(drb.stem_qty), 0) AS stems "
        + BASE + " GROUP BY drb.variety ORDER BY stems DESC LIMIT 8", params, as_dict=True)

    by_farm = frappe.db.sql(
        "SELECT drb.farm AS farm, COUNT(*) AS buckets, COALESCE(SUM(drb.stem_qty), 0) AS stems "
        + BASE + " GROUP BY drb.farm ORDER BY stems DESC LIMIT 8", params, as_dict=True)

    today = frappe.utils.today()
    val_pl = None
    try:
        val_pl = frappe.db.get_single_value("Production Settings", "custom_discard_valuation_price_list")
    except Exception:
        val_pl = None
    if not val_pl:
        val_pl = "EUR Price List"
    val_currency = frappe.db.get_value("Price List", val_pl, "currency")

    rate_map = {}
    need_item = {}
    need_len = {}
    for r in rows:
        if r.get("variety"):
            need_item[r.get("variety")] = 1
        if r.get("length"):
            need_len[r.get("length")] = 1
    if need_item and need_len and val_currency:
        p2 = {"vpl": val_pl}
        def inlist(prefix, values):
            ph = []
            i = 0
            for v in values:
                k = prefix + str(i)
                ph = ph + ["%(" + k + ")s"]
                p2[k] = v
                i = i + 1
            return "(" + ", ".join(ph) + ")"
        sql = ("SELECT item_code, custom_length, price_list_rate, valid_from, valid_upto "
               "FROM `tabItem Price` WHERE selling = 1 AND price_list = %(vpl)s "
               "AND item_code IN " + inlist("it_", list(need_item.keys())) +
               " AND custom_length IN " + inlist("ln_", list(need_len.keys())))
        cand = {}
        for ip in frappe.db.sql(sql, p2, as_dict=True):
            key = (ip["item_code"], ip["custom_length"])
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

    total_foregone = 0.0
    if val_currency:
        tf = frappe.db.sql(
            "SELECT COALESCE(SUM(COALESCE(drb.stem_qty, 0) * ip.price_list_rate), 0) AS total "
            "FROM `tabDiscard Request Bucket` drb "
            "INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent "
            "INNER JOIN `tabItem Price` ip ON ip.price_list = %(vpl)s AND ip.item_code = drb.variety "
            " AND ip.custom_length = drb.stem_length AND ip.selling = 1 "
            " AND ip.valid_from <= %(today)s AND (ip.valid_upto IS NULL OR ip.valid_upto >= %(today)s) "
            "WHERE COALESCE(dr.workflow_state, '') = 'Approved' "
            "AND " + DATE_COL + " BETWEEN %(from_date)s AND %(to_date)s" + rose_cond,
            {"vpl": val_pl, "today": today, "from_date": from_date, "to_date": to_date},
            as_dict=True)[0]
        total_foregone = round(float(tf.get("total") or 0), 2)

    if val_currency == "KES":
        fx_kes = 1.0
    elif val_currency:
        er = frappe.db.sql(
            "SELECT exchange_rate FROM `tabCurrency Exchange` "
            "WHERE from_currency = %(c)s AND to_currency = 'KES' AND for_selling = 1 "
            "AND date <= %(t)s ORDER BY date DESC LIMIT 1",
            {"c": val_currency, "t": today}, as_dict=True)
        fx_kes = float(er[0]["exchange_rate"]) if er else None
    else:
        fx_kes = None
    total_foregone_kes = round(total_foregone * fx_kes, 2) if fx_kes is not None else None

    out_rows = []
    for r in rows:
        r["stems"] = int(r.get("stems") or 0)
        r["age_days"] = int(r["age_days"]) if r.get("age_days") is not None else None
        rt = rate_map.get((r.get("variety"), r.get("length")))
        r["rate"] = float(rt) if rt is not None else None
        r["foregone"] = round(float(rt) * r["stems"], 2) if rt is not None else None
        out_rows = out_rows + [r]
    for r in by_variety:
        r["stems"] = int(r.get("stems") or 0)
        r["buckets"] = int(r.get("buckets") or 0)
    for r in by_farm:
        r["stems"] = int(r.get("stems") or 0)
        r["buckets"] = int(r.get("buckets") or 0)

    frappe.response["message"] = {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "total_buckets": int(agg.get("cnt") or 0),
        "total_stems": int(agg.get("stems") or 0),
        "avg_age_days": round(float(agg.get("avg_age")), 1) if agg.get("avg_age") is not None else None,
        "total_foregone": total_foregone,
        "total_foregone_kes": total_foregone_kes,
        "fx_to_kes": fx_kes,
        "valuation_price_list": val_pl,
        "valuation_currency": val_currency,
        "truncated": len(out_rows) >= 1000,
        "rows": out_rows,
        "by_variety": by_variety,
        "by_farm": by_farm,
    }
