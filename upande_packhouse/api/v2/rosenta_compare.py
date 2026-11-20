# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Rosenta comparison (www/rosenta-comparison.html).

TEMPORARY. Remove this file, the page, the Rosenta Connection doctype and
tests/test_rosenta_compare.py when the parallel run on Rosenta stops.

One call returns one delivery date from both systems, side by side.

ERPNext side: the Packhouse v2 endpoints themselves (Sales Order list, Orders &
Workflow, Packing Insights), never a query of its own, so every ERPNext number
here is the number those pages show. The totals are checked against Orders &
Workflow on every call and a mismatch is returned in `check`.

Rosenta side:
  orders, stems, cancellations    token API  /api/get_orders_summary
  boxes assigned / packed /       Packhouse Operations
  staged / loaded per team        /api/operations/get_packhouse_operations. It needs a signed-in
                                  session: the session is kept in the cache and replaced by signing
                                  in again whenever Rosenta refuses it.

Credentials live in the Rosenta Connection doctype (Password fields).

Date meaning: Sales Order delivery_date (README rule 4). ERPNext orders counted
are submitted ones; drafts are returned separately; cancelled ones are listed.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import frappe
import requests
from frappe import _
from frappe.utils import add_days, getdate, now_datetime, today

from upande_packhouse.api.v2 import orders_workflow, packing_insights, sales_order

ROSENTA = "https://karen.rosenta.com"
LOCATIONS = {"Ravine": "b183d745332a", "Karen": "7c45b7f280cf"}
TIMEOUT = 90
OPERATIONS_PATH = "/api/operations/get_packhouse_operations"
#: The Order Summary page's own call: per order and line, the stem price and its currency.
SPEC_PATH = "/api/insights/get_allocations_summary_by_order_spec"
#: Invoices issued for a delivery date, one row per invoice (a consolidated one covers several orders).
BILLING_PATH = "/api/financials/get_billing_by_delivery"
SESSION_KEY = "rosenta_compare_session"
CACHE_SECONDS = 60  # how long one comparison is shared between viewers
SESSION_TTL = 100 * 60  # Rosenta ends a session after two hours

#: Rosenta team -> ERPNext team names (Packing Team).
TEAM_MAP = [
	("Eldama", ("Eldama A", "Eldama B")),
	("Chepsito", ("Chepsito",)),
	("JMF", ("Jamafa",)),
	("Bravo", ("Bravo",)),
	("Hall A", ("Team A",)),
	("Hall B", ("Team B",)),
]

#: Normalised Rosenta customer name -> ERPNext customer, only where the names differ a lot.
CUSTOMER_OVERRIDES = {
	"FLAMINGO FLOWERS LTD": "FLAMINGO UK",
	"UDMAN FLORAL TRADING LLC": "Udman Floral Trading LLC(Ferns N Petals)",
	"AOA VENTURES LTD KAZAKHSTAN": "AOA VENTURES LTD-KAZ",
	"FATIM FLOWERS LTD USD": "Fatim Flowers Ltd",
}

BOX_TOLERANCE = 0
STEM_TOLERANCE = 0


# ---------------------------------------------------------------- helpers


def _num(x):
	try:
		return int(float(x or 0))
	except (TypeError, ValueError):
		return 0


def _dec(x):
	"""A number with its decimals (money). _num is for counts and drops them."""
	try:
		return float(x or 0)
	except (TypeError, ValueError):
		return 0.0


def _words(s):
	return re.sub(r"[^A-Z0-9]+", " ", str(s or "").upper()).split()


def _key(s):
	return " ".join(_words(s))


def _name_tokens(s):
	"""Words of an order name without 5-digit codes (date codes and Sales Order numbers)."""
	return {w for w in _words(s) if not (w.isdigit() and len(w) == 5)}


def _jaccard(a, b):
	if not a and not b:
		return 0.0
	return len(a & b) / len(a | b)


def _status_word(r_present, e_present, r_val, e_val, tol):
	if not r_present and not e_present:
		return "-"
	if not r_present:
		return "Only in ERPNext"
	if not e_present:
		return "Only in Rosenta"
	if abs(r_val - e_val) <= tol:
		return "Matches"
	return "ERPNext lower" if r_val > e_val else "Rosenta lower"


class _Customers:
	"""Maps a Rosenta customer name to the ERPNext Customer."""

	def __init__(self, names):
		self.by_key = {_key(n): n for n in names}
		self._seen = {}

	def map(self, rosenta_name):
		if rosenta_name in self._seen:
			return self._seen[rosenta_name]
		k = _key(rosenta_name)
		found = self.by_key.get(k) or CUSTOMER_OVERRIDES.get(k)
		if not found:
			words = set(k.split())
			best, score = None, 0.0
			for ek, name in self.by_key.items():
				s = _jaccard(words, set(ek.split()))
				if s > score:
					best, score = name, s
			found = best if score >= 0.8 else None
		self._seen[rosenta_name] = found
		return found


# ---------------------------------------------------------------- Rosenta


def _settings():
	doc = frappe.get_single("Rosenta Connection")
	return {
		"token": doc.get_password("token", raise_exception=False),
		"email": doc.login_email,
		"password": doc.get_password("login_password", raise_exception=False),
	}


def _orders_summary(token, start_iso):
	"""The token API: every order line from `start_iso` up to the next delivery date."""
	if not token:
		frappe.throw(_("The Rosenta API token is not set in Rosenta Connection."))
	r = requests.post(
		ROSENTA + "/api/get_orders_summary",
		headers={"Authorization": token},
		data={"start": start_iso},
		timeout=TIMEOUT,
	)
	try:
		out = r.json()
	except ValueError:
		out = None
	if not isinstance(out, list):
		frappe.throw(_("Rosenta did not return a list of orders."))
	return out


