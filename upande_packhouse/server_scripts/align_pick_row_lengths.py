# ruff: noqa: F821 -- Server Script body: `frappe` is the sandbox global, never imported.
# Server Script (API, POST) -- api_method: align_pick_row_lengths
# Source copy of the live Server Script "Align Pick Row Lengths" (kaitet-group).
#
# Interim fix for packing refusing "<variety> (62cm) is already fully packed" with the
# order still short. A longer bucket allocated or swapped in to fill an order line keeps
# its own length on its pick row (72cm on a 62cm line), and the packing app then splits
# the one order line in two and fills only the first. This sets such ISSUED pick rows to
# the order line's length (Sales Order Item.custom_length) -- only on lines that are
# split (another row of the same line already has the order's length) -- so packing sees
# one line with the full quantity. The server already records the packs at the order's
# length. Each change is written as a comment on the OPL (old -> new) so it can be undone.
# Payload: opl_name (optional; default every open OPL delivering today or later),
# dry_run (1 = only report). Reply (frappe.response["message"]): success, changed rows.
# RestrictedPython: no imports, no tuple unpacking, no names starting with "_", no
# str.format.

opl_name = frappe.form_dict.get("opl_name")
dry_run = frappe.utils.cint(frappe.form_dict.get("dry_run"))

cond = "o.name = %(opl)s" if opl_name else "so.delivery_date >= CURDATE()"
# nosemgrep: frappe-breaks-multitenancy -- Server Script body, run per request
rows = frappe.db.sql(
	"""SELECT pli.name, pli.parent, pli.bucket, pli.item_code, pli.stem_length,
	       soi.custom_length AS so_length
	FROM `tabPick List Item` pli
	JOIN `tabOrder Pick List` o ON o.name = pli.parent AND o.docstatus < 2
	JOIN `tabSales Order` so ON so.name = o.sales_order
	JOIN `tabSales Order Item` soi ON soi.name = pli.sales_order_item
	WHERE pli.parenttype = 'Order Pick List' AND IFNULL(pli.issued, 0) = 1
	  AND IFNULL(pli.not_found, 0) = 0 AND IFNULL(soi.custom_length, '') != ''
	  AND IFNULL(pli.stem_length, '') != soi.custom_length
	  AND EXISTS (SELECT 1 FROM `tabPick List Item` p2
	              WHERE p2.parent = pli.parent AND p2.sales_order_item = pli.sales_order_item
	                AND p2.stem_length = soi.custom_length)
	  AND """
	+ cond,
	{"opl": opl_name or ""},
	as_dict=True,
)

changed = []
by_opl = {}
for r in rows:
	changed.append(
		{
			"opl": r.parent,
			"row": r.name,
			"bucket": r.bucket,
			"item_code": r.item_code,
			"from": r.stem_length,
			"to": r.so_length,
		}
	)
	by_opl.setdefault(r.parent, []).append(r)
	if not dry_run:
		frappe.db.set_value("Pick List Item", r.name, "stem_length", r.so_length, update_modified=False)

if not dry_run:
	for opl in by_opl:
		lines = [
			str(r.bucket) + " " + str(r.item_code) + " " + str(r.stem_length) + " -> " + str(r.so_length)
			for r in by_opl[opl]
		]
		frappe.get_doc("Order Pick List", opl).add_comment(
			"Info", "Pick row length set to the order line's for packing: " + "; ".join(lines)
		)

frappe.response["message"] = {
	"success": True,
	"dry_run": dry_run,
	"count": len(changed),
	"changed": changed,
}
