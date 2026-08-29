# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `stem-movement` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def getBucketTrace():
    # Bucket Trace (JSON API for the Stem Movement dashboard "Trace" tab)
    # API: getBucketTrace  — args: box (Box Label id)
    # Returns box details + origin greenhouses (from Harvest) + per-bucket journey
    # (Harvested -> Received[QC] -> Graded -> Shelved -> Packed -> Discarded).
    try:
        box_name = frappe.utils.cstr(frappe.form_dict.get('box') or '').strip()
        if not box_name:
            frappe.response['message'] = {'success': False, 'error': 'No box label provided'}
        else:
            try:
                box = frappe.get_doc('Box Label', box_name)
            except Exception:
                box = None

            if not box:
                frappe.response['message'] = {'success': False, 'error': 'Box Label not found: ' + box_name}
            else:
                out = {'success': True, 'box': None, 'box_items': [], 'greenhouses': [], 'journey': []}
                out['box'] = {
                    'name': box.name, 'farm': box.get('farm'), 'customer': box.get('customer'),
                    'date': str(box.get('date') or ''), 'box_number': box.get('box_number'),
                    'box_total_count': box.get('box_total_count'),
                    'delivery_point': box.get('delivery_point'),
                    'po': box.get('customer_purchase_order'), 'pack_rate': box.get('pack_rate')
                }
                for it in (box.get('box_item') or []):
                    out['box_items'].append({'variety': it.get('variety'), 'length': it.get('length'), 'qty': it.get('qty')})

                opl_name = box.get('order_pick_list')

                # Attribution: team + who packed + who dispatched
                opl_farm = ''
                out['box']['team'] = ''
                out['box']['consignee'] = box.get('consignee') or ''
                out['box']['packed_by'] = box.owner
                out['box']['dispatched_by'] = ''
                out['box']['driver'] = ''
                out['box']['dispatch_time'] = ''
                if opl_name:
                    ov = frappe.get_all('Order Pick List', filters={'name': opl_name},
                        fields=['team', 'consignee', 'farm'], limit=1)
                    if ov:
                        out['box']['team'] = ov[0].get('team') or ''
                        out['box']['consignee'] = ov[0].get('consignee') or out['box']['consignee']
                        opl_farm = ov[0].get('farm') or ''
                # Dispatch is recorded at farm+date level (not per box), so match on that.
                try:
                    if opl_farm and box.get('date'):
                        df = frappe.get_all('Dispatch Form',
                            filters={'custom_farm': opl_farm, 'custom_date': str(box.get('date'))},
                            fields=['owner', 'custom_truck_drivers_name', 'custom_dispatch_time'],
                            order_by='creation desc', limit=1)
                        if df:
                            out['box']['dispatched_by'] = df[0].get('owner') or ''
                            out['box']['driver'] = df[0].get('custom_truck_drivers_name') or ''
                            dt = df[0].get('custom_dispatch_time')
                            out['box']['dispatch_time'] = str(dt)[:8] if dt else ''
                except Exception:
                    pass

                # Origin greenhouses (from Harvest by farm)
                if box.get('farm'):
                    harvests = frappe.db.sql("""
                        SELECT se.custom_bucket_id AS bucket_id, se.custom_greenhouse AS block,
                               sed.item_code AS item_code, se.custom_stem_length AS stem_length,
                               sed.qty AS quantity
                        FROM `tabStock Entry` se
                        INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
                        WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus = 1
                          AND se.farm = %(farm)s
                        ORDER BY se.creation DESC LIMIT 500
                    """, {'farm': box.get('farm')}, as_dict=True)
                    gh_map = {}
                    for h in harvests:
                        gh = h.get('block')
                        if not gh:
                            continue
                        if gh not in gh_map:
                            gh_map[gh] = {'name': gh, 'total_stems': 0, 'varieties': {}, 'buckets': []}
                        g = gh_map[gh]
                        vk = str(h.get('item_code', '')) + '__' + str(h.get('stem_length', ''))
                        if vk not in g['varieties']:
                            g['varieties'][vk] = {'variety': h.get('item_code', ''), 'length': h.get('stem_length', ''), 'qty': 0}
                        g['varieties'][vk]['qty'] = g['varieties'][vk]['qty'] + (h.get('quantity') or 0)
                        g['total_stems'] = g['total_stems'] + (h.get('quantity') or 0)
                        bid = h.get('bucket_id')
                        if bid and bid not in g['buckets']:
                            g['buckets'].append(bid)
                    res = []
                    for gn in sorted(gh_map.keys()):
                        gd = gh_map[gn]
                        vlist = sorted(gd['varieties'].values(), key=lambda x: -x['qty'])
                        res.append({'name': gd['name'], 'total_stems': gd['total_stems'], 'bucket_count': len(gd['buckets']), 'varieties': vlist})
                    out['greenhouses'] = res

                # Per-bucket journey (buckets + packed from the box's OPL)
                journey = []
                bucket_ids = []
                packed_map = {}
                if opl_name:
                    locs = frappe.get_all('Pick List Item',
                        filters={'parent': opl_name, 'parenttype': 'Order Pick List'},
                        fields=['bucket', 'stock_qty', 'qty', 'custom_box_id'], limit_page_length=0)
                    for loc in locs:
                        bid = loc.get('bucket')
                        if not bid:
                            continue
                        if bid not in bucket_ids:
                            bucket_ids.append(bid)
                        if bid not in packed_map:
                            packed_map[bid] = {'packed': 0, 'bunches': 0, 'group': loc.get('item_group') or '', 'boxes': []}
                        if not packed_map[bid]['group']:
                            packed_map[bid]['group'] = loc.get('item_group') or ''
                        boxid = loc.get('custom_box_id')
                        if boxid:
                            packed_map[bid]['packed'] = packed_map[bid]['packed'] + (loc.get('stock_qty') or 0)
                            packed_map[bid]['bunches'] = packed_map[bid]['bunches'] + (loc.get('qty') or 0)
                            if boxid not in packed_map[bid]['boxes']:
                                packed_map[bid]['boxes'].append(boxid)

                for bid in bucket_ids:
                    stage = {'bucket': bid, 'group': '', 'harvested': None, 'received': None, 'graded': None, 'shelved': None, 'packed': None, 'discarded': None, 'concession': None}
                    pm = packed_map.get(bid)
                    if pm:
                        stage['group'] = pm.get('group', '')
                        if pm.get('packed'):
                            stage['packed'] = {'stems': int(pm['packed']), 'bunches': int(pm.get('bunches') or 0), 'boxes': len(pm['boxes'])}

                    try:
                        hv = frappe.get_all('Stock Entry',
                            filters={'custom_bucket_id': bid, 'stock_entry_type': 'Harvesting', 'docstatus': 1},
                            fields=['posting_date', 'custom_greenhouse', 'custom_harvester'],
                            order_by='posting_date desc', limit=1)
                        if hv:
                            r = hv[0]
                            stage['harvested'] = {'date': str(r.get('posting_date', '')), 'greenhouse': r.get('custom_greenhouse', ''), 'harvester': r.get('custom_harvester', '')}
                    except Exception:
                        pass

                    try:
                        rc = frappe.get_all('Stock Entry',
                            filters={'custom_bucket_id': bid, 'stock_entry_type': ['in', ['Receiving', 'Receiving Accepted', 'Receiving Quarantined', 'Late Receipt']], 'docstatus': 1},
                            fields=['posting_date', 'stock_entry_type', 'to_warehouse'],
                            order_by='posting_date desc', limit=1)
                        if not rc:
                            rc = frappe.get_all('Stock Entry',
                                filters={'custom_bucket_id': bid, 'stock_entry_type': ['in', ['Receiving', 'Receiving Accepted', 'Receiving Quarantined', 'Late Receipt']], 'docstatus': 1},
                                fields=['posting_date', 'stock_entry_type', 'to_warehouse'],
                                order_by='posting_date desc', limit=1)
                        if rc:
                            r = rc[0]
                            tp = r.get('stock_entry_type', '') or ''
                            action = 'Quarantined' if 'Quarantined' in tp else ('Accepted' if 'Accepted' in tp else 'Received')
                            stage['received'] = {'date': str(r.get('posting_date', '')), 'action': action, 'warehouse': r.get('to_warehouse', '')}
                    except Exception:
                        pass

                    try:
                        gr = frappe.get_all('Stock Entry',
                            filters={'custom_bucket_id': bid, 'stock_entry_type': ['in', ['Grading', 'Grading Forecast']], 'docstatus': 1},
                            fields=['posting_date', 'to_warehouse'], order_by='posting_date desc', limit=1)
                        if not gr:
                            gr = frappe.get_all('Stock Entry',
                                filters={'custom_bucket_id': bid, 'stock_entry_type': ['in', ['Grading', 'Grading Forecast']], 'docstatus': 1},
                                fields=['posting_date', 'to_warehouse'], order_by='posting_date desc', limit=1)
                        if gr:
                            r = gr[0]
                            stage['graded'] = {'date': str(r.get('posting_date', '')), 'to': r.get('to_warehouse', '')}
                    except Exception:
                        pass

                    try:
                        sl = frappe.get_all('Shelving Log',
                            filters={'bucket_id': bid},
                            fields=['shelf', 'stem_qty', 'shelved_on'], order_by='shelved_on asc', limit=1)
                        if sl:
                            r = sl[0]
                            stage['shelved'] = {'shelf': r.get('shelf', ''), 'qty': r.get('stem_qty', ''), 'date': str(r.get('shelved_on', ''))}
                    except Exception:
                        pass

                    try:
                        dsc = frappe.get_all('Stock Entry',
                            filters={'custom_bucket_id': bid, 'stock_entry_type': 'Discard', 'docstatus': 1},
                            fields=['name', 'posting_date'], order_by='posting_date desc', limit=1)
                        if dsc:
                            r = dsc[0]
                            reason = ''
                            try:
                                dd = frappe.get_all('Stock Entry Detail', filters={'parent': r.get('name')}, fields=['custom_rejection_reason'], limit=1)
                                if dd:
                                    reason = dd[0].get('custom_rejection_reason', '') or ''
                            except Exception:
                                pass
                            stage['discarded'] = {'date': str(r.get('posting_date', '')), 'reason': reason}
                    except Exception:
                        pass

                    journey.append(stage)

                out['journey'] = journey

                # Reconcile units: per-bucket packed is in stems; box label lists bunches.
                pt_stems = 0
                pt_bunches = 0
                for j in journey:
                    if j['packed']:
                        pt_stems = pt_stems + j['packed']['stems']
                        pt_bunches = pt_bunches + j['packed']['bunches']
                out['box']['packed_stems'] = pt_stems
                out['box']['packed_bunches'] = pt_bunches
                lb = 0
                for it in out['box_items']:
                    lb = lb + (it.get('qty') or 0)
                out['box']['label_bunches'] = int(lb)

                frappe.response['message'] = out
    except Exception as e:
        frappe.response['message'] = {'success': False, 'error': str(e)}


