# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Mobile API — server scripts called by the Upande mobile apps, ported
# verbatim from DB Server Scripts (bench + kaitet-group v15 live) into
# version-controlled whitelisted methods. Bodies keep frappe.form_dict /
# frappe.response exactly as the live scripts used them.

import frappe
from frappe import _
import json
import math


@frappe.whitelist(allow_guest=True, methods=["POST"])
def mobileLogin(usr=None, pwd=None):
    """Mobile-friendly login that returns the session id in the JSON body.

    The stock `/api/method/login` only delivers `sid` via the Set-Cookie header.
    Mobile HTTP stacks (iOS NSURLSession in particular) absorb Set-Cookie into the
    native cookie store and never expose it to JavaScript, so an app that manages
    its own Cookie header can't read `sid` back — the symptom is
    "login succeeded but no session cookie returned". Authenticating here and
    returning `sid` in the body is deterministic across iOS/Android/web.
    """
    from frappe.auth import LoginManager

    usr = usr or frappe.form_dict.get("usr")
    pwd = pwd or frappe.form_dict.get("pwd")
    if not usr or not pwd:
        frappe.throw(_("Both usr and pwd are required."), frappe.AuthenticationError)

    login_manager = frappe.local.login_manager = LoginManager()
    login_manager.authenticate(user=usr, pwd=pwd)   # raises AuthenticationError on bad creds
    login_manager.post_login()                       # creates the session (sets sid + cookie)

    user = frappe.session.user
    user_type = frappe.db.get_value("User", user, "user_type")
    return {
        "success": True,
        "sid": frappe.session.sid,
        "user_id": user,
        "full_name": frappe.db.get_value("User", user, "full_name") or user,
        "system_user": "yes" if user_type == "System User" else "no",
    }


@frappe.whitelist()
def createBunchHandover():
    try:
        data = frappe.request.get_json() or {}
        grader = data.get("grader")
        packer = data.get("packer")
        bunches = data.get("bunches")
        handover_time = data.get("handover_time")

        if not grader or not packer:
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": "Grader and packer are required."}
        else:
            try:
                bunches_val = int(bunches)
            except Exception:
                bunches_val = 0
            if bunches_val <= 0:
                frappe.response["http_status_code"] = 400
                frappe.response["message"] = {"status": "error", "message": "Enter a bunches count greater than zero."}
            else:
                doc = frappe.new_doc("Bunch Handover")
                doc.grader = grader
                doc.packer = packer
                doc.bunches = bunches_val
                if handover_time:
                    doc.handover_time = handover_time
                doc.insert(ignore_permissions=True)
                frappe.db.commit()
                frappe.response["message"] = {"status": "success", "name": doc.name, "bunches": bunches_val}
    except Exception as e:
        frappe.log_error("Bunch Handover Error", str(e))
        frappe.response["http_status_code"] = 500
        frappe.response["message"] = {"status": "error", "message": str(e)}


@frappe.whitelist()
def createLoadingEntry():
    # Record Loading Scan
    # API: record_loading_scan
    # Params: box_label_name, vehicle, temperature, delivery_date
    # Scans a box label, validates it, adds to Loading Sheet, marks Box Label as loaded
    try:
        data = frappe.form_dict.get("data")
        if not data:
            frappe.response["message"] = {"status": "error", "message": "No data provided"}
        else:
            if isinstance(data, str):
                data = frappe.parse_json(data)

            box_label_name = data.get("box_label_name", "")
            vehicle = data.get("vehicle", "")
            temperature = data.get("temperature", 0)
            delivery_date = data.get("delivery_date", "")

            if not box_label_name:
                frappe.response["message"] = {"status": "error", "message": "Box label name is required"}
            elif not frappe.db.exists("Box Label", box_label_name):
                frappe.response["message"] = {"status": "error", "message": "Box label not found: " + box_label_name}
            else:
                # Fetch box label
                box_doc = frappe.get_doc("Box Label", box_label_name)

                # Validations
                if box_doc.loaded == 1:
                    frappe.response["message"] = {"status": "error", "message": "Box already loaded: " + box_label_name}
                else:
                    # Get or create Loading Sheet for today
                    ls_date = delivery_date or frappe.utils.today()
                    ls_name = "LS-" + str(ls_date)

                    if frappe.db.exists("Loading Sheet", ls_name):
                        ls_doc = frappe.get_doc("Loading Sheet", ls_name)
                    else:
                        ls_doc = frappe.new_doc("Loading Sheet")
                        ls_doc.date = ls_date
                        ls_doc.status = "Loading"
                        ls_doc.insert()

                    # Check if this box is already in the loading sheet
                    already_added = False
                    for item in ls_doc.items:
                        if item.box_label_link == box_label_name:
                            already_added = True
                            break

                    if already_added:
                        frappe.response["message"] = {"status": "error", "message": "Box already on loading sheet"}
                    else:
                        # Get loading position from Loading Plan
                        position = 0
                        customer = box_doc.customer or ""
                        dp = box_doc.delivery_point or ""
                        plan_name = "LP-" + str(ls_date)

                        if frappe.db.exists("Loading Plan", plan_name):
                            plan_doc = frappe.get_doc("Loading Plan", plan_name)
                            for pi in plan_doc.loading_plan_items:
                                if pi.customer == customer and pi.delivery_point == dp:
                                    position = pi.loading_position or 0
                                    break

                        # Get farm pack list from box label
                        fpl = box_doc.farm_pack_lis or ""

                        # Add to Loading Sheet
                        ls_doc.append("items", {
                            "farm_pack_list": fpl,
                            "delivery_point": dp,
                            "box_label": box_label_name,
                            "box_id": box_doc.box_number or 0,
                            "customer": customer,
                            "box_label_link": box_label_name,
                            "position": position,
                            "loaded": 1
                        })

                        # Update total boxes
                        ls_doc.total_boxes = len(ls_doc.items)
                        ls_doc.status = "Loading"
                        ls_doc.save()

                        # Mark Box Label as loaded
                        frappe.db.set_value("Box Label", box_label_name, "loaded", 1)

                        frappe.db.commit()

                        frappe.response["message"] = {
                            "status": "success",
                            "message": "Box " + box_label_name + " loaded (Position " + str(position) + ")",
                            "data": {
                                "box_label": box_label_name,
                                "customer": customer,
                                "delivery_point": dp,
                                "position": position,
                                "box_number": box_doc.box_number or 0,
                                "loading_sheet": ls_name,
                                "total_boxes_on_sheet": len(ls_doc.items)
                            }
                        }

    except Exception as e:
        frappe.log_error("record_loading_scan error: " + str(e))
        frappe.response["message"] = {"status": "error", "message": str(e)}


@frappe.whitelist()
def createOrUpdateDispatch():
    try:
        data = frappe.request.get_json() or {}
        delivery_date = data.get("delivery_date") or frappe.utils.add_days(frappe.utils.nowdate(), 1)
        ls_name = "LS-" + str(delivery_date)

        if not frappe.db.exists("Loading Sheet", ls_name):
            frappe.response["data"] = {"status": "error", "message": "No loading sheet for " + str(delivery_date)}
        else:
            ls = frappe.get_doc("Loading Sheet", ls_name)

            # Group loaded boxes by the Sales Order they belong to.
            by_so = {}
            for it in ls.items:
                box_name = it.get("box_label") or it.get("box_label_link")
                if not box_name or not frappe.db.exists("Box Label", box_name):
                    continue
                box = frappe.get_doc("Box Label", box_name)
                so_name = box.customer_purchase_order
                if so_name:
                    by_so.setdefault(so_name, []).append(box)

            results = []
            for so_name, boxes in by_so.items():
                if not frappe.db.exists("Sales Order", so_name):
                    continue
                so = frappe.get_doc("Sales Order", so_name)

                # Aggregate box contents by variety (stems = bunches * bunch size from uom).
                var = {}
                total_boxes = 0
                for box in boxes:
                    total_boxes = total_boxes + 1
                    for bi in box.box_item:
                        v = bi.variety
                        size_digits = "".join([c for c in str(bi.uom or "") if c.isdigit()])
                        bunch_size = int(size_digits) if size_digits else 1
                        stems = (bi.qty or 0) * bunch_size
                        d = var.setdefault(v, {"stems": 0, "boxes": 0, "length": bi.length, "source_farm": bi.source_farm})
                        d["stems"] = d["stems"] + stems
                        d["boxes"] = d["boxes"] + 1

                so_items = {}
                for si in so.items:
                    if si.item_code not in so_items:
                        so_items[si.item_code] = si

                farm = so.get("custom_farm") or (boxes[0].farm if boxes else None)
                farm_code = frappe.db.get_value("Farm", farm, "kephis_farm_id") if farm else ""

                existing = frappe.get_all("Delivery Note", filters={"custom_so": so_name, "docstatus": 0}, pluck="name")
                if existing:
                    dn = frappe.get_doc("Delivery Note", existing[0])
                    dn.items = []
                    action = "updated"
                else:
                    dn = frappe.new_doc("Delivery Note")
                    action = "created"

                dn.customer = so.customer
                dn.company = so.company
                dn.posting_date = frappe.utils.nowdate()
                dn.currency = so.currency
                dn.selling_price_list = so.selling_price_list
                dn.custom_business_unit = "Roses"
                dn.custom_so = so_name
                dn.custom_farm = farm
                dn.custom_freight = so.get("custom_shipping_agent")
                dn.custom_transport_mode = so.get("custom_mode_of_transport")
                dn.custom_brn_ref = so.get("custom_s_number")
                dn.custom_consignee = so.get("custom_consignee")
                dn.custom_delivery_point = so.get("custom_delivery_point")
                dn.custom_flo_id = so.get("custom_customer_flo_id") or so.get("custom_company_flo_id")
                dn.custom_flo_id_2 = so.get("custom_company_flo_id")
                dn.custom_total_boxes = total_boxes
                dn.po_no = so.get("po_no")

                for v in var:
                    d = var[v]
                    soi = so_items.get(v)
                    rate = soi.rate if soi else 0
                    warehouse = (soi.warehouse if soi else None) or "Nanyuki Receiving Cold Store - UFL"
                    ig = frappe.db.get_value("Item", v, "item_group") or ""
                    if ig == "Gypsophila":
                        hsc = "060319"
                        crop = "Gypsophilla"
                    else:
                        hsc = "060311"
                        crop = "Std Rose"
                    stems = d["stems"]
                    boxes_ct = d["boxes"]
                    dn.append("items", {
                        "item_code": v,
                        "qty": stems,
                        "uom": "Stems",
                        "stock_uom": "Stems",
                        "conversion_factor": 1,
                        "rate": rate,
                        "warehouse": warehouse,
                        "custom_length": d["length"],
                        "custom_total_boxes": boxes_ct,
                        "custom_total_stems": stems,
                        "custom_stems_per_box": (stems / boxes_ct) if boxes_ct else 0,
                        "custom_source_farm": d["source_farm"],
                        "custom_farm_codes": farm_code,
                        "custom_hsc": hsc,
                        "custom_crop_type": crop,
                    })

                dn.flags.ignore_permissions = True
                dn.flags.ignore_mandatory = True
                dn.insert(ignore_permissions=True)
                results.append({"delivery_note": dn.name, "action": action, "sales_order": so_name, "boxes": total_boxes})

            frappe.db.commit()
            frappe.response["data"] = {
                "status": "success",
                "message": "Built " + str(len(results)) + " delivery note(s) for " + str(delivery_date),
                "delivery_notes": results,
            }
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error("createOrUpdateDispatch (DN) error", str(e))
        frappe.response["data"] = {"status": "error", "message": str(e)}


