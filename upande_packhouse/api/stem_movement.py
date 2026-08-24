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
                          AND se.custom_farm = %(farm)s
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
            SELECT se.custom_bucket_id AS bid, se.custom_farm AS farm, se.custom_greenhouse AS gh,
                   se.custom_stem_length AS ln, sed.item_code AS variety,
                   COALESCE(SUM(sed.qty),0) AS stems, MIN(se.posting_date) AS d
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1 AND se.stock_entry_type = 'Harvesting'
              AND se.posting_date BETWEEN %(f)s AND %(t)s
              AND se.custom_bucket_id IS NOT NULL AND TRIM(se.custom_bucket_id) != ''
            GROUP BY se.custom_bucket_id, se.custom_farm, se.custom_greenhouse, se.custom_stem_length, sed.item_code
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
            SELECT se.custom_bucket_id AS bid, se.custom_farm AS farm, se.custom_greenhouse AS gh,
                   se.custom_stem_length AS ln, se.stock_entry_type AS t, sed.item_code AS variety,
                   COALESCE(SUM(sed.qty),0) AS stems, MIN(se.posting_date) AS d
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1 AND se.stock_entry_type IN ('Receiving','Late Receipt')
              AND se.posting_date BETWEEN %(f)s AND %(t)s
              AND se.custom_bucket_id IS NOT NULL AND TRIM(se.custom_bucket_id) != ''
            GROUP BY se.custom_bucket_id, se.custom_farm, se.custom_greenhouse, se.custom_stem_length, se.stock_entry_type, sed.item_code
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

        # ── Harvested: Stock Entry (Harvesting) ──
        supply = frappe.db.sql("""
            SELECT se.custom_farm AS farm, sed.item_code AS variety, COALESCE(SUM(sed.qty), 0) AS stems
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1
              AND se.posting_date BETWEEN %(f)s AND %(t)s
              AND se.stock_entry_type = 'Harvesting'
              AND (%(ln)s = '' OR se.custom_stem_length = %(ln)s)
            GROUP BY se.custom_farm, sed.item_code
        """, params, as_dict=True)
        for r in supply:
            add(r.farm, r.variety, 'harvested', r.stems)

        # ── Received + Shelved on the SAME bucket cohort ──
        # Received = Receiving/Late Receipt stems whose posting_date is in the window.
        # Shelved  = of those SAME received buckets, the ones that have a Shelving Log
        #            row dated ON/AFTER this receipt (sl.shelved_on >= se.posting_date)
        #            and by end of window. The ">= posting_date" guard is essential:
        #            bucket codes are physical containers REUSED across days, so a bare
        #            bucket_id match also hits a PRIOR batch's shelving of the same code
        #            (that inflated Shelved above Received). Tying the shelving to this
        #            receipt makes Shelved <= Received real and "Not Shelved" =
        #            Received - Shelved correct, matching the By-Bucket view.
        recv = frappe.db.sql("""
            SELECT se.custom_farm AS farm, sed.item_code AS variety,
                   COALESCE(SUM(sed.qty), 0) AS received,
                   COALESCE(SUM(CASE WHEN EXISTS (
                       SELECT 1 FROM `tabShelving Log` sl
                       WHERE sl.bucket_id = se.custom_bucket_id
                         AND sl.shelved_on >= se.posting_date
                         AND sl.shelved_on <= %(endt)s
                   ) THEN sed.qty ELSE 0 END), 0) AS shelved
            FROM `tabStock Entry` se
            INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
            WHERE se.docstatus = 1
              AND se.posting_date BETWEEN %(f)s AND %(t)s
              AND se.stock_entry_type IN ('Receiving', 'Late Receipt')
              AND (%(ln)s = '' OR se.custom_stem_length = %(ln)s)
            GROUP BY se.custom_farm, sed.item_code
        """, params, as_dict=True)
        for r in recv:
            add(r.farm, r.variety, 'received', r.received)
            add(r.farm, r.variety, 'shelved', r.shelved)

        # ── Issued: Pick List Item custom_issued=1 on an OPL created in the window.
        #    Issuing is an OPL-lifecycle event, so it's scoped by the OPL's own
        #    date_created (like the Loaded transfer stage), NOT Sales Order
        #    delivery_date — it must reflect issuing activity on the selected date. ──
        issued = frappe.db.sql("""
            SELECT pli.warehouse AS wh, pli.item_code AS variety, COALESCE(SUM(pli.stock_qty), 0) AS stems
            FROM `tabPick List Item` pli
            INNER JOIN `tabOrder Pick List` opl
                ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
            WHERE pli.issued = 1
              AND opl.date_created BETWEEN %(f)s AND %(t)s
            GROUP BY pli.warehouse, pli.item_code
        """, params, as_dict=True)
        for r in issued:
            add(r.wh, r.variety, 'issued', r.stems)

        # ── Packed: Farm Pack List -> pack_list_item rows (Dispatch Form Item child),
        #    stems from stock_qty. Scoped by the FPL's pack date
        #    (fpl.creation; Farm Pack List has no date field of its own), NOT Sales
        #    Order delivery_date — so it counts what was actually packed on the
        #    selected date (verified to match the packhouse dashboard's figure). ──
        packed = frappe.db.sql("""
            SELECT pli.source_warehouse AS wh, pli.item_code AS variety,
                   COALESCE(SUM(pli.stock_qty), 0) AS stems
            FROM `tabFarm Pack List` fpl
            INNER JOIN `tabFarm Packlist Item` pli
                ON pli.parent = fpl.name AND pli.parenttype = 'Farm Pack List' AND pli.parentfield = 'pack_list_item'
            WHERE fpl.docstatus != 2
              AND DATE(fpl.creation) BETWEEN %(f)s AND %(t)s
              AND (%(ln)s = '' OR pli.stem_length = %(ln)s)
            GROUP BY pli.source_warehouse, pli.item_code
        """, params, as_dict=True)
        for r in packed:
            add(r.wh, r.variety, 'packed', r.stems)

        # ── Staged: Box Label staged=1, dated in the window by the Box Label's own
        #    `date` (staging activity), NOT Sales Order delivery_date. Stems from box
        #    items; SO join kept only to drop cancelled/closed orders. ──
        staged = frappe.db.sql("""
            SELECT bl.farm AS wh, bi.variety AS variety, COALESCE(SUM(bi.qty), 0) AS stems
            FROM `tabBox Label` bl
            INNER JOIN `tabBox Label Item` bi ON bi.parent = bl.name
            INNER JOIN `tabSales Order` so ON so.name = bl.customer_purchase_order
            WHERE bl.staged = 1 AND so.docstatus = 1
              AND DATE(bl.date) BETWEEN %(f)s AND %(t)s
              AND so.status NOT IN ('Cancelled', 'Closed')
              AND (%(ln)s = '' OR bi.length = %(ln)s)
            GROUP BY bl.farm, bi.variety
        """, params, as_dict=True)
        for r in staged:
            add(r.wh, r.variety, 'staged', r.stems)

        # ── Loaded: OPL transfer — Pick List Item rows loaded onto a trolley/truck
        #    (custom_loaded_in_trolley = 1), stems by stock_qty, scoped by the OPL's
        #    creation date (transfers happen the day the OPL is created). This tracks
        #    the farm→central transfer, NOT Box Label dispatch loading, so a day with
        #    no OPL transfers shows zero. ──
        loaded = frappe.db.sql("""
            SELECT opl.farm AS wh, pli.item_code AS variety,
                   COALESCE(SUM(pli.stock_qty), 0) AS stems
            FROM `tabPick List Item` pli
            INNER JOIN `tabOrder Pick List` opl
                ON pli.parent = opl.name AND pli.parenttype = 'Order Pick List'
            WHERE pli.loaded_in_trolley = 1
              AND opl.date_created BETWEEN %(f)s AND %(t)s
            GROUP BY opl.farm, pli.item_code
        """, params, as_dict=True)
        for r in loaded:
            add(r.wh, r.variety, 'loaded', r.stems)

        # ── Dispatched: Dispatch Form Item (own child), by Dispatch Form custom_date ──
        dispatched = frappe.db.sql("""
            SELECT dni.farm AS farm, dni.item_code AS variety,
                   COALESCE(SUM(dni.stock_qty), 0) AS stems
            FROM `tabDelivery Note` dn
            INNER JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
            WHERE dn.docstatus = 1 AND dn.posting_date BETWEEN %(f)s AND %(t)s
            GROUP BY dni.farm, dni.item_code
        """, params, as_dict=True)
        for r in dispatched:
            add(r.farm, r.variety, 'dispatched', r.stems)

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