class _CacheStore:
	def get(self):
		return frappe.cache.get_value(SESSION_KEY)

	def set(self, value):
		frappe.cache.set_value(SESSION_KEY, value, expires_in_sec=SESSION_TTL)


class RosentaOps:
	"""Packhouse Operations behind Rosenta's sign-in.

	The session cookie is reused until Rosenta refuses a call; then the client signs in again, keeps the
	new cookie and repeats the call once. `store` has get() / set(value). `fetch` only makes the HTTP call,
	so it may run in a worker thread; everything that touches the cache stays in `operations`.
	"""

	def __init__(self, email, password, store=None):
		self.email = email
		self.password = password
		self.store = store or _CacheStore()
		self.cookie = None

	def fetch(self, location_id, delivery_date, path=OPERATIONS_PATH):
		"""One call with the current session; None when Rosenta refuses it."""
		r = requests.post(
			ROSENTA + path,
			data={"location_id": location_id, "delivery_date": delivery_date},
			cookies={"ci_session": self.cookie or ""},
			timeout=TIMEOUT,
		)
		try:
			out = r.json()
		except ValueError:  # a refused call answers "not allowed" followed by the sign-in page
			return None
		if out == []:  # a date with nothing assigned yet answers with an empty list
			return {}
		return out if isinstance(out, dict) else None

	def _sign_in(self):
		if not (self.email and self.password):
			frappe.throw(_("The Rosenta login is not set in Rosenta Connection."))
		s = requests.Session()
		s.post(
			ROSENTA + "/login/cloud",
			data={"email": self.email, "password": self.password},
			timeout=TIMEOUT,
		)
		cookie = s.cookies.get("ci_session")
		if not cookie:
			frappe.throw(_("Rosenta did not accept the login."))
		self.cookie = cookie
		self.store.set(cookie)

	def call(self, path, data):
		"""POST to a Rosenta report endpoint and return its parsed JSON (a list or a dict). Rosenta answers a
		refused call with its sign-in page, so the client signs in again once and repeats the call."""
		if self.cookie is None:
			self.cookie = self.store.get() or ""
		for attempt in (0, 1):
			r = requests.post(
				ROSENTA + path, data=data, cookies={"ci_session": self.cookie or ""}, timeout=TIMEOUT
			)
			try:
				return r.json()
			except ValueError:
				if attempt == 0:
					self._sign_in()
		frappe.throw(_("Rosenta refused the call to {0} after signing in.").format(path))

	def operations(self, location_id, delivery_date):
		"""{team: {order_id: {spec_id: line}}}. Signs in again once if the session is missing or expired."""
		if self.cookie is None:
			self.cookie = self.store.get() or ""
		out = self.fetch(location_id, delivery_date)
		if out is None:
			self._sign_in()
			out = self.fetch(location_id, delivery_date)
		if out is None:
			frappe.throw(_("Rosenta refused the Packhouse Operations call after signing in."))
		return out


def _rosenta_invoices(ops, dmy):
	"""{Rosenta customer: {invoices, orders, boxes, by_currency}} for the delivery date, from Rosenta billing.
	Rosenta invoices per customer, so there is no invoicing by team."""

	def call():
		r = requests.post(
			ROSENTA + BILLING_PATH,
			data={"delivery_date": dmy},
			cookies={"ci_session": ops.cookie or ""},
			timeout=TIMEOUT,
		)
		try:
			out = r.json()
		except ValueError:
			return None
		return out if isinstance(out, list) else None

	ops.operations(next(iter(LOCATIONS.values())), dmy)  # makes sure there is a live session
	rows = call()
	if rows is None:
		ops._sign_in()
		rows = call()
	if rows is None:
		frappe.throw(_("Rosenta refused the billing call."))
	out = {}
	for inv in rows:
		if inv.get("invoice_status") in ("cancelled", "void"):
			continue
		c = out.setdefault(inv.get("account_name"), {"invoices": 0, "orders": set(), "boxes": 0, "by_currency": {}})
		c["invoices"] += 1
		try:
			c["orders"].update(frappe.parse_json(inv.get("order_ids") or "[]"))
		except Exception:
			pass
		c["boxes"] += _num(inv.get("total_boxes"))
		cur = str(inv.get("invoice_currency") or "").upper()
		c["by_currency"][cur] = c["by_currency"].get(cur, 0) + _dec(inv.get("invoice_amount"))
	return {k: {**v, "orders": len(v["orders"])} for k, v in out.items()}


def _line_prices(ops, dmy):
	"""{(order id, line name): [(stem price, currency), ...]} for one delivery date at every location, from
	the Order Summary call. Two lines of one order with the same name are taken in the order Rosenta lists them."""
	out = {}
	for loc in LOCATIONS.values():
		data = ops.fetch(loc, dmy, SPEC_PATH)
		if data is None:
			ops.operations(loc, dmy)  # signs in again if the session ended
			data = ops.fetch(loc, dmy, SPEC_PATH)
		if data is None:
			frappe.throw(_("Rosenta refused the Order Summary call."))
		for order_id, specs in data.items():
			for sp in specs.values():
				out.setdefault((order_id, str(sp.get("spec_name") or "").strip()), []).append(
					(_dec(sp.get("stem_price")), sp.get("currency") or "")
				)
	return out


