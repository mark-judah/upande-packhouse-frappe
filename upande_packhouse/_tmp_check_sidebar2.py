import frappe

def run():
    doc = frappe.get_doc("Workspace Sidebar", "Packhouse")
    for item in doc.items:
        if item.type == "Link" and item.link_type == "URL":
            print(item.idx, item.label, "url=", getattr(item, "url", None), "icon=", getattr(item, "icon", None))