@frappe.whitelist()
def getStemMovementBuckets():
    # Stem Movement - per-bucket journey (end-of-day: what got shelved, what did NOT)
    # API: getStemMovementBuckets
    # Reconciles, per bucket_id, the SAME two sources the funnel uses:
    #   Received -> Stock Entry (Receiving / Late Receipt) by custom_bucket_id, posting_date window
    #   Harvested -> Stock Entry (Harvesting) by custom_bucket_id, posting_date window
    #   Shelved  -> durable Shelving Log by bucket_id (shelved_on <= end of window)
    # A received bucket with no Shelving Log row by EOD is the "received, not shelved" backlog.
    try:
        from_date = frappe.form_dict.get('from_date') or frappe.utils.today()
        to_date   = frappe.form_dict.get('to_date') or frappe.utils.today()
        farm_param = (frappe.form_dict.get('farm') or '').strip()
        variety_param = (frappe.form_dict.get('variety') or '').strip()
        length_param = (frappe.form_dict.get('stem_length') or '').strip()
        group_param = (frappe.form_dict.get('item_group') or '').strip()

        farm_names = [r["name"] for r in frappe.get_all("Farm", fields=["name"])]
        farm_names_sorted = sorted(farm_names, key=lambda n: -len(n))
        farm_set = set(farm_names)

        def resolve_farm(raw):
            val = (raw or "").strip()
            if val in farm_set:
                return val
            for f in farm_names_sorted:
                if val == f or val.startswith(f + " ") or val.startswith(f + "-"):
                    return f
            return val or "Unknown"

        params = {'f': from_date, 't': to_date}
        buckets = {}

        def ensure_bucket(bid):
            if bid not in buckets:
                buckets[bid] = {
                    'bucket_id': bid, 'farm': '', 'greenhouse': '', 'variety': '',
                    'stem_length': '', 'harvested_stems': 0, 'received_stems': 0,
                    'shelved_stems': 0, 'harvested_on': '', 'received_on': '',
                    'received_type': '', 'shelved_on': '', 'shelf': ''
                }
            return buckets[bid]

        # Harvested buckets (Harvesting SE)
        hv = frappe.db.sql("""
            SELECT se.custom_bucket_id AS bid, se.farm AS farm, se.custom_greenhouse AS gh,
                   se.custom_stem_length AS ln, sed.item_code AS variety,
                   COALESCE(SUM(sed.qty),0) AS stems, MIN(se.posting_date) AS d
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1 AND se.stock_entry_type = 'Harvesting'
              AND se.posting_date BETWEEN %(f)s AND %(t)s
              AND se.custom_bucket_id IS NOT NULL AND TRIM(se.custom_bucket_id) != ''
            GROUP BY se.custom_bucket_id, se.farm, se.custom_greenhouse, se.custom_stem_length, sed.item_code
        """, params, as_dict=True)
        for r in hv:
            b = ensure_bucket(r.bid)
            b['farm'] = b['farm'] or (r.farm or '')
            b['greenhouse'] = b['greenhouse'] or (r.gh or '')
            b['variety'] = b['variety'] or (r.variety or '')
            b['stem_length'] = b['stem_length'] or (r.ln or '')
            b['harvested_stems'] = b['harvested_stems'] + (r.stems or 0)
            if not b['harvested_on']:
                b['harvested_on'] = str(r.d or '')

        # Received buckets (Receiving / Late Receipt SE) - mirrors the funnel's "received"
        rc = frappe.db.sql("""
            SELECT se.custom_bucket_id AS bid, se.farm AS farm, se.custom_greenhouse AS gh,
                   se.custom_stem_length AS ln, se.stock_entry_type AS t, sed.item_code AS variety,
                   COALESCE(SUM(sed.qty),0) AS stems, MIN(se.posting_date) AS d
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1 AND se.stock_entry_type IN ('Receiving','Late Receipt')
              AND se.posting_date BETWEEN %(f)s AND %(t)s
              AND se.custom_bucket_id IS NOT NULL AND TRIM(se.custom_bucket_id) != ''
            GROUP BY se.custom_bucket_id, se.farm, se.custom_greenhouse, se.custom_stem_length, se.stock_entry_type, sed.item_code
        """, params, as_dict=True)
        for r in rc:
            b = ensure_bucket(r.bid)
            b['farm'] = b['farm'] or (r.farm or '')
            b['greenhouse'] = b['greenhouse'] or (r.gh or '')
            b['variety'] = b['variety'] or (r.variety or '')
            b['stem_length'] = b['stem_length'] or (r.ln or '')
            b['received_stems'] = b['received_stems'] + (r.stems or 0)
            b['received_type'] = r.t or b['received_type']
            if not b['received_on']:
                b['received_on'] = str(r.d or '')

        # Shelved: durable Shelving Log. Two correctness guards:
        #  (1) case-insensitive bucket match — Shelving Log bucket_id is UPPER, SE ids
        #      are often lower; a plain dict lookup would silently miss them.
        #  (2) only count a shelving row dated ON/AFTER this bucket's receipt/harvest,
        #      because bucket codes are REUSED across days — a bare id match also hits
        #      a prior batch's shelving of the same code.
        bids = list(buckets.keys())
        by_upper = {}
        for k in buckets:
            by_upper[k.upper()] = buckets[k]
        end_dt = to_date + ' 23:59:59'
        i = 0
        while i < len(bids):
            chunk = bids[i:i + 500]
            i = i + 500
            rows = frappe.get_all('Shelving Log',
                filters={'bucket_id': ['in', chunk], 'shelved_on': ['<=', end_dt]},
                fields=['bucket_id', 'shelf', 'stem_qty', 'shelved_on'],
                order_by='shelved_on asc', limit_page_length=0)
            for r in rows:
                b = by_upper.get((r.get('bucket_id') or '').upper())
                if not b:
                    continue
                anchor = (b['received_on'] or b['harvested_on'] or '')[:10]
                son = str(r.get('shelved_on') or '')
                if anchor and son[:10] < anchor:
                    continue
                b['shelved_stems'] = b['shelved_stems'] + (r.get('stem_qty') or 0)
                if not b['shelved_on']:
                    b['shelved_on'] = son
                    b['shelf'] = r.get('shelf') or ''

        # Map each variety to its Item Group (for the item-group filter + column)
        varieties = set(b['variety'] for b in buckets.values() if b['variety'])
        ig_map = {}
        if varieties:
            for r in frappe.get_all('Item', filters={'name': ['in', list(varieties)]},
                                    fields=['name', 'item_group'], limit_page_length=0):
                ig_map[r['name']] = r.get('item_group') or ''

        result = []
        for b in buckets.values():
            b['farm'] = resolve_farm(b['farm'])
            b['item_group'] = ig_map.get(b['variety'], '')
            if farm_param and b['farm'] != farm_param:
                continue
            if variety_param and (b['variety'] or '') != variety_param:
                continue
            if length_param and (b['stem_length'] or '') != length_param:
                continue
            if group_param and (b['item_group'] or '') != group_param:
                continue
            shelved = 1 if b['shelved_on'] else 0
            b['shelved'] = shelved
            b['stems'] = b['received_stems'] or b['harvested_stems']
            if shelved:
                b['status'] = 'Shelved'
            elif b['received_stems'] > 0:
                b['status'] = 'Received, not shelved'
            else:
                b['status'] = 'Harvested, not received'
            result.append(b)

        # Not-shelved first, biggest stem loss first
        result = sorted(result, key=lambda x: (x['shelved'], -(x['stems'] or 0)))

        summary = {
            'buckets': len(result),
            'shelved': sum(1 for b in result if b['shelved']),
            'not_shelved': sum(1 for b in result if not b['shelved']),
            'received_stems': sum(b['received_stems'] for b in result),
            'shelved_stems': sum(b['shelved_stems'] for b in result),
            'not_shelved_stems': sum((b['received_stems'] or b['harvested_stems']) for b in result if not b['shelved'])
        }

        frappe.response['message'] = {
            'success': True, 'from_date': from_date, 'to_date': to_date,
            'farm': farm_param, 'variety': variety_param, 'stem_length': length_param,
            'item_group': group_param, 'buckets': result, 'summary': summary
        }
    except Exception as e:
        frappe.log_error('getStemMovementBuckets error: ' + str(e))
        frappe.response['message'] = {'success': False, 'error': str(e), 'buckets': [], 'summary': {}}


