# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Specifications, Rosenta against ERPNext. TEMPORARY, with the rest of the Rosenta comparison.

A specification is what a customer's order line asks for: the box, how many stems in it, the bunches and what is
in them, the stem length, Fairtrade or not, the consumables. If an order line names a specification that ERPNext
does not have, or has differently, the order cannot be produced the same way in both systems.

Rosenta: the specifications used by the orders of the period (from the order snapshots), with their full
definition from the latest specifications snapshot (api/v2/rosenta_sync.py; Rosenta's own spec names, one
customer can have thousands of old ones, so only the used ones are compared).
ERPNext: the active Specifications.

The two systems do not always name a specification alike, so specs of one customer are paired by name first and
by what they contain second. A pair is then compared on the same things in both systems.
"""

import difflib
import re

import frappe
from frappe import _

from upande_packhouse.api.v2 import rosenta_compare as rc
from upande_packhouse.api.v2 import rosenta_sync as sync

PAIR_MIN = 0.5
#: Rosenta states a stem length by its grade (40, 50, 60); ERPNext by the cut length (42, 52, 62).
LENGTH_OFFSET = 2
SIMILAR = 0.82  # how alike two variety names must be to count as the same
SHOW_NAMES = 6  # names listed when a set differs


def _gate():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in"), frappe.PermissionError)


def _int(x):
	m = re.search(r"\d+", str(x or ""))
	return int(m.group()) if m else 0


def _norm(s):
	return re.sub(r"[^A-Z0-9]+", " ", str(s or "").upper()).strip()


def _close(a, b):
	"""Two names that are the same variety written a little differently (Athena Royal / Athena Royale)."""
	if a == b:
		return True
	words = lambda x: " ".join(sorted(x.split()))  # noqa: E731  (Royal Athena / Athena Royal)
	return (
		max(
			difflib.SequenceMatcher(None, a, b).ratio(),
			difflib.SequenceMatcher(None, words(a), words(b)).ratio(),
		)
		>= SIMILAR
	)


def _vsim(a, b):
	"""How alike two sets of variety names are, 0 to 1, counting near-equal names as equal."""
	if not a or not b:
		return 0.0
	hit_a = sum(1 for x in a if any(_close(x, y) for y in b))
	hit_b = sum(1 for y in b if any(_close(x, y) for x in a))
	return (hit_a / len(a) + hit_b / len(b)) / 2


def _names(items):
	return sorted({_norm(i) for i in items if _norm(i)})


# ---------------------------------------------------------------- the two definitions, in one shape


def rosenta_def(sp):
	"""A Rosenta specification (slim row) as {name, ft, pack_rate, bunches, stems_per_bunch, lengths, varieties,
	any_colours, box, consumables}."""
	bunches = sp.get("b") or []
	per_box = sum(_int(str(b[0]).split(";")[0]) for b in bunches)
	stems_per_bunch, lengths, varieties, any_colours = set(), set(), set(), set()
	for _count, vs in bunches:
		stems_per_bunch.add(sum(_int(v[2]) for v in vs))
		for v in vs:
			if _int(v[1]):
				lengths.add(_int(v[1]))
			name = _norm(v[0])
			if name.startswith("ANY "):
				any_colours.add(name[4:].replace(" VARIETY", "").strip())
			elif name:
				varieties.add(name)
	if not lengths and _int(sp.get("g")):
		lengths.add(_int(sp.get("g")))
	box = next((i[0] for i in sp.get("c") or [] if i[1] == "box"), "")
	return {
		"id": sp.get("i"), "name": (sp.get("n") or "").strip(), "ft": str(sp.get("ft")) == "true", "pack_rate": _int(sp.get("pr")),
		"bunches": per_box, "stems_per_bunch": sorted(x for x in stems_per_bunch if x), "lengths": sorted(lengths),
		"varieties": sorted(varieties), "any_colours": sorted(any_colours), "box": box,
		"consumables": _names(i[0] for i in sp.get("c") or [] if i[1] != "box"),
	}  # fmt: skip


def erp_specs():
	"""{customer: [definition]} of every active ERPNext Specification."""
	specs = frappe.get_all(
		"Specifications",
		filters={"status": "Active"},
		fields=["name", "customer", "spec_name", "ftnft", "box_type", "box_assortment"],
	)
	names = [s.name for s in specs]
	child = {"box_items": {}, "approved_varieties": {}, "consumables": {}}
	for table, dt, fields in (
		(
			"box_items",
			"Spec Box Item",
			["parent", "stems_per_bunch", "length", "bunches_per_box", "pack_rate"],
		),
		("approved_varieties", "Spec Approved Variety", ["parent", "variety", "colour"]),
		("consumables", "Spec Consumable", ["parent", "item_name"]),
	):
		for part in _chunks(names):
			for r in frappe.get_all(
				dt,
				filters={"parent": ["in", part], "parenttype": "Specifications", "parentfield": table},
				fields=fields,
				limit_page_length=0,
			):
				child[table].setdefault(r.parent, []).append(r)
	out = {}
	for s in specs:
		rows, vars_, cons = (
			child["box_items"].get(s.name, []),
			child["approved_varieties"].get(s.name, []),
			child["consumables"].get(s.name, []),
		)
		out.setdefault(s.customer, []).append(
			{
				"id": s.name, "name": (s.spec_name or s.name).strip(), "ft": s.ftnft == "FT",
				"pack_rate": sum(_int(r.pack_rate) or _int(r.stems_per_bunch) * _int(r.bunches_per_box) for r in rows),
				"bunches": sum(_int(r.bunches_per_box) for r in rows),
				"stems_per_bunch": sorted({_int(r.stems_per_bunch) for r in rows if _int(r.stems_per_bunch)}),
				"lengths": sorted({_int(r.length) for r in rows if _int(r.length)}),
				"varieties": _names(v.variety for v in vars_),
				"any_colours": _names(v.colour for v in vars_),
				"box": s.box_type or "",
				"consumables": _names(c.item_name for c in cons),
			}
		)  # fmt: skip
	return out


def _chunks(seq, n=800):
	seq = list(seq)
	for i in range(0, len(seq), n):
		yield seq[i : i + n]


# ---------------------------------------------------------------- comparing


def _shown(names):
	return ", ".join(names[:SHOW_NAMES]) + (
		f" and {len(names) - SHOW_NAMES} more" if len(names) > SHOW_NAMES else ""
	)


def _same_lengths(r, e):
	"""Equal, or ERPNext's is each of Rosenta's plus the grade offset."""
	return r == e or sorted(x + LENGTH_OFFSET for x in r) == e


def diff(r, e):
	"""What differs between a Rosenta and an ERPNext definition: [{field, rosenta, erpnext}]."""
	out = []

	def add(field, a, b):
		out.append({"field": field, "rosenta": a, "erpnext": b})

	if r["ft"] != e["ft"]:
		add("Fairtrade", "FT" if r["ft"] else "NFT", "FT" if e["ft"] else "NFT")
	if r["pack_rate"] != e["pack_rate"]:
		add("Stems per box", r["pack_rate"], e["pack_rate"])
	if r["bunches"] != e["bunches"] and r["bunches"] and e["bunches"]:
		add("Bunches per box", r["bunches"], e["bunches"])
	if r["stems_per_bunch"] != e["stems_per_bunch"] and r["stems_per_bunch"] and e["stems_per_bunch"]:
		add(
			"Stems per bunch",
			", ".join(map(str, r["stems_per_bunch"])),
			", ".join(map(str, e["stems_per_bunch"])),
		)
	if r["lengths"] and e["lengths"] and not _same_lengths(r["lengths"], e["lengths"]):
		add(
			"Stem length",
			", ".join(f"{x} cm" for x in r["lengths"]),
			", ".join(f"{x} cm" for x in e["lengths"]),
		)
	rb, eb = _norm(r["box"]), _norm(e["box"])
	if rb and eb and rb not in eb and eb not in rb:
		add("Box", r["box"], e["box"])
	only_r = [v for v in r["varieties"] if not any(_close(v, w) for w in e["varieties"])]
	only_e = [v for v in e["varieties"] if not any(_close(v, w) for w in r["varieties"])]
	if r["varieties"] and e["varieties"] and (only_r or only_e):
		add(
			"Varieties",
			("only here: " + _shown(only_r)) if only_r else "",
			("only here: " + _shown(only_e)) if only_e else "",
		)
	if r["any_colours"] and e["any_colours"] and set(r["any_colours"]) != set(e["any_colours"]):
		add("Colours", ", ".join(r["any_colours"]), ", ".join(e["any_colours"]))
	cr, ce = (
		[c for c in r["consumables"] if c not in e["consumables"]],
		[c for c in e["consumables"] if c not in r["consumables"]],
	)
	if r["consumables"] and (cr or ce):  # Rosenta lists no consumables on most specs: not a difference then
		add(
			"Consumables",
			("only here: " + _shown(cr)) if cr else "",
			("only here: " + _shown(ce)) if ce else "",
		)
	return out


def _score(r, e):
	"""How likely two specs are the same: their names, and what they contain (variety and length first)."""
	s = rc._jaccard(set(rc._words(r["name"])), set(rc._words(e["name"])))
	if _norm(r["name"]) == _norm(e["name"]):
		s += 1
	same_length = bool(r["lengths"] and e["lengths"] and _same_lengths(r["lengths"], e["lengths"]))
	v = _vsim(r["varieties"], e["varieties"])
	if v >= 0.5 and same_length:  # the samish variety at the same length: the same spec under another name
		s += 0.5 + 0.5 * v
	elif v >= 0.5:
		s += 0.25 * v
	if r["pack_rate"] and r["pack_rate"] == e["pack_rate"]:
		s += 0.2
	return s


def pair_specs(r_list, e_list):
	"""Pair the Rosenta and ERPNext specs of one customer, best match first, one to one."""
	cand = sorted(
		((_score(r, e), i, j) for i, r in enumerate(r_list) for j, e in enumerate(e_list)),
		key=lambda c: -c[0],
	)
	used_r, used_e, out = set(), set(), []
	for sc, i, j in cand:
		if sc < PAIR_MIN or i in used_r or j in used_e:
			continue
		used_r.add(i)
		used_e.add(j)
		d = diff(r_list[i], e_list[j])
		by = (
			"name"
			if _norm(r_list[i]["name"]) == _norm(e_list[j]["name"])
			else (
				"variety and length"
				if _vsim(r_list[i]["varieties"], e_list[j]["varieties"]) >= 0.5
				else "name"
			)
		)
		out.append(
			{
				"rosenta": r_list[i], "erpnext": e_list[j], "state": "Differs" if d else "Matches", "diffs": d,
				"matched_by": by,
			}
		)  # fmt: skip
	out += [
		{"rosenta": r, "erpnext": None, "state": "Only in Rosenta", "diffs": []}
		for i, r in enumerate(r_list) if i not in used_r
	]  # fmt: skip
	out += [
		{"rosenta": None, "erpnext": e, "state": "Only in ERPNext", "diffs": []}
		for j, e in enumerate(e_list) if j not in used_e
	]  # fmt: skip
	return out


def _rosenta_used(orders_by_day, all_specs):
	"""{account name: [definition + use]} of the specs on the orders of the period."""
	by_id = {s["i"]: s for s in all_specs}
	used = {}
	for day, rows in orders_by_day.items():
		for r in rows:
			if r.get("order_status") == "Cancelled":
				continue
			u = used.setdefault(
				r.get("spec_id"),
				{
					"account": r.get("account_name"),
					"spec_name": (r.get("spec_name") or "").strip(),
					"lines": 0,
					"stems": 0,
					"last": day,
				},
			)
			u["lines"] += 1
			u["stems"] += rc._num(r.get("boxes_confirmed")) * rc._num(r.get("pack_rate"))
			u["last"] = max(u["last"], day)
	out = {}
	for spec_id, u in used.items():
		sp = by_id.get(spec_id)
		d = rosenta_def(sp) if sp else {
			"name": u["spec_name"], "ft": False, "pack_rate": 0, "bunches": 0, "stems_per_bunch": [], "lengths": [],
			"varieties": [], "any_colours": [], "box": "", "consumables": [], "unknown": True,
		}  # fmt: skip
		d.update({"lines": u["lines"], "stems": u["stems"], "last": u["last"], "spec_id": spec_id})
		out.setdefault(u["account"], []).append(d)
	return out


@frappe.whitelist()
def get_rosenta_specs(date_from=None, date_to=None):
	"""The specifications of the orders of the period, Rosenta against ERPNext, with what differs.
	Any signed-in user; reads only."""
	_gate()
	try:
		days = sync.days_between(
			date_from or frappe.utils.today(), date_to or date_from or frappe.utils.today()
		)
		orders, meta = {}, {}
		have, meta = _orders(days)
		all_specs, specs_at = sync.load_specs()
		if not all_specs:
			sync.ask_for_specs()
		r_used = _rosenta_used(have["orders"], all_specs)
		e_all = erp_specs()
		customers = rc._Customers(frappe.get_all("Customer", pluck="name"))
		rows = {}
		for account, specs in r_used.items():
			ec = customers.map(account)
			rows.setdefault(
				ec or "?" + str(account), {"customer": ec or account, "r": [], "e": e_all.get(ec, [])}
			)["r"] += specs
		for ec, specs in e_all.items():
			rows.setdefault(ec, {"customer": ec, "r": [], "e": specs})
		out, totals = (
			[],
			{
				"rosenta_used": 0,
				"erpnext_active": 0,
				"matched": 0,
				"differ": 0,
				"only_rosenta": 0,
				"only_erpnext": 0,
				"unknown": 0,
			},
		)
		for r in rows.values():
			pairs = pair_specs(r["r"], r["e"])
			n = {
				s: sum(1 for p in pairs if p["state"] == s)
				for s in ("Matches", "Differs", "Only in Rosenta", "Only in ERPNext")
			}
			totals["rosenta_used"] += len(r["r"])
			totals["erpnext_active"] += len(r["e"])
			totals["matched"] += n["Matches"]
			totals["differ"] += n["Differs"]
			totals["only_rosenta"] += n["Only in Rosenta"]
			totals["only_erpnext"] += n["Only in ERPNext"]
			totals["unknown"] += sum(1 for s in r["r"] if s.get("unknown"))
			out.append(
				{
					"customer": r["customer"], "rosenta": len(r["r"]), "erpnext": len(r["e"]), "matches": n["Matches"],
					"differs": n["Differs"], "only_rosenta": n["Only in Rosenta"], "only_erpnext": n["Only in ERPNext"],
					"stems": sum(s["stems"] for s in r["r"]),
					"status": "Matches" if not (n["Differs"] or n["Only in Rosenta"] or n["Only in ERPNext"]) else
					"Only in Rosenta" if not r["e"] else "Differs",
					"specs": pairs,
				}
			)  # fmt: skip
		out.sort(key=lambda c: (-(c["only_rosenta"] + c["differs"]), -c["stems"]))
		by_field = {}
		for c in out:
			for p in c["specs"]:
				for d in p["diffs"]:
					by_field[d["field"]] = by_field.get(d["field"], 0) + 1
		return {
			"success": True, "from": days[0], "to": days[-1], **meta, "specs_as_of": str(specs_at) if specs_at else None,
			"totals": totals, "by_field": by_field, "customers": out,
		}  # fmt: skip
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="Rosenta specs error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}


def _orders(days):
	have, when = sync.load("orders", days)
	gaps = sync.missing(("orders",), days)
	if gaps:
		sync.ask_for(("orders",), days)
	return {"orders": have}, {
		"days": days,
		"missing": [{"dataset": ds, "day": d} for ds, d in gaps],
		"rosenta_as_of": sync.freshness(when),
	}


# ---------------------------------------------------------------- one pair, in full


def _fmt(x):
	return "" if x in (None, "", 0, 0.0) else str(x)


def _rosenta_full(sp):
	"""A slim Rosenta spec as readable bunches, varieties and consumables."""
	bunches, varieties = [], []
	for n, (count, vs) in enumerate(sp.get("b") or [], 1):
		stems = sum(_int(v[2]) for v in vs)
		per = _int(str(count).split(";")[0])
		bunches.append(
			{
				"n": n,
				"bunches": per,
				"stems": stems,
				"length": (_int(vs[0][1]) if vs else 0) or _int(sp.get("g")),
				"pack_rate": per * stems,
			}
		)
		for v in vs:
			varieties.append(
				{"name": v[0], "bunch": n, "stems": _int(v[2]), "length": _int(v[1]), "cut": v[3] or ""}
			)
	cons = [{"name": i[0], "type": i[1] or "", "qty": i[2] or ""} for i in sp.get("c") or []]
	return bunches, varieties, cons


def _erp_full(doc):
	bunches = [
		{"n": i, "bunches": _int(b.bunches_per_box), "stems": _int(b.stems_per_bunch), "length": _int(b.length), "pack_rate": _int(b.pack_rate), "kind": b.bunch_type or ""}
		for i, b in enumerate(doc.get("box_items") or [], 1)
	]  # fmt: skip
	varieties = [
		{"name": v.variety, "bunch": v.bunch_id or "", "primary": bool(v.is_primary), "colour": v.colour or "", "head": v.headsize_cm or "", "buds": v.budcount or ""}
		for v in doc.get("approved_varieties") or []
	]  # fmt: skip
	cons = [
		{"name": c.item_name or c.item, "type": c.consumable_type or "", "level": c.application_level or "",
		 "qty": c.qty_per_bunch if c.application_level == "Bunch" else c.qty_per_box}
		for c in doc.get("consumables") or []
	]  # fmt: skip
	return bunches, varieties, cons


def _align(r_items, e_items, key="name"):
	"""Pair the items of the two sides by name (near-equal names too); the rest stand alone, in order."""
	used, out = set(), []
	for r in r_items:
		j = next((j for j, e in enumerate(e_items) if j not in used and _norm(e[key]) == _norm(r[key])), None)
		if j is None:
			j = next(
				(j for j, e in enumerate(e_items) if j not in used and _close(_norm(e[key]), _norm(r[key]))),
				None,
			)
		if j is not None:
			used.add(j)
		out.append({"name": r[key], "rosenta": r, "erpnext": e_items[j] if j is not None else None})
	out += [{"name": e[key], "rosenta": None, "erpnext": e} for j, e in enumerate(e_items) if j not in used]
	return out


@frappe.whitelist()
def get_rosenta_spec_detail(rosenta_id=None, erpnext_name=None):
	"""A Rosenta specification and an ERPNext one in full, side by side: header, bunches, varieties and
	consumables. Either may be empty. Any signed-in user; reads only."""
	_gate()
	try:
		sp = next((s for s in sync.load_specs()[0] if s["i"] == rosenta_id), None) if rosenta_id else None
		doc = (
			frappe.get_doc("Specifications", erpnext_name)
			if erpnext_name and frappe.db.exists("Specifications", erpnext_name)
			else None
		)
		rd = rosenta_def(sp) if sp else None
		ed = (
			next((x for lst in erp_specs().values() for x in lst if x["id"] == erpnext_name), None)
			if doc
			else None
		)
		flagged = {x["field"] for x in diff(rd, ed)} if rd and ed else set()
		rb, rv, rc_ = _rosenta_full(sp) if sp else ([], [], [])
		eb, ev, ec = _erp_full(doc.as_dict()) if doc else ([], [], [])
		colours_per_box = (
			f"{_fmt(doc.min_colours_per_box)} to {_fmt(doc.max_colours_per_box)}"
			if doc and (doc.min_colours_per_box or doc.max_colours_per_box)
			else ""
		)
		validity = (
			" to ".join(x for x in (str(doc.valid_from or ""), str(doc.expiry_date or "")) if x)
			if doc
			else ""
		)
		rows = [
			("Specification", sp and sp["n"], doc and doc.spec_name, None),
			("Customer", sp and sp["a"], doc and doc.customer, None),
			("Reference / category", sp and " · ".join(x for x in (sp.get("s"), sp.get("rf")) if x), doc and doc.category_code, None),
			("Fairtrade", sp and ("FT" if rd["ft"] else "NFT"), doc and doc.ftnft, "Fairtrade"),
			("Type and validity", None, doc and " · ".join(x for x in (doc.spec_type, validity) if x), None),
			("Box", sp and rd["box"], doc and doc.box_type, "Box"),
			("Assortment", sp and ("Mixed Box" if len(rd["varieties"]) + len(rd["any_colours"]) > 1 else "Mono Box"), doc and doc.box_assortment, None),
			("Colours per box", None, colours_per_box, None),
			("Stems per box", sp and rd["pack_rate"], doc and ed and ed["pack_rate"], "Stems per box"),
			("Bunches per box", sp and rd["bunches"], doc and ed and ed["bunches"], "Bunches per box"),
			("Stem length", sp and ", ".join(f"{x} cm" for x in rd["lengths"]), doc and ed and ", ".join(f"{x} cm" for x in ed["lengths"]), "Stem length"),
			("Cut stage", sp and ", ".join(sorted({v["cut"] for v in rv if v["cut"]})), doc and doc.cut_stage, None),
			("Defoliation", None, doc and doc.defoliation_length, None),
			("Rubber band", None, doc and " · ".join(x for x in (doc.rubber_band_type, doc.rubber_band_distance_1, doc.rubber_band_distance_2) if x), None),
			("Consumables priced", sp and sp.get("cp"), doc and ("charged" if doc.consumables_charge else "not charged"), None),
			("Documentation charge / certificate of origin", None, doc and " · ".join(x for x in ("documentation charge" if doc.documentation_charge else "", "certificate of origin" if doc.certificate_of_origin else "") if x), None),
		]  # fmt: skip
		return {
			"success": True,
			"header": [
				{"label": a, "rosenta": _fmt(b), "erpnext": _fmt(c), "differs": f in flagged} for a, b, c, f in rows
				if _fmt(b) or _fmt(c)
			],
			"bunches": [
				{"n": i + 1, "rosenta": rb[i] if i < len(rb) else None, "erpnext": eb[i] if i < len(eb) else None}
				for i in range(max(len(rb), len(eb)))
			],
			"varieties": _align(rv, ev),
			"consumables": _align(rc_, ec),
			"has_rosenta": bool(sp), "has_erpnext": bool(doc),
		}  # fmt: skip
	except frappe.PermissionError:
		raise
	except Exception as e:
		frappe.clear_messages()
		frappe.log_error(title="Rosenta spec detail error", message=frappe.get_traceback())
		return {"success": False, "error": str(e)}
