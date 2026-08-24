# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse Dashboard API — ported verbatim from DB Server Scripts
# ("Get Dashboard Data" -> getDashboardData, "Get Boxes To Deliver" -> getBoxesToDeliver).
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getDashboardData():
    # Frappe Server Script (Type: API) - no return, no def, no import, no augmented assignment, no .append/.add/.update

    frappe.response["message"] = {"success": False, "error": "Script failed"}

    try:
        team_filter = frappe.form_dict.get("team_filter", "all")
        period      = frappe.form_dict.get("period", "today")
        from_date   = frappe.form_dict.get("from_date")
        to_date     = frappe.form_dict.get("to_date")
        rose_type   = frappe.form_dict.get("rose_type", "all")

        today    = frappe.utils.today()

        date_filter = None

        if period == "today":
            date_filter = ["date_created", "=", today]
        elif period == "yesterday":
            date_filter = ["date_created", "=", frappe.utils.add_days(today, -1)]
        elif period == "last_7_days":
            date_filter = ["date_created", ">=", frappe.utils.add_days(today, -7)]
        elif period == "custom":
            if from_date and to_date:
                from_d = frappe.utils.getdate(from_date)
                to_d   = frappe.utils.getdate(to_date)
                if from_d > to_d:
                    tmp = from_d
                    from_d = to_d
                    to_d = tmp
                date_filter = ["date_created", "between", [str(from_d), str(to_d)]]
            else:
                date_filter = ["date_created", "=", today]
        else:
            date_filter = ["date_created", "=", today]

        base_filters = []
        if date_filter is not None:
            base_filters = base_filters + [date_filter]
        if str(team_filter).lower() != "all" and str(team_filter).strip():
            team_list = [t.strip() for t in str(team_filter).split(",") if t.strip()]
            if team_list:
                base_filters = base_filters + [["team", "in", team_list]]

        # Get all OPLs with schedule number field
        all_opls_raw = frappe.get_all(
            "Order Pick List",
            filters=base_filters,
            fields=["name", "customer", "order_name", "team", "custom_total_stems", "docstatus", "creation"],
            order_by="creation desc"
        )

        # Sort by schedule number, treating null/empty as high values
        all_opls = []
        scheduled_items = []
        unscheduled_items = []

        i = 0
        while i < len(all_opls_raw):
            opl = all_opls_raw[i]
            schedule_num = opl.get("custom_schedule_number")

            has_schedule = False
            if schedule_num is not None:
                try:
                    num_val = int(schedule_num)
                    if num_val > 0:
                        has_schedule = True
                        scheduled_items = scheduled_items + [(opl, num_val)]
                except:
                    pass

            if not has_schedule:
                unscheduled_items = unscheduled_items + [opl]

            i = i + 1

        # Sort scheduled items by schedule number using bubble sort
        if len(scheduled_items) > 0:
            for i in range(len(scheduled_items)):
                for j in range(len(scheduled_items) - 1):
                    if scheduled_items[j][1] > scheduled_items[j + 1][1]:
                        temp = scheduled_items[j]
                        scheduled_items[j] = scheduled_items[j + 1]
                        scheduled_items[j + 1] = temp

            i = 0
            while i < len(scheduled_items):
                all_opls = all_opls + [scheduled_items[i][0]]
                i = i + 1

        i = 0
        while i < len(unscheduled_items):
            all_opls = all_opls + [unscheduled_items[i]]
            i = i + 1

        opl_names = []
        i = 0
        while i < len(all_opls):
            opl_names = opl_names + [all_opls[i].name]
            i = i + 1

        variety_by_opl = {}
        variety_stems_by_opl = {}
        shelf_by_opl = {}
        total_items_by_opl = {}
        issued_items_by_opl = {}
        total_stems_by_opl_pick = {}
        issued_stems_by_opl = {}
        spray_stems_by_opl = {}
        std_stems_by_opl = {}

        if len(opl_names) > 0:
            locations = frappe.get_all(
                "Pick List Item",
                filters=[["parent", "in", opl_names]],
                fields=["parent", "item_code", "item_name", "shelf", "issued", "stock_qty"]
            )
            pli_codes = []
            pc_i = 0
            while pc_i < len(locations):
                cur_ic = locations[pc_i].item_code
                if cur_ic and cur_ic not in pli_codes:
                    pli_codes = pli_codes + [cur_ic]
                pc_i = pc_i + 1
            item_group_map = {}
            if len(pli_codes) > 0:
                ig_rows = frappe.get_all("Item", filters=[["name", "in", pli_codes]], fields=["name", "item_group"])
                ig_i = 0
                while ig_i < len(ig_rows):
                    item_group_map[ig_rows[ig_i].name] = ig_rows[ig_i].item_group
                    ig_i = ig_i + 1
            i = 0
            while i < len(locations):
                loc = locations[i]
                p = loc.parent
                row_stems = int(loc.stock_qty or 0)

                if (item_group_map.get(loc.item_code, "") or "") == "Spray Roses":
                    spray_stems_by_opl[p] = spray_stems_by_opl.get(p, 0) + row_stems
                else:
                    std_stems_by_opl[p] = std_stems_by_opl.get(p, 0) + row_stems

                current_varieties = variety_by_opl.get(p, set())
                variety_by_opl[p] = current_varieties | {loc.item_name}

                vmap = variety_stems_by_opl.get(p, {})
                vmap[loc.item_name] = vmap.get(loc.item_name, 0) + row_stems
                variety_stems_by_opl[p] = vmap

                shelf = (loc.shelf or "").strip()
                if shelf != "":
                    current_shelves = shelf_by_opl.get(p, set())
                    shelf_by_opl[p] = current_shelves | {shelf}

                current_total = total_items_by_opl.get(p, 0)
                total_items_by_opl[p] = current_total + 1

                current_pick_stems = total_stems_by_opl_pick.get(p, 0)
                total_stems_by_opl_pick[p] = current_pick_stems + row_stems

                if loc.issued == 1:
                    current_issued = issued_items_by_opl.get(p, 0)
                    issued_items_by_opl[p] = current_issued + 1

                    current_issued_stems = issued_stems_by_opl.get(p, 0)
                    issued_stems_by_opl[p] = current_issued_stems + row_stems

                i = i + 1

        # Rose-type filter (Standards vs Spray Roses)
        if str(rose_type).lower() == "spray" or str(rose_type).lower() == "standard":
            want_spray = str(rose_type).lower() == "spray"
            filtered_opls = []
            k = 0
            while k < len(all_opls):
                nm = all_opls[k].name
                keep = spray_stems_by_opl.get(nm, 0) > 0 if want_spray else std_stems_by_opl.get(nm, 0) > 0
                if keep:
                    filtered_opls = filtered_opls + [all_opls[k]]
                k = k + 1
            all_opls = filtered_opls
            opl_names = []
            k = 0
            while k < len(all_opls):
                opl_names = opl_names + [all_opls[k].name]
                k = k + 1

        # ================================================================
        # Expected boxes per OPL — deduplicated for mixed boxes
        # ================================================================
        expected_boxes_by_opl = {}
        if len(opl_names) > 0:
            so_items = frappe.get_all(
                "Sales Order Item",
                filters=[["custom_opl", "in", opl_names], ["docstatus", "=", 1]],
                fields=["custom_opl", "custom_number_of_boxes", "custom_mixed_box",
                        "custom_mix_group", "custom_mixed_bunch", "custom_bunch_group"]
            )

            # Boxes are counted ONCE per group: a mixed bunch shares boxes across the
            # whole custom_bunch_group, a mixed box across the whole custom_mix_group.
            # Straight lines each contribute their own custom_number_of_boxes.
            seen_groups = set()

            i = 0
            while i < len(so_items):
                item = so_items[i]
                oid = item.custom_opl
                boxes = item.custom_number_of_boxes or 0

                bunch_group = str(item.custom_bunch_group or "").strip()
                mix_group = str(item.custom_mix_group or "").strip()
                is_bunch = item.custom_mixed_bunch == 1 and bunch_group != ""
                is_mixed = item.custom_mixed_box == 1 and mix_group != ""

                if is_bunch:
                    group_key = str(oid) + "||bunch||" + bunch_group
                elif is_mixed:
                    group_key = str(oid) + "||mix||" + mix_group
                else:
                    group_key = None

                if group_key is not None:
                    if group_key not in seen_groups:
                        seen_groups = seen_groups | {group_key}
                        current = expected_boxes_by_opl.get(oid, 0)
                        expected_boxes_by_opl[oid] = current + boxes
                else:
                    current = expected_boxes_by_opl.get(oid, 0)
                    expected_boxes_by_opl[oid] = current + boxes

                i = i + 1

        packed_boxes_by_opl = {}
        packed_stems_by_opl = {}

        if len(opl_names) > 0:
            fpls = frappe.get_all(
                "Farm Pack List",
                filters=[["order_pick_list", "in", opl_names], ["docstatus", "!=", 2]],
                fields=["name", "order_pick_list"]
            )
            i = 0
            while i < len(fpls):
                f = fpls[i]
                oid = f.order_pick_list
                try:
                    doc = frappe.get_doc("Farm Pack List", f.name)
                    stems = 0
                    boxes = set()
                    items = doc.pack_list_item or []
                    j = 0
                    while j < len(items):
                        row = items[j]
                        stems = stems + (row.stock_qty or 0)
                        if row.box_id:
                            boxes = boxes | {str(row.box_id).strip()}
                        j = j + 1

                    current_stems = packed_stems_by_opl.get(oid, 0)
                    packed_stems_by_opl[oid] = current_stems + stems

                    current_boxes = packed_boxes_by_opl.get(oid, 0)
                    packed_boxes_by_opl[oid] = current_boxes + len(boxes)
                except:
                    pass
                i = i + 1

        box_labels = []
        boxes_printed_count = 0

        if len(opl_names) > 0:
            bf = [["order_pick_list", "in", opl_names]]
            if date_filter is not None:
                bdf = date_filter[:]
                if bdf[0] == "date_created":
                    bdf[0] = "creation"
                if bdf[1] == "between":
                    if isinstance(bdf[2], list) and len(bdf[2]) == 2:
                        start_date = str(bdf[2][0])
                        end_date = str(bdf[2][1])
                        bdf[2] = [start_date, end_date]
                bf = bf + [bdf]

            box_labels = frappe.get_all(
                "Box Label",
                filters=bf,
                fields=["name", "order_pick_list", "customer", "box_number", "box_total_count"],
                order_by="creation desc"
            )
            boxes_printed_count = len(box_labels)

        processed_opls = []
        ready_to_issue = []
        ready_count = 0

        # ================================================================
        # Accumulators for average issuing / packing KPIs
        # ================================================================
        sum_issuing_pct = 0
        sum_packing_pct = 0
        total_all_pick_stems = 0
        total_all_issued_stems = 0
        total_all_planned_stems = 0
        total_all_packed_stems = 0

        i = 0
        while i < len(all_opls):
            opl = all_opls[i]
            oid = opl.name
            submitted = opl.docstatus == 1

            planned = 0
            try:
                planned = int(float(opl.custom_total_stems or 0))
            except:
                planned = 0

            bunches = planned // 10 if planned > 0 else 0

            ti = total_items_by_opl.get(oid, 0)
            ii = issued_items_by_opl.get(oid, 0)

            pick_stems = total_stems_by_opl_pick.get(oid, 0)
            iss_stems = issued_stems_by_opl.get(oid, 0)

            # Issuing % is now stems-based
            issuing_pct = round(iss_stems / pick_stems * 100) if pick_stems > 0 else 0
            if issuing_pct > 100:
                issuing_pct = 100

            ps = packed_stems_by_opl.get(oid, 0)
            packing_pct = round(ps / planned * 100) if planned > 0 else 0
            if packing_pct > 100:
                packing_pct = 100

            # Accumulate for averages (stems-based)
            sum_issuing_pct = sum_issuing_pct + issuing_pct
            sum_packing_pct = sum_packing_pct + packing_pct
            total_all_pick_stems = total_all_pick_stems + pick_stems
            total_all_issued_stems = total_all_issued_stems + iss_stems
            total_all_planned_stems = total_all_planned_stems + planned
            total_all_packed_stems = total_all_packed_stems + ps

            eb = expected_boxes_by_opl.get(oid, 0)
            pb = packed_boxes_by_opl.get(oid, 0)
            box_prog = str(pb) + "/" + str(eb) if eb > 0 else "0/0"

            vs = variety_by_opl.get(oid, set())
            vmap = variety_stems_by_opl.get(oid, {})
            vpairs = []
            for vn in vmap:
                vpairs = vpairs + [{"name": vn, "stems": int(vmap[vn] or 0)}]
            # sort by stems desc (bubble; per-OPL variety counts are small)
            a = 0
            while a < len(vpairs):
                b = 0
                while b < len(vpairs) - 1:
                    if vpairs[b]["stems"] < vpairs[b + 1]["stems"]:
                        tmpv = vpairs[b]
                        vpairs[b] = vpairs[b + 1]
                        vpairs[b + 1] = tmpv
                    b = b + 1
                a = a + 1
            variety = "N/A"
            if len(vs) == 1:
                variety = list(vs)[0]
            elif len(vs) > 1:
                variety = "Mixed Varieties"

            ss = shelf_by_opl.get(oid, set())
            shelves = "N/A"
            if len(ss) > 0:
                shelves = ", ".join(sorted(ss))

            row = {
                "opl_id": oid,
                "order_name": opl.order_name or oid,
                "customer": opl.customer,
                "team": opl.team or "Unassigned",
                "total_bunches": bunches,
                "total_stems": planned,
                "shelf_locations": shelves,
                "variety": variety,
                "varieties": vpairs,
                "current_stage": "Fully Allocated" if submitted else "Partially Allocated",
                "issuing_percentage": issuing_pct,
                "packing_percentage": packing_pct,
                "box_progress": box_prog,
                "is_ready_to_issue": submitted,
                "is_urgent": False,
                "custom_schedule_number": opl.get("custom_schedule_number") or "",
                "rose_type": ("Mixed" if (spray_stems_by_opl.get(oid, 0) > 0 and std_stems_by_opl.get(oid, 0) > 0) else ("Spray Roses" if spray_stems_by_opl.get(oid, 0) > 0 else ("Standard Roses" if std_stems_by_opl.get(oid, 0) > 0 else "")))
            }

            processed_opls = processed_opls + [row]

            if submitted:
                ready_count = ready_count + 1
                ready_to_issue = ready_to_issue + [row]

            i = i + 1

        total_exp = 0
        for k in expected_boxes_by_opl:
            total_exp = total_exp + expected_boxes_by_opl[k]

        total_pkd = 0
        for k in packed_boxes_by_opl:
            total_pkd = total_pkd + packed_boxes_by_opl[k]

        global_prog = str(total_pkd) + "/" + str(total_exp) if total_exp > 0 else "0/0"

        # ================================================================
        # Compute averages and fulfillment rate
        # ================================================================
        opl_count = len(processed_opls)

        avg_issuing = round(sum_issuing_pct / opl_count) if opl_count > 0 else 0
        avg_packing = round(sum_packing_pct / opl_count) if opl_count > 0 else 0

        # Fulfillment rate: of total pick list stems, what % got issued (stems-based)
        global_issuing_pct = round(total_all_issued_stems / total_all_pick_stems * 100) if total_all_pick_stems > 0 else 0

        # Packing efficiency: of total planned stems, what % got packed
        global_packing_pct = round(total_all_packed_stems / total_all_planned_stems * 100) if total_all_planned_stems > 0 else 0
        if global_packing_pct > 100:
            global_packing_pct = 100

        # Fulfillment rate (BOX-based): of the total boxes ordered across the sales
        # orders in scope, what % have actually been packed (Farm Pack List boxes).
        # total_exp = ordered boxes (SO Item custom_number_of_boxes, mixed-box deduped)
        # total_pkd = packed boxes (distinct box_id on Farm Pack List)
        global_fulfillment_pct = round(total_pkd / total_exp * 100) if total_exp > 0 else 0
        if global_fulfillment_pct > 100:
            global_fulfillment_pct = 100

        # Issuing-to-packing gap: difference between avg packing and avg issuing
        # Positive = packing ahead of issuing (stock waiting to be issued)
        # Negative = issuing ahead of packing (unusual)
        gap = avg_packing - avg_issuing

        frappe.response["message"] = {
            "success": True,
            "total_opls": len(processed_opls),
            "ready_count": ready_count,
            "boxes_printed_today": boxes_printed_count,
            "box_labels_today": box_labels,
            "opls": processed_opls,
            "ready_to_issue": ready_to_issue,
            "total_box_progress": global_prog,
            # New KPI fields (all stems-based)
            "avg_issuing_pct": avg_issuing,
            "avg_packing_pct": avg_packing,
            "global_issuing_pct": global_issuing_pct,
            "global_packing_pct": global_packing_pct,
            "global_fulfillment_pct": global_fulfillment_pct,
            "total_expected_boxes": total_exp,
            "total_packed_boxes": total_pkd,
            "issuing_packing_gap": gap,
            "total_pick_stems": total_all_pick_stems,
            "total_issued_stems": total_all_issued_stems,
            "total_planned_stems": total_all_planned_stems,
            "total_packed_stems": total_all_packed_stems
        }

    except:
        frappe.response["message"] = {
            "success": False,
            "error": "Internal server error"
        }


