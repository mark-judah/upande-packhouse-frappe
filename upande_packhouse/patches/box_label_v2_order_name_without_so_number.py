import frappe

OLD = '{% set order_label = (so.custom_order_name if so and so.get("custom_order_name") else None) or doc.custom_order_name or doc.order_name or "" %}'
NEW = """{% set _on = ((so.custom_order_name if so and so.get("custom_order_name") else None) or doc.custom_order_name or doc.order_name or "") | string %}
{# the order name without the sales order's number at its end: "SQ SHARBATLY - RIYADH 44526-03584" -> "SQ SHARBATLY - RIYADH 44526" #}
{% set _cut = _on.rsplit("-", 1) %}
{% set order_label = (_cut[0] | trim) if (_cut | length == 2 and _cut[1] | length == 5 and _cut[1].isdigit() and (_cut[0] | trim)) else _on %}"""


def execute():
	"""Box Label V2 (the print format labels print with, kept in the database) prints the order name without the
	sales order's number at its end. Only that line changes; a format edited since is left alone."""
	if not frappe.db.exists("Print Format", "Box Label V2"):
		return
	html = frappe.db.get_value("Print Format", "Box Label V2", "html") or ""
	if OLD not in html:
		return
	frappe.db.set_value("Print Format", "Box Label V2", "html", html.replace(OLD, NEW, 1))
