# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Rosenta comparison (www/rosenta-compare.html).

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
SESSION_KEY = "rosenta_compare_session"
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

	def fetch(self, location_id, delivery_date):
		"""One call with the current session; None when Rosenta refuses it."""
		r = requests.post(
			ROSENTA + "/api/operations/get_packhouse_operations",
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


def _erp_team_totals(ins_teams, names, work_day):
	exp = packed = 0
	last = None
	for n in names:
		t = ins_teams.get(n)
		if not t:
			continue
		exp += _num(t.get("exp_boxes"))
		packed += _num(t.get("packed_boxes"))
		if t.get("pack_last") and (last is None or t["pack_last"] > last):
			last = t["pack_last"]
	return {"exp": exp, "packed": packed, "last": f"{work_day} {last}:00" if last else None}


# ---------------------------------------------------------------- the comparison


def _pair_orders(r_list, e_list, label):
	"""Line up Rosenta orders with ERPNext Sales Orders of one customer, by name then box count."""
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

	def erp_fields(eo):
		return {
			"e_name": label.get(eo["name"]) or eo["name"],
			"e_so": eo["name"],
			"e_boxes": _num(eo.get("boxes")),
			"e_stems": _num(eo.get("stems")),
			"e_status": eo.get("status"),
		}

	def ros_fields(ro):
		return {
			"r_name": ro["name"],
			"r_boxes": ro["boxes"],
			"r_stems": ro["stems"],
			"r_status": ro["status"],
		}

	empty_r = {"r_name": "", "r_boxes": 0, "r_stems": 0, "r_status": ""}
	empty_e = {"e_name": "", "e_so": "", "e_boxes": 0, "e_stems": 0, "e_status": ""}
	for sc, i, j in cand:
		if sc < 0.5 or i in used_r or j in used_e:
			continue
		used_r.add(i)
		used_e.add(j)
		out.append({**ros_fields(r_list[i]), **erp_fields(e_list[j])})
	out += [{**ros_fields(ro), **empty_e} for i, ro in enumerate(r_list) if i not in used_r]
	out += [{**empty_r, **erp_fields(eo)} for j, eo in enumerate(e_list) if j not in used_e]
	return out


def _rosenta_orders(rows, dmy):
	"""One entry per Rosenta order of the delivery date, summed over its lines."""
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
			},
		)
		stems = _num(r.get("stems_confirmed"))
		boxes = 0
		try:
			for t in frappe.parse_json(r.get("teams") or "[]"):
				boxes += _num(t.get("box_count"))
		except Exception:
			boxes = 0
		if not boxes and _num(r.get("pack_rate")) > 0:
			boxes = -(-stems // _num(r.get("pack_rate")))  # a line with no team: stems / pack rate
		o["boxes"] += boxes
		o["stems"] += stems
		o["staged"] += _num(r.get("stems_staged"))
		o["loaded"] += _num(r.get("stems_loaded"))
		if _num(r.get("first_box_packed")) > 0 or _num(r.get("stems_staged")) > 0:
			o["activity"] = True
	return orders


@frappe.whitelist()
def get_rosenta_comparison(date=None):
	"""Rosenta and ERPNext side by side for one delivery date. System Manager only; reads only."""
	frappe.only_for("System Manager")
	try:
		return _compare(getdate(date or today()))
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
	orders = _rosenta_orders(summary, dmy)
	ops_error, teams_r = None, {}
	try:
		ops = _operations(RosentaOps(cfg["email"], cfg["password"]), [dmy, next_dmy])
		teams_r = _rosenta_teams(ops.get(dmy), ops.get(next_dmy))
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		ops_error = str(e)

	# ---- ERPNext, as Packhouse v2 shows it
	args = {"from_date": iso, "to_date": iso}
	so_sub = _v2(sales_order.get_sales_orders(status="submitted", **args), "Sales Order list")
	so_dr = _v2(sales_order.get_sales_orders(status="draft", **args), "Sales Order list")
	so_ca = _v2(sales_order.get_sales_orders(status="cancelled", **args), "Sales Order list")
	wf = _v2(orders_workflow.get_orders_workflow(view="customer", **args), "Orders & Workflow")
	ins = _v2(packing_insights.get_packing_insights(**args), "Packing Insights")
	e_orders = so_sub.get("orders") or []
	e_cancel = so_ca.get("orders") or []
	wk = wf.get("kpis") or {}

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
				"customer": name, "r_orders": 0, "e_orders": 0, "r_boxes": 0, "e_boxes": 0, "r_stems": 0,
				"e_stems": 0, "r_staged": 0, "r_loaded": 0, "e_packed": 0, "e_staged": 0, "e_loaded": 0,
				"r_list": [], "e_list": [],
			},
		)  # fmt: skip

	for o in orders.values():
		if o["status"] == "Cancelled":
			continue
		ec = customers.map(o["customer"])
		r = row_for(ec or "?" + str(o["customer"]), ec or o["customer"])
		r["r_orders"] += 1
		r["r_boxes"] += o["boxes"]
		r["r_stems"] += o["stems"]
		r["r_staged"] += o["staged"]
		r["r_loaded"] += o["loaded"]
		r["r_list"].append(o)
	for s in e_orders:
		r = row_for(s["customer"], s.get("customer_name") or s["customer"])
		r["e_orders"] += 1
		r["e_boxes"] += _num(s.get("boxes"))
		r["e_stems"] += _num(s.get("stems"))
		r["e_list"].append(s)
	for sec in wf.get("sections") or []:
		t = sec.get("totals") or {}
		r = row_for(sec.get("key"), sec.get("name") or sec.get("key"))
		r["e_packed"] += _num(t.get("packed_stems"))
		r["e_staged"] += _num(t.get("staged_stems"))
		r["e_loaded"] += _num(t.get("loaded_stems"))

	totals = {k: 0 for k in (
		"r_orders", "e_orders", "r_boxes", "e_boxes", "r_stems", "e_stems",
		"r_staged", "r_loaded", "e_packed", "e_staged", "e_loaded",
	)}  # fmt: skip
	out_customers = []
	for r in rows.values():
		for k in totals:
			totals[k] += r[k]
		out_customers.append(
			{
				**{k: r[k] for k in ("customer", "r_orders", "e_orders", "r_boxes", "e_boxes", "r_stems", "e_stems",
				"r_staged", "r_loaded", "e_packed", "e_staged", "e_loaded")},
				"orders_diff": r["r_boxes"] - r["e_boxes"],
				"orders_status": _status_word(r["r_orders"], r["e_orders"], r["r_boxes"], r["e_boxes"], BOX_TOLERANCE),
				"pack_diff": r["r_staged"] - r["e_packed"],
				"pack_status": _status_word(
					r["r_loaded"] > 0 or r["r_staged"] > 0, r["e_packed"] > 0, r["r_staged"], r["e_packed"], STEM_TOLERANCE
				),
				"pairs": _pair_orders(r["r_list"], r["e_list"], label),
			}
		)  # fmt: skip

	# The ERPNext side must equal what Orders & Workflow shows.
	check = [
		f"{what} {mine} vs {_num(theirs)}"
		for what, mine, theirs in (
			("orders", totals["e_orders"], wk.get("orders")),
			("boxes", totals["e_boxes"], wk.get("boxes")),
			("stems ordered", totals["e_stems"], wk.get("ordered_stems")),
			("stems packed", totals["e_packed"], wk.get("packed_stems")),
		)
		if mine != _num(theirs)
	]

	# ---- teams
	work_day = ins.get("work_day")
	ins_teams = {t.get("team"): t for t in ins.get("teams") or []}
	teams, mapped = [], set()
	for rn, en in TEAM_MAP:
		mapped.update(en)
		e = _erp_team_totals(ins_teams, en, work_day)
		r = teams_r.get(rn)
		teams.append(
			{
				"team": rn, "erp_teams": ", ".join(en),
				"r_assigned": r["assigned"] if r else None, "r_packed": r["packed"] if r else None,
				"r_staged": r["staged"] if r else None, "r_loaded": r["loaded"] if r else None,
				"r_last_packed": r["last_packed"] if r else 0,
				"e_exp_boxes": e["exp"], "e_packed_boxes": e["packed"], "e_last_pack": e["last"],
			}
		)  # fmt: skip
	for rn, r in teams_r.items():
		if rn not in {m[0] for m in TEAM_MAP}:
			teams.append(
				{
					"team": rn, "erp_teams": "(not mapped)", "r_assigned": r["assigned"], "r_packed": r["packed"],
					"r_staged": r["staged"], "r_loaded": r["loaded"], "r_last_packed": r["last_packed"],
					"e_exp_boxes": 0, "e_packed_boxes": 0, "e_last_pack": None,
				}
			)  # fmt: skip
	for en, t in ins_teams.items():
		if en not in mapped:
			teams.append(
				{
					"team": "(not mapped)", "erp_teams": en, "r_assigned": None, "r_packed": None, "r_staged": None,
					"r_loaded": None, "r_last_packed": 0, "e_exp_boxes": _num(t.get("exp_boxes")),
					"e_packed_boxes": _num(t.get("packed_boxes")),
					"e_last_pack": f"{work_day} {t['pack_last']}:00" if t.get("pack_last") else None,
				}
			)  # fmt: skip

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
		"work_day": work_day,
		"customers": out_customers,
		"teams": teams,
		"cancel_rosenta": cancel_r,
		"cancel_erpnext": cancel_e,
	}