def _operations(ops, dmy_list):
	"""Packhouse Operations for each delivery date at every location, locations merged:
	{dmy: {team: {order_id: {spec_id: line}}}}.

	The first call goes alone so a missing or expired session is replaced once; the others then run
	together (HTTP only), and any that Rosenta refuses meanwhile is repeated here, one by one.
	"""
	jobs = [(loc, dmy) for dmy in dmy_list for loc in LOCATIONS.values()]
	results = {jobs[0]: ops.operations(*jobs[0])}
	rest = jobs[1:]
	with ThreadPoolExecutor(max_workers=4) as pool:
		fetched = list(pool.map(lambda j: ops.fetch(*j), rest))
	for j, out in zip(rest, fetched, strict=True):
		results[j] = out if out is not None else ops.operations(*j)
	merged = {}
	for (_loc, dmy), data in results.items():
		merged.setdefault(dmy, {}).update(data or {})
	return merged


def _rosenta_team_dispatch(rows, dmy):
	"""Stems dispatched per Rosenta team: the stems loaded on lines of orders it marks Dispatched or Partially
	Dispatched, shared over the teams of the line by their boxes. Same rule as the customer figure."""
	out = {}
	for r in rows:
		if r.get("order_delivery_date") != dmy or r.get("order_status") not in (R_DISPATCHED, R_PARTLY):
			continue
		try:
			teams = [t for t in frappe.parse_json(r.get("teams") or "[]") if _num(t.get("box_count")) > 0]
		except Exception:
			teams = []
		total = sum(_num(t.get("box_count")) for t in teams)
		for t in teams:
			share = _num(r.get("stems_loaded")) * _num(t.get("box_count")) / total
			out[t.get("team_name")] = out.get(t.get("team_name"), 0) + share
	return {k: round(v) for k, v in out.items()}


def _rosenta_teams(ops_day, ops_next):
	"""Per Rosenta team: boxes assigned / packed / staged / loaded for the delivery date, and the time
	of its last packed box over this and the next delivery date (today's packing is for the next one)."""
	teams = {}

	def row(name):
		return teams.setdefault(
			name, {"assigned": 0, "packed": 0, "staged": 0, "loaded": 0, "last_packed": 0}
		)

	for data, is_day in ((ops_day, True), (ops_next, False)):
		for team, orders in (data or {}).items():
			r = row(team)
			for specs in orders.values():
				for line in specs.values():
					if is_day:
						r["assigned"] += _num(line.get("boxes_assigned"))
						r["packed"] += _num(line.get("boxes_packed"))
						r["staged"] += _num(line.get("boxes_staged"))
						r["loaded"] += _num(line.get("boxes_loaded"))
					if _num(line.get("boxes_packed")) > 0:
						r["last_packed"] = max(r["last_packed"], _num(line.get("last_box_packed")))
	return teams


# ---------------------------------------------------------------- ERPNext (Packhouse v2)


def _v2(out, what):
	if not isinstance(out, dict) or not out.get("success"):
		frappe.throw(
			_("Packhouse v2 {0}: {1}").format(
				what, (out or {}).get("error") if isinstance(out, dict) else out
			)
		)
	if out.get("truncated"):
		frappe.throw(_("Packhouse v2 {0} was truncated.").format(what))
	return out


def _erp_team_totals(ins_teams, names, work_day, wf_teams=None):
	"""A team's boxes in ERPNext. Expected / packed / last pack come from Packing Insights; staged and loaded
	are the Box Label counts Orders & Workflow shows for the team (a staged box counts once it is staged,
	loaded or delivered)."""
	exp = packed = staged = loaded = dispatched = 0
	last = None
	for n in names:
		w = (wf_teams or {}).get(n) or {}
		dispatched += _num(w.get("dispatched_stems"))
		staged += _num(w.get("staged_boxes"))
		loaded += _num(w.get("loaded_boxes"))
		t = ins_teams.get(n)
		if not t:
			continue
		exp += _num(t.get("exp_boxes"))
		packed += _num(t.get("packed_boxes"))
		if t.get("pack_last") and (last is None or t["pack_last"] > last):
			last = t["pack_last"]
	return {
		"exp": exp,
		"packed": packed,
		"staged": staged,
		"loaded": loaded,
		"dispatched": dispatched,
		"last": f"{work_day} {last}:00" if last else None,
	}


# ---------------------------------------------------------------- the comparison

#: What is compared, for each system, per customer and in total. Both systems fill the same keys.
_SIDE_KEYS = (
	"orders",
	"boxes",
	"stems",
	"boxes_packed",
	"boxes_staged",
	"boxes_loaded",
	"stems_packed",
	"stems_loaded",
	"orders_dispatched",
	"stems_dispatched",
	"orders_invoiced",
	"invoices",
)

#: Rosenta order statuses that mean stems have left: "Dispatched" is the whole order, "Partially Dispatched"
#: an order with some lines gone. Rosenta gives no dispatch figure per line, so its stems loaded on such an
#: order stand for stems dispatched.
R_DISPATCHED = "Dispatched"
R_PARTLY = "Partially Dispatched"


def _side():
	return {k: 0 for k in _SIDE_KEYS}


def _money(by_currency, stems):
	"""{value, price, currency, by_currency} from amounts by currency. Over several currencies the value and
	the price are not added up: the currencies are listed and each amount kept in `by_currency`."""
	by_currency = {c: round(v, 2) for c, v in by_currency.items() if v}
	if not by_currency:
		return {"value": None, "price": None, "currency": "", "by_currency": {}}
	if len(by_currency) > 1:
		return {"value": None, "price": None, "currency": " + ".join(sorted(by_currency)), "by_currency": by_currency}
	cur, v = next(iter(by_currency.items()))
	return {"value": v, "price": _price(v, stems), "currency": cur, "by_currency": by_currency}