@frappe.whitelist()
def createOrUpdateFarmPackList():
    try:
        # Get incoming JSON payload
        data = frappe.request.get_json()
        frappe.log_error("Packing payload", data)

        # Extract values from JSON
        sale_order_id = data.get("custom_sales_order")
        customer_id = data.get("custom_customer")
        user_farm = data.get("custom_farm")
        # The app sometimes sends a warehouse (e.g. "Kapkolia Receiving Cold
        # Store - UFL") where a Farm is expected. Resolve it to the real Farm
        # via the warehouse's custom_farm so the Farm Pack List link is valid.
        if user_farm and not frappe.db.exists("Farm", user_farm):
            _resolved = frappe.db.get_value("Warehouse", user_farm, "custom_farm")
            if _resolved:
                user_farm = _resolved
        order_pick_list_id = data.get("custom_order_pick_list")
        items = data.get("items")

        if not sale_order_id:
            frappe.throw(_("Sale Order ID is required"))

        if isinstance(items, str):
            import json
            items = json.loads(items)

        if not items or not isinstance(items, list):
            frappe.throw(_("Invalid or missing item list"))

        # Get the Order Pick List to fetch correct warehouse
        order_pick_list = frappe.get_doc("Order Pick List", order_pick_list_id)

        processed_items = []
        already_packed_bunches = []

        for idx, entry in enumerate(items):
            item_code = entry.get("item_code")
            bunch_uom = entry.get("bunch_uom")
            bunch_id = entry.get("bunch_id")  # Will be None for standard roses
            stem_length = entry.get("custom_stem_length")
            box_id = entry.get("box_id") or "1"
            bunch_qty = entry.get("bunch_qty") or 1

            # bunch_id is NOT required for standard roses (manual entry)
            if not all([item_code, bunch_uom, stem_length]):
                continue

            # === Only validate bunch grading for spray roses (when bunch_id exists) ===
            if bunch_id:
                grading_stock_entry = frappe.get_all(
                    "Stock Entry",
                    fields=["name", "custom_bunch_id", "custom_scanned_packing"],
                    filters={"custom_bunch_id": bunch_id},
                    limit=1
                )

                if not grading_stock_entry:
                    frappe.throw("This bunch has not been graded in the system. Perform the grading scan on it to enable packing")

                if grading_stock_entry[0].get("custom_scanned_packing") == 1:
                    already_packed_bunches.append({
                        "bunch_id": bunch_id,
                        "item_code": item_code,
                        "stem_length": stem_length,
                        "box_id": box_id
                    })
                    continue

            source_warehouse = None
            for location in order_pick_list.table_ytkc:
                if (location.item_code == item_code and
                    location.get("stem_length") == stem_length and
                    location.uom == bunch_uom):
                    source_warehouse = location.source_warehouse
                    break

            if not source_warehouse and order_pick_list.table_ytkc:
                source_warehouse = order_pick_list.table_ytkc[0].source_warehouse

            try:
                stems_per_bunch = int(bunch_uom.split("(")[1].split(")")[0])
            except (IndexError, ValueError, AttributeError):
                # UOM without a "(N)" bunch size (e.g. "Stems") = 1 stem per unit.
                stems_per_bunch = 1

            # For standard roses: bunch_qty comes from frontend, stems = qty * stems_per_bunch
            # For spray roses: bunch_qty = 1, stems = stems_per_bunch
            number_of_stems = stems_per_bunch * bunch_qty

            item_entry = {
                "doctype": "pack_list_item",
                "item_code": item_code,
                "bunch_uom": bunch_uom,
                "bunch_qty": bunch_qty,
                "source_warehouse": source_warehouse,
                "sales_order": sale_order_id,
                "customer": customer_id,
                "stock_qty": number_of_stems,
                "stem_length": stem_length,
                "box_id": box_id,
                "bunch_id": bunch_id
            }
            processed_items.append(item_entry)

        if already_packed_bunches and not processed_items:
            bunch_list = ", ".join([b["bunch_id"] for b in already_packed_bunches])
            frappe.throw(_(f"All scanned bunches have already been packed: {bunch_list}"))

        if not processed_items:
            frappe.throw(_("No valid entries to process"))

        # =========================================================
        # OVER-PACK GUARD (authoritative server-side backstop)
        # Rejects writes that would exceed (a) the ordered quantity per
        # variety, or (b) a box's packrate. The app enforces these too;
        # this protects against any client/API that doesn't.
        # =========================================================
        def guard_int(v):
            try:
                return int(float(v))
            except (ValueError, TypeError):
                return 0

        def ordered_stems_of(it):
            # custom_ordered_quantity is the confirmed/ordered stems when set, but
            # it is frequently left at 0 (unpopulated). Treat 0/blank as "unset"
            # and fall back to the line's real ordered stems (qty x conversion),
            # otherwise the over-pack guard blocks packing the whole order.
            oq = it.get("custom_ordered_quantity")
            try:
                oq = float(oq) if oq not in (None, "") else 0
            except (ValueError, TypeError):
                oq = 0
            if oq:
                return oq
            return (it.get("qty") or 0) * (it.get("conversion_factor") or 1)

        # Match the SO lines THIS OPL covers — by the exact Sales Order Item its
        # rows reference when available, else by item_code (older OPLs).
        opl_so_item_names = set()
        opl_item_codes = set()
        for loc in order_pick_list.table_ytkc:
            if loc.get("custom_sale_order_item"):
                opl_so_item_names.add(loc.get("custom_sale_order_item"))
            if loc.item_code:
                opl_item_codes.add(loc.item_code)

        guard_so = frappe.get_doc("Sales Order", sale_order_id)
        ordered_by_item = {}
        packrate_by_item = {}
        for soi in guard_so.items:
            if opl_so_item_names:
                if soi.name not in opl_so_item_names:
                    continue
            elif soi.item_code not in opl_item_codes:
                continue
            ordered_by_item[soi.item_code] = ordered_by_item.get(soi.item_code, 0) + ordered_stems_of(soi)
            pr = guard_int(soi.get("custom_packrate"))
            if pr and soi.item_code not in packrate_by_item:
                packrate_by_item[soi.item_code] = pr

        # Stems already packed on the pack list we will append to.
        existing_total_by_item = {}
        existing_by_item_box = {}
        guard_existing = frappe.get_all(
            "Farm Pack List",
            filters={
                "order_pick_list": order_pick_list_id
            },  # one Farm Pack List per OPL (name is per-OPL); farm/SO can differ across buckets
            limit=1
        )
        if guard_existing:
            gdoc = frappe.get_doc("Farm Pack List", guard_existing[0].name)
            for r in gdoc.pack_list_item:
                st = r.stock_qty or 0
                existing_total_by_item[r.item_code] = existing_total_by_item.get(r.item_code, 0) + st
                bx = str(r.box_id or "1")
                existing_by_item_box[(r.item_code, bx)] = existing_by_item_box.get((r.item_code, bx), 0) + st

        # Stems arriving in this request.
        incoming_total_by_item = {}
        incoming_by_item_box = {}
        for it in processed_items:
            st = it.get("stock_qty") or 0
            ic = it.get("item_code")
            bx = str(it.get("box_id") or "1")
            incoming_total_by_item[ic] = incoming_total_by_item.get(ic, 0) + st
            incoming_by_item_box[(ic, bx)] = incoming_by_item_box.get((ic, bx), 0) + st

        # (a) Ordered-quantity cap per variety.
        for ic, inc in incoming_total_by_item.items():
            ordered = ordered_by_item.get(ic)
            if ordered is None:
                continue
            already = existing_total_by_item.get(ic, 0)
            if already + inc > ordered:
                allowed = max(0, int(ordered - already))
                frappe.throw(_(
                    f"Over-pack blocked for {ic}: {int(ordered)} stems ordered, "
                    f"{int(already)} already packed - only {allowed} more allowed."
                ))

        # (b) Box-capacity cap per variety per box (where a packrate is set).
        for (ic, bx), inc in incoming_by_item_box.items():
            cap = packrate_by_item.get(ic)
            if not cap:
                continue
            already = existing_by_item_box.get((ic, bx), 0)
            if already + inc > cap:
                allowed = max(0, int(cap - already))
                frappe.throw(_(
                    f"Over-pack blocked: box {bx} for {ic} holds {int(cap)} stems "
                    f"({int(already)} already in it, only {allowed} more allowed). Use another box."
                ))
        # ===== END OVER-PACK GUARD =====

        existing_doc = frappe.get_all(
            "Farm Pack List",
            filters={
                "order_pick_list": order_pick_list_id
            },  # one Farm Pack List per OPL (name is per-OPL); farm/SO can differ across buckets
            limit=1
        )

        if existing_doc:
            doc = frappe.get_doc("Farm Pack List", existing_doc[0].name)

            # Group incoming items
            grouped_incoming = {}
            for item in processed_items:
                key = (item["item_code"], item["stem_length"], item["bunch_uom"], item["box_id"])
                if key not in grouped_incoming:
                    grouped_incoming[key] = {
                        "bunch_qty": 0,
                        "stock_qty": 0,
                        "source_warehouse": item["source_warehouse"]
                    }
                current_qty = grouped_incoming[key]["bunch_qty"]
                current_stems = grouped_incoming[key]["stock_qty"]
                grouped_incoming[key]["bunch_qty"] = current_qty + item["bunch_qty"]
                grouped_incoming[key]["stock_qty"] = current_stems + item["stock_qty"]

            # Update existing rows
            for key_tuple, vals in grouped_incoming.items():
                item_code = key_tuple[0]
                stem_length = key_tuple[1]
                bunch_uom = key_tuple[2]
                box_id = key_tuple[3]

                matched = False
                for row in doc.pack_list_item:
                    if (row.item_code == item_code and
                        row.stem_length == stem_length and
                        row.bunch_uom == bunch_uom and
                        str(row.box_id or "") == str(box_id)):
                        row.bunch_qty = (row.bunch_qty or 0) + vals["bunch_qty"]
                        row.stock_qty = (row.stock_qty or 0) + vals["stock_qty"]
                        matched = True
                        break

                if not matched:
                    doc.append("pack_list_item", {
                        "item_code": item_code,
                        "bunch_uom": bunch_uom,
                        "bunch_qty": vals["bunch_qty"],
                        "source_warehouse": vals["source_warehouse"],
                        "sales_order": sale_order_id,
                        "customer": customer_id,
                        "stock_qty": vals["stock_qty"],
                        "stem_length": stem_length,
                        "box_id": box_id
                    })

            doc.save()

            # === Only mark Stock Entry for spray roses (items with bunch_id) ===
            for item in processed_items:
                if item.get("bunch_id"):
                    stock_entries = frappe.get_all("Stock Entry", filters={"custom_bunch_id": item["bunch_id"]}, limit=1)
                    if stock_entries:
                        se = frappe.get_doc("Stock Entry", stock_entries[0].name)
                        se.db_set("custom_scanned_packing", 1)
                        se.db_set("custom_opl_scanned", order_pick_list_id)

            frappe.db.commit()

            response_message = f"Farm Pack List updated with {len(processed_items)} bunch(es) across boxes"
            if already_packed_bunches:
                packed_list = ", ".join([b["bunch_id"] for b in already_packed_bunches])
                response_message += f". Skipped already packed: {packed_list}"

            frappe.response['data'] = {
                "status": "updated",
                "message": response_message,
                "docname": doc.name,
                "already_packed": already_packed_bunches,
                "newly_packed": len(processed_items)
            }

        else:
            # CREATE NEW - Group by box + variety + length + uom
            grouped_items = {}
            for item in processed_items:
                key = (item["item_code"], item["stem_length"], item["bunch_uom"], item["box_id"])
                if key not in grouped_items:
                    grouped_items[key] = {
                        "doctype": "pack_list_item",
                        "item_code": item["item_code"],
                        "bunch_uom": item["bunch_uom"],
                        "bunch_qty": 0,
                        "source_warehouse": item["source_warehouse"],
                        "sales_order": sale_order_id,
                        "customer": customer_id,
                        "stock_qty": 0,
                        "stem_length": item["stem_length"],
                        "box_id": item["box_id"]
                    }
                current_qty = grouped_items[key]["bunch_qty"]
                current_stems = grouped_items[key]["stock_qty"]
                grouped_items[key]["bunch_qty"] = current_qty + item["bunch_qty"]
                grouped_items[key]["stock_qty"] = current_stems + item["stock_qty"]

            aggregated_items = list(grouped_items.values())

            doc = frappe.new_doc("Farm Pack List")
            doc.sales_order = sale_order_id
            doc.customer = customer_id
            doc.farm = user_farm
            doc.order_pick_list = order_pick_list_id

            for item in aggregated_items:
                doc.append("pack_list_item", item)

            doc.insert()

            # === Only mark Stock Entry for spray roses (items with bunch_id) ===
            for item in processed_items:
                if item.get("bunch_id"):
                    stock_entries = frappe.get_all("Stock Entry", filters={"custom_bunch_id": item["bunch_id"]}, limit=1)
                    if stock_entries:
                        se = frappe.get_doc("Stock Entry", stock_entries[0].name)
                        se.db_set("custom_scanned_packing", 1)
                        se.db_set("custom_opl_scanned", order_pick_list_id)

            frappe.db.commit()

            response_message = f"Farm Pack List created with {len(aggregated_items)} grouped box entries"
            if already_packed_bunches:
                packed_list = ", ".join([b["bunch_id"] for b in already_packed_bunches])
                response_message += f". Skipped already packed: {packed_list}"

            frappe.response['data'] = {
                "status": "created",
                "message": response_message,
                "docname": doc.name,
                "already_packed": already_packed_bunches,
                "newly_packed": len(aggregated_items)
            }

    except Exception as e:
        frappe.log_error(message=str(e), title="Farm Pack List Packing Error")
        frappe.throw(_("Error processing packing: ") + str(e))


@frappe.whitelist()
def createStagingEntry():
    payload = frappe.request.get_json()
    frappe.log_error("Staging Payload", payload)
    if not payload:
        frappe.response.update({
            "status": "error",
            "message": "No JSON payload received"
        })
    else:
        box_label_id = payload.get("box_label")
        location = payload.get("location")

        if not box_label_id:
            frappe.response.update({
                "status": "error",
                "message": "'box_label' is required"
            })
        else:
            box = None
            try:
                box = frappe.get_doc("Box Label", box_label_id)
            except frappe.DoesNotExistError:
                frappe.response.update({
                    "status": "error",
                    "message": "Box Label %s does not exist" % box_label_id
                })

            if box:
                if box.staged == 1:
                    frappe.response.update({
                        "status": "duplicate",
                        "message": "Box %s has already been staged" % box_label_id
                    })
                else:
                    box.staged = 1
                    # Record WHERE the box was staged in the dispatch coldstore
                    # (QR that maps to a physical place). Optional for older callers.
                    if location:
                        box.staging_location = location
                    box.save(ignore_permissions=True)
                    frappe.db.commit()

                    frappe.response.update({
                        "status": "success",
                        "location": location,
                        "message": "Box %s staged successfully%s" % (
                            box_label_id,
                            (" at %s" % location) if location else ""
                        )
                    })

    # Catch-all for any unhandled exception
    if "status" not in frappe.response or frappe.response["status"] not in ["success", "error", "duplicate"]:
        try:
            frappe.log_error("Box staging script - unhandled error", frappe.get_traceback())
            frappe.db.rollback()
        except:
            pass
        frappe.response.update({
            "status": "error",
            "message": "Unexpected server error while processing box scan"
        })


@frappe.whitelist()
def dispatchBucketTrip():
    name = frappe.form_dict.get("name")
    if not name or not frappe.db.exists("Bucket Request Trip", name):
        frappe.response["message"] = {"status": "error", "message": "Trip not found."}
    else:
        current = frappe.db.get_value("Bucket Request Trip", name, "status")
        if current not in ("Draft", "Scheduled"):
            frappe.response["message"] = {
                "status": "error",
                "message": "Trip is " + str(current) + "; only Draft/Scheduled trips can be dispatched.",
            }
        else:
            frappe.db.set_value("Bucket Request Trip", name, {
                "status": "Dispatched",
                "dispatched_at": frappe.utils.now(),
            })
            frappe.db.commit()
            frappe.response["message"] = {"status": "success", "name": name, "trip_status": "Dispatched"}


@frappe.whitelist()
def fetchDispatchLoadedOrders():
    # Fetch Dispatch Loaded Orders
    # API: fetchDispatchLoadedOrders
    # Loading defines dispatch: return the orders that have been LOADED (from the
    # day's Loading Sheet LS-<delivery_date>), grouped by Sales Order and shown by
    # order name. This is the read-only dispatch list.
    # Param: delivery_date (optional; defaults to tomorrow, matching loading).
    try:
        delivery_date = frappe.form_dict.get("delivery_date") or frappe.utils.add_days(frappe.utils.today(), 1)
        ls_name = "LS-" + str(delivery_date)

        orders_by_so = {}
        total_boxes = 0

        if frappe.db.exists("Loading Sheet", ls_name):
            ls_doc = frappe.get_doc("Loading Sheet", ls_name)
            for it in ls_doc.items:
                bl_name = it.box_label_link or it.box_label or ""
                if not bl_name or not frappe.db.exists("Box Label", bl_name):
                    continue
                bl = frappe.db.get_value(
                    "Box Label", bl_name,
                    ["customer_purchase_order", "customer", "delivery_point", "farm", "consignee", "order_pick_list"],
                    as_dict=True
                )
                so_name = (bl.customer_purchase_order or "") if bl else ""
                key = so_name or bl_name
                if key not in orders_by_so:
                    order_name = so_name
                    if so_name and frappe.db.exists("Sales Order", so_name):
                        order_name = frappe.db.get_value("Sales Order", so_name, "custom_order_name") or so_name
                    orders_by_so[key] = {
                        "sales_order": so_name,
                        "order_name": order_name or key,
                        "customer": (bl.customer or "") if bl else "",
                        "delivery_point": (bl.delivery_point or "") if bl else "",
                        "farm": (bl.farm or "") if bl else "",
                        "consignee": (bl.consignee or "") if bl else "",
                        "boxes_loaded": 0,
                    }
                orders_by_so[key]["boxes_loaded"] = orders_by_so[key]["boxes_loaded"] + 1
                total_boxes = total_boxes + 1

        orders = sorted(orders_by_so.values(), key=lambda o: (o["delivery_point"], o["order_name"]))

        frappe.response["message"] = {
            "status": "success",
            "message": "Loaded orders fetched",
            "data": {
                "delivery_date": str(delivery_date),
                "loading_sheet": ls_name if frappe.db.exists("Loading Sheet", ls_name) else None,
                "total_boxes": total_boxes,
                "total_orders": len(orders),
                "orders": orders,
            }
        }
    except Exception as e:
        frappe.log_error("fetchDispatchLoadedOrders error: " + str(e))
        frappe.response["message"] = {"status": "error", "message": str(e), "data": {}}


