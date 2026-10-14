# ruff: noqa: F821 -- Server Script body: `frappe` is the sandbox global, never imported.
# Server Script (API, POST) -- api_method: pack_while_waiting_replacement
# Source copy of the live Server Script "Pack While Waiting Replacement" (kaitet-group).
#
# Issuing: an order at least PCT% issued whose other buckets the farm marked not found,
# with no replacement found yet, goes to packing now with what was issued, and the team
# moves on to its next order. It is NOT recorded short (Complete short does that): when
# a replacement is found it is issued to the order and packed into the rest of its boxes.
# Payload: opl_name. Reply (frappe.response["message"]): success, message, issued_pct.
# RestrictedPython: no imports, no tuple unpacking, no names starting with "_", no
# str.format.

opl_name = frappe.form_dict.get("opl_name")
# Production Settings > Pack While Waiting for a Replacement From (%); 50 until set.
# nosemgrep: frappe-breaks-multitenancy -- Server Script body, run per request
pct_setting = frappe.get_doc("Production Settings").get("pack_waiting_replacement_pct")
min_pct = frappe.utils.flt(pct_setting) if pct_setting not in (None, "") else 50.0
out = {"success": False}


def check(opl):
	"""Why `opl` can't go to packing yet (None = it can), its issued %, its not-found rows."""
	rows = [r for r in opl.table_ytkc if r.bucket]
	missing = [r for r in rows if r.not_found]
	moving = sorted(
		set(
			[
				r.bucket
				for r in rows
				if not r.not_found
				and not r.issued
				and (r.awaiting_transfer or r.loaded_in_trolley or r.in_transit)
			]
		)
	)
	open_rows = sorted(set([r.bucket for r in rows if not r.not_found and not r.issued]))
	total = sum([frappe.utils.flt(r.stock_qty) for r in rows])
	issued = sum([frappe.utils.flt(r.stock_qty) for r in rows if r.issued])
	pct = round(100.0 * issued / total) if total else 0
	name = opl.order_name or opl.name
	if opl.docstatus == 2:
		return [name + " is cancelled.", pct, missing]
	if opl.get("short_accepted"):
		return [name + " is already completed short.", pct, missing]
	if not missing:
		return ["No bucket of " + name + " is marked not found.", pct, missing]
	if moving:
		return ["Bucket(s) " + ", ".join(moving) + " are still coming by truck.", pct, missing]
	if open_rows:
		return ["Issue bucket(s) " + ", ".join(open_rows) + " first.", pct, missing]
	if pct < min_pct:
		return [
			"Only " + str(pct) + "% is issued; packing can start from " + str(int(min_pct)) + "%.",
			pct,
			missing,
		]
	return [None, pct, missing]


# nosemgrep: frappe-breaks-multitenancy -- Server Script body, run per request
if not opl_name or not frappe.db.exists("Order Pick List", opl_name):
	out["message"] = "Order Pick List not found."
else:
	# nosemgrep: frappe-breaks-multitenancy -- Server Script body, run per request
	opl = frappe.get_doc("Order Pick List", opl_name)
	verdict = check(opl)
	out["issued_pct"] = verdict[1]
	if verdict[0]:
		out["message"] = verdict[0]
	else:
		submit_error = None
		if opl.docstatus == 0:
			opl.flags.ignore_permissions = True
			opl.flags.ignore_links = True
			try:
				opl.submit()
			except Exception as e:
				submit_error = frappe.utils.strip_html(str(e))
		if submit_error:
			out["message"] = submit_error
		else:
			opl.add_comment(
				"Info",
				"Packing while waiting for a replacement: "
				+ str(verdict[1])
				+ "% issued, not found "
				+ ", ".join(sorted(set([r.bucket for r in verdict[2]]))),
			)
			out["success"] = True
			out["message"] = (
				(opl.order_name or opl_name)
				+ " goes to packing with "
				+ str(verdict[1])
				+ "% issued. Replace the not-found bucket(s) when stock is found."
			)

frappe.response["message"] = out