def _add_value(side, by_currency, delivery_notes=None):
	"""Add the value of the orders, the price per stem and, for ERPNext, the delivery notes to a side."""
	side.update(_money(by_currency, side["stems"]))
	if delivery_notes is not None:
		side["delivery_notes"] = delivery_notes


def _erp_line_label(el):
	return el.get("mix_name") or " ".join(x for x in (el.get("variety"), el.get("length")) if x)


def _pair_lines(r_lines, e_lines):
	"""Line up the lines of one Rosenta order with those of its Sales Order: same variety, same pack rate."""
	cand = []
	for i, rl in enumerate(r_lines):
		rtok = set(_words(rl["line"]))
		for j, el in enumerate(e_lines):
			vtok = set(_words(el.get("variety")))
			score = _jaccard(rtok, set(_words(el.get("mix_name"))))
			if vtok and vtok <= rtok:
				score += 2
			if rl["pack_rate"] and rl["pack_rate"] == el["pack_rate"]:
				score += 1
			cand.append((score, i, j))
	cand.sort(key=lambda c: -c[0])
	used_r, used_e, out = set(), set(), []
	for score, i, j in cand:
		if score < 1 or i in used_r or j in used_e:
			continue
		used_r.add(i)
		used_e.add(j)
		out.append({"label": r_lines[i]["line"], "rosenta": r_lines[i], "erpnext": e_lines[j]})
	out += [
		{"label": rl["line"], "rosenta": rl, "erpnext": None}
		for i, rl in enumerate(r_lines)
		if i not in used_r
	]
	out += [
		{"label": _erp_line_label(el), "rosenta": None, "erpnext": el}
		for j, el in enumerate(e_lines)
		if j not in used_e
	]
	return out


def _pair_orders(r_list, e_list, label, e_lines):
	"""Line up Rosenta orders with ERPNext Sales Orders of one customer (by name, then box count), and
	each pair's lines. An order only one system has comes back with the other side empty."""
	cand = []
	for i, ro in enumerate(r_list):
		rt = _name_tokens(ro["name"])
		for j, eo in enumerate(e_list):
			sc = _jaccard(rt, _name_tokens(label.get(eo["name"])))
			if ro["boxes"] == _num(eo.get("boxes")):
				sc += 0.2
			cand.append((sc, i, j))
	cand.sort(key=lambda c: -c[0])
	used_r, used_e, out = set(), set(), []

	def ros(ro):
		return {
			"name": ro["name"],
			"status": ro["status"],
			"boxes": ro["boxes"],
			"stems": ro["stems"],
			**_money(ro.get("value") or {}, ro["stems"]),
		}

	def erp(eo):
		return {
			"name": label.get(eo["name"]) or eo["name"],
			"so": eo["name"],
			"status": eo.get("status"),
			"boxes": _num(eo.get("boxes")),
			"stems": _num(eo.get("stems")),
			"value": _dec(eo.get("value")),
			"price": _price(_dec(eo.get("value")), _num(eo.get("stems"))),
			"currency": eo.get("currency") or "",
		}

	for sc, i, j in cand:
		if sc < 0.5 or i in used_r or j in used_e:
			continue
		used_r.add(i)
		used_e.add(j)
		lines = _pair_lines(r_list[i]["lines"], e_lines.get(e_list[j]["name"], []))
		out.append({"rosenta": ros(r_list[i]), "erpnext": erp(e_list[j]), "lines": lines})
	for i, ro in enumerate(r_list):
		if i not in used_r:
			out.append({"rosenta": ros(ro), "erpnext": None, "lines": _pair_lines(ro["lines"], [])})
	for j, eo in enumerate(e_list):
		if j not in used_e:
			out.append(
				{"rosenta": None, "erpnext": erp(eo), "lines": _pair_lines([], e_lines.get(eo["name"], []))}
			)
	return out