@frappe.whitelist()
def fetchLoadingData():
    # Get Loading Data
    # API: get_loading_data

    try:
        farm = frappe.form_dict.get("farm", "")
        vehicle = frappe.form_dict.get("vehicle", "")

        today = frappe.utils.today()
        # Delivery date to view: caller-supplied (any day) or default to tomorrow.
        tomorrow = frappe.form_dict.get("delivery_date") or frappe.utils.add_days(today, 1)

        # 1. DISPATCH TRUCKS
        vehicles_raw = frappe.get_all(
            "Vehicle",
            filters={"custom_dispatch_truck": 1},
            fields=["name", "license_plate"],
            order_by="license_plate asc"
        )

        vehicles = [{"name": v.name, "license_plate": v.license_plate} for v in vehicles_raw]

        # 2. LOADING SHEET
        ls_name = "LS-" + str(tomorrow)
        loaded_boxes = []
        loading_sheet_status = ""
        ls_loaded_by_stop = {}
        ls_loaded_by_so = {}

        if frappe.db.exists("Loading Sheet", ls_name):
            ls_doc = frappe.get_doc("Loading Sheet", ls_name)
            loading_sheet_status = ls_doc.status or ""

            for ls_item in ls_doc.items:
                bl_customer = ls_item.customer or ""
                bl_dp = ls_item.delivery_point or ""

                loaded_boxes.append({
                    "box_label": ls_item.box_label or "",
                    "box_id": ls_item.box_id or 0,
                    "customer": bl_customer,
                    "delivery_point": bl_dp,
                    "position": ls_item.position or 0,
                    "farm_pack_list": ls_item.farm_pack_list or ""
                })

                stop_key = bl_customer + "|" + bl_dp
                ls_loaded_by_stop[stop_key] = ls_loaded_by_stop.get(stop_key, 0) + 1

                bl_name = ls_item.box_label_link or ls_item.box_label or ""
                if bl_name and frappe.db.exists("Box Label", bl_name):
                    so_name = frappe.db.get_value("Box Label", bl_name, "customer_purchase_order") or ""
                    if so_name:
                        ls_loaded_by_so[so_name] = ls_loaded_by_so.get(so_name, 0) + 1

        # 3. LOADING PLAN
        loading_plan = None
        plan_items = []
        plan_name = ""

        # Get all plans for UI
        all_plans = frappe.get_all(
            "Loading Plan",
            filters={"delivery_date": tomorrow},
            fields=["name", "vehicle"],
            order_by="name asc"
        )

        available_plans = [
            {
                "name": ap.name,
                "vehicle": ap.vehicle or ""
            }
            for ap in all_plans
        ]

        # Fetch specific plan — by explicit plan NAME (preferred; plans may have no
        # vehicle and there can be several per day), else by vehicle, else first of day.
        plan_param = frappe.form_dict.get("plan")
        if plan_param and frappe.db.exists("Loading Plan", plan_param):
            plan_name = plan_param
            loading_plan = frappe.get_doc("Loading Plan", plan_name)
        elif vehicle:
            plan_search = frappe.get_all(
                "Loading Plan",
                filters={
                    "delivery_date": tomorrow,
                    "vehicle": vehicle
                },
                fields=["name"],
                limit_page_length=1
            )

            if plan_search:
                plan_name = plan_search[0].name
                loading_plan = frappe.get_doc("Loading Plan", plan_name)

        else:
            # fallback: first plan of the day
            if all_plans:
                plan_name = all_plans[0].name
                loading_plan = frappe.get_doc("Loading Plan", plan_name)

        # 4. PROCESS PLAN ITEMS
        if loading_plan:
            sorted_items = sorted(
                loading_plan.loading_plan_items,
                key=lambda x: x.loading_position or 0
            )

            for item in sorted_items:
                customer = item.customer or ""
                delivery_point = item.delivery_point or ""
                position = item.loading_position or 0

                item_box_type = item.box_type or ""
                item_number_of_boxes = item.number_of_boxes or 0

                if not customer:
                    continue

                so_filters = {
                    "delivery_date": tomorrow,
                    "docstatus": 1,
                    "customer": customer
                }

                if delivery_point:
                    so_filters["custom_delivery_point"] = delivery_point

                if farm:
                    so_filters["custom_farm"] = farm

                sales_orders = frappe.get_all(
                    "Sales Order",
                    filters=so_filters,
                    fields=[
                        "name", "custom_order_name", "customer_name",
                        "custom_truck_details", "custom_consignee",
                        "custom_shipping_agent", "total_qty"
                    ]
                )

                orders = []
                item_boxes_allocated = 0
                item_boxes_packed = 0
                item_boxes_staged = 0
                item_boxes_loaded = 0

                for so in sales_orders:
                    so_name = so.name

                    # ALLOCATED
                    opls = frappe.get_all(
                        "Order Pick List",
                        filters={"sales_order": so_name, "docstatus": 1},
                        fields=["name", "custom_total_stems"]
                    )

                    boxes_allocated = 0
                    for opl in opls:
                        total_stems = int(opl.custom_total_stems or 0)

                        packrate_data = frappe.get_all(
                            "Pick List Item",
                            filters={"parent": opl.name},
                            fields=["packrate"],
                            limit_page_length=1
                        )

                        packrate = 200
                        if packrate_data and packrate_data[0].packrate:
                            packrate = int(packrate_data[0].packrate or 200)

                        if packrate > 0 and total_stems > 0:
                            boxes_allocated += -(-total_stems // packrate)

                    # PACKED
                    # v16: Farm Pack List has no direct custom_sales_order link; it
                    # links to the Order Pick List (order_pick_list). Reach the FPLs
                    # for this Sales Order through its OPLs (computed above).
                    boxes_packed = 0
                    opl_names_for_pack = [o.name for o in opls]
                    fpls = []
                    if opl_names_for_pack:
                        fpls = frappe.get_all(
                            "Farm Pack List",
                            filters={
                                "order_pick_list": ["in", opl_names_for_pack],
                                "docstatus": ["!=", 2],
                            },
                            fields=["name"]
                        )

                    if fpls:
                        fpl_names = [f.name for f in fpls]

                        packed_box_data = frappe.db.sql("""
                            SELECT COUNT(DISTINCT box_id) as box_count
                            FROM `tabFarm Packlist Item`
                            WHERE parent IN %(fpl_names)s
                            AND bunch_qty > 0
                        """, {"fpl_names": fpl_names}, as_dict=True)

                        if packed_box_data:
                            boxes_packed = int(packed_box_data[0].box_count or 0)

                    # STAGED
                    boxes_staged = frappe.db.count(
                        "Box Label",
                        filters={"customer_purchase_order": so_name, "loaded": 0}
                    ) or 0

                    # LOADED
                    bl_loaded = frappe.db.count(
                        "Box Label",
                        filters={"customer_purchase_order": so_name, "loaded": 1}
                    ) or 0

                    ls_loaded = ls_loaded_by_so.get(so_name, 0)
                    boxes_loaded = max(bl_loaded, ls_loaded)

                    orders.append({
                        "sales_order": so_name,
                        "order_name": so.custom_order_name or so_name,
                        "truck_details": so.custom_truck_details or "",
                        "consignee": so.custom_consignee or "",
                        "shipping_agent": so.custom_shipping_agent or "",
                        "total_qty": float(so.total_qty or 0),
                        "boxes_allocated": boxes_allocated,
                        "boxes_packed": boxes_packed,
                        "boxes_staged": boxes_staged,
                        "boxes_loaded": boxes_loaded
                    })

                    item_boxes_allocated += boxes_allocated
                    item_boxes_packed += boxes_packed
                    item_boxes_staged += boxes_staged
                    item_boxes_loaded += boxes_loaded

                stop_key = customer + "|" + delivery_point
                ls_stop_loaded = ls_loaded_by_stop.get(stop_key, 0)

                if item_boxes_loaded == 0 and ls_stop_loaded > 0:
                    item_boxes_loaded = ls_stop_loaded

                plan_items.append({
                    "loading_position": position,
                    "customer": customer,
                    "delivery_point": delivery_point,
                    "box_type": item_box_type,
                    "number_of_boxes": item_number_of_boxes,
                    "orders": orders,
                    "boxes_allocated": item_boxes_allocated,
                    "boxes_packed": item_boxes_packed,
                    "boxes_staged": item_boxes_staged,
                    "boxes_loaded": item_boxes_loaded
                })

        # 5. TOTALS
        totals = {
            "total_boxes_allocated": sum(p["boxes_allocated"] for p in plan_items),
            "total_boxes_packed": sum(p["boxes_packed"] for p in plan_items),
            "total_boxes_staged": sum(p["boxes_staged"] for p in plan_items),
            "total_boxes_loaded": sum(p["boxes_loaded"] for p in plan_items),
            "total_stops": len(plan_items)
        }

        response_data = {
            "delivery_date": str(tomorrow),
            "selected_vehicle": (loading_plan.get("vehicle") if loading_plan else "") or vehicle,
            "has_loading_plan": loading_plan is not None,
            "loading_plan_name": plan_name if loading_plan else None,
            "available_plans": available_plans,
            "plan_items": plan_items,
            "vehicles": vehicles,
            "totals": totals,
            "loading_sheet": {
                "name": ls_name if frappe.db.exists("Loading Sheet", ls_name) else None,
                "status": loading_sheet_status,
                "total_boxes": len(loaded_boxes),
                "loaded_boxes": loaded_boxes
            }
        }

        frappe.response["message"] = {
            "status": "success",
            "message": "Loading data fetched successfully",
            "data": response_data
        }

    except Exception as e:
        frappe.log_error("get_loading_data error: " + str(e))
        frappe.response["message"] = {
            "status": "error",
            "message": str(e),
            "data": {}
        }


@frappe.whitelist()
def fetchPicklists():
    try:
        # Optional ?date=YYYY-MM-DD to view a past day's picklists; defaults to today.
        requested_date = frappe.form_dict.get('date')
        today = requested_date if requested_date else frappe.utils.today()
        current_user = frappe.session.user

        # Determine user's farm location
        farm_filter = None
        employee = frappe.get_all(
            "Employee",
            filters={"user_id": current_user, "status": "Active"},
            fields=["custom_group_name"],
            limit=1
        )

        if employee:
            group_name = (employee[0].get("custom_group_name") or "").lower()
            if "ravine" in group_name:
                farm_filter = "Kapkolia"
            elif "karen" in group_name:
                farm_filter = "Karen"

        # Filter by the Sales Order's DELIVERY date (pack today for tomorrow's
        # shipments), not the pick list creation date. `opl.sales_order` is the real
        # link; `order_name` is only a display label.
        if farm_filter:
            result = frappe.db.sql("""
                SELECT opl.name AS opl_name, opl.order_name AS order_name,
                       opl.item_group AS item_group, opl.custom_total_stems AS planned_stems,
                       opl.team AS team
                FROM `tabOrder Pick List` opl
                INNER JOIN `tabSales Order` so ON so.name = opl.sales_order
                WHERE opl.docstatus = 1 AND so.delivery_date = %s AND opl.farm = %s
                ORDER BY opl.creation DESC LIMIT 500
            """, (today, farm_filter), as_list=True)
        else:
            result = frappe.db.sql("""
                SELECT opl.name AS opl_name, opl.order_name AS order_name,
                       opl.item_group AS item_group, opl.custom_total_stems AS planned_stems,
                       opl.team AS team
                FROM `tabOrder Pick List` opl
                INNER JOIN `tabSales Order` so ON so.name = opl.sales_order
                WHERE opl.docstatus = 1 AND so.delivery_date = %s
                ORDER BY opl.creation DESC LIMIT 500
            """, (today,), as_list=True)

        # Exclude fully-packed OPLs, using the SAME packed/planned definition as
        # get_pick_list_with_farm_pack_list (the app's packing guide):
        #   planned = Order Pick List.custom_total_stems  (== guide planned_total)
        #   packed  = sum of stock_qty (or bunch_qty*10) over the rows
        #             of the latest Farm Pack List for the OPL, counting only rows
        #             that have an item_code.
        # An OPL is hidden ONLY when planned > 0 AND packed >= planned, so partially
        # packed or unpacked orders are never hidden.
        opl_names = [r[0] for r in result]
        packed_by_opl = {}

        if opl_names:
            fpls = frappe.db.get_all(
                "Farm Pack List",
                filters={"order_pick_list": ["in", opl_names]},
                fields=["name", "order_pick_list"],
                order_by="creation desc"
            )
            # Latest Farm Pack List per OPL (first seen, since ordered creation desc).
            latest_fpl = {}
            fpl_to_opl = {}
            for fp in fpls:
                opl_ref = fp.get("order_pick_list")
                if opl_ref and opl_ref not in latest_fpl:
                    latest_fpl[opl_ref] = fp.get("name")
                    fpl_to_opl[fp.get("name")] = opl_ref

            fpl_names = list(latest_fpl.values())
            if fpl_names:
                items = frappe.db.get_all(
                    "Farm Packlist Item",
                    filters={"parent": ["in", fpl_names], "parentfield": "pack_list_item"},
                    fields=["parent", "item_code", "stock_qty", "bunch_qty"]
                )
                for it in items:
                    if not it.get("item_code"):
                        continue
                    stems = it.get("stock_qty") or ((it.get("bunch_qty") or 0) * 10)
                    if not stems:
                        continue
                    opl_ref = fpl_to_opl.get(it.get("parent"))
                    if opl_ref:
                        packed_by_opl[opl_ref] = packed_by_opl.get(opl_ref, 0) + stems

        opl_list = []
        for r in result:
            try:
                planned = float(r[3] or 0)
            except Exception:
                planned = 0
            packed = packed_by_opl.get(r[0], 0)
            if planned > 0 and packed >= planned:
                continue
            opl_list.append(dict(opl_name=r[0], order_name=r[1], item_group=r[2], team=r[4]))

        frappe.response['message'] = {
            'success': True,
            'data': opl_list,
            'count': len(opl_list)
        }
    except Exception as e:
        frappe.response['message'] = {
            'success': False,
            'error': str(e),
            'data': []
        }


@frappe.whitelist()
def fetchStockEntryByBunch():
    try:
        data = frappe.request.get_json()
        bunch_id = data.get("custom_bunch_id")
        action = data.get("action", "packing")  # Default to "packing" if not provided

        if not bunch_id:
            frappe.throw("Bunch ID is required")

        # Fetch Stock Entry with the given bunch_id
        stock_entries = frappe.get_all(
            "Stock Entry",
            filters={
                "custom_bunch_id": bunch_id
            },
            fields=[
                "name",
                "custom_stem_length",
                "custom_bunched_by",
                "custom_bunch_id",
                "custom_scanned_grading",
                "custom_scanned_packing",
                "custom_greenhouse",
                "stock_entry_type",
                "custom_graded_by",
                "to_warehouse",
                "posting_date",
                "posting_time"
            ],
            limit=1
        )

        # Initialize result variable
        result = {}

        # If no stock entry found
        if not stock_entries:
            if action == "packing":
                frappe.throw("This bunch has not been graded in the system. Perform the grading scan on it to enable packing")
            elif action == "grading":
                # For grading action, return empty object
                result = {}
        else:
            stock_entry = stock_entries[0]

            # For packing action: check if already packed
            if action == "packing":
                if stock_entry.custom_scanned_packing:
                    frappe.throw("This bunch has already been packed")

            # Get item details from Stock Entry Detail child table
            items = frappe.get_all(
                "Stock Entry Detail",
                filters={
                    "parent": stock_entry.name,
                    "parenttype": "Stock Entry"
                },
                fields=[
                    "item_code",
                    "qty",
                    "uom"
                ]
            )

            # Extract item details (assuming first item in the list is the main item)
            item_code = items[0].get("item_code") if items else None
            qty = items[0].get("qty") if items else 0
            uom = items[0].get("uom") if items else None

            # Format the response as a single object (Map) to match your Flutter app's expectation
            result = {
                "name": stock_entry.name,
                "scanned_grading": stock_entry.custom_scanned_grading,  # Note: matches fromJson expectation
                "scanned_packing": stock_entry.custom_scanned_packing,  # Note: matches fromJson expectation
                "greenhouse": stock_entry.custom_greenhouse,  # Note: matches fromJson expectation
                "stock_entry_type": stock_entry.stock_entry_type,
                "graded_by": stock_entry.custom_graded_by,  # Note: matches fromJson expectation
                "bunch_id": stock_entry.custom_bunch_id,  # Note: matches fromJson expectation
                "stem_length": stock_entry.custom_stem_length,  # Note: matches fromJson expectation
                "bunch_size": stock_entry.custom_bunched_by,  # Note: matches fromJson expectation
                "to_warehouse": stock_entry.to_warehouse,
                "posting_date": stock_entry.posting_date,
                "posting_time": stock_entry.posting_time,
                # Item details
                "items": items  # This will be used by fromJson to extract item_code and uom
            }

        frappe.response['message'] = result

    except Exception as e:
        frappe.log_error(f"Error fetching Stock Entry by Bunch ID: {bunch_id}", str(e))
        frappe.response['message'] = {
            "error": True,
            "message": str(e)
        }


@frappe.whitelist()
def getCurrentUserRoles():
    try:
        user = frappe.session.user
        rows = frappe.get_all(
            "Has Role",
            filters={"parent": user, "parenttype": "User"},
            fields=["role"],
        )
        roles = [r["role"] for r in rows if r.get("role")]
        frappe.response["data"] = {
            "user": user,
            "roles": roles,
        }
    except Exception as e:
        frappe.log_error("getCurrentUserRoles error: " + str(e))
        frappe.response["http_status_code"] = 500
        frappe.response["data"] = {"error": str(e)}


@frappe.whitelist()
def getPostHarvestStaff():
    graders = frappe.get_all(
        "Employee",
        filters={"custom_farm": "Post Harvest", "designation": "Grader", "status": "Active"},
        fields=["name", "employee_name"],
        order_by="employee_name asc"
    )
    packers = frappe.get_all(
        "Employee",
        filters={"custom_farm": "Post Harvest", "designation": "Packer", "status": "Active"},
        fields=["name", "employee_name"],
        order_by="employee_name asc"
    )
    frappe.response["message"] = {"status": "success", "graders": graders, "packers": packers}


@frappe.whitelist()
def getReadySaleOrderItems():
    try:
        # Optional ?date=YYYY-MM-DD (defaults to today). Lists submitted Order Pick Lists
        # created that day that still have at least one UNISSUED bucket, aggregated per order.
        requested_date = frappe.form_dict.get('date')
        day = requested_date if requested_date else frappe.utils.today()

        # Filter by the Sales Order DELIVERY date, not the pick list creation date.
        so_names = frappe.get_all("Sales Order", filters={"delivery_date": day}, pluck="name")
        ready_orders = frappe.get_all(
            "Order Pick List",
            filters={"docstatus": 1, "sales_order": ["in", so_names]},
            fields=["name", "order_name", "item_group", "team"],
        ) if so_names else []

        opl_names = [o.name for o in ready_orders]
        opls_with_unissued = set()
        if opl_names:
            unissued_rows = frappe.get_all(
                "Pick List Item",
                filters={"parent": ["in", opl_names], "parenttype": "Order Pick List", "issued": 0},
                fields=["parent"],
                distinct=True,
            )
            for r in unissued_rows:
                opls_with_unissued.add(r.parent)

        order_groups = {}
        order_teams = {}
        for opl in ready_orders:
            if opl.name not in opls_with_unissued:
                continue
            order_name = opl.get("order_name")
            if not order_name:
                continue
            groups = order_groups.setdefault(order_name, set())
            if opl.get("item_group"):
                groups.add(opl.get("item_group"))
            teams = order_teams.setdefault(order_name, set())
            if opl.get("team"):
                teams.add(opl.get("team"))

        orders = [
            {"name": name, "custom_item_group": sorted(groups), "custom_team": sorted(order_teams.get(name, set()))}
            for name, groups in sorted(order_groups.items())
        ]
        frappe.response["orders"] = orders
        frappe.response["message"] = "Found " + str(len(orders)) + " orders ready for packing"
    except Exception as error:
        frappe.log_error("Fetch Ready Orders Error: " + str(error))
        frappe.throw("Error fetching ready orders: " + str(error))


@frappe.whitelist()
def getReadySaleOrderItemsData():
    try:
        order_name = frappe.form_dict.get('custom_order_name')

        if not order_name:
            frappe.throw("Order name is required. Please provide 'custom_order_name' parameter.")

        order_name = ' '.join(order_name.split())

        # Search Order Pick Lists directly by custom_order_name
        all_opls = frappe.get_all(
            "Order Pick List",
            fields=["name", "order_name", "sales_order"],
            filters={"docstatus": 1}
        )

        matching_opls = []
        for opl in all_opls:
            if opl.order_name:
                normalized = ' '.join(opl.order_name.split())
                if normalized == order_name:
                    matching_opls.append(opl.name)

        if not matching_opls:
            frappe.response["packing_list"] = []
            frappe.response["message"] = f"No submitted Order Pick List found with order name: {order_name}"
        else:
            # Team each OPL is assigned to (shown to the issuer so they hand the
            # buckets to the right team).
            opl_team = {}
            for opl in frappe.get_all(
                "Order Pick List",
                filters={"name": ["in", matching_opls]},
                fields=["name", "team"]
            ):
                opl_team[opl.name] = opl.team or ""

            # Fetch ALL buckets for the order (issued + unissued) so the app can
            # show issuing progress; the app hides issued ones from the scan list.
            pick_list_items = frappe.get_all(
                "Pick List Item",
                filters={
                    "parent": ["in", matching_opls],
                    "docstatus": 1,
                    "parenttype": "Order Pick List"
                },
                fields=[
                    "name",
                    "item_code",
                    "bucket",
                    "stem_length",
                    "shelf",
                    "custom_sale_order_item",
                    "stock_qty",
                    "qty",
                    "parent as opl_name",
                    "custom_ready_for_packing",
                    "issued",
                    "creation"
                ],
                order_by="creation desc"
            )

            if not pick_list_items:
                frappe.response["packing_list"] = []
                frappe.response["message"] = f"No pick list items found for order: {order_name}"
            else:
                packing_list = []

                for pli in pick_list_items:
                    so_item_info = None
                    try:
                        so_item_info = frappe.get_doc("Sales Order Item", pli.custom_sale_order_item)
                    except:
                        continue

                    downgrade_to = None
                    if so_item_info and so_item_info.custom_length and pli.stem_length:
                        try:
                            so_length = int(so_item_info.custom_length.replace('cm', '').strip())
                            pli_length = int(pli.stem_length.replace('cm', '').strip())
                            if pli_length > so_length:
                                downgrade_to = so_item_info.custom_length
                        except:
                            pass

                    qty_stems = pli.stock_qty or pli.qty or 0

                    packing_item = {
                        "variety": pli.item_code,
                        "bucket": pli.bucket,
                        "stem_length": pli.stem_length,
                        "shelf": pli.shelf,
                        "custom_sale_order_item": pli.custom_sale_order_item,
                        "opl_name": pli.opl_name,
                        "team": opl_team.get(pli.opl_name) or "Unassigned",
                        "qty": qty_stems,
                        "mixed": 1 if so_item_info and so_item_info.custom_mixed_box else 0,
                        "downgrade_to": downgrade_to,
                        "is_ready": True,
                        "is_issued": 1 if pli.issued else 0,
                        "was_marked_ready": pli.custom_ready_for_packing or 0
                    }

                    packing_list.append(packing_item)

                frappe.response["packing_list"] = packing_list
                frappe.response["message"] = f"Found {len(packing_list)} buckets from submitted pick lists"

    except Exception as error:
        frappe.log_error(f"Packing List Error: {str(error)}")
        frappe.response["packing_list"] = []
        frappe.response["message"] = f"Error generating packing list: {str(error)}"


@frappe.whitelist()
def getSchedulerData():
    # Packhouse Scheduler
    # API: getSchedulerData
    # Returns OPLs for a given delivery date with child table items (transfer status + timestamps)

    delivery_date = frappe.form_dict.get('delivery_date') or frappe.utils.today()

    try:
        opls = frappe.db.sql("""
            SELECT
                opl.name,
                opl.customer,
                opl.order_name,
                opl.farm,
                opl.team,
                opl.custom_total_stems,
                NULL AS custom_issuing_percentage,
                NULL AS custom_status,
                NULL AS custom_truck_details,
                NULL AS custom_consignee,
                opl.sales_order,
                opl.date_created,
                opl.docstatus,

                IFNULL(bl.box_count, 0)     AS box_labels_count,
                IFNULL(bl.staged_count, 0)  AS staged_boxes,
                IFNULL(ls.loaded_count, 0)  AS loaded_boxes

            FROM `tabOrder Pick List` opl
            LEFT JOIN `tabSales Order` so ON so.name = opl.sales_order
            LEFT JOIN (
                SELECT bl_i.order_pick_list AS opl_name,
                       COUNT(bl_i.name) AS box_count,
                       SUM(CASE WHEN bl_i.loaded = 1 THEN 1 ELSE 0 END) AS staged_count
                FROM `tabBox Label` bl_i
                GROUP BY bl_i.order_pick_list
            ) bl ON bl.opl_name = opl.name
            LEFT JOIN (
                SELECT bl_ls.order_pick_list AS opl_name,
                       SUM(CASE WHEN 1 = 1 THEN 1 ELSE 0 END) AS loaded_count
                FROM `tabLoading Sheet Item` lsi
                INNER JOIN `tabBox Label` bl_ls ON bl_ls.name = lsi.box_label_link
                GROUP BY bl_ls.order_pick_list
            ) ls ON ls.opl_name = opl.name

            WHERE opl.docstatus IN (0, 1)
              AND (
                  so.delivery_date = %(delivery_date)s
                  OR (opl.sales_order IS NULL AND opl.date_created = %(delivery_date)s)
                  OR (opl.sales_order = '' AND opl.date_created = %(delivery_date)s)
              )
            ORDER BY opl.customer, opl.name
        """, { 'delivery_date': delivery_date }, as_dict=True)

        opl_names = [o['name'] for o in opls]
        items_map = {}
        if opl_names:
            CHUNK = 50
            for i in range(0, len(opl_names), CHUNK):
                chunk = opl_names[i:i+CHUNK]
                placeholders = ', '.join(['%s'] * len(chunk))
                items = frappe.db.sql(f"""
                    SELECT
                        parent, idx, item_code, item_name,
                        shelf, bucket, warehouse,
                        stem_length, stock_qty, custom_box_id,
                        awaiting_transfer,
                        loaded_in_trolley, trolley_id,
                        in_transit, transit_truck,
                        shelved, issued,
                        custom_ready_for_packing,
                        sales_order_item,
                        modified
                    FROM `tabPick List Item`
                    WHERE parent IN ({placeholders})
                      AND parenttype = 'Order Pick List'
                    ORDER BY parent, idx
                """, chunk, as_dict=True)
                for item in items:
                    p = item['parent']
                    if p not in items_map:
                        items_map[p] = []
                    # Convert modified to string for JSON
                    if item.get('modified'):
                        item['modified'] = str(item['modified'])
                    items_map[p].append(item)

        # Specs + packrate from the Sales Order Items the OPL's lines reference.
        soi_names = []
        for p in items_map:
            rows = items_map[p]
            j = 0
            while j < len(rows):
                si = rows[j].get('sales_order_item')
                if si and si not in soi_names:
                    soi_names = soi_names + [si]
                j = j + 1
        soi_map = {}
        if soi_names:
            for i in range(0, len(soi_names), 50):
                chunk = soi_names[i:i+50]
                placeholders = ', '.join(['%s'] * len(chunk))
                srows = frappe.db.sql(f"""
                    SELECT name, item_code, custom_line, custom_length,
                           custom_number_of_boxes, custom_box_type
                    FROM `tabSales Order Item`
                    WHERE name IN ({placeholders})
                """, chunk, as_dict=True)
                for sr in srows:
                    soi_map[sr['name']] = sr

        for opl in opls:
            opl['locations'] = items_map.get(opl['name'], [])
            opl['box_labels_count'] = int(opl.get('box_labels_count') or 0)
            opl['staged_boxes'] = int(opl.get('staged_boxes') or 0)
            opl['loaded_boxes'] = int(opl.get('loaded_boxes') or 0)
            specs = []
            seen_si = {}
            boxes_total = 0
            locs = opl['locations']
            k = 0
            while k < len(locs):
                si = locs[k].get('sales_order_item')
                if si and si in soi_map and si not in seen_si:
                    seen_si[si] = 1
                    sr = soi_map[si]
                    bx = int(sr.get('custom_number_of_boxes') or 0)
                    boxes_total = boxes_total + bx
                    specs = specs + [{'spec': sr.get('custom_line'), 'variety': sr.get('item_code'), 'length': sr.get('custom_length'), 'box_type': sr.get('custom_box_type'), 'boxes': bx}]
                k = k + 1
            opl['specs'] = specs
            opl['planned_boxes'] = boxes_total
            ts = 0
            try:
                ts = int(float(opl.get('custom_total_stems') or 0))
            except:
                ts = 0
            opl['packrate'] = int(round(ts / boxes_total)) if boxes_total > 0 else 0

        frappe.response['message'] = {
            'success': True,
            'data': opls,
            'date': delivery_date
        }
    except Exception as e:
        frappe.log_error('getSchedulerData error: ' + str(e))
        frappe.response['message'] = {
            'success': False,
            'error': str(e),
            'data': []
        }


@frappe.whitelist()
def getSchedulerMeta():
    # Frappe Server Script (Type: API), api_method = getSchedulerMeta
    # Companion read endpoint for the Packhouse Scheduler page.
    # Returns the global takt threshold + the persisted queue order (schedule_number)
    # for the supplied OPL names. getSchedulerData (app method) does not return these.
    # Payload: { "names": ["OPL-xxx", ...] }
    # Constraints: no return/def/import/+=/.append — use list=list+[x], dict[k]=v

    frappe.response["message"] = {"success": False, "error": "Script failed"}

    try:
        # names arrive as a "|~|"-joined string (safe_exec blocks json/imports)
        names_raw = frappe.form_dict.get("names") or ""
        names = []
        parts = names_raw.split("|~|")
        p = 0
        while p < len(parts):
            v = parts[p].strip()
            if v != "":
                names = names + [v]
            p = p + 1

        takt = frappe.db.get_single_value("Production Settings", "custom_takt_time") or 0

        schedule = {}
        created = {}
        if names and isinstance(names, list) and len(names) > 0:
            rows = frappe.get_all(
                "Order Pick List",
                filters=[["name", "in", names]],
                fields=["name", "schedule_number", "creation"]
            )
            i = 0
            while i < len(rows):
                r = rows[i]
                num = 0
                try:
                    num = int(float(r.schedule_number or 0))
                except:
                    num = 0
                schedule[r.name] = num
                created[r.name] = str(r.creation)
                i = i + 1

        # Packed signal = a Farm Pack List exists for the OPL (draft OR submitted).
        # Workflow: Ready = OPL submitted but packing not yet started. The moment packing
        # starts a Farm Pack List is created; it stays DRAFT until the user submits it, but
        # its mere existence means the order has moved into packing -> Packed. So a draft
        # pack list counts as packed (docstatus != 2 excludes only cancelled).
        packed = {}
        if names and isinstance(names, list) and len(names) > 0:
            fpls = frappe.get_all("Farm Pack List",
                                  filters=[["order_pick_list", "in", names], ["docstatus", "!=", 2]],
                                  fields=["order_pick_list"])
            q = 0
            while q < len(fpls):
                on = fpls[q].order_pick_list
                if on:
                    packed[on] = 1
                q = q + 1

        frappe.response["message"] = {"success": True, "takt_minutes": takt, "schedule": schedule, "created": created, "packed": packed}

    except Exception as e:
        frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def getTransferScheduleData():
    # Transfer Scheduling — read feed for the sales team's trip planner
    # (Web Page `transfer-control`). Returns, for a delivery window:
    #   orders   — every pick list that still has buckets to transfer, broken down
    #              BY FARM and BY VARIETY (buckets + stems). "order" = one Order Pick
    #              List (a mixed pick list may carry several varieties from several
    #              farms; they must arrive together).
    #   vehicles — vehicles with a capacity set (trolleys x buckets/trolley = buckets).
    #   trips    — existing Bucket Request Trip records + their order rows.
    # Kapkolia is the packhouse; buckets already shelved there are done and excluded.
    PACK = 'Kapkolia'
    fd = frappe.form_dict
    from_date = fd.get('from_date') or frappe.utils.add_days(frappe.utils.today(), 1)
    to_date = fd.get('to_date') or frappe.utils.add_days(frappe.utils.today(), 1)

    # ── Schedule gate: only orders that are ON a Packhouse Schedule are transfer-
    #    plannable. NOTE the schedule is created on the PROCESSING day (the day before
    #    delivery), so its schedule_date does NOT equal the delivery date — we must NOT
    #    tie the gate to the delivery window (that was the bug: an order delivering the
    #    7th is scheduled on the 6th, so a schedule_date-in-[7th] gate missed it). Gate on
    #    membership only, bounded to recent schedules; the delivery window + transfer
    #    flags below do the actual scoping. Latest schedule wins for a duplicated OPL. ──
    lo = frappe.utils.add_days(from_date, -14)
    sched_rows = frappe.db.sql("""
        SELECT pso.order_pick_list AS opl, pso.sequence AS seq, ps.team AS team
        FROM `tabPackhouse Schedule Order` pso
        JOIN `tabPackhouse Schedule` ps ON ps.name = pso.parent
        WHERE ps.schedule_date >= %(lo)s
        ORDER BY ps.schedule_date DESC
    """, {'lo': lo}, as_dict=True)
    sched_map = {}
    for s in sched_rows:
        op = s.get('opl')
        if op and op not in sched_map:
            sched_map[op] = {'seq': int(s.get('seq') or 0), 'team': s.get('team') or ''}

    rows = frappe.db.sql("""
        SELECT pli.parent AS opl, o.order_name AS order_name, so.customer AS customer,
               o.sales_order AS so, so.delivery_date AS delivery_date,
               o.custom_truck_details AS truck, o.custom_is_mixed_box_pick_list AS mixed,
               o.schedule_number AS schedule, o.team AS team,
               pli.bucket AS bucket, pli.item_code AS variety, pli.stock_qty AS stems,
               SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse, ''), pli.warehouse), ' ', 1) AS farm,
               pli.awaiting_transfer AS aw, pli.loaded_in_trolley AS ld, pli.in_transit AS tr
        FROM `tabPick List Item` pli
        JOIN `tabOrder Pick List` o ON o.name = pli.parent
        LEFT JOIN `tabSales Order` so ON so.name = o.sales_order
        WHERE pli.parenttype = 'Order Pick List' AND o.docstatus < 2
          AND so.delivery_date BETWEEN %(f)s AND %(t)s
          AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)
          AND pli.shelved = 0
          AND o.name IN (
              SELECT pso2.order_pick_list FROM `tabPackhouse Schedule Order` pso2
              JOIN `tabPackhouse Schedule` ps2 ON ps2.name = pso2.parent
              WHERE ps2.schedule_date >= %(lo)s
          )
        ORDER BY so.delivery_date, pli.parent
    """, {'f': from_date, 't': to_date, 'lo': lo}, as_dict=True)

    # Aggregate per OPL -> farm -> variety, de-duping physical buckets (one bucket can
    # appear on several box lines of a mixed order but is one physical unit). Helper
    # maps are kept SEPARATE from the returned dicts — the sandbox forbids dict keys
    # starting with "_", so nothing internal leaks into the payload.
    orders = {}          # opl -> returned meta dict
    farm_agg = {}        # opl -> { farm -> { buckets, stems, var -> {variety -> {buckets, stems}} } }
    order_of = []
    seen = {}
    for r in rows:
        opl = r['opl']
        farm = r.get('farm') or '?'
        if farm == PACK:
            continue
        bkt = r.get('bucket') or ''
        dk = opl + '|' + str(bkt)
        if bkt and (dk in seen):
            continue
        if bkt:
            seen[dk] = 1
        if opl not in orders:
            sm = sched_map.get(opl) or {}
            orders[opl] = {
                'opl': opl, 'order_name': r.get('order_name') or opl, 'customer': r.get('customer') or '',
                'so': r.get('so') or '', 'delivery_date': str(r.get('delivery_date') or ''),
                'truck': (r.get('truck') or '').strip(), 'mixed': 1 if int(r.get('mixed') or 0) else 0,
                'schedule': sm.get('seq') or 0, 'team': sm.get('team') or (r.get('team') or ''),
                'total_buckets': 0, 'total_stems': 0,
            }
            farm_agg[opl] = {}
            order_of.append(opl)
        o = orders[opl]
        stems = int(r.get('stems') or 0)
        variety = r.get('variety') or '?'
        o['total_buckets'] = o['total_buckets'] + 1
        o['total_stems'] = o['total_stems'] + stems
        fa = farm_agg[opl]
        if farm not in fa:
            fa[farm] = {'buckets': 0, 'stems': 0, 'var': {}}
        fm = fa[farm]
        fm['buckets'] = fm['buckets'] + 1
        fm['stems'] = fm['stems'] + stems
        if variety not in fm['var']:
            fm['var'][variety] = {'buckets': 0, 'stems': 0}
        vv = fm['var'][variety]
        vv['buckets'] = vv['buckets'] + 1
        vv['stems'] = vv['stems'] + stems

    order_list = []
    for opl in order_of:
        o = orders[opl]
        fa = farm_agg[opl]
        farms = []
        fnames = sorted(fa.keys())
        for fn in fnames:
            fm = fa[fn]
            vs = []
            for vn in fm['var']:
                vv = fm['var'][vn]
                vs.append({'variety': vn, 'buckets': vv['buckets'], 'stems': vv['stems']})
            farms.append({'farm': fn, 'buckets': fm['buckets'], 'stems': fm['stems'], 'varieties': vs})
        o['farms'] = farms
        order_list.append(o)

    # Order the plan by team then schedule sequence (the packhouse schedule order).
    pairs = []
    i = 0
    while i < len(order_list):
        o = order_list[i]
        pairs.append((o.get('team') or '', int(o.get('schedule') or 0), i))
        i = i + 1
    pairs.sort()
    sorted_list = []
    j = 0
    while j < len(pairs):
        sorted_list.append(order_list[pairs[j][2]])
        j = j + 1
    order_list = sorted_list

    # Vehicles with a capacity set, restricted to the INTERNAL LOGISTICS fleet (the ones
    # actually used for farm-to-packhouse bucket transfers) — excludes contracted/other
    # vehicles that happen to have a capacity set for unrelated reasons.
    veh = frappe.db.sql("""
        SELECT name, custom_trolley_capacity AS trolleys, custom_buckets_per_trolley AS bpt
        FROM `tabVehicle`
        WHERE COALESCE(custom_trolley_capacity, 0) > 0 AND COALESCE(custom_buckets_per_trolley, 0) > 0
          AND COALESCE(custom_is_internal_logistics_truck, 0) = 1
        ORDER BY name
    """, as_dict=True)
    vehicles = []
    for v in veh:
        t = int(v.get('trolleys') or 0)
        b = int(v.get('bpt') or 0)
        vehicles.append({'name': v['name'], 'trolleys': t, 'buckets_per_trolley': b, 'capacity_buckets': t * b})

    # Existing trips + their order rows. trip_date = the day the TRUCK physically runs
    # (today), not the delivery-date window used for orders above — a trip moving tomorrow's
    # scheduled produce is still driven today. Only today's trips are shown; yesterday's runs
    # don't clutter the planner.
    today_str = str(frappe.utils.today())
    trip_rows = frappe.get_all(
        "Bucket Request Trip",
        filters={"trip_date": today_str},
        fields=["name", "vehicle", "trip_date", "status", "notes", "collection_order", "farm", "total_buckets", "total_stems", "capacity_buckets", "dispatched_at", "received_at"],
        order_by="creation desc", limit_page_length=0,
    )
    trips = []
    for t in trip_rows:
        items = frappe.get_all(
            "Bucket Request Trip Order",
            filters={"parent": t["name"], "parenttype": "Bucket Request Trip"},
            fields=["order_pick_list", "order_name", "customer", "farm", "varieties", "buckets", "stems", "full_farm_buckets", "is_partial"],
            limit_page_length=0,
        )
        for it in items:
            it['full_farm_buckets'] = int(it.get('full_farm_buckets') or it.get('buckets') or 0)
            it['is_partial'] = 1 if int(it.get('is_partial') or 0) else 0
        trips.append({
            'name': t['name'], 'vehicle': t.get('vehicle') or '', 'trip_date': str(t.get('trip_date') or ''),
            'status': t.get('status') or 'Draft', 'notes': t.get('notes') or '',
            'collection_order': t.get('collection_order') or '', 'farm': t.get('farm') or '',
            'total_buckets': int(t.get('total_buckets') or 0), 'total_stems': int(t.get('total_stems') or 0),
            'capacity_buckets': int(t.get('capacity_buckets') or 0), 'orders': items,
            'dispatched_at': str(t.get('dispatched_at') or ''), 'received_at': str(t.get('received_at') or ''),
        })

    # ── Live bucket-stage breakdown per (opl, farm) for TODAY'S TRIPS ONLY — lets the
    #    app show an accurate awaiting/trolley/in-transit/shelved summary per trip and
    #    per farm stop, instead of the vehicle-wide truck_status aggregate below (which
    #    can't tell trips on the same vehicle apart, and never counted shelved buckets
    #    at all). Mirrors getFarmPlannedTrips's portion_state logic exactly, just not
    #    filtered to one farm. Purely computed for display -- nothing stored.
    # NOTE: RestrictedPython forbids identifiers starting with "_", so loop/temp
    # names below use a "row"/"trip"/"line" vocabulary instead of underscores.
    trip_opls = set()
    for triprow in trips:
        for lineitem in triprow['orders']:
            if lineitem.get('order_pick_list'):
                trip_opls.add(lineitem['order_pick_list'])

    bucket_stage = {}  # "opl||farm" -> {awaiting, loaded, in_transit, shelved}
    if trip_opls:
        pli_rows = frappe.get_all(
            "Pick List Item",
            filters={"parent": ["in", list(trip_opls)], "parenttype": "Order Pick List", "custom_bucket": ["!=", ""]},
            fields=["parent", "custom_bucket", "warehouse", "custom_source_warehouse",
                    "custom_awaiting_transfer", "custom_loaded_in_trolley", "custom_in_transit", "custom_shelved"],
            limit_page_length=0,
        )
        seen_stage_bkt = {}
        for row in pli_rows:
            wh = row.get('custom_source_warehouse') or row.get('warehouse') or ''
            farm = wh.split(' ')[0] if wh else ''
            opl = row.get('parent')
            bkt = row.get('custom_bucket') or ''
            dk = str(opl) + '|' + str(bkt).lower()
            if bkt and dk in seen_stage_bkt:
                continue
            if bkt:
                seen_stage_bkt[dk] = 1
            key = str(opl) + '||' + farm
            if key not in bucket_stage:
                bucket_stage[key] = {'awaiting': 0, 'loaded': 0, 'in_transit': 0, 'shelved': 0}
            stage = bucket_stage[key]
            if int(row.get('custom_shelved') or 0):
                stage['shelved'] = stage['shelved'] + 1
            elif int(row.get('custom_in_transit') or 0):
                stage['in_transit'] = stage['in_transit'] + 1
            elif int(row.get('custom_loaded_in_trolley') or 0):
                stage['loaded'] = stage['loaded'] + 1
            elif int(row.get('custom_awaiting_transfer') or 0):
                stage['awaiting'] = stage['awaiting'] + 1

    for triprow in trips:
        for lineitem in triprow['orders']:
            stagekey = str(lineitem.get('order_pick_list')) + '||' + str(lineitem.get('farm') or '')
            stage = bucket_stage.get(stagekey)
            lineitem['awaiting'] = stage['awaiting'] if stage else 0
            lineitem['loaded'] = stage['loaded'] if stage else 0
            lineitem['in_transit'] = stage['in_transit'] if stage else 0
            lineitem['shelved'] = stage['shelved'] if stage else 0

    # ── Truck physical status: where is each truck NOW, from its Pick List Item buckets
    #    (custom_transit_truck). Per bucket take the most-advanced phase; the latest-modified
    #    bucket places the truck. shelved = arrived at Kapkolia; in_transit = on the way;
    #    loaded/awaiting = loading at the farm. loading_pct = loaded/(loaded+awaiting).
    #    BUG FIXED 2026-08-07: this used to gate on the SALES ORDER's delivery_date window
    #    (from_date/to_date) — but custom_transit_truck is set once when a bucket is loaded
    #    and never cleared, so that gate pulled in every bucket EVER flagged to a truck for
    #    any order that happens to deliver inside whatever window the page is showing —
    #    stale/historical loads, not "where is the truck right now" (caught live: KTCB 443K
    #    showed 148 total buckets against a same-day trip that only ever committed 57).
    #    "Right now" means TODAY's flagging activity, full stop — gate on that instead. ──
    tk_rows = frappe.db.sql("""
        SELECT pli.transit_truck AS truck,
               pli.awaiting_transfer AS aw, pli.loaded_in_trolley AS ld,
               pli.in_transit AS tr, pli.shelved AS sh,
               SUBSTRING_INDEX(COALESCE(NULLIF(pli.source_warehouse,''),pli.warehouse),' ',1) AS farm,
               pli.modified AS modified
        FROM `tabPick List Item` pli
        JOIN `tabOrder Pick List` o ON o.name = pli.parent
        WHERE pli.parenttype = 'Order Pick List' AND o.docstatus < 2
          AND pli.transit_truck IS NOT NULL AND pli.transit_truck != ''
          AND DATE(pli.modified) = %(td)s
    """, {'td': today_str}, as_dict=True)
    tmap = {}
    for r in tk_rows:
        tk = r.get('truck') or ''
        if not tk:
            continue
        st = tmap.get(tk)
        if not st:
            st = {'truck': tk, 'total': 0, 'awaiting': 0, 'loaded': 0, 'in_transit': 0, 'shelved': 0, 'last': '', 'last_phase': '', 'farm': ''}
            tmap[tk] = st
        sh = int(r.get('sh') or 0)
        tr = int(r.get('tr') or 0)
        ld = int(r.get('ld') or 0)
        aw = int(r.get('aw') or 0)
        phase = ''
        if sh:
            phase = 'shelved'
        elif tr:
            phase = 'in_transit'
        elif ld:
            phase = 'loaded'
        elif aw:
            phase = 'awaiting'
        st['total'] = st['total'] + 1
        if phase == 'shelved':
            st['shelved'] = st['shelved'] + 1
        elif phase == 'in_transit':
            st['in_transit'] = st['in_transit'] + 1
        elif phase == 'loaded':
            st['loaded'] = st['loaded'] + 1
        elif phase == 'awaiting':
            st['awaiting'] = st['awaiting'] + 1
        md = str(r.get('modified') or '')
        if md > st['last']:
            st['last'] = md
            st['last_phase'] = phase
            st['farm'] = r.get('farm') or ''
    truck_status = []
    for tk in tmap:
        st = tmap[tk]
        loadtot = st['loaded'] + st['awaiting']
        loading_pct = 100
        if loadtot > 0:
            loading_pct = int(round(st['loaded'] * 100.0 / loadtot))
        lp = st['last_phase']
        loc = 'unknown'
        if lp == 'shelved':
            loc = 'arrived'
        elif lp == 'in_transit':
            loc = 'in_transit'
        elif lp == 'loaded' or lp == 'awaiting':
            loc = 'loading'
        truck_status.append({
            'truck': st['truck'], 'total': st['total'], 'awaiting': st['awaiting'],
            'loaded': st['loaded'], 'in_transit': st['in_transit'], 'shelved': st['shelved'],
            'location': loc, 'farm': st['farm'], 'loading_pct': loading_pct, 'last': st['last'],
        })

    # Inter-farm road distances (Farm Distance doctype) — for collection-route optimisation
    # on the client. Symmetric, so one direction per pair is enough. `name` is included so
    # the client can save a Bucket Logistics Route's legs as links to these exact records.
    dist_rows = frappe.get_all("Farm Distance", fields=["name", "from_farm", "to_farm", "distance_km", "is_road_leg"], limit_page_length=0)
    distances = []
    for d in dist_rows:
        distances.append({'name': d.get('name') or '', 'a': d.get('from_farm') or '', 'b': d.get('to_farm') or '',
                           'km': float(d.get('distance_km') or 0), 'leg': 1 if int(d.get('is_road_leg') or 0) else 0})

    # Today's planned truck routes (Bucket Logistics Route) — decided each morning, one
    # doc per (date, vehicle). Drives which farms a truck is allowed to serve when
    # distributing schedules. A truck with NO route today is left unrestricted (can serve
    # any farm) so the feature degrades gracefully until routes are actually set up.
    route_rows = frappe.get_all(
        "Bucket Logistics Route",
        filters={"route_date": today_str},
        fields=["name", "vehicle", "total_km"],
        limit_page_length=0,
    )
    routes = []
    for rr in route_rows:
        legs = frappe.get_all(
            "Bucket Logistics Route Leg",
            filters={"parent": rr["name"], "parenttype": "Bucket Logistics Route"},
            fields=["leg", "from_farm", "to_farm", "distance_km"],
            order_by="idx asc", limit_page_length=0,
        )
        farms = {}
        for lg in legs:
            if lg.get('from_farm') and lg.get('from_farm') != PACK:
                farms[lg['from_farm']] = 1
            if lg.get('to_farm') and lg.get('to_farm') != PACK:
                farms[lg['to_farm']] = 1
        routes.append({
            'name': rr['name'], 'vehicle': rr.get('vehicle') or '',
            'total_km': float(rr.get('total_km') or 0),
            'legs': legs, 'farms': list(farms.keys()),
        })

    frappe.response['orders'] = order_list
    frappe.response['vehicles'] = vehicles
    frappe.response['trips'] = trips
    frappe.response['truck_status'] = truck_status
    frappe.response['distances'] = distances
    frappe.response['routes'] = routes
    frappe.response['packhouse'] = PACK
    frappe.response['window'] = {'from': str(from_date), 'to': str(to_date)}
    frappe.response['generated_at'] = str(frappe.utils.now())


@frappe.whitelist()
def get_pick_list_with_farm_pack_list():
    pick_list_id = frappe.form_dict.get('pick_list_id')

    # ── Recover OPL names truncated in transit ──────────────────────────────────
    # The mobile app sends pick_list_id un-encoded. OPL names containing '&'
    # (e.g. "OPL-UK & IE Flora Group Limited-1270607") get split by the query
    # parser, so form_dict only keeps the part before '&' ("OPL-UK "). The full
    # value still survives in the raw query string, so rebuild it from there when
    # the form_dict value does not resolve to a real Order Pick List.
    try:
        if not pick_list_id or not frappe.db.exists('Order Pick List', pick_list_id):
            raw_qs = frappe.request.query_string
            if raw_qs:
                raw_qs = raw_qs.decode('utf-8')
                if 'pick_list_id=' in raw_qs:
                    candidate = raw_qs.split('pick_list_id=', 1)[1]
                    candidate = candidate.replace('%20', ' ').replace('+', ' ').strip()
                    if frappe.db.exists('Order Pick List', candidate):
                        pick_list_id = candidate
    except Exception:
        pass

    try:
        # Fetch Order Pick List
        order_pick_list = frappe.get_doc('Order Pick List', pick_list_id).as_dict()

        # Consolidate locations by variety, stem length, and bunch size
        consolidated_locations = {}

        for location in order_pick_list.get('table_ytkc', []):
            key = (
                location.get('item_code'),
                location.get('stem_length'),
                location.get('uom'),
                location.get('conversion_factor')
            )

            if key in consolidated_locations:
                consolidated_locations[key]['qty'] = consolidated_locations[key]['qty'] + location.get('qty', 0)
                consolidated_locations[key]['stock_qty'] = consolidated_locations[key]['stock_qty'] + location.get('stock_qty', 0)
                consolidated_locations[key]['picked_qty'] = consolidated_locations[key]['picked_qty'] + location.get('picked_qty', 0)
                consolidated_locations[key]['stock_reserved_qty'] = consolidated_locations[key]['stock_reserved_qty'] + location.get('stock_reserved_qty', 0)

                if 'custom_buckets' not in consolidated_locations[key]:
                    consolidated_locations[key]['custom_buckets'] = [consolidated_locations[key].get('bucket')]
                    consolidated_locations[key]['custom_shelves'] = [consolidated_locations[key].get('shelf')]

                consolidated_locations[key]['custom_buckets'].append(location.get('bucket'))
                consolidated_locations[key]['custom_shelves'].append(location.get('shelf'))
            else:
                consolidated_locations[key] = location.copy()

        order_pick_list['table_ytkc'] = list(consolidated_locations.values())

        for idx, location in enumerate(order_pick_list['table_ytkc'], start=1):
            location['idx'] = idx
            so_item_ref = location.get('custom_sale_order_item')
            if so_item_ref:
                spec_row = frappe.db.get_value('Sales Order Item', so_item_ref, ['custom_line', 'custom_length'])
                if spec_row:
                    location['custom_spec'] = spec_row[0]
                    location['custom_so_length'] = spec_row[1]

        # Item group per variety -> packing tells scan-per-bunch (spray) from manual
        # entry (standard) per line. Also expose the rows as `locations` under the
        # field names the mobile packing app reads (the child table is table_ytkc,
        # with stem_length / source_warehouse).
        variety_codes = []
        for location in order_pick_list['table_ytkc']:
            ic = location.get('item_code')
            if ic and ic not in variety_codes:
                variety_codes.append(ic)
        item_groups = {}
        if variety_codes:
            for it in frappe.get_all('Item', filters={'name': ['in', variety_codes]}, fields=['name', 'item_group']):
                item_groups[it.get('name')] = it.get('item_group')
        for location in order_pick_list['table_ytkc']:
            location['item_group'] = item_groups.get(location.get('item_code'), '')
            location['warehouse'] = location.get('source_warehouse')
            location['custom_stem_length'] = location.get('stem_length')
        order_pick_list['locations'] = order_pick_list['table_ytkc']

        # ================================
        # Load ONLY the latest SUBMITTED Farm Pack List
        # ================================
        farm_pack_list_names = frappe.db.get_all(
            'Farm Pack List',
            filters={
                'order_pick_list': pick_list_id
            },
            fields=['name'],
            order_by='creation desc',
            limit=1
        )
        farm_pack_list_with_items = []
        for fp in farm_pack_list_names:
            doc = frappe.get_doc('Farm Pack List', fp.name).as_dict()
            farm_pack_list_with_items.append(doc)

        # ================================
        # Build Packing Guide Summary
        # ================================

        opl_doc = frappe.get_doc('Order Pick List', pick_list_id)
        sales_order = opl_doc.sales_order

        packing_guide = {"error": "No Sales Order linked"}

        if sales_order:
            so = frappe.get_doc('Sales Order', sales_order)

            # Current mix group from this OPL
            current_mix_group = opl_doc.get("mix_group")

            # Packed stems — only from current submitted FPL
            packed_stems_by_item = {}
            packed_total = 0

            for fpl in farm_pack_list_with_items:
                for row in fpl.get('pack_list_item', []):
                    stems = row.get('stock_qty') or (row.get('bunch_qty', 0) * 10)
                    item_code = row.get('item_code')
                    if item_code and stems:
                        packed_stems_by_item[item_code] = packed_stems_by_item.get(item_code, 0) + stems
                        packed_total += stems

            def safe_int(val):
                if val is None:
                    return 0
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    return 0

            # =========================================================
            # DETERMINE PACKING MODE FOR THIS SPECIFIC OPL
            #
            # Instead of checking the entire SO globally, we determine
            # the mode based on what items THIS OPL actually covers.
            #
            # - If current_mix_group is set, find SO items in that group
            #   that are marked as mixed → MIXED mode
            # - Otherwise, match OPL item_codes to SO items and use
            #   STRAIGHT mode for those items
            # =========================================================

            # Get the item_codes present in this OPL
            opl_item_codes = set()
            for loc in order_pick_list.get('table_ytkc', []):
                item_code = loc.get('item_code')
                if item_code:
                    opl_item_codes.add(item_code)

            # Sales Order Item(s) this OPL's rows actually reference. Matching the
            # plan by these (not by item_code) is what makes the numbers reconcile
            # when the same variety sits on more than one Sales Order line.
            opl_so_item_names = set()
            for loc in opl_doc.get('table_ytkc', []):
                soi = loc.get('custom_sale_order_item')
                if soi:
                    opl_so_item_names.add(soi)

            # Determine if THIS OPL is handling mixed-box items
            is_mixed = False
            if current_mix_group:
                # Check if any SO items in this mix group are actually mixed
                mix_group_items = [
                    item for item in so.items
                    if str(item.get("custom_mix_group") or "") == str(current_mix_group)
                       and item.custom_mixed_box == 1
                ]
                if mix_group_items:
                    is_mixed = True

            # ── Detect MIXED BUNCH for this OPL: colour lines of one bouquet that
            #    are NOT flagged custom_mixed_box; signalled by the linked
            #    Specification having box_items with bunch_type == "Mixed Bunch". ──
            is_mixed_bunch = False
            if not is_mixed:
                for bunch_item in so.items:
                    if opl_so_item_names and bunch_item.name not in opl_so_item_names:
                        continue
                    bunch_cl = bunch_item.get("custom_line")
                    if bunch_cl and frappe.db.exists("Spec Box Item", {"parent": bunch_cl, "bunch_type": "Mixed Bunch"}):
                        is_mixed_bunch = True
                        break

            # =========================================================
            # MIXED BUNCH: one bouquet from several colour lines sharing ONE box
            # set — boxes come from a single line (not summed), packrate = sum of
            # custom_packrate_mixed_box (stems per box). Mirrors the mixed-box math.
            # =========================================================
            if is_mixed_bunch:
                if opl_so_item_names:
                    relevant_items = [item for item in so.items if item.name in opl_so_item_names]
                else:
                    relevant_items = [item for item in so.items if item.item_code in opl_item_codes]

                planned_stems_by_item = {}
                planned_total = 0
                planned_boxes_from_order = 0
                box_type = "Mixed Bunch"
                packrate_mixed_box_sum = 0

                for item in relevant_items:
                    ordered_stems = item.get('custom_ordered_quantity')
                    if ordered_stems is not None:
                        stems = ordered_stems
                    else:
                        qty = item.get('qty') or 0
                        conv = item.get('conversion_factor') or 1
                        stems = qty * conv
                    planned_stems_by_item[item.item_code] = planned_stems_by_item.get(item.item_code, 0) + stems
                    planned_total += stems
                    if planned_boxes_from_order == 0:
                        planned_boxes_from_order = safe_int(item.custom_number_of_boxes)
                    box_type = item.custom_box_type or "Mixed Bunch"
                    packrate_mixed_box_sum += safe_int(item.get('custom_packrate_mixed_box'))

                calculated_boxes = round(planned_total / packrate_mixed_box_sum, 1) if packrate_mixed_box_sum > 0 else 0
                planned_boxes = planned_boxes_from_order or calculated_boxes
                packrate_per_box = round(planned_total / planned_boxes) if planned_boxes > 0 else safe_int(packrate_mixed_box_sum)

            # =========================================================
            # STRAIGHT BOXES: Filter SO items to match THIS OPL only
            # =========================================================
            elif not is_mixed:
                # Match the plan to THIS OPL by the exact Sales Order Item its rows
                # reference (custom_sale_order_item). Matching by item_code grabbed
                # the wrong line when the same variety appears on multiple SO lines,
                # so planned stems / boxes / packrate stopped reconciling. Fall back
                # to item_code matching for older OPLs that lack the reference.
                if opl_so_item_names:
                    relevant_items = [item for item in so.items if item.name in opl_so_item_names]
                else:
                    relevant_items = [item for item in so.items if item.item_code in opl_item_codes]

                packrate_per_box = 0
                planned_boxes_from_order = 0
                box_type = "Standard"

                planned_stems_by_item = {}
                planned_total = 0

                for item in relevant_items:
                    ordered_stems = item.get('custom_ordered_quantity')
                    if ordered_stems is not None:
                        stems = ordered_stems
                    else:
                        qty = item.get('qty') or 0
                        conv = item.get('conversion_factor') or 1
                        stems = qty * conv

                    # Sum across every matched line so the totals add up.
                    item_code = item.item_code
                    planned_stems_by_item[item_code] = planned_stems_by_item.get(item_code, 0) + stems
                    planned_total += stems

                    if packrate_per_box == 0:
                        packrate_per_box = safe_int(item.custom_packrate)
                    planned_boxes_from_order += safe_int(item.custom_number_of_boxes)
                    if box_type == "Standard":
                        box_type = item.custom_box_type or "Standard"

                calculated_boxes = round(planned_total / packrate_per_box, 1) if packrate_per_box > 0 and planned_total > 0 else 0
                planned_boxes = planned_boxes_from_order or calculated_boxes

            # =========================================================
            # MIXED BOXES: Use items from the matching mix group
            # =========================================================
            else:
                relevant_items = [
                    item for item in so.items
                    if str(item.get("custom_mix_group") or "") == str(current_mix_group)
                ]

                planned_stems_by_item = {}
                planned_total = 0
                planned_boxes_from_order = 0
                box_type = "Mixed"

                # Sum of custom_packrate_mixed_box = stems per box across all varieties
                packrate_mixed_box_sum = 0

                for item in relevant_items:
                    if item.custom_mixed_box != 1:
                        continue

                    ordered_stems = item.get('custom_ordered_quantity')
                    if ordered_stems is not None:
                        stems = ordered_stems
                    else:
                        qty = item.get('qty') or 0
                        conv = item.get('conversion_factor') or 1
                        stems = qty * conv
                    planned_stems_by_item[item.item_code] = planned_stems_by_item.get(item.item_code, 0) + stems
                    planned_total += stems

                    if planned_boxes_from_order == 0:
                        planned_boxes_from_order = safe_int(item.custom_number_of_boxes)

                    box_type = item.custom_box_type or "Mixed"

                    # Accumulate per-variety packrate for fallback calculation
                    packrate_mixed_box_sum += safe_int(item.get('custom_packrate_mixed_box'))

                # FIX: Use sum of custom_packrate_mixed_box as the per-box packrate for fallback
                calculated_boxes = round(planned_total / packrate_mixed_box_sum, 1) if packrate_mixed_box_sum > 0 else 0
                planned_boxes = planned_boxes_from_order or calculated_boxes

                # For mixed boxes, packrate = total planned stems / number of boxes
                packrate_per_box = round(planned_total / planned_boxes) if planned_boxes > 0 else safe_int(planned_total)

            # Build per-item guide
            items_guide = []
            for item_code, planned in planned_stems_by_item.items():
                packed = packed_stems_by_item.get(item_code, 0)
                remaining = planned - packed
                percentage = round(packed / planned * 100, 1) if planned > 0 else 100

                boxes_contrib = round(planned / packrate_per_box, 2) if packrate_per_box > 0 else 0

                items_guide.append({
                    "item_code": item_code,
                    "item_name": frappe.db.get_value("Item", item_code, "item_name"),
                    "length": "",
                    "planned_stems": planned,
                    "packed_stems": packed,
                    "remaining_stems": remaining,
                    "percentage_complete": percentage,
                    "boxes_contribution": boxes_contrib
                })

            overall_percentage = round(packed_total / planned_total * 100, 1) if planned_total > 0 else 100

            # Item group drives packing UX (spray roses = scan bunch QRs,
            # standard roses = manual entry). OPL.item_group is often unset, so
            # fall back to the OPL's actual variety (Item.item_group) — the
            # source of truth — so standards never get the bunch-scan page.
            _opl_group = opl_doc.get("item_group") or ""
            if not _opl_group:
                _fv = frappe.db.get_value(
                    "Pick List Item",
                    {"parent": opl_doc.name, "parenttype": "Order Pick List"},
                    "item_code",
                )
                if _fv:
                    _opl_group = frappe.db.get_value("Item", _fv, "item_group") or ""

            packing_guide = {
                "pick_list": opl_doc.name,
                "sales_order": sales_order,
                "item_group": _opl_group,
                "customer": so.customer_name,
                "delivery_date": so.delivery_date,
                "farm": opl_doc.get("farm") or so.get("custom_farm") or "",
                "is_mixed": is_mixed,
                "box_type": box_type,
                "planned_boxes": planned_boxes,
                "packrate_per_box": packrate_per_box,
                "planned_total_stems": planned_total,
                "packed_total_stems": packed_total,
                "remaining_stems": planned_total - packed_total,
                "overall_percentage": overall_percentage,
                "items": items_guide
            }

            # =========================================================
            # MIXED BUNCH enrichment (additive — never affects the above).
            # A mixed bunch = one bouquet built from several colour/variety
            # components, defined on the linked Specification's box_items rows
            # where bunch_type == "Mixed Bunch". Detected from the SPEC (not from
            # custom_mixed_bunch) so it works regardless of allocation-side fields.
            # Adds: is_mixed_bunch, bouquet_guide (pick N stems of each colour),
            # spec, spec_image (File attached to the Specification).
            # =========================================================
            try:
                spec_names = []
                for item in so.items:
                    if opl_so_item_names and item.name not in opl_so_item_names:
                        continue
                    cl = item.get("custom_line")
                    if cl and cl not in spec_names:
                        spec_names.append(cl)

                bunch_guide = []
                bunch_spec = None
                for sp_name in spec_names:
                    try:
                        spec_doc = frappe.get_doc("Specifications", sp_name)
                    except Exception:
                        continue
                    rows = [
                        r for r in (spec_doc.get("box_items") or [])
                        if r.get("bunch_type") == "Mixed Bunch" and r.get("variety")
                    ]
                    if rows:
                        bunch_spec = sp_name
                        for r in rows:
                            bunch_guide.append({
                                "colour": r.get("colour"),
                                "variety": r.get("variety"),
                                "variety_name": frappe.db.get_value("Item", r.get("variety"), "item_name") or r.get("variety"),
                                "stems_per_bunch": safe_int(r.get("stems_per_bunch")),
                                "length": r.get("length"),
                            })
                        break

                spec_image = None
                if bunch_spec:
                    img_files = frappe.db.get_all(
                        "File",
                        filters={"attached_to_doctype": "Specifications", "attached_to_name": bunch_spec},
                        fields=["file_url"],
                        order_by="creation desc",
                        limit=1,
                    )
                    if img_files:
                        spec_image = img_files[0].get("file_url")

                packing_guide["is_mixed_bunch"] = bool(bunch_guide)
                packing_guide["bouquet_guide"] = bunch_guide
                packing_guide["spec"] = bunch_spec
                packing_guide["spec_image"] = spec_image
            except Exception as enrich_err:
                frappe.log_error(f"mixed-bunch enrich: {enrich_err}", "Pick List Packing Guide Script")

        frappe.response['data'] = {
            'order_pick_list': order_pick_list,
            'farm_pack_lists': farm_pack_list_with_items,
            'packing_guide': packing_guide
        }

    except Exception as e:
        frappe.log_error(f"Error in get_pick_list_with_farm_packs: {e}", "Pick List Packing Guide Script")
        frappe.response['data'] = None
        frappe.response['http_status_code'] = 500


@frappe.whitelist()
def issueBucketToSaleOrderItem():
    try:
        # Get payload from request
        payload = frappe.request.json
        frappe.log_error("Issuing from coldstore payload", payload)

        bucket_id = payload.get('bucket')
        sale_order_item = payload.get('sale_order_item')
        opl_name = payload.get('opl_name')

        # ----------------------------------------------------------------------
        # SCHEDULER ENFORCEMENT (optional, per-farm + per-TEAM, per delivery date)
        # ----------------------------------------------------------------------
        # Flip allow_scheduler_validation to 1 to enforce the packhouse schedule:
        # a bucket for an OPL with a higher custom_schedule_number cannot be issued
        # until every lower-numbered OPL of the SAME FARM, SAME TEAM and SAME delivery
        # date has reached scheduler_issue_threshold % issued (issued Pick List Item
        # rows / total rows). e.g. 50 => once a line is 50% issued the team may start
        # its next line. Set to 100 to require a line be fully issued before moving on.
        #
        # Each TEAM has its own queue, so several lines (one per team) can be the
        # "next" line at once and teams never wait on each other. A team's own earlier
        # line still gates its later lines: e.g. with line 1 -> team W and line 6 -> team W,
        # team W cannot start #6 until #1 is at/above the threshold; meanwhile lines
        # 2,3,4 for teams X,Y,Z are all issuable in parallel.
        # Numbering is global per-day but compared only within (farm, team), so
        # Kapkolia's #9 never blocks Karen's #10, and team X's #2 never blocks team W.
        # 0 = skip entirely and issue as usual (no behaviour change).
        allow_scheduler_validation = 0
        scheduler_issue_threshold = 50
        schedule_blocked = False
        if allow_scheduler_validation and opl_name:
            cur_opl = frappe.db.get_value(
                'Order Pick List', opl_name,
                ['custom_schedule_number', 'farm', 'team', 'sales_order'],
                as_dict=True
            )
            cur_num = 0
            if cur_opl:
                try:
                    cur_num = int(float(cur_opl.custom_schedule_number or 0))
                except Exception:
                    cur_num = 0

            cur_dd = None
            if cur_opl and cur_opl.sales_order:
                cur_dd = frappe.db.get_value('Sales Order', cur_opl.sales_order, 'delivery_date')

            # Only enforce when this OPL is scheduled and we know its farm + team + delivery date
            if cur_opl and cur_num > 0 and cur_opl.farm and cur_opl.team and cur_dd:
                so_rows = frappe.get_all(
                    'Sales Order',
                    filters=[['delivery_date', '=', cur_dd]],
                    fields=['name']
                )
                so_list = []
                for so in so_rows:
                    so_list.append(so.name)

                lower_opls = []
                if so_list:
                    sibs = frappe.get_all(
                        'Order Pick List',
                        filters=[
                            ['docstatus', '=', 1],
                            ['farm', '=', cur_opl.farm],
                            ['team', '=', cur_opl.team],
                            ['sales_order', 'in', so_list]
                        ],
                        fields=['name', 'custom_schedule_number']
                    )
                    for sib in sibs:
                        snum = 0
                        try:
                            snum = int(float(sib.custom_schedule_number or 0))
                        except Exception:
                            snum = 0
                        if snum > 0 and snum < cur_num:
                            lower_opls.append({'name': sib.name, 'num': snum})

                # A lower-numbered OPL is "done" only when ALL its Pick List Item rows are issued
                blockers = []
                if lower_opls:
                    lower_names = []
                    for lo in lower_opls:
                        lower_names.append(lo['name'])
                    plis = frappe.get_all(
                        'Pick List Item',
                        filters=[['parent', 'in', lower_names]],
                        fields=['parent', 'issued']
                    )
                    total_by = {}
                    issued_by = {}
                    for pli in plis:
                        total_by[pli.parent] = total_by.get(pli.parent, 0) + 1
                        if pli.issued:
                            issued_by[pli.parent] = issued_by.get(pli.parent, 0) + 1
                    for lo in lower_opls:
                        t = total_by.get(lo['name'], 0)
                        done = issued_by.get(lo['name'], 0)
                        # A line with no buckets to issue (t == 0) can never reach the
                        # threshold, so treat it as satisfied to avoid a permanent deadlock.
                        pct = (100.0 * done / t) if t else 100.0
                        if pct < scheduler_issue_threshold:
                            blockers.append(lo['num'])

                if blockers:
                    start_from = min(blockers)
                    schedule_blocked = True
                    frappe.response.message = (
                        f"Schedule order for team {cur_opl.team} ({cur_opl.farm}): start from #{start_from}. "
                        f"Order #{start_from} is below {scheduler_issue_threshold}% issued, so #{cur_num} cannot be issued before it."
                    )
                    frappe.response.http_status_code = 409
                    frappe.response.data = {
                        'status': 'schedule_blocked',
                        'farm': cur_opl.farm,
                        'team': cur_opl.team,
                        'attempted_schedule_number': cur_num,
                        'start_from': start_from,
                        'threshold_percent': scheduler_issue_threshold,
                        'pending_lower_numbers': sorted(blockers)
                    }

        # -------------------------------
        # Validation
        # -------------------------------
        if schedule_blocked:
            pass
        elif not bucket_id:
            frappe.response.message = "Error: Bucket ID is required"
            frappe.response.http_status_code = 400
        elif not sale_order_item:
            frappe.response.message = "Error: Sale Order Item is required"
            frappe.response.http_status_code = 400
        else:
            # -------------------------------
            # Get latest Stock Entry for bucket
            # -------------------------------
            stock_entries = frappe.db.get_list(
                'Stock Entry',
                filters={
                    'custom_bucket_id': bucket_id,
                    'docstatus': 1
                },
                fields=['name', 'creation', 'custom_issued_to'],
                order_by='creation desc',
                limit=1
            )

            if not stock_entries:
                frappe.response.message = f"No stock entry found with bucket ID: {bucket_id}"
                frappe.response.http_status_code = 404
            else:
                latest_stock_entry = stock_entries[0]
                stock_entry_name = latest_stock_entry['name']
                current_issued_to = latest_stock_entry.get('custom_issued_to')

                # -------------------------------
                # Already issued check
                # -------------------------------
                if current_issued_to == sale_order_item:
                    frappe.response.message = (
                        f"Stock Entry {stock_entry_name} is already issued "
                        f"to sale order item {sale_order_item}"
                    )
                    frappe.response.http_status_code = 409
                    frappe.response.data = {
                        'stock_entry': stock_entry_name,
                        'bucket_id': bucket_id,
                        'current_issued_to': current_issued_to,
                        'requested_issued_to': sale_order_item,
                        'status': 'already_issued'
                    }
                else:
                    # -------------------------------
                    # Issue bucket (update Stock Entry)
                    # -------------------------------
                    frappe.db.set_value(
                        'Stock Entry',
                        stock_entry_name,
                        'custom_issued_to',
                        sale_order_item
                    )

                    # -------------------------------
                    # UPDATE ONLY THE TARGET OPL'S CHILD ROW (Pick List Item)
                    # -------------------------------
                    # Scope to the OPL being issued. A bucket can be allocated to
                    # MORE THAN ONE OPL; filtering by bucket alone would mark it
                    # issued in every OPL ("issued itself"). Fall back to bucket-only
                    # for older callers that don't send opl_name.
                    pli_filters = {'bucket': bucket_id}
                    if opl_name:
                        pli_filters['parent'] = opl_name
                    pick_list_items = frappe.db.get_all(
                        'Pick List Item',
                        filters=pli_filters,
                        fields=['name', 'parent']
                    )

                    updated_opls = set()
                    for item in pick_list_items:
                        # Mark this child location as issued
                        frappe.db.set_value(
                            'Pick List Item',
                            item.name,
                            'issued',
                            1
                        )
                        updated_opls.add(item.parent)

                    # -------------------------------
                    # DO NOT UPDATE PARENT OPL FIELDS
                    # (issued and custom_issuing_percentage remain untouched)
                    # -------------------------------

                    # -------------------------------
                    # REMOVE BUCKET FROM SHELF
                    # -------------------------------
                    shelf_items = frappe.db.get_all(
                        'Shelf Item',
                        filters={'bucket_id': bucket_id},
                        fields=['name', 'parent']
                    )

                    removed_from_shelf = []
                    for item in shelf_items:
                        # Durable removal log: flip this bucket's Shelving Log from
                        # 'Shelved' to 'Issued to Sales Order' (with removed_on) so the
                        # removal survives the Shelf Item hard-delete below.
                        try:
                            existing_log = frappe.db.get_value('Shelving Log', {'shelf_item': item.name}, 'name')
                            if existing_log:
                                frappe.db.set_value('Shelving Log', existing_log, {
                                    'reason': 'Issued to Sales Order',
                                    'removed_on': frappe.utils.now_datetime()
                                })
                            else:
                                log = frappe.new_doc('Shelving Log')
                                log.bucket_id = bucket_id
                                log.shelf = item.parent
                                log.shelf_item = item.name
                                log.reason = 'Issued to Sales Order'
                                log.shelved_by = frappe.session.user
                                log.removed_on = frappe.utils.now_datetime()
                                log.insert(ignore_permissions=True)
                        except Exception:
                            frappe.log_error('Shelving Log on issue failed', str(bucket_id))
                        frappe.delete_doc('Shelf Item', item.name, force=1)
                        removed_from_shelf.append(item.parent)
                        # Touch parent shelf to refresh UI/modified time
                        frappe.db.set_value('Shelf', item.parent, 'modified', frappe.utils.now())

                    # -------------------------------
                    # ISSUE FROM THE COLD STORE (Material Transfer)
                    # -------------------------------
                    # Move the bucket's stems out of their current warehouse into the
                    # delivery warehouse mapped in the Roses SO Warehouse Mapping.
                    # Nothing hardcoded: the target comes from Roses-MAP (source ->
                    # delivery); the source is where the stock currently sits (the
                    # bucket's receiving entry).
                    issue_transfer = None
                    se_detail = frappe.db.get_all(
                        'Stock Entry Detail',
                        filters={'parent': stock_entry_name},
                        fields=['item_code', 'qty', 'uom', 't_warehouse', 's_warehouse'],
                        limit=1
                    )
                    if se_detail:
                        det = se_detail[0]
                        src_wh = det.t_warehouse or det.s_warehouse
                        target_wh = None
                        map_rows = frappe.db.get_all(
                            'SO Warehouse Mapping Item',
                            filters={'parent': 'Roses-MAP'},
                            fields=['source_warehouse', 'delivery_warehouse']
                        )
                        for r in map_rows:
                            if r.source_warehouse == src_wh:
                                target_wh = r.delivery_warehouse
                                break
                        if not target_wh and map_rows:
                            target_wh = map_rows[0].delivery_warehouse
                        if src_wh and target_wh and det.item_code and det.qty:
                            transfer = frappe.new_doc('Stock Entry')
                            transfer.stock_entry_type = 'Issue From The Cold Store'
                            transfer.company = frappe.db.get_value('Warehouse', src_wh, 'company')
                            transfer.custom_business_unit = 'Roses'
                            transfer.farm = frappe.db.get_value('Stock Entry', stock_entry_name, 'farm')
                            transfer.custom_bucket_id = bucket_id
                            transfer.custom_issued_to = sale_order_item
                            transfer.custom_receiving_entry = stock_entry_name
                            transfer.append('items', {
                                'item_code': det.item_code,
                                'qty': det.qty,
                                'uom': det.uom,
                                'conversion_factor': 1,
                                's_warehouse': src_wh,
                                't_warehouse': target_wh,
                                'allow_zero_valuation_rate': 1,
                                'basic_rate': 0,
                            })
                            transfer.insert(ignore_permissions=True)
                            transfer.submit()
                            issue_transfer = transfer.name

                    # -------------------------------
                    # Commit all changes
                    # -------------------------------
                    frappe.db.commit()

                    frappe.response.message = (
                        f"Bucket {bucket_id} successfully issued to {sale_order_item}, "
                        f"and removed from shelf"
                    )
                    frappe.response.http_status_code = 200
                    frappe.response.data = {
                        'stock_entry': stock_entry_name,
                        'bucket_id': bucket_id,
                        'previous_issued_to': current_issued_to,
                        'new_issued_to': sale_order_item,
                        'updated_child_rows': len(pick_list_items),
                        'affected_opls': list(updated_opls),
                        'removed_from_shelf_count': len(removed_from_shelf),
                        'shelves_affected': list(set(removed_from_shelf)),
                        'updated_at': frappe.utils.now(),
                        'issue_transfer': issue_transfer,
                        'status': 'issued_and_child_updated'
                    }

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error("Coldstore Issue Error",e)
        frappe.response.message = f"Error issuing bucket: {str(e)}"
        frappe.response.http_status_code = 500
        frappe.response.data = {
            'error': str(e)
        }


@frappe.whitelist()
def receiveBucketTrip():
    name = frappe.form_dict.get("name")
    if not name or not frappe.db.exists("Bucket Request Trip", name):
        frappe.response["message"] = {"status": "error", "message": "Trip not found."}
    else:
        current = frappe.db.get_value("Bucket Request Trip", name, "status")
        if current != "Dispatched":
            frappe.response["message"] = {
                "status": "error",
                "message": "Trip is " + str(current) + "; only Dispatched trips can be received.",
            }
        else:
            frappe.db.set_value("Bucket Request Trip", name, {
                "status": "Received",
                "received_at": frappe.utils.now(),
            })
            frappe.db.commit()
            frappe.response["message"] = {"status": "success", "name": name, "trip_status": "Received"}


@frappe.whitelist()
def reportAppVersion():
    # reportAppVersion
    # Record one Mobile App Version Log row per authenticated user per day.
    # Sandbox rules:
    #   - no imports
    #   - no return statements (set frappe.response instead)
    #   - no augmented assignment, no in-place slicing
    #   - no hasattr/isinstance, no os/file/exec
    #   - use str() for date conversions

    user = frappe.session.user

    if user == "Guest":
        frappe.throw("Authentication required.")

    data = frappe.form_dict or {}
    app_version = data.get("app_version")
    platform = data.get("platform")
    device_model = data.get("device_model")

    if not app_version:
        frappe.throw("app_version is required.")

    today_str = str(frappe.utils.today())
    now_str = str(frappe.utils.now())

    existing = frappe.db.get_all(
        "Mobile App Version Log",
        filters={
            "user": user,
            "reported_on": [">=", today_str + " 00:00:00"],
        },
        fields=["name"],
        limit_page_length=1,
        order_by="reported_on desc",
    )

    if existing:
        log_name = existing[0]["name"]
        frappe.db.set_value(
            "Mobile App Version Log",
            log_name,
            {
                "app_version": app_version,
                "platform": platform,
                "device_model": device_model,
                "reported_on": now_str,
            },
        )
        action = "updated"
    else:
        doc = frappe.get_doc({
            "doctype": "Mobile App Version Log",
            "user": user,
            "app_version": app_version,
            "platform": platform,
            "device_model": device_model,
            "reported_on": now_str,
        })
        doc.insert(ignore_permissions=True)
        log_name = doc.name
        action = "created"

    frappe.response["message"] = {
        "ok": True,
        "name": log_name,
        "action": action,
        "user": user,
        "app_version": app_version,
    }


@frappe.whitelist()
def setSchedulerOrder():
    # Frappe Server Script (Type: API), api_method = setSchedulerOrder
    # Persists the drag order of the Ready Lines column.
    # Payload: { "order": ["OPL-xxx", "OPL-yyy", ...] }  (top -> bottom)
    # Writes schedule_number = 1..N onto each OPL in that order.
    # Constraints: no return/def/import/+=/.append — use list = list + [x], dict[k]=v

    frappe.response["message"] = {"success": False, "error": "Script failed"}

    try:
        # order arrives as a "|~|"-joined string (safe_exec blocks json/imports)
        order_raw = frappe.form_dict.get("order") or ""
        order = []
        parts = order_raw.split("|~|")
        p = 0
        while p < len(parts):
            v = parts[p].strip()
            if v != "":
                order = order + [v]
            p = p + 1

        if not order:
            frappe.response["message"] = {"success": False, "error": "No order provided"}
        else:
            updated = 0
            i = 0
            while i < len(order):
                oid = order[i]
                if frappe.db.exists("Order Pick List", oid):
                    frappe.db.set_value("Order Pick List", oid, "schedule_number", i + 1)
                    updated = updated + 1
                i = i + 1
            frappe.db.commit()
            frappe.response["message"] = {"success": True, "updated": updated}

    except Exception as e:
        frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def shelveBucket():
    """Shelve a received bucket onto a Shelf (adapted from the v15 shelving server
    script to the v16 schema). A bucket may now carry SEVERAL varieties (multi-item
    receiving), so one Shelf Item is created per received variety. Shelf capacity is
    enforced by counting DISTINCT buckets on the shelf (max 2), not rows.

    Schema notes vs v15: receiving/harvest entries key on `bucket_id`
    (not custom_bucket_id); stem length falls back to the receiving entry's
    `custom_stem_length`; Shelf Item has no `custom_stem_length`; Bucket Reuse
    Anomaly does not exist here (guarded)."""

    data = frappe.request.get_json() or {}
    result = {}

    # ── VALIDATION ───────────────────────────────────────────────────────────
    result["passed"] = True
    result["stale_transfer_shelves"] = []
    shelf_id = data.get("shelf_id")
    bucket_id = data.get("bucket_id")
    farm = data.get("farm")

    def _fail(reason, message):
        result["passed"] = False
        frappe.response["data"] = {"status": "failed", "reason": reason, "message": message,
                                   "payload": {"shelf_id": shelf_id, "bucket_id": bucket_id}}

    if not shelf_id:
        _fail("shelf_id_not_null", "Shelf ID is missing."); return
    if not bucket_id:
        _fail("bucket_id_not_null", "Bucket ID is missing."); return
    if not farm:
        _fail("farm_not_null", "Farm is missing."); return

    # load / create the shelf
    if frappe.db.exists("Shelf", shelf_id):
        shelf_doc = frappe.get_doc("Shelf", shelf_id)
    else:
        shelf_doc = frappe.new_doc("Shelf")
        shelf_doc.name = shelf_id
        shelf_doc.shelf_id = shelf_id
        shelf_doc.insert(ignore_permissions=True)
    result["shelf_doc"] = shelf_doc

    # duplicate on THIS shelf
    for item in (shelf_doc.items or []):
        if (item.bucket_id or "").lower() == bucket_id.lower():
            _fail("duplicate_entry", "The bucket has already been shelved."); return

    # duplicate on ANOTHER shelf (transfer buckets self-heal, otherwise block)
    other_shelves = frappe.get_all("Shelf Item",
        filters={"bucket_id": bucket_id, "parent": ["!=", shelf_id]}, fields=["name", "parent"])
    if other_shelves:
        transfer_rows = frappe.db.sql("""
            SELECT pli.name FROM `tabPick List Item` pli
            JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND opl.docstatus = 0
            WHERE pli.parenttype = 'Order Pick List' AND pli.bucket = %s
              AND (pli.awaiting_transfer = 1 OR pli.in_transit = 1
                   OR pli.loaded_in_trolley = 1) LIMIT 1""", bucket_id, as_dict=True)
        if transfer_rows:
            result["stale_transfer_shelves"] = other_shelves
        else:
            _fail("duplicate_entry", "The bucket has already been shelved on shelf {0}.".format(other_shelves[0].parent)); return

    # capacity — count DISTINCT buckets on the shelf, not rows (a bucket spans
    # several Shelf Item rows when it carries several varieties).
    existing_buckets = {(it.bucket_id or "").lower() for it in (shelf_doc.items or [])}
    existing_buckets.discard(bucket_id.lower())
    if len(existing_buckets) >= 2:
        _fail("two_buckets_per_shelf", "The shelf is full."); return

    # ── FETCH LATEST RECEIVING ───────────────────────────────────────────────
    entries = frappe.get_all("Stock Entry",
        filters={"stock_entry_type": ["in", ["Receiving", "Late Receipt"]],
                 "custom_bucket_id": bucket_id, "docstatus": 1},
        fields=["name"], order_by="creation desc", limit=1)
    if not entries:
        frappe.response["data"] = {"status": "failed", "reason": "not_received",
            "message": "This bucket has no Receiving or Late Receipt entry.",
            "payload": {"bucket_id": bucket_id}}
        return
    receiving_doc = frappe.get_doc("Stock Entry", entries[0].name)

    # ── FRESHNESS GATES (harvest->receiving <=1 day; receiving not stale) ─────
    today_date = frappe.utils.getdate(frappe.utils.today())
    recv_date = frappe.utils.getdate(receiving_doc.posting_date)
    harvest_entry = frappe.get_all("Stock Entry",
        filters={"stock_entry_type": "Harvesting", "custom_bucket_id": bucket_id,
                 "posting_date": recv_date, "docstatus": 1},
        fields=["name", "posting_date"], order_by="creation desc", limit=1) or \
        frappe.get_all("Stock Entry",
        filters={"stock_entry_type": "Harvesting", "custom_bucket_id": bucket_id,
                 "posting_date": frappe.utils.add_days(recv_date, -1), "docstatus": 1},
        fields=["name", "posting_date"], order_by="creation desc", limit=1)
    if not harvest_entry:
        frappe.response["data"] = {"status": "failed", "reason": "no_matching_harvest",
            "message": "No harvesting entry found for bucket {0} within 1 day of receiving date ({1}).".format(bucket_id, recv_date),
            "payload": {"bucket_id": bucket_id, "received_on": str(recv_date)}}
        return
    harvest_date = frappe.utils.getdate(harvest_entry[0].posting_date)
    gap = (recv_date - harvest_date).days
    if gap > 1:
        frappe.response["data"] = {"status": "failed", "reason": "harvest_receiving_gap_too_large",
            "message": "Bucket harvested on {0} but received on {1} ({2} days apart). Maximum allowed gap is 1 day.".format(harvest_date, recv_date, gap),
            "payload": {"bucket_id": bucket_id, "harvested_on": str(harvest_date), "received_on": str(recv_date), "gap_days": gap}}
        return
    origin_farm = receiving_doc.get("farm") or farm
    max_allowed_days = 50 if origin_farm and origin_farm.lower() == "kapkolia" else 40
    days_since = (today_date - recv_date).days
    if days_since > max_allowed_days:
        frappe.response["data"] = {"status": "failed", "reason": "stale_receiving_date",
            "message": "Cannot shelf bucket — received on {0} ({1} days ago). Maximum allowed for {2} is {3} day(s).".format(recv_date, days_since, origin_farm, max_allowed_days),
            "payload": {"bucket_id": bucket_id, "origin_farm": origin_farm, "received_on": str(recv_date), "days_since_receiving": days_since, "max_allowed_days": max_allowed_days}}
        return

    # ── TRANSIT / OPL updates for transfer buckets (local buckets untouched) ──
    _shelve_update_transit_status(bucket_id, shelf_id, result)

    # ── SHELVE: one Shelf Item per received variety ──────────────────────────
    stem_length = receiving_doc.get("custom_stem_length")
    variety = receiving_doc.items[0].item_code if receiving_doc.items else None
    origin_greenhouse = receiving_doc.items[0].s_warehouse if receiving_doc.items else None
    shelf_doc.farm = farm
    total_qty = 0
    for ri in receiving_doc.items:
        new_item = shelf_doc.append("items", {})
        new_item.bucket_id = bucket_id
        new_item.variety = ri.item_code
        new_item.date_added = frappe.utils.now_datetime()
        new_item.stem_length = stem_length
        new_item.stem_qty = ri.qty
        new_item.greenhouse = ri.s_warehouse
        new_item.warehouse = ri.t_warehouse
        new_item.farm = farm
        new_item.harvest_date = harvest_date
        new_item.receiving_date = recv_date
        total_qty += (ri.qty or 0)
    shelf_doc.save(ignore_permissions=True)

    # skipped-transfer self-heal (remove stale remote Shelf Items; anomaly guarded)
    for shi in (result.get("stale_transfer_shelves") or []):
        try:
            frappe.delete_doc("Shelf Item", shi.get("name"), force=1, ignore_permissions=True)
            frappe.db.set_value("Shelf", shi.get("parent"), "modified", frappe.utils.now())
        except Exception:
            pass

    # clear BAS transit flags so the balance becomes allocatable
    _shelve_update_bas(bucket_id, variety, farm, shelf_id, result)
    _shelve_check_submit_opl(bucket_id, result)

    frappe.db.commit()
    frappe.response["data"] = {"status": "success",
        "message": "Bucket {0} shelved successfully with {1} stems.".format(bucket_id, total_qty),
        "payload": {"shelf_id": shelf_id, "bucket_id": bucket_id, "stems": total_qty,
                    "stem_length": stem_length, "transit_updated": result.get("transit_updated", False),
                    "bas_updated": result.get("bas_updated", False),
                    "opl_submitted": result.get("opl_submitted", [])}}


def _shelve_update_transit_status(bucket_id, shelf_id, result):
    result["transit_updated"] = False
    rows = frappe.get_all("Pick List Item", filters={"bucket": bucket_id}, fields=["name", "parent"])
    updated = []
    for r in rows:
        opl = frappe.get_doc("Order Pick List", r.parent)
        if opl.docstatus != 0:
            continue
        changed = False
        for row in opl.locations:
            if row.name == r.name:
                if (row.in_transit or 0) == 1 or (row.awaiting_transfer or 0) == 1 or (row.loaded_in_trolley or 0) == 1:
                    row.in_transit = 0; row.awaiting_transfer = 0
                    row.loaded_in_trolley = 0; row.shelved = 1; row.shelf = shelf_id
                    changed = True
                break
        if changed:
            opl.save(ignore_permissions=True); updated.append(r.parent)
    if updated:
        result["transit_updated"] = True; result["transit_opl"] = updated[0]


def _shelve_update_bas(bucket_id, variety, farm, shelf_id, result):
    result["bas_updated"] = False
    try:
        bas_name = frappe.db.get_value("Bucket Allocation Status", {"bucket_id": bucket_id, "item_code": variety}, "name")
        if bas_name:
            bas = frappe.get_doc("Bucket Allocation Status", bas_name)
            if bas.in_transit == 1:
                bas.in_transit = 0; bas.shelf_farm = farm; bas.shelf_location = shelf_id
                bas.available_quantity = (bas.total_quantity or 0) - (bas.allocated_quantity or 0)
                bas.save(ignore_permissions=True)
                result["bas_updated"] = True; result["bas_available_qty"] = bas.available_quantity
    except Exception:
        frappe.log_error("BAS Transit Update Failed", frappe.get_traceback())


def _shelve_check_submit_opl(bucket_id, result):
    result["opl_submitted"] = []
    try:
        for row in frappe.db.sql("SELECT DISTINCT parent FROM `tabPick List Item` WHERE bucket = %s", bucket_id, as_dict=True):
            opl = frappe.get_doc("Order Pick List", row.parent)
            if opl.docstatus == 1:
                continue
            all_ready = True
            for loc in opl.locations:
                is_transfer = (loc.in_transit == 1 or loc.awaiting_transfer == 1 or (loc.loaded_in_trolley or 0) == 1)
                if is_transfer and (loc.shelved or 0) != 1:
                    all_ready = False; break
            if all_ready:
                opl.flags.ignore_permissions = True; opl.submit(); result["opl_submitted"].append(row.parent)
    except Exception:
        frappe.log_error("OPL Auto-Submit Check Failed", frappe.get_traceback())


# ============================================================
# BUCKET DISPATCH — empty buckets sent from the packhouse/coldroom to a farm
# ahead of harvest, scanned onto a truck, then scanned again as "received" at
# the farm on arrival. The desktop reconciliation dashboard compares, per farm
# per day: how many were dispatched, how many were actually used in that
# farm's Harvesting Stock Entries, and how many were confirmed received at the
# farm — the gaps are the operational signal (buckets lost/short in transit,
# or sent but not used).
# ============================================================
@frappe.whitelist()
def getInternalLogisticsTrucks():
    # Vehicles used for internal farm/packhouse logistics — same fleet the
    # existing Bucket Requests "Load to truck" picker uses (Vehicle whose
    # "Dispatch Truck?" flag is UNCHECKED; that flag marks the CUSTOMER
    # delivery fleet, so unchecked == internal logistics).
    try:
        rows = frappe.get_all(
            "Vehicle",
            filters={"custom_dispatch_truck": 0},
            fields=["name", "license_plate"],
            order_by="name asc",
            limit_page_length=0,
        )
        frappe.response["message"] = {
            "status": "success",
            "trucks": [{"name": r.name, "license_plate": r.license_plate or ""} for r in rows],
        }
    except Exception as e:
        frappe.log_error("getInternalLogisticsTrucks failed", frappe.get_traceback())
        frappe.response["message"] = {"status": "error", "message": str(e), "trucks": []}


@frappe.whitelist(methods=["POST"])
def createBucketDispatch():
    # Scan a batch of empty buckets onto a truck bound for a farm. One call at
    # the end of the scan (mirrors createHarvestStockEntry/submitBatchQuality)
    # rather than one round-trip per scan.
    try:
        payload = frappe.request.get_json() or frappe.form_dict
        target_farm = payload.get("target_farm")
        vehicle = payload.get("vehicle")
        bucket_ids = payload.get("bucket_ids") or []
        remarks = payload.get("remarks") or ""

        if isinstance(bucket_ids, str):
            bucket_ids = json.loads(bucket_ids) if bucket_ids else []

        if not target_farm:
            frappe.response["status"] = "error"
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": _("Target farm is required")}
            return
        if not vehicle:
            frappe.response["status"] = "error"
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": _("Vehicle is required")}
            return
        if not bucket_ids:
            frappe.response["status"] = "error"
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": _("Scan at least one bucket")}
            return
        if not frappe.db.exists("Farm", target_farm):
            frappe.response["status"] = "error"
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": _("Unknown farm: {0}").format(target_farm)}
            return
        if not frappe.db.exists("Vehicle", vehicle):
            frappe.response["status"] = "error"
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": _("Unknown vehicle: {0}").format(vehicle)}
            return

        # Soft check only — some fleets may not have the flag maintained, so this
        # warns via the response rather than blocking the dispatch outright.
        is_internal = frappe.db.get_value("Vehicle", vehicle, "custom_dispatch_truck")
        warning = ""
        if is_internal:
            warning = _("{0} is flagged as a customer-delivery truck, not internal logistics.").format(vehicle)

        now = frappe.utils.now()
        seen = set()
        rows = []
        for b in bucket_ids:
            bid = str(b).strip()
            if not bid or bid in seen:
                continue
            seen.add(bid)
            rows.append({"bucket_id": bid, "scanned_time": now})

        doc = frappe.get_doc({
            "doctype": "Bucket Dispatch",
            "target_farm": target_farm,
            "vehicle": vehicle,
            "dispatched_by": frappe.session.user,
            "dispatch_datetime": now,
            "remarks": remarks,
            "buckets": rows,
        })
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.response["message"] = {
            "status": "success",
            "message": _("{0} buckets dispatched to {1}").format(len(rows), target_farm),
            "name": doc.name,
            "total_dispatched": doc.total_dispatched,
            "warning": warning,
        }
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error("createBucketDispatch failed", frappe.get_traceback())
        frappe.response["status"] = "error"
        frappe.response["http_status_code"] = 500
        frappe.response["message"] = {"status": "error", "message": str(e)}


@frappe.whitelist()
def getPendingBucketDispatchesForFarm():
    # For the farm-side receiving screen: dispatches targeting this farm that
    # aren't fully received yet, each with its still-outstanding buckets.
    try:
        farm = frappe.form_dict.get("farm")
        if not farm:
            frappe.response["message"] = {"status": "error", "message": _("Farm is required"), "dispatches": []}
            return

        names = frappe.get_all(
            "Bucket Dispatch",
            filters={"target_farm": farm, "status": ["!=", "Fully Received"]},
            fields=["name"],
            order_by="dispatch_datetime desc",
            limit_page_length=0,
        )

        dispatches = []
        for row in names:
            doc = frappe.get_doc("Bucket Dispatch", row.name)
            dispatches.append({
                "name": doc.name,
                "target_farm": doc.target_farm,
                "vehicle": doc.vehicle,
                "dispatch_datetime": str(doc.dispatch_datetime or ""),
                "status": doc.status,
                "total_dispatched": doc.total_dispatched,
                "total_received": doc.total_received,
                "buckets": [
                    {"bucket_id": b.bucket_id, "received": int(b.received or 0)}
                    for b in doc.buckets
                ],
            })

        frappe.response["message"] = {"status": "success", "dispatches": dispatches}
    except Exception as e:
        frappe.log_error("getPendingBucketDispatchesForFarm failed", frappe.get_traceback())
        frappe.response["message"] = {"status": "error", "message": str(e), "dispatches": []}


@frappe.whitelist(methods=["POST"])
def receiveBucketDispatch():
    # A farm operator scans buckets in as they come off the truck. bucket_ids
    # not resolvable to any pending dispatch for this farm are reported back
    # as "unmatched" rather than silently ignored — that mismatch is exactly
    # the kind of thing the reconciliation dashboard needs surfaced early.
    try:
        payload = frappe.request.get_json() or frappe.form_dict
        farm = payload.get("farm")
        bucket_ids = payload.get("bucket_ids") or []
        dispatch_name = payload.get("dispatch_name")

        if isinstance(bucket_ids, str):
            bucket_ids = json.loads(bucket_ids) if bucket_ids else []

        if not farm:
            frappe.response["status"] = "error"
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": _("Farm is required")}
            return
        if not bucket_ids:
            frappe.response["status"] = "error"
            frappe.response["http_status_code"] = 400
            frappe.response["message"] = {"status": "error", "message": _("Scan at least one bucket")}
            return

        filters = {"target_farm": farm, "status": ["!=", "Fully Received"]}
        if dispatch_name:
            filters = {"name": dispatch_name}

        open_names = frappe.get_all("Bucket Dispatch", filters=filters, fields=["name"], order_by="dispatch_datetime asc")

        remaining = set(str(b).strip() for b in bucket_ids if str(b).strip())
        received_now = []
        now = frappe.utils.now()

        for row in open_names:
            if not remaining:
                break
            doc = frappe.get_doc("Bucket Dispatch", row.name)
            changed = False
            for b in doc.buckets:
                if b.bucket_id in remaining and not b.received:
                    b.received = 1
                    b.received_time = now
                    b.received_by = frappe.session.user
                    remaining.discard(b.bucket_id)
                    received_now.append({"bucket_id": b.bucket_id, "dispatch": doc.name})
                    changed = True
            if changed:
                doc.save(ignore_permissions=True)

        frappe.db.commit()

        frappe.response["message"] = {
            "status": "success",
            "message": _("{0} bucket(s) received").format(len(received_now)),
            "received": received_now,
            "unmatched": sorted(remaining),
        }
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error("receiveBucketDispatch failed", frappe.get_traceback())
        frappe.response["status"] = "error"
        frappe.response["http_status_code"] = 500
        frappe.response["message"] = {"status": "error", "message": str(e)}


@frappe.whitelist()
def getBucketReconciliation():
    # Dashboard feed: per farm, for one day — dispatched (empty buckets sent
    # out), harvested (distinct buckets used on that farm's submitted
    # Harvesting Stock Entries), received (buckets scanned back in at the
    # farm). All three are independent counts; the gaps between them are the
    # point of the report, not an error condition.
    try:
        date = frappe.form_dict.get("date") or frappe.utils.today()
        farm = frappe.form_dict.get("farm")

        farms = [farm] if farm else [f.name for f in frappe.get_all("Farm", fields=["name"])]
        if not farms:
            frappe.response["message"] = {"status": "success", "date": str(date), "farms": []}
            return

        farm_ph = ", ".join(["%s"] * len(farms))

        dispatched_rows = frappe.db.sql(f"""
            SELECT bd.target_farm AS farm, COUNT(bdi.name) AS cnt
            FROM `tabBucket Dispatch` bd
            INNER JOIN `tabBucket Dispatch Item` bdi ON bdi.parent = bd.name
            WHERE bd.target_farm IN ({farm_ph}) AND DATE(bd.dispatch_datetime) = %s
            GROUP BY bd.target_farm
        """, farms + [date], as_dict=True)
        dispatched_map = {r.farm: r.cnt for r in dispatched_rows}

        received_rows = frappe.db.sql(f"""
            SELECT bd.target_farm AS farm, COUNT(bdi.name) AS cnt
            FROM `tabBucket Dispatch` bd
            INNER JOIN `tabBucket Dispatch Item` bdi ON bdi.parent = bd.name
            WHERE bd.target_farm IN ({farm_ph}) AND bdi.received = 1
              AND DATE(bdi.received_time) = %s
            GROUP BY bd.target_farm
        """, farms + [date], as_dict=True)
        received_map = {r.farm: r.cnt for r in received_rows}

        harvested_rows = frappe.db.sql(f"""
            SELECT farm, COUNT(DISTINCT custom_bucket_id) AS cnt
            FROM `tabStock Entry`
            WHERE stock_entry_type = 'Harvesting' AND docstatus = 1
              AND farm IN ({farm_ph}) AND posting_date = %s
              AND COALESCE(custom_bucket_id, '') != ''
            GROUP BY farm
        """, farms + [date], as_dict=True)
        harvested_map = {r.farm: r.cnt for r in harvested_rows}

        out = []
        for f in sorted(farms):
            dispatched = dispatched_map.get(f, 0)
            harvested = harvested_map.get(f, 0)
            received = received_map.get(f, 0)
            if not (dispatched or harvested or received):
                continue
            out.append({
                "farm": f,
                "dispatched": dispatched,
                "harvested": harvested,
                "received": received,
                "balance_dispatched_vs_received": dispatched - received,
                "balance_dispatched_vs_harvested": dispatched - harvested,
            })

        frappe.response["message"] = {"status": "success", "date": str(date), "farms": out}
    except Exception as e:
        frappe.log_error("getBucketReconciliation failed", frappe.get_traceback())
        frappe.response["message"] = {"status": "error", "message": str(e), "farms": []}
