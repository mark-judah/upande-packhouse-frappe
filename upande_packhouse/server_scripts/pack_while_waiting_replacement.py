# ruff: noqa: F821 -- Server Script body: `frappe` is the sandbox global, never imported.
# Server Script (API, POST) -- api_method: pack_while_waiting_replacement
# Source copy of the live Server Script "Pack While Waiting Replacement" (kaitet-group).
#
# Issuing: an order at least PCT% issued goes to packing now with what was issued, and
# the team moves on to its next order. Nothing is marked not found: the buckets not
# issued yet stay as they are and are issued to the order when they turn up. The order
# is flagged Packing What Was Issued (Order Pick List.pack_issued_now) so packing lists
# it, and packing stops at the stems issued until more is issued; the order is NOT
# recorded short (Complete short does that).
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
	"""Why `opl` can't go to packing yet (None = it can), and its issued %."""
	rows = [r for r in opl.table_ytkc if r.bucket]
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
	open_rows = [r for r in rows if not r.not_found and not r.issued]
	total = sum([frappe.utils.flt(r.stock_qty) for r in rows])
	issued = sum([frappe.utils.flt(r.stock_qty) for r in rows if r.issued])
	pct = round(100.0 * issued / total) if total else 0
	name = opl.order_name or opl.name
	if opl.docstatus == 2:
		return [name + " is cancelled.", pct]
	if opl.get("short_accepted"):
		return [name + " is already completed short.", pct]
	if opl.get("pack_issued_now"):
		return [name + " is already packing what was issued.", pct]
	if moving:
		return ["Bucket(s) " + ", ".join(moving) + " are still coming by truck.", pct]
	if not open_rows:
		return [name + " is fully issued.", pct]
	if pct < min_pct:
		return [
			"Only " + str(pct) + "% is issued; packing can start from " + str(int(min_pct)) + "%.",
			pct,
		]
	return [None, pct]


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
			frappe.db.set_value("Order Pick List", opl_name, "pack_issued_now", 1, update_modified=False)
			opl.add_comment(
				"Info",
				"Packing what was issued: "
				+ str(verdict[1])
				+ "% issued; the rest is issued to the order as it is found.",
			)
			out["success"] = True
			out["message"] = (
				(opl.order_name or opl_name)
				+ " goes to packing with "
				+ str(verdict[1])
				+ "% issued. Issue the rest as it is found."
			)

frappe.response["message"] = out