@frappe.whitelist()
def getStemMovementData():
    # Stem Movement Data
    # API: getStemMovementData
    # Throughput funnel of stems across stages, grouped by (canonical) farm + variety:
    #   Harvested / Received  -> Stock Entry (posting_date window)   [matches production-dashboard]
    #   Shelved               -> Shelf Item (LIVE snapshot, not window-scoped)
    #   Issued / Packed / Staged / Dispatched -> demand side, scoped to Sales Orders
    #         whose delivery_date is in the window (dispatched by Dispatch Form custom_date).
    # Warehouse-style farm strings (cold stores) are normalised to a Farm via resolve_farm.
    try:
        from_date = frappe.form_dict.get('from_date') or frappe.utils.today()
        to_date   = frappe.form_dict.get('to_date') or frappe.utils.today()
        farm_param = (frappe.form_dict.get('farm') or '').strip()
        variety_param = (frappe.form_dict.get('variety') or '').strip()
        length_param = (frappe.form_dict.get('stem_length') or '').strip()
        group_param = (frappe.form_dict.get('item_group') or '').strip()

        group_items = None
        if group_param:
            group_items = set(r['name'] for r in frappe.get_all('Item', filters={'item_group': group_param}, fields=['name'], limit_page_length=0))

        farm_names = [r["name"] for r in frappe.get_all("Farm", fields=["name"])]
        farm_names_sorted = sorted(farm_names, key=lambda n: -len(n))
        farm_set = set(farm_names)

        def resolve_farm(raw):
            val = (raw or "").strip()
            if val in farm_set:
                return val
            for f in farm_names_sorted:
                if val == f or val.startswith(f + " ") or val.startswith(f + "-"):
                    return f
            return val or "Unknown"

        STAGES = ['harvested', 'received', 'shelved', 'issued', 'packed', 'staged', 'loaded', 'dispatched']
        farm_map = {}

        def ensure(farm):
            if farm not in farm_map:
                rec = {'farm': farm, 'varieties': {}}
                for s in STAGES:
                    rec[s] = 0
                farm_map[farm] = rec
            return farm_map[farm]

        def ensure_var(rec, variety):
            vs = rec['varieties']
            if variety not in vs:
                vrec = {'variety': variety}
                for s in STAGES:
                    vrec[s] = 0
                vs[variety] = vrec
            return vs[variety]

        def add(raw_farm, variety, stage, stems):
            farm = resolve_farm(raw_farm)
            if farm_param and farm != farm_param:
                return
            if variety_param and (variety or '') != variety_param:
                return
            if group_items is not None and (variety or '') not in group_items:
                return
            stems = stems or 0
            rec = ensure(farm)
            rec[stage] = rec[stage] + stems
            v = ensure_var(rec, variety or 'Unknown')
            v[stage] = v[stage] + stems

        params = {'f': from_date, 't': to_date, 'ln': length_param, 'endt': to_date + ' 23:59:59'}

        # ── All 8 gate flows for a [f, t] window, yielded as (raw_farm, variety,
        #    stage, stems) tuples. Factored out so the compare window reuses the
        #    exact same queries/scoping as the primary window. ──
        def _flow_rows(f, t):
            prm = {'f': f, 't': t, 'ln': length_param, 'endt': t + ' 23:59:59'}
            out_rows = []
            # Harvested
            for r in frappe.db.sql("""
                SELECT se.farm AS farm, sed.item_code AS variety, COALESCE(SUM(sed.qty), 0) AS stems
                FROM `tabStock Entry` se
                INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
                WHERE se.docstatus = 1 AND se.posting_date BETWEEN %(f)s AND %(t)s
                  AND se.stock_entry_type = 'Harvesting'
                  AND (%(ln)s = '' OR se.custom_stem_length = %(ln)s)
                GROUP BY se.farm, sed.item_code
            """, prm, as_dict=True):
                out_rows.append((r.farm, r.variety, 'harvested', r.stems))
            # Received + Shelved (same received-bucket cohort; shelving tied to this
            # receipt via sl.shelved_on >= se.posting_date so reused bucket codes
            # from a prior batch don't inflate Shelved above Received)
            for r in frappe.db.sql("""
                SELECT se.farm AS farm, sed.item_code AS variety,
                       COALESCE(SUM(sed.qty), 0) AS received,
                       COALESCE(SUM(CASE WHEN EXISTS (
                           SELECT 1 FROM `tabShelving Log` sl
                           WHERE sl.bucket_id = se.custom_bucket_id
                             AND sl.shelved_on >= se.posting_date AND sl.shelved_on <= %(endt)s
                       ) THEN sed.qty ELSE 0 END), 0) AS shelved
                FROM `tabStock Entry` se
                INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
                WHERE se.docstatus = 1 AND se.posting_date BETWEEN %(f)s AND %(t)s
                  AND se.stock_entry_type IN ('Receiving', 'Late Receipt')
                  AND (%(ln)s = '' OR se.custom_stem_length = %(ln)s)
                GROUP BY se.farm, sed.item_code
            """, prm, as_dict=True):
                out_rows.append((r.farm, r.variety, 'received', r.received))
                out_rows.append((r.farm, r.variety, 'shelved', r.shelved))
            # Issued (Pick List Item issued=1, scoped by OPL date_created)
            for r in frappe.db.sql("""
                SELECT pli.warehouse AS wh, pli.item_code AS variety, COALESCE(SUM(pli.stock_qty), 0) AS stems
                FROM `tabPick List Item` pli
                INNER JOIN `tabOrder Pick List` opl ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
                WHERE pli.issued = 1 AND opl.date_created BETWEEN %(f)s AND %(t)s
                GROUP BY pli.warehouse, pli.item_code
            """, prm, as_dict=True):
                out_rows.append((r.wh, r.variety, 'issued', r.stems))
            # Packed (Farm Packlist Item, scoped by FPL creation date)
            for r in frappe.db.sql("""
                SELECT pli.source_warehouse AS wh, pli.item_code AS variety, COALESCE(SUM(pli.stock_qty), 0) AS stems
                FROM `tabFarm Pack List` fpl
                INNER JOIN `tabFarm Packlist Item` pli
                    ON pli.parent = fpl.name AND pli.parenttype = 'Farm Pack List' AND pli.parentfield = 'pack_list_item'
                WHERE fpl.docstatus != 2 AND DATE(fpl.creation) BETWEEN %(f)s AND %(t)s
                  AND (%(ln)s = '' OR pli.stem_length = %(ln)s)
                GROUP BY pli.source_warehouse, pli.item_code
            """, prm, as_dict=True):
                out_rows.append((r.wh, r.variety, 'packed', r.stems))
            # Staged (Box Label staged=1, by Box Label date)
            for r in frappe.db.sql("""
                SELECT bl.farm AS wh, bi.variety AS variety, COALESCE(SUM(bi.qty), 0) AS stems
                FROM `tabBox Label` bl
                INNER JOIN `tabBox Label Item` bi ON bi.parent = bl.name
                INNER JOIN `tabSales Order` so ON so.name = bl.customer_purchase_order
                WHERE bl.staged = 1 AND so.docstatus = 1 AND DATE(bl.date) BETWEEN %(f)s AND %(t)s
                  AND so.status NOT IN ('Cancelled', 'Closed')
                  AND (%(ln)s = '' OR bi.length = %(ln)s)
                GROUP BY bl.farm, bi.variety
            """, prm, as_dict=True):
                out_rows.append((r.wh, r.variety, 'staged', r.stems))
            # Loaded (OPL transfer, Pick List Item loaded_in_trolley=1, by OPL date_created)
            for r in frappe.db.sql("""
                SELECT opl.farm AS wh, pli.item_code AS variety, COALESCE(SUM(pli.stock_qty), 0) AS stems
                FROM `tabPick List Item` pli
                INNER JOIN `tabOrder Pick List` opl ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
                WHERE pli.loaded_in_trolley = 1 AND opl.date_created BETWEEN %(f)s AND %(t)s
                GROUP BY opl.farm, pli.item_code
            """, prm, as_dict=True):
                out_rows.append((r.wh, r.variety, 'loaded', r.stems))
            # Dispatched (Delivery Note, by posting_date)
            for r in frappe.db.sql("""
                SELECT dni.farm AS farm, dni.item_code AS variety, COALESCE(SUM(dni.stock_qty), 0) AS stems
                FROM `tabDelivery Note` dn
                INNER JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
                WHERE dn.docstatus = 1 AND dn.posting_date BETWEEN %(f)s AND %(t)s
                GROUP BY dni.farm, dni.item_code
            """, prm, as_dict=True):
                out_rows.append((r.farm, r.variety, 'dispatched', r.stems))
            return out_rows

        # Same 8 flows, but grouped by DAY → (day, raw_farm, variety, stage, stems).
        # Powers the per-gate daily trend sparklines (Phase 2). Each gate keeps its
        # own activity date (posting_date / OPL date_created / FPL creation / Box date).
        def _daily_flow_rows(f, t):
            prm = {'f': f, 't': t, 'ln': length_param, 'endt': t + ' 23:59:59'}
            rows = []
            for r in frappe.db.sql("""
                SELECT se.posting_date AS d, se.farm AS farm, sed.item_code AS variety, COALESCE(SUM(sed.qty),0) AS s
                FROM `tabStock Entry` se INNER JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
                WHERE se.docstatus=1 AND se.posting_date BETWEEN %(f)s AND %(t)s AND se.stock_entry_type='Harvesting'
                  AND (%(ln)s='' OR se.custom_stem_length=%(ln)s)
                GROUP BY se.posting_date, se.farm, sed.item_code
            """, prm, as_dict=True):
                rows.append((str(r.d), r.farm, r.variety, 'harvested', r.s))
            for r in frappe.db.sql("""
                SELECT se.posting_date AS d, se.farm AS farm, sed.item_code AS variety,
                       COALESCE(SUM(sed.qty),0) AS received,
                       COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM `tabShelving Log` sl
                           WHERE sl.bucket_id=se.custom_bucket_id AND sl.shelved_on>=se.posting_date AND sl.shelved_on<=%(endt)s)
                           THEN sed.qty ELSE 0 END),0) AS shelved
                FROM `tabStock Entry` se INNER JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
                WHERE se.docstatus=1 AND se.posting_date BETWEEN %(f)s AND %(t)s
                  AND se.stock_entry_type IN ('Receiving','Late Receipt') AND (%(ln)s='' OR se.custom_stem_length=%(ln)s)
                GROUP BY se.posting_date, se.farm, sed.item_code
            """, prm, as_dict=True):
                rows.append((str(r.d), r.farm, r.variety, 'received', r.received))
                rows.append((str(r.d), r.farm, r.variety, 'shelved', r.shelved))
            for r in frappe.db.sql("""
                SELECT opl.date_created AS d, pli.warehouse AS wh, pli.item_code AS variety, COALESCE(SUM(pli.stock_qty),0) AS s
                FROM `tabPick List Item` pli INNER JOIN `tabOrder Pick List` opl ON pli.parent=opl.name AND pli.parenttype='Order Pick List'
                WHERE pli.issued=1 AND opl.date_created BETWEEN %(f)s AND %(t)s
                GROUP BY opl.date_created, pli.warehouse, pli.item_code
            """, prm, as_dict=True):
                rows.append((str(r.d), r.wh, r.variety, 'issued', r.s))
            for r in frappe.db.sql("""
                SELECT DATE(fpl.creation) AS d, pli.source_warehouse AS wh, pli.item_code AS variety, COALESCE(SUM(pli.stock_qty),0) AS s
                FROM `tabFarm Pack List` fpl INNER JOIN `tabFarm Packlist Item` pli
                    ON pli.parent=fpl.name AND pli.parenttype='Farm Pack List' AND pli.parentfield='pack_list_item'
                WHERE fpl.docstatus!=2 AND DATE(fpl.creation) BETWEEN %(f)s AND %(t)s AND (%(ln)s='' OR pli.stem_length=%(ln)s)
                GROUP BY DATE(fpl.creation), pli.source_warehouse, pli.item_code
            """, prm, as_dict=True):
                rows.append((str(r.d), r.wh, r.variety, 'packed', r.s))
            for r in frappe.db.sql("""
                SELECT DATE(bl.date) AS d, bl.farm AS wh, bi.variety AS variety, COALESCE(SUM(bi.qty),0) AS s
                FROM `tabBox Label` bl INNER JOIN `tabBox Label Item` bi ON bi.parent=bl.name
                INNER JOIN `tabSales Order` so ON so.name=bl.customer_purchase_order
                WHERE bl.staged=1 AND so.docstatus=1 AND DATE(bl.date) BETWEEN %(f)s AND %(t)s
                  AND so.status NOT IN ('Cancelled','Closed') AND (%(ln)s='' OR bi.length=%(ln)s)
                GROUP BY DATE(bl.date), bl.farm, bi.variety
            """, prm, as_dict=True):
                rows.append((str(r.d), r.wh, r.variety, 'staged', r.s))
            for r in frappe.db.sql("""
                SELECT opl.date_created AS d, opl.farm AS wh, pli.item_code AS variety, COALESCE(SUM(pli.stock_qty),0) AS s
                FROM `tabPick List Item` pli INNER JOIN `tabOrder Pick List` opl ON pli.parent=opl.name AND pli.parenttype='Order Pick List'
                WHERE pli.loaded_in_trolley=1 AND opl.date_created BETWEEN %(f)s AND %(t)s
                GROUP BY opl.date_created, opl.farm, pli.item_code
            """, prm, as_dict=True):
                rows.append((str(r.d), r.wh, r.variety, 'loaded', r.s))
            for r in frappe.db.sql("""
                SELECT dn.posting_date AS d, dni.farm AS farm, dni.item_code AS variety, COALESCE(SUM(dni.stock_qty),0) AS s
                FROM `tabDelivery Note` dn INNER JOIN `tabDelivery Note Item` dni ON dni.parent=dn.name
                WHERE dn.docstatus=1 AND dn.posting_date BETWEEN %(f)s AND %(t)s
                GROUP BY dn.posting_date, dni.farm, dni.item_code
            """, prm, as_dict=True):
                rows.append((str(r.d), r.farm, r.variety, 'dispatched', r.s))
            return rows

        for rf, v, st, s in _flow_rows(from_date, to_date):
            add(rf, v, st, s)

        farms = list(farm_map.values())
        for rec in farms:
            # Received but not shelved = received-in-window minus shelved-in-window (floored at 0).
            for v in rec['varieties'].values():
                v['received_not_shelved'] = max(0, (v.get('received', 0) or 0) - (v.get('shelved', 0) or 0))
            rec['received_not_shelved'] = max(0, (rec.get('received', 0) or 0) - (rec.get('shelved', 0) or 0))
            rec['varieties'] = sorted(rec['varieties'].values(),
                                      key=lambda v: (v.get('harvested', 0) + v.get('dispatched', 0)), reverse=True)
        farms = sorted(farms, key=lambda x: (x['harvested'] or x['dispatched'] or x['shelved']), reverse=True)

        totals = {}
        for s in STAGES:
            totals[s] = sum(r[s] for r in farms)
        # Sum the per-farm (already floored) backlogs so the total matches the table
        # column and reflects real unshelved receipts even when other farms over-shelved.
        totals['received_not_shelved'] = sum(r['received_not_shelved'] for r in farms)

        # ══════════════════════════════════════════════════════════════════════
        # PIPELINE (stock-and-flow). A gate value above is a FLOW — stems that
        # crossed that gate during the window. What sits BETWEEN two gates right
        # now is a STOCK (a buffer / WIP queue). Mixing them in one funnel is what
        # produced impossibilities like "packed 116% of issued". Here each buffer
        # is its own row governed by the identity:
        #       opening + inflow − outflow = closing (level now)
        # For buffers with a clean live source we measure the level + oldest age
        # directly; for the rest we report the net flow and flag a negative
        # (impossible) balance — a skipped step or an unrecorded/duplicated txn.
        # ══════════════════════════════════════════════════════════════════════
        def _buf_where(col_farm, col_var, col_len):
            cl, p = [], {}
            if farm_param and col_farm:
                cl.append(col_farm + " LIKE %(bf)s"); p['bf'] = farm_param + '%'
            if variety_param and col_var:
                cl.append(col_var + " = %(bv)s"); p['bv'] = variety_param
            if length_param and col_len:
                cl.append(col_len + " = %(bl)s"); p['bl'] = length_param
            return ((" AND " + " AND ".join(cl)) if cl else ""), p

        def _age_hours(dtval):
            if not dtval:
                return None
            try:
                return round(frappe.utils.time_diff_in_hours(frappe.utils.now_datetime(), dtval), 1)
            except Exception:
                return None

        # ── in transit: harvested (recent) with no receiving yet ──
        w, p = _buf_where('se.farm', 'sed.item_code', 'se.custom_stem_length')
        r = frappe.db.sql("""
            SELECT COALESCE(SUM(sed.qty),0) AS lvl,
                   COUNT(DISTINCT se.custom_bucket_id) AS buckets, MIN(se.creation) AS oldest
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1 AND se.stock_entry_type = 'Harvesting'
              AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 2 DAY)
              AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id != ''
              AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` rc WHERE rc.docstatus = 1
                  AND rc.stock_entry_type IN ('Receiving','Late Receipt')
                  AND rc.custom_bucket_id = se.custom_bucket_id AND rc.posting_date >= se.posting_date)
        """ + w, p, as_dict=True)[0]
        in_transit = {'level': int(r.lvl or 0), 'buckets': int(r.buckets or 0), 'age_hours': _age_hours(r.oldest)}

        # ── unshelved: received (recent) not on a shelf, not discarded, not issued ──
        w, p = _buf_where('se.farm', 'sed.item_code', 'se.custom_stem_length')
        r = frappe.db.sql("""
            SELECT COALESCE(SUM(sed.qty),0) AS lvl,
                   COUNT(DISTINCT se.custom_bucket_id) AS buckets, MIN(se.creation) AS oldest
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1 AND se.stock_entry_type IN ('Receiving','Late Receipt')
              AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
              AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id != ''
              AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = se.custom_bucket_id AND si.stem_qty > 0)
              AND NOT EXISTS (SELECT 1 FROM `tabStock Entry` d WHERE d.docstatus = 1
                  AND d.stock_entry_type = 'Discard' AND d.custom_bucket_id = se.custom_bucket_id AND d.posting_date >= se.posting_date)
              AND NOT EXISTS (SELECT 1 FROM `tabPick List Item` pli WHERE pli.bucket = se.custom_bucket_id AND pli.issued = 1)
        """ + w, p, as_dict=True)[0]
        unshelved = {'level': int(r.lvl or 0), 'buckets': int(r.buckets or 0), 'age_hours': _age_hours(r.oldest)}

        # ── on shelf: live Shelf Item stock (shelved, not yet issued) ──
        w, p = _buf_where('s.farm', 'si.variety', 'si.stem_length')
        r = frappe.db.sql("""
            SELECT COALESCE(SUM(si.stem_qty),0) AS lvl,
                   COUNT(DISTINCT si.bucket_id) AS buckets,
                   MIN(COALESCE(si.receiving_date, si.date_added)) AS oldest
            FROM `tabShelf` s INNER JOIN `tabShelf Item` si ON s.name = si.parent
            WHERE si.stem_qty > 0 AND si.variety IS NOT NULL AND TRIM(si.variety) != ''
        """ + w, p, as_dict=True)[0]
        on_shelf = {'level': int(r.lvl or 0), 'buckets': int(r.buckets or 0), 'age_hours': _age_hours(r.oldest)}

        # ── pack hall: issued to the pack hall (Pick List Item issued=1) but not yet
        #    boxed (no custom_box_label). Age from the OPL that issued it. ──
        w, p = _buf_where('pli.farm', 'pli.item_code', 'pli.stem_length')
        r = frappe.db.sql("""
            SELECT COALESCE(SUM(pli.stock_qty),0) AS lvl, COUNT(*) AS n, MIN(opl.creation) AS oldest
            FROM `tabPick List Item` pli
            INNER JOIN `tabOrder Pick List` opl ON opl.name = pli.parent AND pli.parenttype = 'Order Pick List'
            WHERE pli.issued = 1 AND (pli.custom_box_label IS NULL OR pli.custom_box_label = '')
              AND opl.date_created >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        """ + w, p, as_dict=True)[0]
        pack_hall = {'level': int(r.lvl or 0), 'buckets': int(r.n or 0), 'age_hours': _age_hours(r.oldest)}

        # ── the three box-level buffers all live on Box Label flags:
        #    packed => a Box Label exists; staged / loaded / delivered are booleans. ──
        def _box_buf(cond):
            w2, p2 = _buf_where('bl.farm', 'bi.variety', 'bl.length')
            row = frappe.db.sql("""
                SELECT COALESCE(SUM(bi.qty),0) AS lvl, COUNT(DISTINCT bl.name) AS n, MIN(bl.creation) AS oldest
                FROM `tabBox Label` bl INNER JOIN `tabBox Label Item` bi ON bi.parent = bl.name
                WHERE {cond}
                  AND bl.date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            """.replace("{cond}", cond) + w2, p2, as_dict=True)[0]
            return {'level': int(row.lvl or 0), 'buckets': int(row.n or 0), 'age_hours': _age_hours(row.oldest)}

        awaiting_stage = _box_buf("IFNULL(bl.staged,0)=0 AND IFNULL(bl.delivered,0)=0")
        staged_buf     = _box_buf("IFNULL(bl.staged,0)=1 AND IFNULL(bl.loaded,0)=0 AND IFNULL(bl.delivered,0)=0")
        on_truck       = _box_buf("IFNULL(bl.loaded,0)=1 AND IFNULL(bl.delivered,0)=0")

        # Buffer defs: (key, label, inflow gate, outflow gate, live level dict)
        _buf_defs = [
            ('in_transit',     'In transit',     'harvested', 'received',   in_transit),
            ('unshelved',      'Unshelved',      'received',  'shelved',    unshelved),
            ('on_shelf',       'On shelf',       'shelved',   'issued',     on_shelf),
            ('pack_hall',      'Pack hall',      'issued',    'packed',     pack_hall),
            ('awaiting_stage', 'Awaiting stage', 'packed',    'staged',     awaiting_stage),
            ('staged',         'Staged',         'staged',    'loaded',     staged_buf),
            ('on_truck',       'On truck',       'loaded',    'dispatched', on_truck),
        ]
        buffers = []
        for key, label, gin, gout, live in _buf_defs:
            inflow = totals.get(gin, 0) or 0
            outflow = totals.get(gout, 0) or 0
            net = inflow - outflow
            measured = live is not None
            if measured:
                level = live['level']
                # Balance identity: opening = level_now − inflow + outflow.
                # A negative opening is physically impossible — outflow exceeded
                # what was available, so a step is unrecorded or double-counted.
                opening = level - inflow + outflow
                impossible = opening < -0.5
                shortfall = int(max(0, -opening))
            else:
                level = None
                impossible = net < 0
                shortfall = int(max(0, -net))
            buffers.append({
                'key': key, 'label': label, 'from_gate': gin, 'to_gate': gout,
                'inflow': inflow, 'outflow': outflow, 'net': net,
                'measured': measured, 'impossible': impossible, 'shortfall': shortfall,
                'level': level,
                'age_hours': (live['age_hours'] if measured else None),
                'buckets': (live.get('buckets') if measured else None),
            })

        held_in_pipeline = sum((b.get('level') or 0) for b in buffers if b.get('measured'))

        # ── Cohort funnel: take THIS window's harvest and follow those exact buckets
        #    forward. Unlike the gate flows (which each count all activity that
        #    crossed the gate), this is one declining cohort — a legitimate funnel,
        #    expected to be mostly incomplete same-day. Each downstream gate is guarded
        #    by ">= that bucket's own harvest date" so a reused bucket code from a
        #    prior batch can't leak into the cohort. ──
        cw, cp = _buf_where('h.farm', 'h.variety', None)
        cohort_params = dict(params); cohort_params.update(cp)
        crow = frappe.db.sql("""
            SELECT
              COALESCE(SUM(h.stems),0) AS harvested,
              COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM `tabStock Entry` rc
                    WHERE rc.docstatus=1 AND rc.stock_entry_type IN ('Receiving','Late Receipt')
                      AND rc.custom_bucket_id=h.bucket AND rc.posting_date>=h.hdate) THEN h.stems ELSE 0 END),0) AS received,
              COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM `tabShelving Log` sl
                    WHERE sl.bucket_id=h.bucket AND sl.shelved_on>=h.hdate) THEN h.stems ELSE 0 END),0) AS shelved,
              COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM `tabPick List Item` pli
                    INNER JOIN `tabOrder Pick List` opl ON opl.name=pli.parent AND pli.parenttype='Order Pick List'
                    WHERE pli.bucket=h.bucket AND pli.issued=1 AND opl.date_created>=h.hdate) THEN h.stems ELSE 0 END),0) AS issued,
              COALESCE(SUM(CASE WHEN EXISTS (SELECT 1 FROM `tabPick List Item` pli2
                    INNER JOIN `tabOrder Pick List` opl2 ON opl2.name=pli2.parent AND pli2.parenttype='Order Pick List'
                    WHERE pli2.bucket=h.bucket AND pli2.custom_box_label IS NOT NULL AND pli2.custom_box_label<>''
                      AND opl2.date_created>=h.hdate) THEN h.stems ELSE 0 END),0) AS packed
            FROM (
              SELECT se.custom_bucket_id AS bucket, se.farm AS farm, sed.item_code AS variety,
                     SUM(sed.qty) AS stems, MIN(se.posting_date) AS hdate
              FROM `tabStock Entry` se INNER JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
              WHERE se.docstatus=1 AND se.stock_entry_type='Harvesting'
                AND se.posting_date BETWEEN %(f)s AND %(t)s
                AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id<>''
                AND (%(ln)s='' OR se.custom_stem_length=%(ln)s)
              GROUP BY se.custom_bucket_id, se.farm, sed.item_code
            ) h
            WHERE 1=1 """ + cw + """
        """, cohort_params, as_dict=True)[0]
        cohort = [
            {'key': 'harvested', 'label': 'Harvested', 'stems': int(crow.harvested or 0)},
            {'key': 'received',  'label': 'Received',  'stems': int(crow.received or 0)},
            {'key': 'shelved',   'label': 'Shelved',   'stems': int(crow.shelved or 0)},
            {'key': 'issued',    'label': 'Issued',    'stems': int(crow.issued or 0)},
            {'key': 'packed',    'label': 'Packed',    'stems': int(crow.packed or 0)},
        ]

        pipeline = {
            'buffers': buffers,
            'held_in_pipeline': held_in_pipeline,
            'cut_today': totals.get('harvested', 0),
            'dispatched_today': totals.get('dispatched', 0),
            'cohort': cohort,
        }

        # ── Compare window (optional): the same 8 gate flows over a second date
        #    range, so each gate can show a delta vs a chosen comparison period
        #    (previous period, same week last month, or any custom range). Uses the
        #    exact same _flow_rows scoping + the current farm/variety/group filters. ──
        compare = None
        compare_from = (frappe.form_dict.get('compare_from') or '').strip()
        compare_to = (frappe.form_dict.get('compare_to') or '').strip()
        if compare_from and compare_to:
            ctot = {s: 0 for s in STAGES}
            for rf, v, st, s in _flow_rows(compare_from, compare_to):
                farm = resolve_farm(rf)
                if farm_param and farm != farm_param:
                    continue
                if variety_param and (v or '') != variety_param:
                    continue
                if group_items is not None and (v or '') not in group_items:
                    continue
                ctot[st] = ctot[st] + (s or 0)
            deltas = {}
            for st in STAGES:
                cur = totals.get(st, 0) or 0
                base = ctot.get(st, 0) or 0
                deltas[st] = {
                    'abs': cur - base,
                    'pct': (round((cur - base) / base * 100, 1) if base else None),
                }
            compare = {'from': compare_from, 'to': compare_to, 'totals': ctot, 'deltas': deltas}

        # ══ Phase 2 — TRENDS: per-gate daily throughput series over the range, plus
        #    the compare range aligned by position (when equal length), and a
        #    per-buffer "balance broke" streak (days the daily flow imbalance went
        #    negative — outflow > inflow that day). ══
        from datetime import timedelta
        d0 = frappe.utils.getdate(from_date)
        d1 = frappe.utils.getdate(to_date)
        ndays = (d1 - d0).days + 1
        trends = None
        if 1 <= ndays <= 92:
            def _agg_daily(rows):
                daily = {}
                for day, rf, v, st, s in rows:
                    farm = resolve_farm(rf)
                    if farm_param and farm != farm_param:
                        continue
                    if variety_param and (v or '') != variety_param:
                        continue
                    if group_items is not None and (v or '') not in group_items:
                        continue
                    daily.setdefault(day, {x: 0 for x in STAGES})
                    daily[day][st] = daily[day][st] + (s or 0)
                return daily

            pd_map = _agg_daily(_daily_flow_rows(from_date, to_date))
            dates = [str(d0 + timedelta(days=i)) for i in range(ndays)]
            gates_series = {st: [round(pd_map.get(dt, {}).get(st, 0)) for dt in dates] for st in STAGES}
            _buf_gates = [('in_transit', 'harvested', 'received'), ('unshelved', 'received', 'shelved'),
                          ('on_shelf', 'shelved', 'issued'), ('pack_hall', 'issued', 'packed'),
                          ('awaiting_stage', 'packed', 'staged'), ('staged', 'staged', 'loaded'),
                          ('on_truck', 'loaded', 'dispatched')]
            buf_broke = {}
            for bkey, gin, gout in _buf_gates:
                broke = sum(1 for dt in dates
                            if (pd_map.get(dt, {}).get(gin, 0) - pd_map.get(dt, {}).get(gout, 0)) < 0)
                buf_broke[bkey] = {'broke': broke, 'total': ndays}
            trends = {'dates': dates, 'gates': gates_series, 'buffer_broke': buf_broke}
            if compare_from and compare_to:
                cd0 = frappe.utils.getdate(compare_from)
                cd1 = frappe.utils.getdate(compare_to)
                if (cd1 - cd0).days + 1 == ndays:
                    cpd = _agg_daily(_daily_flow_rows(compare_from, compare_to))
                    cdates = [str(cd0 + timedelta(days=i)) for i in range(ndays)]
                    trends['compare_dates'] = cdates
                    trends['compare_gates'] = {st: [round(cpd.get(dt, {}).get(st, 0)) for dt in cdates] for st in STAGES}

        # ══ Phase 3 — FORECAST: demand due by upcoming delivery date vs the stock we
        #    currently hold in the pipeline to meet it. ══
        fw, fp = _buf_where('so.custom_farm', 'soi.item_code', 'soi.custom_length')
        if group_param:
            fw = fw + " AND soi.item_group = %(grp)s"
            fp['grp'] = group_param
        demand_rows = frappe.db.sql("""
            SELECT so.delivery_date AS d, COALESCE(SUM(soi.qty * soi.conversion_factor), 0) AS stems
            FROM `tabSales Order Item` soi
            INNER JOIN `tabSales Order` so ON so.name = soi.parent
            WHERE so.docstatus = 1 AND so.status NOT IN ('Cancelled', 'Closed', 'Completed')
              AND so.delivery_date >= CURDATE() AND soi.item_code IS NOT NULL
        """ + fw + """
            GROUP BY so.delivery_date ORDER BY so.delivery_date
        """, fp, as_dict=True)
        forecast = {
            'available': held_in_pipeline,
            'on_shelf': on_shelf['level'],
            'demand': [{'date': str(r.d), 'stems': int(r.stems or 0)} for r in demand_rows if r.d],
        }

        # Filter dropdown options (variety / length / item group) present in the
        # window's supply — computed unfiltered by variety/length/group so the user
        # can freely change the selection.
        opt_rows = frappe.db.sql("""
            SELECT DISTINCT sed.item_code AS variety, se.custom_stem_length AS ln, i.item_group AS ig
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            LEFT JOIN `tabItem` i ON i.name = sed.item_code
            WHERE se.docstatus = 1
              AND se.posting_date BETWEEN %(f)s AND %(t)s
              AND se.stock_entry_type IN ('Harvesting', 'Receiving', 'Late Receipt')
        """, {'f': from_date, 't': to_date}, as_dict=True)
        ov = set(); ol = set(); og = set()
        for r in opt_rows:
            if r.variety:
                ov.add(r.variety)
            if r.ln:
                ol.add(r.ln)
            if r.ig:
                og.add(r.ig)
        options = {'varieties': sorted(ov), 'lengths': sorted(ol), 'item_groups': sorted(og)}

        frappe.response['message'] = {
            'success': True,
            'from_date': from_date,
            'to_date': to_date,
            'farm': farm_param,
            'variety': variety_param,
            'stem_length': length_param,
            'item_group': group_param,
            'stages': STAGES,
            'farms': farms,
            'totals': totals,
            'pipeline': pipeline,
            'compare': compare,
            'trends': trends,
            'forecast': forecast,
            'options': options
        }
    except Exception as e:
        frappe.log_error('getStemMovementData error: ' + str(e))
        frappe.response['message'] = {'success': False, 'error': str(e), 'farms': [], 'totals': {}}


