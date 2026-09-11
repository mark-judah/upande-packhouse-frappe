import frappe
import json

def run():
    ws_sidebars = frappe.get_all("Workspace Sidebar", filters={"title": ["like", "%ackhouse%"]}, fields=["name", "title"])
    print("Sidebars matching packhouse:", ws_sidebars)
    for row in ws_sidebars:
        doc = frappe.get_doc("Workspace Sidebar", row.name)
        print("\n--- Sidebar:", doc.name, "title=", doc.title, "---")
        for item in doc.items:
            print(" ", item.idx, item.type, "child=", item.child, "label=", getattr(item, "label", None) or getattr(item, "link_to", None), "link_type=", getattr(item, "link_type", None), "link_to=", getattr(item, "link_to", None))
