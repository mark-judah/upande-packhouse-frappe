# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `packhouse-scheduler` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getScheduledOrders():
    # Frappe Server Script (API), api_method = getScheduledOrders
    # Flat map of every order on a Packhouse Schedule for a date -> {team, sequence}.
    # Powers the scheduler's scheduled/unscheduled marking (default = unscheduled).
    # Params: { date? } -> { status, date, scheduled: { <opl>: {team, sequence} } }
    fd = frappe.form_dict
    sdate = fd.get('date') or frappe.utils.today()
    scheduled = {}
    names = frappe.get_all("Packhouse Schedule", filters={"schedule_date": sdate}, pluck="name")
    i = 0
    while i < len(names):
        doc = frappe.get_doc("Packhouse Schedule", names[i])
        rows = doc.orders or []
        j = 0
        while j < len(rows):
            r = rows[j]
            if r.order_pick_list:
                scheduled[r.order_pick_list] = {"team": doc.team, "sequence": int(r.sequence or 0)}
            j = j + 1
        i = i + 1
    frappe.response["message"] = {"status": "success", "date": str(sdate), "scheduled": scheduled}


@frappe.whitelist()
def getSchedulerFeed():
    # Frappe Server Script (Type: API), api_method = getSchedulerFeed
    # Unified feed for the redesigned Packhouse Scheduler (2 tabs: Unscheduled + Schedule).
    # Returns every SCHEDULABLE Order Pick List for a date = draft (docstatus 0) or
    # submitted (docstatus 1) with ZERO issued buckets. Once ANY bucket is issued the
    # order is being processed and drops off entirely (user rule).
    # Each row carries what both tabs need: team, stems, bucket count, farm count, the
    # transfer state, and the mixed-type (from the Sales Order Items).
    # Variations (for the Unscheduled tab's show/hide control):
    #   draft_plain    - docstatus 0, no transfer activity on any line
    #   draft_transfer - docstatus 0, at least one line awaiting/loaded/in-transit
    #   submitted      - docstatus 1 (not yet issued)
    # Mixed-type (from Sales Order Item of the OPL):
    #   Mixed bunch  - any SO item custom_mixed_bunch = 1
    #   Mixed box    - any SO item custom_mixed_box = 1 (or OPL custom_is_mixed_box_pick_list)
    #   Straight box - otherwise
    # Payload: { date: "YYYY-MM-DD" }
    # safe_exec: no import / no += / no def / no f-strings. for-loops + .append + .get ok.

    frappe.response["message"] = {"success": False, "error": "Script failed"}

    try:
        fd = frappe.form_dict
        dd = fd.get("date") or frappe.utils.today()

        takt = frappe.db.get_single_value("Production Settings", "custom_takt_time") or 0

        # `dd` is the DELIVERY date. An OPL's shipping/delivery date lives on its Sales
        # Order (OPL has none of its own), so join it. The Schedule tab reads/writes the
        # Packhouse Schedule under the PROCESSING day (delivery - 1) — handled client-side.
        opls = frappe.db.sql("""
            SELECT o.name AS name, o.customer AS customer, o.order_name AS custom_order_name,
                   o.team AS custom_team, o.custom_total_stems AS custom_total_stems,
                   o.docstatus AS docstatus, 0 AS custom_is_mixed_box_pick_list,
                   o.creation AS creation
            FROM `tabOrder Pick List` o
            JOIN `tabSales Order` so ON so.name = o.sales_order
            WHERE so.delivery_date = %(dd)s AND o.docstatus < 2
            ORDER BY o.creation ASC
        """, {"dd": dd}, as_dict=True)

        names = []
        oi = 0
        while oi < len(opls):
            names.append(opls[oi].name)
            oi = oi + 1

        # ---- per-OPL line stats (buckets, farms, transfer, issued) ----
        stats = {}
        if len(names) > 0:
            plis = frappe.get_all(
                "Pick List Item",
                filters=[["parent", "in", names]],
                fields=["parent", "bucket", "source_warehouse",
                        "item_code", "issued", "awaiting_transfer",
                        "loaded_in_trolley", "in_transit"],
                limit_page_length=0,
            )
            pj = 0
            while pj < len(plis):
                r = plis[pj]
                op = r.get("parent")
                st = stats.get(op)
                if not st:
                    st = {"buckets": {}, "farms": {}, "varieties": {}, "transfer": 0, "issued": 0, "lines": 0}
                    stats[op] = st
                b = r.get("bucket")
                if b:
                    st["buckets"][b] = 1
                wh = (r.get("source_warehouse") or "").strip()
                farm = ""
                if wh:
                    farm = wh.split(" ")[0]
                if farm:
                    st["farms"][farm] = 1
                v = r.get("item_code")
                if v:
                    st["varieties"][v] = 1
                st["lines"] = st["lines"] + 1
                if int(r.get("issued") or 0) == 1:
                    st["issued"] = st["issued"] + 1
                xf = int(r.get("awaiting_transfer") or 0) + int(r.get("loaded_in_trolley") or 0) + int(r.get("in_transit") or 0)
                if xf > 0:
                    st["transfer"] = 1
                pj = pj + 1

        # ---- mixed-type from Sales Order Items ----
        mixed = {}
        if len(names) > 0:
            soi = frappe.get_all(
                "Sales Order Item",
                filters=[["custom_opl", "in", names]],
                fields=["custom_opl", "custom_mixed_box", "custom_mixed_bunch"],
                limit_page_length=0,
            )
            sj = 0
            while sj < len(soi):
                s = soi[sj]
                op = s.get("custom_opl")
                m = mixed.get(op)
                if not m:
                    m = {"box": 0, "bunch": 0}
                    mixed[op] = m
                if int(s.get("custom_mixed_box") or 0) == 1:
                    m["box"] = 1
                if int(s.get("custom_mixed_bunch") or 0) == 1:
                    m["bunch"] = 1
                sj = sj + 1

        out = []
        ci = 0
        while ci < len(opls):
            o = opls[ci]
            op = o.name
            st = stats.get(op) or {"buckets": {}, "farms": {}, "varieties": {}, "transfer": 0, "issued": 0, "lines": 0}

            # user rule: hide as soon as any bucket is issued (being processed)
            if st["issued"] > 0:
                ci = ci + 1
                continue

            m = mixed.get(op) or {"box": 0, "bunch": 0}
            if m["bunch"] == 1:
                mtype = "Mixed bunch"
            elif m["box"] == 1 or int(o.get("custom_is_mixed_box_pick_list") or 0) == 1:
                mtype = "Mixed box"
            else:
                mtype = "Straight box"

            ds = int(o.get("docstatus") or 0)
            has_x = st["transfer"]
            if ds == 0 and has_x == 0:
                variation = "draft_plain"
            elif ds == 0 and has_x == 1:
                variation = "draft_transfer"
            else:
                variation = "submitted"

            stems = 0
            try:
                stems = int(float(o.get("custom_total_stems") or 0))
            except:
                stems = 0

            farm_list = sorted(st["farms"].keys())

            row = {
                "opl": op,
                "order_name": o.get("custom_order_name") or op,
                "customer": o.get("customer") or "",
                "team": o.get("custom_team") or "",
                "total_stems": stems,
                "n_buckets": len(st["buckets"]),
                "n_farms": len(farm_list),
                "farms": farm_list,
                "n_varieties": len(st["varieties"]),
                "has_transfer": has_x,
                "mixed_type": mtype,
                "docstatus": ds,
                "variation": variation,
            }
            out.append(row)
            ci = ci + 1

        frappe.response["message"] = {"success": True, "data": out, "date": str(dd), "count": len(out), "takt_minutes": takt}

    except Exception as e:
        frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def saveDaySchedule():
    # Frappe Server Script (API), api_method = saveDaySchedule
    # Reorder IS the schedule: takes the global ordered ready list and rebuilds each
    # team's Packhouse Schedule doc (orders in the global order, resequenced 1..N per team).
    # Params: { date?, order }  order = "|~|"-joined OPL names in global order.
    fd = frappe.form_dict
    sdate = fd.get('date') or frappe.utils.today()
    order_raw = fd.get('order') or ''
    opls = []
    parts = order_raw.split("|~|") if order_raw else []
    p = 0
    while p < len(parts):
        v = (parts[p] or "").strip()
        if v:
            opls.append(v)
        p = p + 1

    info_map = {}
    if opls:
        recs = frappe.get_all("Order Pick List", filters={"name": ["in", opls]},
                              fields=["name", "team", "order_name", "customer"], limit_page_length=0)
        r = 0
        while r < len(recs):
            info_map[recs[r]["name"]] = recs[r]
            r = r + 1

    by_team = {}
    team_order = []
    i = 0
    while i < len(opls):
        info = info_map.get(opls[i])
        if info:
            team = info.get("team") or ""
            if team:
                if team not in by_team:
                    by_team[team] = []
                    team_order.append(team)
                by_team[team].append(opls[i])
        i = i + 1

    saved = {}
    k = 0
    while k < len(team_order):
        team = team_order[k]
        name = "PSCH-" + str(sdate) + "-" + str(team)
        if frappe.db.exists("Packhouse Schedule", name):
            doc = frappe.get_doc("Packhouse Schedule", name)
        else:
            doc = frappe.new_doc("Packhouse Schedule")
            doc.schedule_date = sdate
            doc.team = team
        doc.set("orders", [])
        rows = by_team[team]
        seq = 1
        j = 0
        while j < len(rows):
            info = info_map.get(rows[j]) or {}
            row = doc.append("orders", {})
            row.order_pick_list = rows[j]
            row.order_name = info.get("order_name") or ""
            row.customer = info.get("customer") or ""
            row.sequence = seq
            seq = seq + 1
            j = j + 1
        doc.save(ignore_permissions=True)
        saved[team] = len(rows)
        k = k + 1
    frappe.db.commit()
    frappe.response["message"] = {"status": "success", "saved": saved}
