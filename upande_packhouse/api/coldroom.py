# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `cold-room` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def fetchColdroomData():
    # Cold Room Dashboard Data
    # API: fetchColdroomData

    farm_filter = frappe.form_dict.get('farm', '')

    try:
        farm_where = ""
        if farm_filter:
            farm_where = "AND s.farm = %(farm_filter)s"
        query_params = {'farm_filter': farm_filter} if farm_filter else {}

        se_farm_where = ""
        if farm_filter:
            se_farm_where = "AND se.custom_farm = %(farm_filter)s"

        opl_farm_where = ""
        if farm_filter:
            opl_farm_where = "AND opl.farm = %(farm_filter)s"

        # 1. Shelf stock (aggregated by variety/length/farm)
        stock_data = frappe.db.sql("""
            SELECT
                si.variety AS item_code,
                si.stem_length,
                s.farm AS farm_name,
                SUM(si.stem_qty) AS available_stems,
                COUNT(DISTINCT si.bucket_id) AS bucket_count,
                GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR ', ') AS shelf_names
            FROM `tabShelf` s
            INNER JOIN `tabShelf Item` si ON s.name = si.parent
            INNER JOIN `tabFarm` f ON s.farm = f.name
            WHERE si.bucket_id IS NOT NULL AND si.bucket_id != ''
                AND si.stem_qty > 0
                AND f.company = 'Karen Roses'
                """ + farm_where + """
            GROUP BY si.variety, si.stem_length, s.farm
            ORDER BY si.variety, si.stem_length, s.farm
        """, query_params, as_dict=True)

        # 1b. Received but NOT shelved (in coldroom, awaiting shelving):
        #     intake received within the last 3 days, bucket not currently on any shelf,
        #     and not discarded. Same shape as stock_data so the UI can show it as a
        #     second "Not Shelved" column alongside the shelved figure.
        not_shelved_data = frappe.db.sql("""
            SELECT
                sed.item_code AS item_code,
                se.custom_stem_length AS stem_length,
                se.custom_farm AS farm_name,
                SUM(sed.qty) AS available_stems,
                COUNT(DISTINCT se.custom_bucket_id) AS bucket_count
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            INNER JOIN `tabFarm` f ON se.custom_farm = f.name
            WHERE se.docstatus = 1
                AND se.stock_entry_type = 'Harvesting'
                AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
                AND f.company = 'Karen Roses'
                AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id != ''
                """ + se_farm_where + """
                AND NOT EXISTS (
                    SELECT 1 FROM `tabShelf Item` si2
                    WHERE si2.bucket_id = se.custom_bucket_id AND si2.stem_qty > 0
                )
                AND NOT EXISTS (
                    SELECT 1 FROM `tabStock Entry` d
                    WHERE d.docstatus = 1 AND d.stock_entry_type = 'Discard'
                        AND d.custom_bucket_id = se.custom_bucket_id
                        AND d.posting_date >= se.posting_date
                )
                AND NOT EXISTS (
                    SELECT 1 FROM `tabPick List Item` pli
                    WHERE pli.bucket = se.custom_bucket_id AND pli.issued = 1
                )
            GROUP BY sed.item_code, se.custom_stem_length, se.custom_farm
            ORDER BY sed.item_code, se.custom_stem_length, se.custom_farm
        """, query_params, as_dict=True)

        total_not_shelved_stems = 0
        total_not_shelved_buckets = 0
        for r in not_shelved_data:
            total_not_shelved_stems = total_not_shelved_stems + (r.get('available_stems', 0) or 0)
            total_not_shelved_buckets = total_not_shelved_buckets + (r.get('bucket_count', 0) or 0)

        # 2. All bucket_ids currently on shelves + stems
        shelf_buckets = frappe.db.sql("""
            SELECT si.bucket_id, si.stem_qty
            FROM `tabShelf` s
            INNER JOIN `tabShelf Item` si ON s.name = si.parent
            INNER JOIN `tabFarm` f ON s.farm = f.name
            WHERE si.bucket_id IS NOT NULL AND si.bucket_id != ''
                AND si.stem_qty > 0
                AND f.company = 'Karen Roses'
                """ + farm_where + """
        """, query_params, as_dict=True)

        shelf_bucket_set = set()
        shelf_bucket_stems = {}
        for row in shelf_buckets:
            bid = row.get('bucket_id')
            if bid:
                shelf_bucket_set.add(bid)
                shelf_bucket_stems[bid] = row.get('stem_qty', 0) or 0

        shelf_bucket_ids = list(shelf_bucket_set)

        # 3. Age: latest harvest date per bucket using SQL IN
        age_buckets = {}
        total_age = 0
        age_count = 0
        oldest_age = 0

        if shelf_bucket_ids:
            bucket_list = []
            for bid in shelf_bucket_ids:
                bucket_list.append("'" + bid.replace("'", "''") + "'")
            in_clause = ",".join(bucket_list)

            harvest_ages = frappe.db.sql("""
                SELECT
                    h.custom_bucket_id AS bucket_id,
                    DATEDIFF(CURDATE(), MAX(h.posting_date)) AS age_days
                FROM `tabStock Entry` h
                WHERE h.docstatus = 1
                    AND h.stock_entry_type = 'Harvesting'
                    AND h.custom_bucket_id IN (""" + in_clause + """)
                GROUP BY h.custom_bucket_id
            """, as_dict=True)

            for row in harvest_ages:
                bid = row.get('bucket_id')
                days = row.get('age_days') or 0
                if days < 0:
                    days = 0
                stems = shelf_bucket_stems.get(bid, 0)
                total_age = total_age + days
                age_count = age_count + 1
                if days > oldest_age:
                    oldest_age = days
                if days == 0:
                    label = 'Today'
                elif days == 1:
                    label = '1 day'
                else:
                    label = frappe.utils.cstr(days) + ' days'
                age_buckets[label] = age_buckets.get(label, 0) + stems

        avg_age = round(total_age / age_count, 1) if age_count > 0 else 0

        age_order = []
        for key in age_buckets:
            if key == 'Today':
                age_order.append((0, key, age_buckets[key]))
            elif key == '1 day':
                age_order.append((1, key, age_buckets[key]))
            else:
                parts = key.split(' ')
                num = 0
                try:
                    num = int(parts[0])
                except Exception:
                    num = 999
                age_order.append((num, key, age_buckets[key]))
        age_order.sort(key=lambda x: x[0])

        age_dist_labels = []
        age_dist_values = []
        for item in age_order:
            age_dist_labels.append(item[1])
            age_dist_values.append(item[2])

        # 4. Incoming: received TODAY, subtract shelved
        received_today = frappe.db.sql("""
            SELECT
                se.custom_bucket_id AS bucket_id,
                se.custom_farm AS farm_name,
                SUM(sed.qty) AS stems
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1
                AND se.stock_entry_type = 'Receiving'
                AND se.posting_date = CURDATE()
                AND se.company = 'Karen Roses'
                AND se.custom_bucket_id IS NOT NULL
                AND se.custom_bucket_id != ''
                """ + se_farm_where + """
            GROUP BY se.custom_bucket_id, se.custom_farm
        """, query_params, as_dict=True)

        incoming_stems_map = {}
        total_incoming_stems = 0
        total_incoming_buckets = 0
        total_received_today_stems = 0
        total_received_today_buckets = 0

        for r in received_today:
            bid = r.get('bucket_id')
            fn = r.get('farm_name', 'Unknown')
            s = r.get('stems', 0) or 0
            total_received_today_stems = total_received_today_stems + s
            total_received_today_buckets = total_received_today_buckets + 1
            if bid not in shelf_bucket_set:
                incoming_stems_map[fn] = incoming_stems_map.get(fn, 0) + s
                total_incoming_stems = total_incoming_stems + s
                total_incoming_buckets = total_incoming_buckets + 1

        # 5. Allocated TODAY but not issued (Order Pick List)
        alloc_data = frappe.db.sql("""
            SELECT
                SUM(pli.stock_qty) AS allocated_stems,
                COUNT(*) AS allocated_buckets
            FROM `tabOrder Pick List` opl
            INNER JOIN `tabPick List Item` pli ON pli.parent = opl.name
            WHERE opl.docstatus = 1
                AND opl.date_created = CURDATE()
                AND pli.issued = 0
                """ + opl_farm_where + """
        """, query_params, as_dict=True)

        total_allocated_stems = 0
        total_allocated_buckets = 0
        if alloc_data and alloc_data[0]:
            total_allocated_stems = alloc_data[0].get('allocated_stems', 0) or 0
            total_allocated_buckets = alloc_data[0].get('allocated_buckets', 0) or 0

        # 6. Discards TODAY per farm
        discard_data = frappe.db.sql("""
            SELECT
                SUM(sed.qty) AS discard_stems,
                COUNT(DISTINCT se.name) AS discard_entries
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1
                AND se.stock_entry_type = 'Discard'
                AND se.posting_date = CURDATE()
                AND se.company = 'Karen Roses'
                """ + se_farm_where + """
        """, query_params, as_dict=True)

        total_discard_stems = 0
        total_discard_entries = 0
        if discard_data and discard_data[0]:
            total_discard_stems = discard_data[0].get('discard_stems', 0) or 0
            total_discard_entries = discard_data[0].get('discard_entries', 0) or 0

        # 7. Cold store capacity
        capacity_data = []
        try:
            capacity_data = frappe.db.sql("""
                SELECT csc.farm, csc.cold_store_warehouse, csc.sensor_name,
                    csc.max_shelves, csc.max_buckets, csc.max_stems,
                    csc.temp_min, csc.temp_max, csc.humidity_min, csc.humidity_max
                FROM `tabCold Store Capacity` csc
                ORDER BY csc.farm
            """, as_dict=True)
        except Exception:
            capacity_data = []

        # 8. Production settings
        settings = {'amber_time': 24, 'discard_age': 48}
        try:
            settings_rows = frappe.db.sql("SELECT amber_time, discard_age FROM `tabProduction Settings` LIMIT 1", as_dict=True)
            if settings_rows:
                settings = settings_rows[0]
        except Exception:
            pass

        # 9. ALL Karen Roses farms
        available_farms = frappe.db.sql("""
            SELECT name, company, abbreviation, '' AS custom_location
            FROM `tabFarm` WHERE company = 'Karen Roses'
            ORDER BY name
        """, as_dict=True)

        # --- Stock aggregations ---
        total_stems = 0
        total_buckets = 0
        variety_set = set()
        shelf_set = set()
        variety_stems = {}
        length_stems = {}
        farm_stems = {}
        farm_buckets = {}

        for r in stock_data:
            avail = r.get('available_stems', 0) or 0
            bkts = r.get('bucket_count', 0) or 0
            total_stems = total_stems + avail
            total_buckets = total_buckets + bkts
            ic = r.get('item_code', '')
            if ic:
                variety_set.add(ic)
            names = r.get('shelf_names', '') or ''
            for n in names.split(', '):
                stripped = n.strip()
                if stripped:
                    shelf_set.add(stripped)
            v = r.get('item_code', 'Unknown')
            variety_stems[v] = variety_stems.get(v, 0) + avail
            sl = r.get('stem_length', 'Unknown') or 'Unknown'
            length_stems[sl] = length_stems.get(sl, 0) + avail
            fn = r.get('farm_name', 'Unknown')
            farm_stems[fn] = farm_stems.get(fn, 0) + avail
            farm_buckets[fn] = farm_buckets.get(fn, 0) + bkts

        frappe.response['message'] = {
            'success': True,
            'stock_data': stock_data,
            'not_shelved_data': not_shelved_data,
            'capacity_data': capacity_data,
            'available_farms': available_farms,
            'settings': settings,
            'aggregations': {
                'total_stems': total_stems,
                'total_buckets': total_buckets,
                'total_varieties': len(variety_set),
                'total_shelves': len(shelf_set),
                'avg_age_days': avg_age,
                'oldest_age_days': oldest_age,
                'age_dist_labels': age_dist_labels,
                'age_dist_values': age_dist_values,
                'variety_stems': variety_stems,
                'length_stems': length_stems,
                'farm_stems': farm_stems,
                'farm_buckets': farm_buckets,
                'incoming_stems': incoming_stems_map,
                'total_incoming_stems': total_incoming_stems,
                'total_incoming_buckets': total_incoming_buckets,
                'total_received_today_stems': total_received_today_stems,
                'total_received_today_buckets': total_received_today_buckets,
                'total_allocated_stems': total_allocated_stems,
                'total_allocated_buckets': total_allocated_buckets,
                'total_discard_stems': total_discard_stems,
                'total_discard_entries': total_discard_entries,
                'total_not_shelved_stems': total_not_shelved_stems,
                'total_not_shelved_buckets': total_not_shelved_buckets
            }
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback())
        frappe.response['message'] = {
            'success': False,
            'error': 'Server error - check Error Log'
        }