@frappe.whitelist()
def getBoxesToDeliver():
    try:
        frm = frappe.form_dict.get('from_date') or frappe.utils.today()
        to = frappe.form_dict.get('to_date') or frm
        teams_raw = frappe.form_dict.get('teams') or ''
        teams = [t.strip() for t in teams_raw.split(',') if t.strip()]

        # Sales Orders shipping in the window (exclude drafts + cancelled/closed/completed).
        so_rows = frappe.get_all(
            "Sales Order",
            filters={
                "delivery_date": ["between", [frm, to]],
                "docstatus": 1,
                "status": ["not in", ["Cancelled", "Closed", "Completed"]],
            },
            fields=["name"],
            limit_page_length=0,
        )
        so_names = [s.name for s in so_rows]

        # Team filter: Sales Orders carry no team; map via their OPLs.
        if teams and so_names:
            opl_rows = frappe.get_all(
                "Order Pick List",
                filters={"team": ["in", teams], "sales_order": ["in", so_names]},
                fields=["sales_order"],
                limit_page_length=0,
            )
            team_so = set(x.sales_order for x in opl_rows if x.sales_order)
            so_names = [n for n in so_names if n in team_so]

        boxes = 0
        stems = 0
        if so_names:
            items = frappe.get_all(
                "Sales Order Item",
                filters={"parent": ["in", so_names], "parenttype": "Sales Order"},
                fields=["parent", "idx", "stock_qty", "custom_number_of_boxes",
                        "custom_mixed_box", "custom_mix_group",
                        "custom_mixed_bunch", "custom_bunch_group"],
                limit_page_length=0,
            )
            # Boxes counted ONCE per group (mixed bunch -> custom_bunch_group,
            # mixed box -> custom_mix_group, else per straight line). Stems per line.
            seen = set()
            for it in items:
                stems = stems + (it.get("stock_qty") or 0)
                bg = str(it.get("custom_bunch_group") or "").strip()
                mg = str(it.get("custom_mix_group") or "").strip()
                if it.get("custom_mixed_bunch") == 1 and bg != "":
                    key = str(it.parent) + "||bunch||" + bg
                elif it.get("custom_mixed_box") == 1 and mg != "":
                    key = str(it.parent) + "||mix||" + mg
                else:
                    key = str(it.parent) + "||line||" + str(it.idx)
                if key not in seen:
                    seen = seen | {key}
                    boxes = boxes + (it.get("custom_number_of_boxes") or 0)

        frappe.response["message"] = {"orders": len(so_names), "boxes": boxes, "stems": stems}
    except Exception as e:
        frappe.response["message"] = {"error": str(e), "orders": 0, "boxes": 0, "stems": 0}
