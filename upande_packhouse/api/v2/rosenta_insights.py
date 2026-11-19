# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Rosenta against ERPNext: money, greenhouse revenue and fill rate. TEMPORARY, with the rest.

Rosenta is read from the snapshots api/v2/rosenta_sync.py keeps, never live, so any number of people can
look without Rosenta noticing. ERPNext is read live (it is ours). All three take a range of delivery dates.

Money      booked -> dispatched -> invoiced -> paid, and what was dispatched but not yet invoiced.
           ERPNext counts the flower business only (Sales Order business unit "Roses"); its other
           businesses are not in this comparison.
Greenhouse revenue of the stems issued, by farm (Rosenta also by greenhouse); ERPNext by farm from the
           pick lists, since a pick row knows its farm but not its greenhouse.
Fill       ordered -> allocated -> issued -> packed -> loaded -> dispatched, per customer.

Any amount is kept per currency and never added across currencies.
"""

import frappe
from frappe import _

from upande_packhouse.api.v2 import orders_workflow
from upande_packhouse.api.v2 import rosenta_compare as rc
from upande_packhouse.api.v2 import rosenta_sync as sync

BUSINESS_UNIT = "Roses"
CANCELLED = ("cancelled", "void", "canceled")


def _cur(x):
	return str(x or "").upper()


def _add(d, cur, v):
	d[cur] = d.get(cur, 0) + v


def _cash(by_currency):
	"""Amounts by currency as {by_currency, currency} for the page."""
	m = rc._money({c: v for c, v in by_currency.items()}, 0)
	return {"by_currency": m["by_currency"], "currency": m["currency"]}


def _unbilled(dispatched, invoiced):
	return {c: max(0.0, v - invoiced.get(c, 0)) for c, v in dispatched.items() if v - invoiced.get(c, 0) > 0.5}


def _same(a, b, tol=1.0):
	"""'Matches' / 'ERPNext lower' / 'ERPNext higher' / 'Differs' for two {currency: amount}, Rosenta a."""
	a, b = {c: v for c, v in a.items() if v}, {c: v for c, v in b.items() if v}
	if not a and not b:
		return "-"
	if not a:
		return "Only in ERPNext"
	if not b:
		return "Only in Rosenta"
	diffs = [b.get(c, 0) - a.get(c, 0) for c in set(a) | set(b)]
	if all(abs(d) <= tol for d in diffs):
		return "Matches"
	if all(d <= tol for d in diffs):
		return "ERPNext lower"
	if all(d >= -tol for d in diffs):
		return "ERPNext higher"
	return "Differs"


def _gate():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in"), frappe.PermissionError)


def _meta(datasets, days):
	have, when = {}, {}
	for ds in datasets:
		have[ds], when[ds] = sync.load(ds, days)
	gaps = sync.missing(datasets, days)
	if gaps:
		sync.ask_for(datasets, days)
	return have, {
		"days": days,
		"missing": [{"dataset": ds, "day": d} for ds, d in gaps],
		"rosenta_as_of": sync.freshness({ds + d: t for ds, w in when.items() for d, t in w.items()}),
	}


# ---------------------------------------------------------------- money


def _rosenta_money(have):
	per = {}

	def row(name):
		return per.setdefault(
			name,
			{"booked": {}, "dispatched": {}, "invoiced": {}, "paid": {}, "invoices": 0, "unpaid": 0},
		)

	for rows in have["orders"].values():
		for r in rows:
			if r.get("order_status") == "Cancelled":
				continue
			price, cur = rc._dec(r.get("stem_price")), _cur(r.get("currency"))
			rate = rc._num(r.get("pack_rate"))
			x = row(r.get("account_name"))
			_add(x["booked"], cur, rc._num(r.get("boxes_confirmed")) * rate * price)
			if r.get("order_status") in (rc.R_DISPATCHED, rc.R_PARTLY):
				_add(x["dispatched"], cur, rc._num(r.get("boxes_loaded")) * rate * price)
	seen = set()
	for rows in have["billing"].values():
		for r in rows:
			if r.get("invoice_status") in CANCELLED or r.get("invoice_name") in seen:
				continue
			seen.add(r.get("invoice_name"))  # a consolidated invoice can be listed under several days
			x, cur = row(r.get("account_name")), _cur(r.get("invoice_currency"))
			amount = rc._dec(r.get("invoice_amount"))
			_add(x["invoiced"], cur, amount)
			_add(x["paid"], cur, rc._dec(r.get("paid_amount")))
			x["invoices"] += 1
			x["unpaid"] += 0 if r.get("invoice_paid") == "paid" else 1
	return per


def _chunks(seq, n=800):
	seq = list(seq)
	for i in range(0, len(seq), n):
		yield seq[i : i + n]


def _erp_money(date_from, date_to):
	"""ERPNext, per customer: Sales Orders delivering in the range, what went out on Delivery Notes against
	them, and the Sales Invoices (and what is paid of them) raised for them."""
	per = {}

	def row(c):
		return per.setdefault(
			c,
			{"booked": {}, "dispatched": {}, "invoiced": {}, "paid": {}, "invoices": 0, "unpaid": 0,
			 "delivery_notes": 0, "name": c},
		)  # fmt: skip

	sos = frappe.db.sql(
		"""SELECT name, customer, customer_name, currency, net_total FROM `tabSales Order`
		   WHERE docstatus = 1 AND custom_business_unit = %s AND delivery_date BETWEEN %s AND %s""",
		(BUSINESS_UNIT, date_from, date_to),
		as_dict=True,
	)
	owner = {s.name: s.customer for s in sos}
	for s in sos:
		x = row(s.customer)
		x["name"] = s.customer_name or s.customer
		_add(x["booked"], _cur(s.currency), float(s.net_total or 0))
	dns, sis = {}, {}
	for part in _chunks(owner):
		for r in frappe.db.sql(
			"""SELECT dni.against_sales_order so, dn.name dn, dn.currency, SUM(dni.net_amount) amount
			   FROM `tabDelivery Note Item` dni JOIN `tabDelivery Note` dn ON dn.name = dni.parent
			   WHERE dn.docstatus = 1 AND IFNULL(dn.is_return, 0) = 0 AND dni.against_sales_order IN %(s)s
			   GROUP BY dni.against_sales_order, dn.name, dn.currency""",
			{"s": part},
			as_dict=True,
		):
			x = row(owner[r.so])
			_add(x["dispatched"], _cur(r.currency), float(r.amount or 0))
			dns.setdefault(owner[r.so], set()).add(r.dn)
		for r in frappe.db.sql(
			"""SELECT sii.sales_order so, si.name si, si.currency, si.grand_total, si.outstanding_amount,
			          SUM(sii.net_amount) amount
			   FROM `tabSales Invoice Item` sii JOIN `tabSales Invoice` si ON si.name = sii.parent
			   WHERE si.docstatus = 1 AND sii.sales_order IN %(s)s
			   GROUP BY sii.sales_order, si.name, si.currency, si.grand_total, si.outstanding_amount""",
			{"s": part},
			as_dict=True,
		):
			x, cur = row(owner[r.so]), _cur(r.currency)
			total = float(r.grand_total or 0)
			share = (total - float(r.outstanding_amount or 0)) / total if total else 0
			_add(x["invoiced"], cur, float(r.amount or 0))
			_add(x["paid"], cur, float(r.amount or 0) * share)
			sis.setdefault(owner[r.so], {})[r.si] = float(r.outstanding_amount or 0) <= 0.5
	for c, n in dns.items():
		per[c]["delivery_notes"] = len(n)
	for c, inv in sis.items():
		per[c]["invoices"] = len(inv)
		per[c]["unpaid"] = sum(1 for paid in inv.values() if not paid)
	return per


def _side(x):
	if x is None:
		return None
	return {
		"booked": _cash(x["booked"]),
		"dispatched": _cash(x["dispatched"]),
		"invoiced": _cash(x["invoiced"]),
		"paid": _cash(x["paid"]),
		"unbilled": _cash(_unbilled(x["dispatched"], x["invoiced"])),
		"invoices": x["invoices"],
		"unpaid": x["unpaid"],
		**({"delivery_notes": x["delivery_notes"]} if "delivery_notes" in x else {}),
	}


def _merge(into, x):
	for k in ("booked", "dispatched", "invoiced", "paid"):
		for c, v in x[k].items():
			_add(into[k], c, v)
	into["invoices"] += x["invoices"]
	into["unpaid"] += x["unpaid"]


@frappe.whitelist()
def get_rosenta_money(date_from=None, date_to=None):
	"""Booked, dispatched, invoiced and paid per customer, Rosenta against ERPNext, for the delivery dates
	in the range. Any signed-in user; reads only."""
	_gate()
	try:
		days = sync.days_between(date_from or frappe.utils.today(), date_to or date_from or frappe.utils.today())
		have, meta = _meta(("orders", "billing"), days)
		r_per, e_per = _rosenta_money(have), _erp_money(days[0], days[-1])
		customers = rc._Customers(frappe.get_all("Customer", pluck="name"))
		rows = {}
		for name, x in r_per.items():
			ec = customers.map(name)
			k = ec or "?" + str(name)
			r = rows.setdefault(k, {"customer": ec or name, "r": None, "e": None})
			if r["r"] is None:
				r["r"] = {"booked": {}, "dispatched": {}, "invoiced": {}, "paid": {}, "invoices": 0, "unpaid": 0}
			_merge(r["r"], x)
		for c, x in e_per.items():
			r = rows.setdefault(c, {"customer": x["name"], "r": None, "e": None})
			r["e"] = x
		empty = {"booked": {}, "dispatched": {}, "invoiced": {}, "paid": {}, "invoices": 0, "unpaid": 0}
		tot_r, tot_e = {**{k: {} for k in ("booked", "dispatched", "invoiced", "paid")}, "invoices": 0, "unpaid": 0}, {
			**{k: {} for k in ("booked", "dispatched", "invoiced", "paid")}, "invoices": 0, "unpaid": 0,
			"delivery_notes": 0,
		}  # fmt: skip
		out = []
		for r in rows.values():
			ro, er = r["r"] or empty, r["e"] or {**empty, "delivery_notes": 0}
			_merge(tot_r, ro)
			_merge(tot_e, er)
			tot_e["delivery_notes"] += er.get("delivery_notes", 0)
			out.append(
				{
					"customer": r["customer"],
					"rosenta": _side(ro),
					"erpnext": _side(er),
					"booked_status": _same(ro["booked"], er["booked"], 1.0),
					"invoiced_status": _same(ro["invoiced"], er["invoiced"], 1.0),
					"paid_status": _same(ro["paid"], er["paid"], 1.0),
				}
			)
		return {
			"success": True,
			"from": days[0],
			"to": days[-1],
			**meta,
			"totals": {"rosenta": _side(tot_r), "erpnext": _side(tot_e)},
			"customers": out,
		}
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="Rosenta money error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}


# ---------------------------------------------------------------- greenhouse revenue


def _farm_key(name):
	k = rc._key(name)
	return k[:-5].strip() if k.endswith(" FARM") else k


def _erp_farm_revenue(date_from, date_to):
	"""Revenue of the stems issued by farm in ERPNext: issued pick rows at the price per stem of their Sales Order line (its amount over its stems: the rate is per bunch)."""
	rows = frappe.db.sql(
		"""SELECT pli.farm, so.currency, SUM(pli.stock_qty) stems, SUM(pli.stock_qty * soi.net_amount / NULLIF(soi.stock_qty, 0)) revenue
		   FROM `tabPick List Item` pli
		   JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
		   JOIN `tabSales Order Item` soi ON soi.name = pli.sales_order_item
		   JOIN `tabSales Order` so ON so.name = soi.parent
		   WHERE opl.docstatus < 2 AND pli.issued = 1 AND so.docstatus = 1 AND so.custom_business_unit = %s
		     AND so.delivery_date BETWEEN %s AND %s
		   GROUP BY pli.farm, so.currency""",
		(BUSINESS_UNIT, date_from, date_to),
		as_dict=True,
	)
	out = {}
	for r in rows:
		f = out.setdefault(_farm_key(r.farm), {"farm": r.farm or "(no farm)", "stems": 0, "value": {}})
		f["stems"] += float(r.stems or 0)
		_add(f["value"], _cur(r.currency), float(r.revenue or 0))
	return out


@frappe.whitelist()
def get_rosenta_greenhouse(date_from=None, date_to=None):
	"""Revenue of the stems issued, by farm (Rosenta also by greenhouse and variety) against ERPNext by farm."""
	_gate()
	try:
		days = sync.days_between(date_from or frappe.utils.today(), date_to or date_from or frappe.utils.today())
		have, meta = _meta(("greenhouse",), days)
		farms, houses = {}, {}
		for rows in have["greenhouse"].values():
			for r in rows:
				k, cur = _farm_key(r.get("farm_name")), _cur(r.get("currency"))
				stems, revenue = rc._num(r.get("stems_issued")), rc._dec(r.get("revenue"))
				f = farms.setdefault(k, {"farm": r.get("farm_name"), "stems": 0, "value": {}})
				f["stems"] += stems
				_add(f["value"], cur, revenue)
				g = houses.setdefault(
					(k, r.get("greenhouse_name")),
					{"farm": r.get("farm_name"), "greenhouse": r.get("greenhouse_name"), "stems": 0, "value": {}},
				)
				g["stems"] += stems
				_add(g["value"], cur, revenue)
		erp = _erp_farm_revenue(days[0], days[-1])

		def side(x):
			if x is None:
				return None
			m = rc._money(x["value"], x["stems"])
			return {"stems": x["stems"], "value": m["value"], "price": m["price"], "currency": m["currency"],
					"by_currency": m["by_currency"]}  # fmt: skip

		out = []
		for k in set(farms) | set(erp):
			r, e = farms.get(k), erp.get(k)
			out.append(
				{
					"farm": (r or e)["farm"],
					"rosenta": side(r),
					"erpnext": side(e),
					"status": _same((r or {}).get("value", {}), (e or {}).get("value", {}), 1.0),
					"greenhouses": sorted(
						(
							{"greenhouse": g["greenhouse"], **side(g)}
							for (fk, _gh), g in houses.items()
							if fk == k
						),
						key=lambda g: -g["stems"],
					),
				}
			)
		out.sort(key=lambda f: -((f["rosenta"] or {}).get("stems") or 0))
		return {"success": True, "from": days[0], "to": days[-1], **meta, "farms": out}
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="Rosenta greenhouse error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}


# ---------------------------------------------------------------- fill rate

STAGES = ("ordered", "allocated", "issued", "packed", "loaded", "dispatched")


def _fill_side():
	return dict.fromkeys(STAGES, 0)


@frappe.whitelist()
def get_rosenta_fill(date_from=None, date_to=None):
	"""Stems at every stage from ordered to dispatched, per customer, Rosenta against ERPNext."""
	_gate()
	try:
		days = sync.days_between(date_from or frappe.utils.today(), date_to or date_from or frappe.utils.today())
		have, meta = _meta(("fill", "orders"), days)
		customers = rc._Customers(frappe.get_all("Customer", pluck="name"))
		rows = {}

		def row(k, name):
			return rows.setdefault(k, {"customer": name, "rosenta": _fill_side(), "erpnext": _fill_side()})

		for rs in have["fill"].values():  # Rosenta's own issued and dispatched stems, line by line
			for r in rs:
				ec = customers.map(r.get("customer"))
				x = row(ec or "?" + str(r.get("customer")), ec or r.get("customer"))["rosenta"]
				x["issued"] += rc._num(r.get("issued"))
				x["dispatched"] += rc._num(r.get("dispatched"))
		for rs in have["orders"].values():  # every order line: ordered, allocated, packed, loaded
			for r in rs:
				if r.get("order_status") == "Cancelled":
					continue
				ec = customers.map(r.get("account_name"))
				x = row(ec or "?" + str(r.get("account_name")), ec or r.get("account_name"))["rosenta"]
				rate = rc._num(r.get("pack_rate"))
				x["ordered"] += rc._num(r.get("boxes_confirmed")) * rate
				x["allocated"] += rc._num(r.get("boxes_allocated")) * rate
				x["packed"] += rc._num(r.get("boxes_staged")) * rate  # staged straight after packing
				x["loaded"] += rc._num(r.get("boxes_loaded")) * rate
		wf = orders_workflow.get_orders_workflow(view="customer", from_date=days[0], to_date=days[-1])
		if not isinstance(wf, dict) or not wf.get("success"):
			frappe.throw(_("Orders & Workflow: {0}").format((wf or {}).get("error")))
		for sec in wf.get("sections") or []:
			t = sec.get("totals") or {}
			x = row(sec.get("key"), sec.get("name") or sec.get("key"))["erpnext"]
			x["ordered"] += rc._num(t.get("ordered_stems"))
			x["allocated"] += rc._num(t.get("picked_stems"))
			x["issued"] += rc._num(t.get("issued_stems"))
			x["packed"] += rc._num(t.get("packed_stems"))
			x["loaded"] += rc._num(t.get("loaded_stems"))
			x["dispatched"] += rc._num(t.get("dispatched_stems"))
		out, tot = [], {"rosenta": _fill_side(), "erpnext": _fill_side()}
		for r in rows.values():
			for who in tot:
				for st in STAGES:
					tot[who][st] += r[who][st]
			ro, er = r["rosenta"], r["erpnext"]
			out.append(
				{
					**r,
					"status": rc._status_word(
						ro["ordered"] > 0, er["ordered"] > 0, ro["dispatched"], er["dispatched"], 0
					),
					"fill_rosenta": round(100 * ro["dispatched"] / ro["ordered"], 1) if ro["ordered"] else None,
					"fill_erpnext": round(100 * er["dispatched"] / er["ordered"], 1) if er["ordered"] else None,
				}
			)
		for who in tot:
			t_ = tot[who]
			t_["fill"] = round(100 * t_["dispatched"] / t_["ordered"], 1) if t_["ordered"] else None
		out.sort(key=lambda r: -abs(r["erpnext"]["dispatched"] - r["rosenta"]["dispatched"]))
		return {"success": True, "from": days[0], "to": days[-1], **meta, "totals": tot, "customers": out}
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="Rosenta fill error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}