@frappe.whitelist()
def getColdroomBuckets():
    # Cold Room — bucket-level lists for the "Buckets" tab
    # API: getColdroomBuckets  (arg: farm)
    # Sections: age per bucket (on shelf) | discard-list but still shelved |
    #           received-not-shelved (per farm, today) | requested-not-issued (today)
    farm = frappe.form_dict.get('farm', '')
    try:
        fw_shelf = " AND s.farm = %(farm)s" if farm else ""
        fw_se = " AND se.custom_farm = %(farm)s" if farm else ""
        fw_opl = " AND opl.farm = %(farm)s" if farm else ""
        fw_dr = " AND dr.farm = %(farm)s" if farm else ""
        p = {'farm': farm} if farm else {}

        # On-shelf buckets
        shelf_rows = frappe.db.sql("""
            SELECT si.bucket_id AS bucket_id, si.variety AS variety, si.stem_length AS stem_length,
                   s.farm AS farm, s.name AS shelf, si.stem_qty AS stem_qty
            FROM `tabShelf` s
            INNER JOIN `tabShelf Item` si ON si.parent = s.name
            INNER JOIN `tabFarm` f ON s.farm = f.name
            WHERE si.bucket_id IS NOT NULL AND si.bucket_id != '' AND si.stem_qty > 0
              AND f.company = 'Karen Roses'""" + fw_shelf + """
        """, p, as_dict=True)

        shelf_set = set()
        for r in shelf_rows:
            if r.get('bucket_id'):
                shelf_set.add(r['bucket_id'])

        # Age per bucket = today - latest Harvesting date (matches the dashboard avg/oldest)
        age_map = {}
        ids = list(shelf_set)
        if ids:
            parts = []
            for b in ids:
                parts.append("'" + b.replace("'", "''") + "'")
            inc = ",".join(parts)
            arows = frappe.db.sql("""
                SELECT h.custom_bucket_id AS bucket_id, DATEDIFF(CURDATE(), MAX(h.posting_date)) AS age_days
                FROM `tabStock Entry` h
                WHERE h.docstatus = 1 AND h.stock_entry_type = 'Harvesting'
                  AND h.custom_bucket_id IN (""" + inc + """)
                GROUP BY h.custom_bucket_id
            """, as_dict=True)
            for r in arows:
                d = r.get('age_days') or 0
                if d < 0:
                    d = 0
                age_map[r['bucket_id']] = d

        age = []
        for r in shelf_rows:
            bid = r['bucket_id']
            age.append({'bucket_id': bid, 'variety': r.get('variety'), 'stem_length': r.get('stem_length'),
                        'farm': r.get('farm'), 'shelf': r.get('shelf'), 'stem_qty': r.get('stem_qty') or 0,
                        'age_days': age_map.get(bid)})
        age.sort(key=lambda x: (999999 if x['age_days'] is None else x['age_days'], x.get('variety') or ''))

        # In a discard request, not yet discarded, but still has a live shelf item
        # Today's discard requests (the active list), deduped per bucket, that a live
        # shelf item still exists for. Scoped to today so reused bucket codes from old
        # unactioned requests don't inflate the list.
        disc = frappe.db.sql("""
            SELECT drb.bucket_id AS bucket_id, MAX(drb.variety) AS variety, MAX(drb.stem_length) AS stem_length,
                   MAX(drb.farm) AS farm, MAX(drb.shelf) AS shelf, MAX(drb.stem_qty) AS stem_qty,
                   MAX(drb.age_days) AS age_days, MIN(drb.parent) AS request
            FROM `tabDiscard Request Bucket` drb
            INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent AND dr.docstatus = 1
            WHERE IFNULL(drb.discarded, 0) = 0
              AND dr.creation >= CURDATE()
              AND EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = drb.bucket_id AND si.stem_qty > 0)""" + fw_dr + """
            GROUP BY drb.bucket_id
            ORDER BY age_days DESC
        """, p, as_dict=True)

        # Received today, not on any shelf (per farm)
        recv = frappe.db.sql("""
            SELECT se.custom_bucket_id AS bucket_id, se.custom_farm AS farm,
                   sed.item_code AS variety, se.custom_stem_length AS stem_length,
                   SUM(sed.qty) AS stems
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1 AND se.stock_entry_type = 'Receiving' AND se.posting_date = CURDATE()
              AND se.company = 'Karen Roses'
              AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id != ''""" + fw_se + """
            GROUP BY se.custom_bucket_id, se.custom_farm, sed.item_code, se.custom_stem_length
        """, p, as_dict=True)
        recv_out = []
        for r in recv:
            if r.get('bucket_id') in shelf_set:
                continue
            recv_out.append({'bucket_id': r['bucket_id'], 'farm': r.get('farm'), 'variety': r.get('variety'),
                             'stem_length': r.get('stem_length'), 'stems': r.get('stems') or 0})

        # Requested (allocated) today but not issued
        req = frappe.db.sql("""
            SELECT pli.bucket AS bucket_id, pli.item_code AS variety, pli.stem_length AS stem_length,
                   opl.farm AS farm, pli.stock_qty AS stems, opl.name AS opl, opl.customer AS customer
            FROM `tabOrder Pick List` opl
            INNER JOIN `tabPick List Item` pli ON pli.parent = opl.name
            WHERE opl.docstatus = 1 AND opl.date_created = CURDATE() AND IFNULL(pli.issued, 0) = 0""" + fw_opl + """
            ORDER BY opl.farm, pli.item_code
        """, p, as_dict=True)

        frappe.response['message'] = {
            'success': True,
            'age': age,
            'discard_shelved': disc,
            'received_not_shelved': recv_out,
            'requested_not_issued': req,
            'summary': {
                'age_buckets': len(age),
                'discard_shelved': len(disc),
                'received_not_shelved': len(recv_out),
                'requested_not_issued': len(req)
            }
        }
    except Exception as e:
        frappe.log_error(frappe.get_traceback())
        frappe.response['message'] = {'success': False, 'error': str(e)}
