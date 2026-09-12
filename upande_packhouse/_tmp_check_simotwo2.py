import frappe
def run():
    rows = frappe.db.sql("""
        SELECT se.custom_greenhouse, sei.item_code, se.custom_harvester, se.name
        FROM `tabStock Entry` se
        JOIN `tabStock Entry Detail` sei ON sei.parent = se.name
        WHERE se.stock_entry_type = 'Harvesting' AND se.custom_greenhouse LIKE 'Simotwo%'
        ORDER BY se.creation DESC LIMIT 5
    """, as_dict=True)
    for r in rows:
        print(r)
