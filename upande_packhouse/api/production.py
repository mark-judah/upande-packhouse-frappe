# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Packhouse `packhouse-production` page API — ported verbatim from DB Server Scripts.
# Bodies keep frappe.form_dict / frappe.response as the live scripts used.

import frappe


@frappe.whitelist()
def get_packhouse_production_by_variety():
    # Packhouse dashboard — Production by Variety (per stem length, all farms)
    # Harvest production lives in Stock Entry (stock_entry_type='Harvesting', docstatus=1):
    #   stems = sed.qty, variety = sed.item_code/item_name, stem length = se.custom_stem_length,
    #   farm = se.custom_farm, date = se.posting_date.
    from_date = frappe.form_dict.get('from_date') or frappe.utils.today()
    to_date = frappe.form_dict.get('to_date') or frappe.utils.today()
    rose_type = (frappe.form_dict.get('rose_type') or 'all').lower()

    params = {'from_date': from_date, 'to_date': to_date}
    ig_cond = ''
    if rose_type == 'standard':
        ig_cond = ' AND i.item_group = %(ig)s'
        params['ig'] = 'Standard Roses'
    elif rose_type == 'spray':
        ig_cond = ' AND i.item_group = %(ig)s'
        params['ig'] = 'Spray Roses'

    # Location filter: Karen = the Karen farm, Ravine = the remote farms. Resolved from
    # Farm.custom_location so it stays correct as farms are added/re-assigned.
    location = frappe.form_dict.get('location') or ''
    loc_cond = ''
    if location:
        loc_cond = " AND se.custom_farm IN (SELECT name FROM `tabFarm` WHERE custom_location = %(location)s)"
        params['location'] = location

    rows = frappe.db.sql("""
        SELECT
            sed.item_code AS item_code,
            i.item_name AS item_name,
            COALESCE(NULLIF(se.custom_stem_length, ''), 'No Length') AS stem_length,
            COALESCE(NULLIF(se.custom_farm, ''), 'Unknown') AS farm,
            COALESCE(SUM(sed.qty), 0) AS stems
        FROM `tabStock Entry` se
        JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
        LEFT JOIN `tabItem` i ON i.item_code = sed.item_code
        WHERE se.docstatus = 1
          AND se.stock_entry_type = 'Harvesting'
          AND se.posting_date BETWEEN %(from_date)s AND %(to_date)s
    """ + ig_cond + loc_cond + """
        GROUP BY sed.item_code, COALESCE(NULLIF(se.custom_stem_length, ''), 'No Length'),
                 COALESCE(NULLIF(se.custom_farm, ''), 'Unknown')
    """, params, as_dict=True)

    vmap = {}
    total = 0
    for r in rows:
        ic = r.item_code or 'Unknown'
        stems = r.stems or 0
        total = total + stems
        if ic not in vmap:
            vmap[ic] = {'item_code': ic, 'item_name': r.item_name or ic, 'total_stems': 0, 'lengths': {}, 'farms': {}}
        vmap[ic]['total_stems'] = vmap[ic]['total_stems'] + stems
        sl = r.stem_length or 'No Length'
        vmap[ic]['lengths'][sl] = vmap[ic]['lengths'].get(sl, 0) + stems
        fm = r.farm or 'Unknown'
        vmap[ic]['farms'][fm] = vmap[ic]['farms'].get(fm, 0) + stems

    def sl_key(s):
        digits = ''.join(ch for ch in str(s) if ch.isdigit())
        return int(digits) if digits else 99999

    varieties = []
    for v in vmap.values():
        lengths = [{'stem_length': k, 'stems': val} for k, val in v['lengths'].items()]
        lengths.sort(key=lambda x: sl_key(x['stem_length']))
        v['lengths'] = lengths
        farms = [{'farm': k, 'stems': val} for k, val in v['farms'].items()]
        farms.sort(key=lambda x: x['stems'], reverse=True)
        v['farms'] = farms
        varieties.append(v)
    varieties.sort(key=lambda x: x['total_stems'], reverse=True)

    frappe.response['message'] = {
        'success': True,
        'from_date': from_date,
        'to_date': to_date,
        'rose_type': rose_type,
        'total_stems': total,
        'variety_count': len(varieties),
        'varieties': varieties,
    }
