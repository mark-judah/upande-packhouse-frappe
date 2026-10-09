# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Order Fulfilment v2 (www/order-fulfilment-v2.html).

Fulfilment is measured on what actually left the farm:

    Supplied vs ordered    = sum(dispatched stems) / sum(ordered stems)
    Supplied vs confirmed  = sum(dispatched stems) / sum(confirmed stems)

  ordered     pipeline.fetch_lines  -> ordered_stems
  confirmed   pipeline.attach_pipeline -> confirmed_stems (Confirmed Stems)
  supplied    pipeline.attach_pipeline -> dispatched_stems (submitted,
              non-return Delivery Note Items by so_detail)
  packed      pipeline.attach_pipeline -> packed_stems (kept for the drill-down)

Percentages are always ratios of summed stems at the level shown (line,
order, customer, account manager, page), never averages of percentages.
A zero denominator returns None ("—" on the page), never 0 % or infinity.

Date meaning: Sales Order delivery_date (README rule 4). Orders: submitted.
Farm / region: the ORDER's farm (README rule 6, region.order_farm_sql): Sales
Order.farm, else custom_farm, else the Order Pick List farm. Orders with no
farm drop out while a farm or region filter is set; a pick list is not needed.

The pipeline is attached to every line in the date range first and the other
filters are applied afterwards, so a Delivery Note row without so_detail is
attributed to the same line whatever the filters are.
"""

from collections import defaultdict

import frappe

from upande_packhouse.api.v2.core import pipeline
from upande_packhouse.api.v2.core import region as region_mod
from upande_packhouse.api.v2.core import rose as rose_mod

UNASSIGNED = "Unassigned"
MEASURES = ("ordered_stems", "confirmed_stems", "supplied_stems", "packed_stems")


def _pct(num, den):
	"""num / den as a percentage (1 dp), None when there is nothing to divide by."""
	return round(num * 100.0 / den, 1) if den else None


def _totals(rows):
	t = {m: float(sum(r[m] for r in rows)) for m in MEASURES}
	t["supplied_vs_ordered_pct"] = _pct(t["supplied_stems"], t["ordered_stems"])
	t["supplied_vs_confirmed_pct"] = _pct(t["supplied_stems"], t["confirmed_stems"])
	t["to_supply_stems"] = max(0.0, t["ordered_stems"] - t["supplied_stems"])
	return t


def _order_farms(sos):
	"""{sales_order: farm} — the ORDER's farm (README rule 6, core/region.order_farm_sql):
	Sales Order.farm, else custom_farm, else its Order Pick List farm. "" = no farm."""
	if not sos:
		return {}
	expr = region_mod.order_farm_sql("so", "opl")
	rows = frappe.db.sql(
		f"""SELECT so.name, MIN({expr}) AS farm
		   FROM `tabSales Order` so
		   LEFT JOIN `tabOrder Pick List` opl ON opl.sales_order = so.name AND opl.docstatus < 2
		   WHERE so.name IN %(s)s GROUP BY so.name""",  # nosemgrep: fixed SQL from region helper
		{"s": tuple(sos)},
	)
	return {n: (f or "") for n, f in rows}


def _managers(customers):
	"""{customer: (manager_key, manager_name)} from Customer.account_manager."""
	if not customers:
		return {}
	rows = frappe.db.sql(
		"""SELECT c.name, c.account_manager AS email,
		          COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), c.account_manager) AS mname
		   FROM `tabCustomer` c LEFT JOIN `tabUser` u ON u.name = c.account_manager
		   WHERE c.name IN %(c)s""",
		{"c": tuple(customers)},
		as_dict=True,
	)
	out = {r.name: ((r.email or UNASSIGNED), (r.mname or UNASSIGNED)) for r in rows}
	return {c: out.get(c, (UNASSIGNED, UNASSIGNED)) for c in customers}


def _rank_key(m):
	"""Best fill rate first; no figure (None) after any figure; Unassigned last."""
	so = m["supplied_vs_ordered_pct"]
	sc = m["supplied_vs_confirmed_pct"]
	return (
		m["manager"] == UNASSIGNED,
		so is None,
		-(so or 0),
		sc is None,
		-(sc or 0),
		-m["ordered_stems"],
		m["manager_name"],
	)


@frappe.whitelist()
def get_order_fulfilment(
	from_date=None, to_date=None, region=None, farm=None, customer=None, manager=None, rose=None, q=None
):
	"""Supplied vs ordered / confirmed per account manager, customer, order and line."""
	today = frappe.utils.today()
	from_date = from_date or today
	to_date = to_date or from_date
	if from_date > to_date:
		from_date, to_date = to_date, from_date
	q = (q or "").strip().lower()

	lines = pipeline.fetch_lines(delivery_from=from_date, delivery_to=to_date)
	opls = pipeline.attach_pipeline(lines)

	sos = tuple({ln.parent for ln in lines})
	order_names = {}
	if sos:
		order_names = dict(
			frappe.db.sql(
				"SELECT name, custom_order_name FROM `tabSales Order` WHERE name IN %(s)s", {"s": sos}
			)
		)
	mgr = _managers({ln.customer for ln in lines})
	farm_of = _order_farms(sos)

	# Filter options come from the whole date range so any value can be picked.
	options = {
		"customers": sorted(
			{(ln.customer, ln.customer_name or ln.customer) for ln in lines},
			key=lambda x: (x[1] or "").lower(),
		),
		"managers": sorted(
			{mgr[ln.customer] for ln in lines}, key=lambda x: (x[0] == UNASSIGNED, x[1].lower())
		),
		"farms": sorted(
			set(region_mod.REGIONS["Karen"] + region_mod.REGIONS["Ravine"])
			| {f for f in farm_of.values() if f}
		),
	}

	farms = region_mod.farms_for(region=region, farm=farm)
	farm_set = set(farms) if farms is not None else None
	rose_kind = rose_mod.normalize(rose)

	def keep(ln):
		if farm_set is not None and farm_of.get(ln.parent, "") not in farm_set:
			return False
		if rose_kind != "all" and ln.rose_type != rose_kind:
			return False
		if customer and ln.customer != customer:
			return False
		if q:
			hay = " ".join(
				str(x or "")
				for x in (
					ln.customer,
					ln.customer_name,
					ln.parent,
					order_names.get(ln.parent),
					ln.item_code,
					ln.item_name,
				)
			).lower()
			if q not in hay:
				return False
		return True

	rows = []
	for ln in lines:
		if not keep(ln):
			continue
		mkey, mname = mgr[ln.customer]
		rows.append(
			frappe._dict(
				soi=ln.name,
				idx=ln.idx,
				sales_order=ln.parent,
				order_name=order_names.get(ln.parent) or "",
				delivery_date=str(ln.delivery_date),
				customer=ln.customer,
				customer_name=ln.customer_name or ln.customer,
				manager=mkey,
				manager_name=mname,
				variety=ln.item_code,
				item_name=ln.item_name,
				length=ln.custom_length or "",
				rose_type=ln.rose_type,
				farm=farm_of.get(ln.parent, ""),
				ordered_stems=float(ln.ordered_stems or 0),
				confirmed_stems=float(ln.confirmed_stems or 0),
				supplied_stems=float(ln.dispatched_stems or 0),
				packed_stems=float(ln.packed_stems or 0),
			)
		)

	# Account managers: every filter except the manager pick, so the chart can
	# be used to switch between managers (the selected one is highlighted).
	by_mgr = defaultdict(list)
	for r in rows:
		by_mgr[r.manager].append(r)
	managers = []
	for key, rs in by_mgr.items():
		m = _totals(rs)
		m.update(
			manager=key,
			manager_name=rs[0].manager_name,
			customers=len({r.customer for r in rs}),
			orders=len({r.sales_order for r in rs}),
		)
		managers.append(m)
	managers.sort(key=_rank_key)

	if manager:
		rows = [r for r in rows if r.manager == manager]

	# customer -> orders -> lines; totals at every level summed from lines.
	cust = {}
	for r in rows:
		c = cust.setdefault(
			r.customer,
			{
				"customer": r.customer,
				"customer_name": r.customer_name,
				"manager": r.manager,
				"manager_name": r.manager_name,
				"_orders": {},
			},
		)
		o = c["_orders"].setdefault(
			r.sales_order,
			{
				"sales_order": r.sales_order,
				"order_name": r.order_name,
				"delivery_date": r.delivery_date,
				"lines": [],
			},
		)
		ln = dict(r)
		ln["supplied_vs_ordered_pct"] = _pct(r.supplied_stems, r.ordered_stems)
		ln["supplied_vs_confirmed_pct"] = _pct(r.supplied_stems, r.confirmed_stems)
		o["lines"].append(ln)

	customers = []
	for c in cust.values():
		orders = []
		for o in c.pop("_orders").values():
			o["lines"].sort(key=lambda x: x["idx"])
			o.update(_totals(o["lines"]))
			orders.append(o)
		orders.sort(key=lambda o: (o["delivery_date"], o["sales_order"]))
		c.update(_totals(orders))
		c["orders"] = orders
		c["order_count"] = len(orders)
		customers.append(c)
	customers.sort(key=lambda c: -c["ordered_stems"])

	k = _totals(customers)
	k.update(
		customers=len(customers),
		orders=sum(c["order_count"] for c in customers),
		lines=len(rows),
	)
	return {
		"success": True,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"kpis": k,
		"managers": managers,
		"customers": customers,
		"options": {
			"customers": [{"value": a, "label": b} for a, b in options["customers"]],
			"managers": [{"value": a, "label": b} for a, b in options["managers"]],
			"farms": options["farms"],
		},
	}