@frappe.whitelist()
def searchBoxLabels():
    # API: searchBoxLabels  — typeahead for the Box Traceability tab
    try:
        q = frappe.utils.cstr(frappe.form_dict.get('q') or '').strip()
        filt = {}
        if q:
            filt = {'name': ['like', '%' + q + '%']}
        rows = frappe.get_all('Box Label', filters=filt,
            fields=['name', 'customer', 'date'],
            order_by='modified desc', limit_page_length=15)
        frappe.response['message'] = {'success': True, 'results': rows}
    except Exception as e:
        frappe.response['message'] = {'success': False, 'error': str(e), 'results': []}


@frappe.whitelist()
def getPackhouseFlowBoard():
    # Packhouse Flow board — the stock-and-flow view.
    # Returns the exact shape the #pf-root board renders: totals (today's 8 gate
    # FLOWS), 7 buffers (each with opening/inflow/outflow/live/age + a reconstructed
    # 7-day level series), the On-shelf ledger from Shelving Log, the last 3 days of
    # gate flows, and today's single cohort followed forward.
    try:
        today = frappe.utils.today()
        d0 = frappe.utils.getdate(today)
        morning = today + " 06:00:00"
        STAGES = ['harvested', 'received', 'shelved', 'issued', 'packed', 'staged', 'loaded', 'dispatched']

        # ── Filters (Location = greenhouse origin, Farm, Variety) ──
        # farm/variety are carried on every gate + buffer, so they filter the whole
        # board. Greenhouse (location) is only recorded upstream (on the harvest /
        # receiving Stock Entry), so it constrains the upstream gates + the SE-based
        # buffers + the cohort; downstream gates (issued..dispatched), which don't
        # carry a greenhouse, are left unconstrained by it.
        flt_farm = (frappe.form_dict.get('farm') or '').strip()
        flt_var = (frappe.form_dict.get('variety') or '').strip()
        flt_loc = (frappe.form_dict.get('location') or '').strip()
        flt = {'flt_farm': flt_farm, 'flt_var': flt_var, 'flt_loc': flt_loc}

        def _frag(farm_col=None, var_col=None, loc_col=None):
            # Guard-style clauses: each is a no-op when its param is empty, so the
            # same fragment string works filtered and unfiltered with no branching.
            s = ""
            if farm_col:
                s += " AND (%(flt_farm)s = '' OR " + farm_col + " LIKE CONCAT(%(flt_farm)s, '%%'))"
            if var_col:
                s += " AND (%(flt_var)s = '' OR " + var_col + " = %(flt_var)s)"
            if loc_col:
                s += " AND (%(flt_loc)s = '' OR " + loc_col + " = %(flt_loc)s)"
            return s

        def _flows(f, t):
            p = {"f": f, "t": t, "endt": t + " 23:59:59"}
            p.update(flt)
            tt = {s: 0 for s in STAGES}

            def q(sql, key):
                r = frappe.db.sql(sql, p, as_dict=True)
                tt[key] = int((r[0].s or 0) if r and r[0].s is not None else 0)
            q("SELECT COALESCE(SUM(sed.qty),0) s FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent=se.name WHERE se.docstatus=1 AND se.stock_entry_type='Harvesting' AND se.posting_date BETWEEN %(f)s AND %(t)s" + _frag('se.farm', 'sed.item_code', 'se.custom_greenhouse'), 'harvested')
            q("SELECT COALESCE(SUM(sed.qty),0) s FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent=se.name WHERE se.docstatus=1 AND se.stock_entry_type IN ('Receiving','Late Receipt') AND se.posting_date BETWEEN %(f)s AND %(t)s" + _frag('se.farm', 'sed.item_code', 'se.custom_greenhouse'), 'received')
            q("SELECT COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM `tabShelving Log` sl WHERE sl.bucket_id=se.custom_bucket_id AND sl.shelved_on>=se.posting_date AND sl.shelved_on<=%(endt)s) THEN sed.qty ELSE 0 END),0) s FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent=se.name WHERE se.docstatus=1 AND se.stock_entry_type IN ('Receiving','Late Receipt') AND se.posting_date BETWEEN %(f)s AND %(t)s" + _frag('se.farm', 'sed.item_code', 'se.custom_greenhouse'), 'shelved')
            q("SELECT COALESCE(SUM(pli.stock_qty),0) s FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name=pli.parent AND pli.parenttype='Order Pick List' WHERE pli.issued=1 AND opl.date_created BETWEEN %(f)s AND %(t)s" + _frag('opl.farm', 'pli.item_code', None), 'issued')
            q("SELECT COALESCE(SUM(pli.stock_qty),0) s FROM `tabFarm Pack List` fpl JOIN `tabFarm Packlist Item` pli ON pli.parent=fpl.name AND pli.parenttype='Farm Pack List' AND pli.parentfield='pack_list_item' WHERE fpl.docstatus!=2 AND DATE(fpl.creation) BETWEEN %(f)s AND %(t)s" + _frag('pli.source_warehouse', 'pli.item_code', None), 'packed')
            q("SELECT COALESCE(SUM(bi.qty),0) s FROM `tabBox Label` bl JOIN `tabBox Label Item` bi ON bi.parent=bl.name JOIN `tabSales Order` so ON so.name=bl.customer_purchase_order WHERE bl.staged=1 AND so.docstatus=1 AND DATE(bl.date) BETWEEN %(f)s AND %(t)s AND so.status NOT IN ('Cancelled','Closed')" + _frag('bl.farm', 'bi.variety', None), 'staged')
            q("SELECT COALESCE(SUM(pli.stock_qty),0) s FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name=pli.parent AND pli.parenttype='Order Pick List' WHERE pli.loaded_in_trolley=1 AND opl.date_created BETWEEN %(f)s AND %(t)s" + _frag('opl.farm', 'pli.item_code', None), 'loaded')
            q("SELECT COALESCE(SUM(dni.stock_qty),0) s FROM `tabDelivery Note` dn JOIN `tabDelivery Note Item` dni ON dni.parent=dn.name WHERE dn.docstatus=1 AND dn.posting_date BETWEEN %(f)s AND %(t)s" + _frag('dni.farm', 'dni.item_code', None), 'dispatched')
            return tt

        from datetime import timedelta
        totals = _flows(today, today)

        # last 7 days of flows (for level-series reconstruction) + last 3 for the matrix
        day_flows = {}
        for i in range(7):
            di = str(d0 - timedelta(days=i))
            day_flows[di] = _flows(di, di)

        def _age(dtval):
            if not dtval:
                return None
            try:
                return round(frappe.utils.time_diff_in_hours(frappe.utils.now_datetime(), dtval), 1)
            except Exception:
                return None

        # ── live buffer levels + oldest age ──
        def one(sql, params=None):
            r = frappe.db.sql(sql, {**flt, **(params or {})}, as_dict=True)
            return r[0] if r else frappe._dict({"lvl": 0, "oldest": None})

        in_transit = one("""SELECT COALESCE(SUM(sed.qty),0) lvl, MIN(se.creation) oldest
            FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
            WHERE se.docstatus=1 AND se.stock_entry_type='Harvesting'
              AND se.posting_date>=DATE_SUB(CURDATE(),INTERVAL 2 DAY) AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id!=''
              AND NOT EXISTS(SELECT 1 FROM `tabStock Entry` rc WHERE rc.docstatus=1 AND rc.stock_entry_type IN ('Receiving','Late Receipt') AND rc.custom_bucket_id=se.custom_bucket_id AND rc.posting_date>=se.posting_date)""" + _frag('se.farm', 'sed.item_code', 'se.custom_greenhouse'))
        unshelved = one("""SELECT COALESCE(SUM(sed.qty),0) lvl, MIN(se.creation) oldest
            FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
            WHERE se.docstatus=1 AND se.stock_entry_type IN ('Receiving','Late Receipt')
              AND se.posting_date>=DATE_SUB(CURDATE(),INTERVAL 3 DAY) AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id!=''
              AND NOT EXISTS(SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id=se.custom_bucket_id AND si.stem_qty>0)
              AND NOT EXISTS(SELECT 1 FROM `tabStock Entry` d WHERE d.docstatus=1 AND d.stock_entry_type='Discard' AND d.custom_bucket_id=se.custom_bucket_id AND d.posting_date>=se.posting_date)
              AND NOT EXISTS(SELECT 1 FROM `tabPick List Item` pli WHERE pli.bucket=se.custom_bucket_id AND pli.issued=1)""" + _frag('se.farm', 'sed.item_code', 'se.custom_greenhouse'))
        on_shelf = one("""SELECT COALESCE(SUM(si.stem_qty),0) lvl, MIN(COALESCE(si.receiving_date,si.date_added)) oldest
            FROM `tabShelf` s JOIN `tabShelf Item` si ON s.name=si.parent WHERE si.stem_qty>0 AND si.variety IS NOT NULL AND TRIM(si.variety)!=''""" + _frag('s.farm', 'si.variety', None))
        pack_hall = one("""SELECT COALESCE(SUM(pli.stock_qty),0) lvl, MIN(opl.creation) oldest
            FROM `tabPick List Item` pli JOIN `tabOrder Pick List` opl ON opl.name=pli.parent AND pli.parenttype='Order Pick List'
            WHERE pli.issued=1 AND (pli.custom_box_label IS NULL OR pli.custom_box_label='') AND opl.date_created>=DATE_SUB(CURDATE(),INTERVAL 7 DAY)""" + _frag('opl.farm', 'pli.item_code', None))

        def box_buf(cond):
            return one("SELECT COALESCE(SUM(bi.qty),0) lvl, MIN(bl.creation) oldest FROM `tabBox Label` bl JOIN `tabBox Label Item` bi ON bi.parent=bl.name WHERE " + cond + " AND bl.date>=DATE_SUB(CURDATE(),INTERVAL 7 DAY)" + _frag('bl.farm', 'bi.variety', None))
        awaiting = box_buf("IFNULL(bl.staged,0)=0 AND IFNULL(bl.delivered,0)=0")
        staged_b = box_buf("IFNULL(bl.staged,0)=1 AND IFNULL(bl.loaded,0)=0 AND IFNULL(bl.delivered,0)=0")
        on_truck = box_buf("IFNULL(bl.loaded,0)=1 AND IFNULL(bl.delivered,0)=0")

        # ── On-shelf ledger from Shelving Log (the real reconciliation) ──
        def slq(sql, params):
            r = frappe.db.sql(sql, params, as_dict=True)
            return int((r[0].s or 0) if r and r[0].s is not None else 0)
        sh_shelved_in = slq("SELECT COALESCE(SUM(stem_qty),0) s FROM `tabShelving Log` WHERE DATE(shelved_on)=%(d)s", {"d": today})
        sh_issued_out = slq("SELECT COALESCE(SUM(stem_qty),0) s FROM `tabShelving Log` WHERE DATE(removed_on)=%(d)s AND (reason LIKE '%%Issue%%' OR reason LIKE '%%Pick%%' OR reason LIKE '%%Sales%%')", {"d": today})
        sh_discard_out = slq("SELECT COALESCE(SUM(stem_qty),0) s FROM `tabShelving Log` WHERE DATE(removed_on)=%(d)s AND NOT (reason LIKE '%%Issue%%' OR reason LIKE '%%Pick%%' OR reason LIKE '%%Sales%%' OR reason LIKE '%%Shelved%%')", {"d": today})
        sh_opening = slq("SELECT COALESCE(SUM(stem_qty),0) s FROM `tabShelving Log` WHERE shelved_on < %(m)s AND (removed_on IS NULL OR removed_on >= %(m)s)", {"m": morning})
        sh_now = int(on_shelf.lvl or 0)
        sh_open_oldest = one("SELECT MIN(shelved_on) oldest FROM `tabShelving Log` WHERE shelved_on < %(m)s AND (removed_on IS NULL OR removed_on >= %(m)s)", {"m": morning}).oldest
        # hourly step across today, reconstructed from shelved_on(+) / removed_on(−) events
        step = []
        run = sh_opening
        events = frappe.db.sql("""SELECT HOUR(shelved_on) h, SUM(stem_qty) q, 'in' k FROM `tabShelving Log` WHERE DATE(shelved_on)=%(d)s GROUP BY HOUR(shelved_on)
            UNION ALL SELECT HOUR(removed_on) h, SUM(stem_qty) q, 'out' k FROM `tabShelving Log` WHERE DATE(removed_on)=%(d)s GROUP BY HOUR(removed_on)""", {"d": today}, as_dict=True)
        delta = {}
        for e in events:
            hh = int(e.h if e.h is not None else 0)
            delta[hh] = delta.get(hh, 0) + (int(e.q or 0) if e.k == 'in' else -int(e.q or 0))
        for hh in range(6, 19):
            run = run + delta.get(hh, 0)
            step.append({"t": "%02d" % hh, "v": run})
        # age bands of what's still on the shelf
        bands_raw = frappe.db.sql("""SELECT CASE
                WHEN TIMESTAMPDIFF(HOUR, COALESCE(si.receiving_date, si.date_added), NOW()) < 24 THEN 0
                WHEN TIMESTAMPDIFF(HOUR, COALESCE(si.receiving_date, si.date_added), NOW()) < 48 THEN 1
                WHEN TIMESTAMPDIFF(HOUR, COALESCE(si.receiving_date, si.date_added), NOW()) < 72 THEN 2 ELSE 3 END band,
              COALESCE(SUM(si.stem_qty),0) q
            FROM `tabShelf` s JOIN `tabShelf Item` si ON s.name=si.parent WHERE si.stem_qty>0""" + _frag('s.farm', 'si.variety', None) + " GROUP BY band", flt, as_dict=True)
        bmap = {int(b.band): int(b.q or 0) for b in bands_raw}
        age_bands = [
            {"label": "< 24 h", "qty": bmap.get(0, 0), "tone": "ok"},
            {"label": "24–48 h", "qty": bmap.get(1, 0), "tone": "warn"},
            {"label": "48–72 h", "qty": bmap.get(2, 0), "tone": "bad"},
            {"label": "> 72 h", "qty": bmap.get(3, 0), "tone": "bad"},
        ]
        discard_reasons = [{"reason": r.reason, "qty": int(r.q or 0)} for r in frappe.db.sql(
            "SELECT reason, SUM(stem_qty) q FROM `tabShelving Log` WHERE DATE(removed_on)=%(d)s AND NOT (reason LIKE '%%Issue%%' OR reason LIKE '%%Shelved%%' OR reason LIKE '%%Pick%%' OR reason LIKE '%%Sales%%') GROUP BY reason ORDER BY q DESC", {"d": today}, as_dict=True)]

        # ── assemble the 7 buffers ──
        def build_buf(key, name, gate, live_row, gin, gout_list):
            live = int(live_row.lvl or 0)
            inflow = [{"label": g, "qty": totals.get(g, 0)} for g in [gin]]
            outflow = [{"label": lbl, "qty": qty} for (lbl, qty) in gout_list]
            in_q = sum(x["qty"] for x in inflow)
            out_q = sum(x["qty"] for x in outflow)
            # opening: reconstructed from the identity, but a real buffer cannot have
            # started negative — so clamp at 0. When live − in + out < 0 the flows
            # can't be reconciled with any non-negative opening, and the board's
            # equation-vs-live check then surfaces it as a break (the honest signal).
            # The shelf overrides this with its true Shelving-Log opening.
            opening = max(0, live - in_q + out_q)
            # 7-day level series, reconstructed backward from the live level using
            # daily net (in − out). Range = its own 7-day min/max.
            days_sorted = [str(d0 - timedelta(days=i)) for i in range(6, -1, -1)]
            net_by_day = []
            for di in days_sorted:
                dfin = day_flows[di].get(gin, 0)
                dfout = sum(day_flows[di].get(l, 0) if l in STAGES else 0 for (l, _q) in gout_list)
                net_by_day.append(dfin - dfout)
            series = [0] * 7
            series[6] = live
            for i in range(5, -1, -1):
                series[i] = max(0, series[i + 1] - net_by_day[i + 1])
            rng = [min(series), max(series)]
            return {"key": key, "name": name, "gate": gate,
                    "opening": opening, "inflow": inflow, "outflow": outflow,
                    "live": live, "oldest_h": _age(live_row.oldest),
                    "range7": rng, "series7": series}

        buffers = [
            build_buf("in_transit", "From the field", "Cut, not yet received", in_transit, "harvested", [("received", totals["received"])]),
            build_buf("unshelved", "In the cold store", "Received, not yet shelved", unshelved, "received", [("shelved", totals["shelved"])]),
            build_buf("on_shelf", "On the shelf", "Shelved, not yet issued", on_shelf, "shelved", [("issued", totals["issued"]), ("discarded", sh_discard_out)]),
            build_buf("pack_hall", "In the packhouse", "Issued, not yet packed", pack_hall, "issued", [("packed", totals["packed"])]),
            build_buf("awaiting_stage", "Packed, waiting to stage", "Packed, not yet staged", awaiting, "packed", [("staged", totals["staged"])]),
            build_buf("staged", "Staged for dispatch", "Staged, not yet loaded", staged_b, "staged", [("loaded", totals["loaded"])]),
            build_buf("on_truck", "On the truck", "Loaded, not yet dispatched", on_truck, "loaded", [("dispatched", totals["dispatched"])]),
        ]
        # shelf carries its real ledger opening + reasons
        for b in buffers:
            if b["key"] == "on_shelf":
                b["opening"] = sh_opening
                b["outflow"] = [{"label": "issued", "qty": sh_issued_out}, {"label": "discarded", "qty": sh_discard_out}]

        held = sum(max(0, b["live"]) for b in buffers)

        # Opening/in/out come from the Shelving Log; "now" is the live Shelf Item
        # count. They can drift when stock leaves the shelf (or a count is edited)
        # without a matching log row — so carry the ledger-reconciled total and the
        # unreconciled variance explicitly, and let the waterfall show it as its own
        # step instead of an unexplained jump into "on hand".
        sh_reconciled = sh_opening + sh_shelved_in - sh_issued_out - sh_discard_out
        shelf = {
            "opening": sh_opening, "shelved_in": sh_shelved_in, "issued_out": sh_issued_out,
            "discarded_out": sh_discard_out, "now": sh_now,
            "reconciled": sh_reconciled, "variance": sh_now - sh_reconciled,
            "opening_oldest_h": _age(sh_open_oldest), "now_oldest_h": _age(on_shelf.oldest),
            "buckets_in": slq("SELECT COUNT(DISTINCT bucket_id) s FROM `tabShelving Log` WHERE DATE(shelved_on)=%(d)s", {"d": today}),
            "age_bands": age_bands, "threshold_h": 48, "step": step, "discard_reasons": discard_reasons,
        }

        # ── last 3 days matrix ──
        days = []
        for i in (2, 1, 0):
            di = str(d0 - timedelta(days=i))
            days.append({"label": frappe.utils.getdate(di).strftime("%a %d"), "today": (i == 0), "t": day_flows[di]})

        # ── today's cohort followed forward ──
        crow = frappe.db.sql("""
            SELECT COALESCE(SUM(h.stems),0) harvested,
              COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM `tabStock Entry` rc WHERE rc.docstatus=1 AND rc.stock_entry_type IN ('Receiving','Late Receipt') AND rc.custom_bucket_id=h.bucket AND rc.posting_date>=h.hdate) THEN h.stems ELSE 0 END),0) received,
              COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM `tabShelving Log` sl WHERE sl.bucket_id=h.bucket AND sl.shelved_on>=h.hdate) THEN h.stems ELSE 0 END),0) shelved,
              COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name=pli.parent WHERE pli.bucket=h.bucket AND pli.issued=1 AND o.date_created>=h.hdate) THEN h.stems ELSE 0 END),0) issued,
              COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM `tabPick List Item` p2 JOIN `tabOrder Pick List` o2 ON o2.name=p2.parent WHERE p2.bucket=h.bucket AND p2.custom_box_label IS NOT NULL AND p2.custom_box_label<>'' AND o2.date_created>=h.hdate) THEN h.stems ELSE 0 END),0) packed
            FROM (SELECT se.custom_bucket_id bucket, SUM(sed.qty) stems, MIN(se.posting_date) hdate
              FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
              WHERE se.docstatus=1 AND se.stock_entry_type='Harvesting' AND se.posting_date=%(d)s
                AND se.custom_bucket_id IS NOT NULL AND se.custom_bucket_id<>'' """ + _frag('se.farm', 'sed.item_code', 'se.custom_greenhouse') + """ GROUP BY se.custom_bucket_id) h
        """, {**flt, "d": today}, as_dict=True)[0]
        reached = {"harvested": int(crow.harvested or 0), "received": int(crow.received or 0), "shelved": int(crow.shelved or 0),
                   "issued": int(crow.issued or 0), "packed": int(crow.packed or 0), "staged": 0, "loaded": 0, "dispatched": 0}
        held_c = {}
        for a, b in [("received", "harvested"), ("shelved", "received"), ("issued", "shelved"), ("packed", "issued")]:
            held_c[a] = max(0, reached[b] - reached[a])
        cohort = {"cut": reached["harvested"], "reached": reached, "held": held_c}

        # ── filter option lists (last 60 days of movement) for the topbar dropdowns ──
        def _distinct(col, sql):
            return [r.get('v') for r in frappe.db.sql(sql, as_dict=True) if r.get('v')]
        farm_opts = _distinct('farm', """SELECT DISTINCT farm v FROM `tabStock Entry`
            WHERE stock_entry_type IN ('Harvesting','Receiving','Late Receipt')
              AND posting_date >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
              AND farm IS NOT NULL AND TRIM(farm) <> '' ORDER BY farm""")
        var_opts = _distinct('variety', """SELECT DISTINCT sed.item_code v
            FROM `tabStock Entry` se JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.stock_entry_type IN ('Harvesting','Receiving','Late Receipt')
              AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
              AND sed.item_code IS NOT NULL AND TRIM(sed.item_code) <> '' ORDER BY sed.item_code""")
        loc_opts = _distinct('location', """SELECT DISTINCT custom_greenhouse v FROM `tabStock Entry`
            WHERE stock_entry_type IN ('Harvesting','Receiving','Late Receipt')
              AND posting_date >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
              AND custom_greenhouse IS NOT NULL AND TRIM(custom_greenhouse) <> '' ORDER BY custom_greenhouse""")

        board = {
            "as_of": frappe.utils.now_datetime().strftime("%a %d %b %Y, %H:%M"),
            "window": "06:00 → now",
            "totals": totals, "buffers": buffers, "shelf": shelf, "days": days, "cohort": cohort,
            "options": {"farms": farm_opts, "varieties": var_opts, "locations": loc_opts},
            "filters": {"farm": flt_farm, "variety": flt_var, "location": flt_loc},
            "filtered": bool(flt_farm or flt_var or flt_loc),
        }
        frappe.response['message'] = {"success": True, "board": board}
    except Exception as e:
        frappe.log_error("getPackhouseFlowBoard error: " + str(e))
        frappe.response['message'] = {"success": False, "error": str(e)}