def _rosenta_orders(rows, dmy, prices=None):
	"""One entry per Rosenta order of the delivery date, summed over its lines (and the lines themselves)."""
	orders = {}
	for r in rows:
		if r.get("order_delivery_date") != dmy:
			continue
		o = orders.setdefault(
			r.get("order_id"),
			{
				"order_id": r.get("order_id"),
				"name": str(r.get("order_name") or "").strip(),
				"customer": r.get("customer"),
				"status": r.get("order_status"),
				"boxes": 0,
				"stems": 0,
				"staged": 0,
				"loaded": 0,
				"reason": r.get("cancellation_reason") or "",
				"activity": False,
				"lines": [],
				"value": {},
			},
		)
		stems = _num(r.get("stems_confirmed"))
		boxes, teams = 0, []
		try:
			for t in frappe.parse_json(r.get("teams") or "[]"):
				boxes += _num(t.get("box_count"))
				teams.append(t.get("team_name"))
		except Exception:
			boxes, teams = 0, []
		if not boxes and _num(r.get("pack_rate")) > 0:
			boxes = -(-stems // _num(r.get("pack_rate")))  # a line with no team: stems / pack rate
		o["boxes"] += boxes
		o["stems"] += stems
		o["staged"] += _num(r.get("stems_staged"))
		o["loaded"] += _num(r.get("stems_loaded"))
		line_name = str(r.get("line") or "").strip()
		given = (prices or {}).get((r.get("order_id"), line_name)) or []
		price, cur = given.pop(0) if given else (None, "")
		if price is not None:
			o["value"][cur] = o["value"].get(cur, 0) + price * stems
		o["lines"].append(
			{
				"line": str(r.get("line") or "").strip(),
				"length": _num(r.get("grade")),
				"box_type": r.get("box_type") or "",
				"boxes": boxes,
				"pack_rate": _num(r.get("pack_rate")),
				"stems": stems,
				"price": price,
				"value": None if price is None else round(price * stems, 2),
			}
		)
		if _num(r.get("first_box_packed")) > 0 or _num(r.get("stems_staged")) > 0:
			o["activity"] = True
	return orders


NOT_ALLOCATED = "Not allocated"


def _alloc_row():
	return {"orders": set(), "lines": 0, "boxes": 0, "stems": 0}


def _rosenta_allocations(rows, dmy):
	"""What Rosenta has allocated to each team for the delivery date, from the order lines' team split:
	{team: {orders, lines, boxes, stems}}. A line with no team is "Not allocated" at its confirmed stems."""
	out = {}
	for r in rows:
		if r.get("order_delivery_date") != dmy or r.get("order_status") == "Cancelled":
			continue
		rate = _num(r.get("pack_rate"))
		try:
			teams = [t for t in frappe.parse_json(r.get("teams") or "[]") if _num(t.get("box_count")) > 0]
		except Exception:
			teams = []
		if not teams:
			parts = [(NOT_ALLOCATED, -(-_num(r.get("stems_confirmed")) // rate) if rate else 0, _num(r.get("stems_confirmed")))]
		else:
			parts = [(t.get("team_name"), _num(t.get("box_count")), _num(t.get("box_count")) * rate) for t in teams]
		for name, boxes, stems in parts:
			a = out.setdefault(name, _alloc_row())
			a["orders"].add(r.get("order_id"))
			a["lines"] += 1
			a["boxes"] += boxes
			a["stems"] += stems
	return {k: {**v, "orders": len(v["orders"])} for k, v in out.items()}


def _erp_allocations(wf_team, names, unallocated=False):
	"""ERPNext's allocation to the given Packing Teams: stems on their pick lists, from Orders & Workflow
	by team. For the "Not allocated" group it is the stems still ordered with no pick list, and no boxes.
	None when none of the teams has anything."""
	secs = {sec.get("name"): sec.get("totals") or {} for sec in wf_team.get("sections") or []}
	have = [secs[n] for n in names if n in secs]
	if not have:
		return None
	return {
		"orders": sum(_num(t.get("orders")) for t in have),
		"lines": sum(_num(t.get("lines")) for t in have),
		"boxes": None if unallocated else sum(_num(t.get("planned_boxes")) for t in have),
		"stems": sum(_num(t.get("ordered_stems" if unallocated else "picked_stems")) for t in have),
	}


def _rosenta_customer_boxes(ops_day):
	"""Boxes packed / staged / loaded per Rosenta customer, from Packhouse Operations (one row per team)."""
	out = {}
	for orders in (ops_day or {}).values():
		for specs in orders.values():
			for line in specs.values():
				c = out.setdefault(line.get("customer"), {"packed": 0, "staged": 0, "loaded": 0})
				c["packed"] += _num(line.get("boxes_packed"))
				c["staged"] += _num(line.get("boxes_staged"))
				c["loaded"] += _num(line.get("boxes_loaded"))
	return out


def _price(value, stems):
	return round(value / stems, 4) if stems else None


def _erp_lines(wf, amounts=None):
	"""{sales order: [line]} from Orders & Workflow, one line per box line as Rosenta shows it. In ERPNext
	each variety of a mixed box is its own Sales Order Item (same mix name, same box count, its own stems
	per box), so those are folded into one line: the boxes once, the pack rate and stems summed, and the
	varieties kept for display. A line on several pick lists carries its numbers on the first one only.
	`amounts` is {Sales Order Item: amount}, for the value and the price per stem of each line."""
	amounts = amounts or {}
	items = {}
	for sec in wf.get("sections") or []:
		for it in sec.get("rows") or []:
			if it.get("primary"):
				items.setdefault(it.get("sales_order"), []).append(it)
	out = {}
	for so, rows in items.items():
		groups = {}
		for it in rows:
			mix = _key(it.get("mix_name"))
			k = (mix, _num(it.get("line_boxes")), it.get("box_type") or "") if mix else ("", id(it), "")
			groups.setdefault(k, []).append(it)
		for grp in groups.values():
			first = grp[0]
			mixed = len(grp) > 1
			out.setdefault(so, []).append(
				{
					"variety": first.get("variety") or "",
					"varieties": [
						{"name": g.get("variety") or "", "per_box": _num(g.get("stems_per_box"))} for g in grp
					]
					if mixed
					else [],
					"mixed": mixed,
					"length": first.get("length") or "",
					"box_type": first.get("box_type") or "",
					"mix_name": first.get("mix_name") or "",
					"boxes": _num(first.get("line_boxes")),
					"pack_rate": sum(_num(g.get("stems_per_box")) for g in grp),
					"stems": sum(_num(g.get("ordered_stems")) for g in grp),
					"value": sum(_dec(amounts.get(g.get("soi"))) for g in grp),
					"price": _price(
						sum(_dec(amounts.get(g.get("soi"))) for g in grp),
						sum(_num(g.get("ordered_stems")) for g in grp),
					),
				}
			)
	return out


@frappe.whitelist()
def get_rosenta_comparison(date=None):
	"""Rosenta and ERPNext side by side for one delivery date. Any signed-in user; reads only."""
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in"), frappe.PermissionError)
	key = f"rosenta_compare:{getdate(date or today())}"
	cached = frappe.cache.get_value(key)  # whoever asks within a minute gets the same answer, Rosenta is not asked again
	if cached:
		return cached
	try:
		out = _compare(getdate(date or today()))
		if out.get("success"):
			frappe.cache.set_value(key, out, expires_in_sec=CACHE_SECONDS)
		return out
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="Rosenta comparison error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}


def _compare(d):
	iso = str(d)
	dmy = f"{d.day}/{d.month}/{d.year}"
	nxt = add_days(d, 1)
	next_dmy = f"{nxt.day}/{nxt.month}/{nxt.year}"
	cfg = _settings()

	# ---- Rosenta
	summary = _orders_summary(cfg["token"], iso)
	client = RosentaOps(cfg["email"], cfg["password"])
	ops_error, price_error, teams_r, cust_boxes = None, None, {}, None
	try:
		ops = _operations(client, [dmy, next_dmy])
		teams_r = _rosenta_teams(ops.get(dmy), ops.get(next_dmy))
		cust_boxes = _rosenta_customer_boxes(ops.get(dmy))
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		ops_error = str(e)
	try:  # the same sign-in; a failure here only leaves the Rosenta prices out
		prices = _line_prices(client, dmy)
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		prices, price_error = None, str(e)
	orders = _rosenta_orders(summary, dmy, prices)
	invoice_error = None
	try:
		r_invoices = _rosenta_invoices(client, dmy)
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		r_invoices, invoice_error = None, str(e)

	# ---- ERPNext, as Packhouse v2 shows it
	args = {"from_date": iso, "to_date": iso}
	so_sub = _v2(sales_order.get_sales_orders(status="submitted", **args), "Sales Order list")
	so_dr = _v2(sales_order.get_sales_orders(status="draft", **args), "Sales Order list")
	so_ca = _v2(sales_order.get_sales_orders(status="cancelled", **args), "Sales Order list")
	wf = _v2(orders_workflow.get_orders_workflow(view="customer", **args), "Orders & Workflow")
	wf_team = _v2(orders_workflow.get_orders_workflow(view="team", **args), "Orders & Workflow by team")
	ins = _v2(packing_insights.get_packing_insights(**args), "Packing Insights")
	e_orders = so_sub.get("orders") or []
	e_cancel = so_ca.get("orders") or []
	wk = wf.get("kpis") or {}
	soi = [it.get("soi") for sec in wf.get("sections") or [] for it in sec.get("rows") or [] if it.get("soi")]
	amounts = (
		{
			x.name: x.amount
			for x in frappe.get_all("Sales Order Item", filters={"name": ["in", soi]}, fields=["name", "amount"])
		}
		if soi
		else {}
	)  # the line's value only; every other number comes from the Packhouse pages
	e_lines = _erp_lines(wf, amounts)

	names = [s["name"] for s in e_orders + e_cancel]
	label, replaced = {}, {}
	if names:  # labels only, never a number
		for x in frappe.get_all(
			"Sales Order", filters={"name": ["in", names]}, fields=["name", "custom_order_name"]
		):
			label[x.name] = x.custom_order_name
		for x in frappe.get_all(
			"Sales Order", filters={"amended_from": ["in", names]}, fields=["name", "amended_from"]
		):
			replaced[x.amended_from] = x.name

	customers = _Customers(frappe.get_all("Customer", pluck="name"))

	# ---- customers
	rows = {}

	def row_for(k, name):
		return rows.setdefault(
			k,
			{
				"customer": name,
				"rosenta": _side(),
				"erpnext": _side(),
				"r_list": [],
				"e_list": [],
				"r_value": {},
				"e_value": {},
				"dns": set(),
				"sis": set(),
				"r_inv": {},
				"e_inv": {},
			},
		)

	for o in orders.values():
		if o["status"] == "Cancelled":
			continue
		ec = customers.map(o["customer"])
		r = row_for(ec or "?" + str(o["customer"]), ec or o["customer"])
		side = r["rosenta"]
		side["orders"] += 1
		side["boxes"] += o["boxes"]
		side["stems"] += o["stems"]
		# Rosenta counts staged stems; boxes are staged straight after packing, so they stand for packed
		side["stems_packed"] += o["staged"]
		side["stems_loaded"] += o["loaded"]
		for cur, v in o["value"].items():
			r["r_value"][cur] = r["r_value"].get(cur, 0) + v
		if o["status"] in (R_DISPATCHED, R_PARTLY):
			side["stems_dispatched"] += o["loaded"]
		if o["status"] == R_DISPATCHED:
			side["orders_dispatched"] += 1
		r["r_list"].append(o)
	for cname, inv in (r_invoices or {}).items():
		ec = customers.map(cname)
		rr = row_for(ec or "?" + str(cname), ec or cname)
		rr["rosenta"]["orders_invoiced"] += inv["orders"]
		rr["rosenta"]["invoices"] += inv["invoices"]
		for cur, v in inv["by_currency"].items():
			rr["r_inv"][cur] = rr["r_inv"].get(cur, 0) + v
	for cname, b in (cust_boxes or {}).items():
		ec = customers.map(cname)
		side = row_for(ec or "?" + str(cname), ec or cname)["rosenta"]
		side["boxes_packed"] += b["packed"]
		side["boxes_staged"] += b["staged"]
		side["boxes_loaded"] += b["loaded"]
	for s in e_orders:
		r = row_for(s["customer"], s.get("customer_name") or s["customer"])
		r["erpnext"]["orders"] += 1
		r["erpnext"]["boxes"] += _num(s.get("boxes"))
		r["erpnext"]["stems"] += _num(s.get("stems"))
		cur = s.get("currency") or ""
		r["e_value"][cur] = r["e_value"].get(cur, 0) + _dec(s.get("value"))
		r["e_list"].append(s)
	for sec in wf.get("sections") or []:
		t = sec.get("totals") or {}
		side = row_for(sec.get("key"), sec.get("name") or sec.get("key"))["erpnext"]
		side["stems_packed"] += _num(t.get("packed_stems"))
		side["stems_loaded"] += _num(t.get("loaded_stems"))
		side["boxes_packed"] += _num(t.get("complete_boxes"))
		side["boxes_staged"] += _num(t.get("staged_boxes"))
		side["boxes_loaded"] += _num(t.get("loaded_boxes"))
		side["stems_dispatched"] += _num(t.get("dispatched_stems"))
		sent, ordered = {}, {}
		for it in sec.get("rows") or []:
			if not it.get("primary"):
				continue
			so = it.get("sales_order")
			ordered[so] = ordered.get(so, 0) + _num(it.get("ordered_stems"))
			sent[so] = sent.get(so, 0) + _num(it.get("dispatched_stems"))
			rows[sec.get("key")]["dns"].update(it.get("delivery_notes") or [])
		side["orders_dispatched"] += sum(1 for so, n in ordered.items() if n and sent[so] >= n - 0.5)
		side["orders_invoiced"] += len(
			{it.get("sales_order") for it in sec.get("rows") or [] if it.get("primary") and it.get("sales_invoices")}
		)
		for it in sec.get("rows") or []:
			rows[sec.get("key")]["sis"].update(it.get("sales_invoices") or [])

	all_sis = sorted({n for r in rows.values() for n in r["sis"]})
	si_amount = {
		x.name: (x.currency, _dec(x.grand_total))
		for x in (
			frappe.get_all(
				"Sales Invoice",
				filters={"name": ["in", all_sis], "docstatus": 1},
				fields=["name", "grand_total", "currency"],
			)
			if all_sis
			else []
		)
	}
	for r in rows.values():
		r["e_invoices"] = [n for n in r["sis"] if n in si_amount]
		for n in r["e_invoices"]:
			cur, v = si_amount[n]
			r["e_inv"][cur] = r["e_inv"].get(cur, 0) + v
		r["erpnext"]["invoices"] = len(r["e_invoices"])

	totals = {"rosenta": _side(), "erpnext": _side()}
	r_all, e_all, dns_all = {}, {}, set()
	ri_all, ei_all = {}, {}
	out_customers = []
	for r in rows.values():
		for who in totals:
			for k in _SIDE_KEYS:
				totals[who][k] += r[who][k]
		ro, er = r["rosenta"], r["erpnext"]
		_add_value(er, r["e_value"], len(r["dns"]))
		_add_value(ro, r["r_value"])
		er["invoiced"] = _money(r["e_inv"], 0)
		ro["invoiced"] = _money(r["r_inv"], 0)
		for cur, v in r["e_inv"].items():
			ei_all[cur] = ei_all.get(cur, 0) + v
		for cur, v in r["r_inv"].items():
			ri_all[cur] = ri_all.get(cur, 0) + v
		dns_all |= r["dns"]
		for cur, v in r["e_value"].items():
			e_all[cur] = e_all.get(cur, 0) + v
		for cur, v in r["r_value"].items():
			r_all[cur] = r_all.get(cur, 0) + v
		boxes_known = cust_boxes is not None
		out_customers.append(
			{
				"customer": r["customer"],
				"rosenta": {
					**ro,
					**(
						{}
						if boxes_known
						else {k: None for k in ("boxes_packed", "boxes_staged", "boxes_loaded")}
					),
				},
				"erpnext": er,
				"orders_status": _status_word(
					ro["orders"], er["orders"], ro["boxes"], er["boxes"], BOX_TOLERANCE
				),
				"pack_status": _status_word(
					ro["stems_loaded"] > 0 or ro["stems_packed"] > 0,
					er["stems_packed"] > 0,
					ro["stems_packed"],
					er["stems_packed"],
					STEM_TOLERANCE,
				),
				"dispatch_status": _status_word(
					ro["stems_dispatched"] > 0,
					er["stems_dispatched"] > 0,
					ro["stems_dispatched"],
					er["stems_dispatched"],
					STEM_TOLERANCE,
				),
				"invoice_status": _status_word(
					ro["orders_invoiced"] > 0,
					er["orders_invoiced"] > 0,
					ro["orders_invoiced"],
					er["orders_invoiced"],
					0,
				),
				"pairs": _pair_orders(r["r_list"], r["e_list"], label, e_lines),
			}
		)
	if cust_boxes is None:
		for who_key in ("boxes_packed", "boxes_staged", "boxes_loaded"):
			totals["rosenta"][who_key] = None
	if r_invoices is None:  # Rosenta billing could not be read: show nothing rather than zero
		for side in [totals["rosenta"]] + [c["rosenta"] for c in out_customers]:
			side.update({"orders_invoiced": None, "invoices": None, "invoiced": None})
		for c in out_customers:
			c["invoice_status"] = "-"

	te = totals["erpnext"]
	_add_value(te, e_all, len(dns_all))
	_add_value(totals["rosenta"], r_all)
	totals["erpnext"]["invoiced"] = _money(ei_all, 0)
	totals["rosenta"]["invoiced"] = _money(ri_all, 0)
	# The ERPNext side must equal what Orders & Workflow shows.
	check = [
		f"{what} {mine} vs {_num(theirs)}"
		for what, mine, theirs in (
			("orders", te["orders"], wk.get("orders")),
			("boxes", te["boxes"], wk.get("boxes")),
			("stems ordered", te["stems"], wk.get("ordered_stems")),
			("stems packed", te["stems_packed"], wk.get("packed_stems")),
		)
		if mine != _num(theirs)
	]

	# ---- teams
	work_day = ins.get("work_day")
	ins_teams = {t.get("team"): t for t in ins.get("teams") or []}
	wf_teams = {sec.get("name"): sec.get("totals") or {} for sec in wf_team.get("sections") or []}

	r_disp = _rosenta_team_dispatch(summary, dmy)

	def team_row(name, erp_names, r, e_t):
		return {
			"team": name,
			"erp_teams": erp_names,
			"rosenta": None
			if r is None
			else {
				"assigned": r["assigned"],
				"packed": r["packed"],
				"staged": r["staged"],
				"loaded": r["loaded"],
				"dispatched": r_disp.get(name, 0),
				"last_packed": r["last_packed"],
			},
			"erpnext": None
			if e_t is None
			else {
				"expected": e_t["exp"],
				"packed": e_t["packed"],
				"staged": e_t["staged"],
				"loaded": e_t["loaded"],
				"dispatched": e_t["dispatched"],
				"last_pack": e_t["last"],
			},
		}

	teams, mapped = [], set()
	for rn, en in TEAM_MAP:
		mapped.update(en)
		teams.append(
			team_row(rn, ", ".join(en), teams_r.get(rn), _erp_team_totals(ins_teams, en, work_day, wf_teams))
		)
	for rn, r in teams_r.items():
		if rn not in {m[0] for m in TEAM_MAP}:
			teams.append(team_row(rn, "(not mapped)", r, None))
	for en, t in ins_teams.items():
		if en not in mapped:
			teams.append(
				team_row("(not mapped)", en, None, _erp_team_totals(ins_teams, (en,), work_day, wf_teams))
			)

	# ---- allocations by team
	r_alloc = _rosenta_allocations(summary, dmy)
	mapped_r = {m[0] for m in TEAM_MAP}
	allocations = []
	for rn, en in TEAM_MAP:
		allocations.append({"team": rn, "erp_teams": ", ".join(en), "rosenta": r_alloc.get(rn), "erpnext": _erp_allocations(wf_team, en)})
	allocations.append({"team": NOT_ALLOCATED, "erp_teams": NOT_ALLOCATED, "rosenta": r_alloc.get(NOT_ALLOCATED), "erpnext": _erp_allocations(wf_team, (NOT_ALLOCATED,), True)})
	for rn, a in r_alloc.items():
		if rn not in mapped_r and rn != NOT_ALLOCATED:
			allocations.append({"team": rn, "erp_teams": "(not mapped)", "rosenta": a, "erpnext": None})
	for sec in wf_team.get("sections") or []:
		n = sec.get("name")
		if n not in mapped and n != NOT_ALLOCATED:
			allocations.append({"team": "(not mapped)", "erp_teams": n, "rosenta": None, "erpnext": _erp_allocations(wf_team, (n,))})

	# ---- cancellations
	def find_erp(order, pool):
		ec = customers.map(order["customer"])
		tok = _name_tokens(order["name"])
		best, score = None, 0.0
		for s in pool:
			if s["customer"] != ec:
				continue
			sc = _jaccard(tok, _name_tokens(label.get(s["name"])))
			if sc > score:
				best, score = s, sc
		return best if score >= 0.5 else None

	cancel_r = []
	for o in orders.values():
		if o["status"] != "Cancelled":
			continue
		live, dead = find_erp(o, e_orders), find_erp(o, e_cancel)
		other = f"Open ({live.get('status')})" if live else "Cancelled" if dead else "No matching order"
		cancel_r.append(
			{
				"customer": o["customer"], "name": o["name"], "stems": o["stems"], "boxes": o["boxes"],
				"staged": o["staged"], "loaded": o["loaded"], "activity": o["activity"], "reason": o["reason"],
				"erpnext": other,
			}
		)  # fmt: skip
	cancel_e = []
	for s in e_cancel:
		tok = _name_tokens(label.get(s["name"]))
		best, score = None, 0.0
		for o in orders.values():
			if customers.map(o["customer"]) != s["customer"]:
				continue
			sc = _jaccard(tok, _name_tokens(o["name"]))
			if sc > score:
				best, score = o, sc
		cancel_e.append(
			{
				"customer": s.get("customer_name") or s["customer"], "so": s["name"], "name": label.get(s["name"]),
				"boxes": _num(s.get("boxes")), "stems": _num(s.get("stems")),
				"replaced_by": replaced.get(s["name"]) or "",
				"rosenta": str(best["status"]) if best is not None and score >= 0.5 else "No matching order",
			}
		)  # fmt: skip

	dk = so_dr.get("kpis") or {}
	return {
		"success": True,
		"date": iso,
		"fetched": str(now_datetime()),
		"totals": totals,
		"drafts": {
			"orders": _num(dk.get("orders")),
			"boxes": _num(dk.get("boxes")),
			"stems": _num(dk.get("stems")),
		},
		"check": check,
		"ops_error": ops_error,
		"price_error": price_error,
		"invoice_error": invoice_error,
		"work_day": work_day,
		"customers": out_customers,
		"teams": teams,
		"allocations": allocations,
		"cancel_rosenta": cancel_r,
		"cancel_erpnext": cancel_e,
	}
