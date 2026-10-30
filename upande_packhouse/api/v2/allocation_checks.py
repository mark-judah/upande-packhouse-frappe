# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""The Checks tab of Allocation Planning v2: what Sales Allocation will stop on or regret, found for the whole
day's plan before anyone allocates. Nothing here writes.

  mix_incomplete  red    a mixed box or bunch that cannot be packed whole: a variety of it is not covered
  cut_stage       red    held buckets at a cut stage the order does not take (Sales Allocation does not offer them)
  missing_bucket  red    a held bucket is no longer on the shelf
  short           red    a variety and length the day's orders need more of than the shelf and forecast cover
  downgrade       red /  a longer length confirmed while orders for that length are short (red), or on stock
                  amber  that is not past the amber time, so allocation asks for a downgrade reason (amber)
  squeeze         amber  a customer who accepts other varieties holds stock a customer who accepts only it needs
  partial_box     amber  a line covered for some boxes and part of the next: the part box cannot be packed
  forecast        amber  stems confirmed from the forecast beyond what we are sure of
  non_moving      amber  stock past the amber time that open orders could use (same, longer or an approved variety)
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_packhouse import allocation_plan as plans
from upande_packhouse.api.v2 import allocation_guards as guards
from upande_packhouse.api.v2 import allocation_nonmoving as nm
from upande_packhouse.api.v2 import allocation_playground as pg

RANK = {"red": 0, "amber": 1}
_n = guards._n
_cm = guards._cm


def _norm_stage(value):
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import _norm_cut_stage

	return _norm_cut_stage(value)


def per_box(ln):
	"""Stems of the line in one box: its pack rate (a mix's per-box share), else its stems over its boxes."""
	mixed = cint(ln.custom_mixed_box) or cint(ln.custom_mixed_bunch)
	rate = flt(ln.custom_packrate_mixed_box if mixed else ln.custom_packrate)
	return rate or flt(ln.ordered_stems) / (cint(ln.custom_number_of_boxes) or 1)


def mix_key(ln):
	"""The box a line is packed in with others: its mixed box group or its mixed bunch group (kept apart:
	group "1" of each can sit on the same order)."""
	if cint(ln.custom_mixed_bunch) and ln.custom_bunch_group:
		return (ln.parent, "bunch", ln.custom_bunch_group)
	if cint(ln.custom_mixed_box) and ln.custom_mix_group:
		return (ln.parent, "box", ln.custom_mix_group)
	return None


def _check(code, severity, title, message, ln=None, actions=None, **extra):
	out = {"code": code, "severity": severity, "title": title, "message": message, "actions": actions or []}
	if ln is not None:
		out.update(
			sales_order=ln.parent,
			customer=ln.customer_name or ln.customer,
			soi=ln.name,
			variety=ln.item_code,
			length=ln.custom_length or "",
		)
	out.update(extra)
	return out


def _open(ln):
	return {"kind": "open_order", "label": _("Open this order"), "order": ln.parent}


def _pool(variety, length):
	return {
		"kind": "pool",
		"label": _("Go to {0} {1}").format(variety, length),
		"variety": variety,
		"length": length,
	}


@frappe.whitelist()
def get_plan_checks(delivery_date: str | None = None, farm: str | None = None):
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	model = nm.full_model(day, farm)
	mine = {ln.name for ln in pg._lines_for(day, farm)}
	model.lines = [ln for ln in model.lines if ln.name in mine]
	plan = pg._plan_name(day, farm)
	confs = plans.live_confirmations(plan=plan) if plan else []
	covered = {ln.name: flt(ln.allocated_stems) + _planned(model, ln.name) for ln in model.lines}

	checks = []
	checks += _mixes(model, covered)
	checks += _partial_boxes(model, covered)
	checks += _downgrades(model, confs)
	checks += _cut_stages(model, confs)
	checks += _missing(model, confs)
	checks += _short_and_forecast(model)
	checks += _squeezes(model, confs)
	checks += _non_moving(model)
	checks += _open_options(model, day)
	checks.sort(key=lambda c: (RANK[c["severity"]], c.get("customer") or "", c["title"]))
	counts = defaultdict(int)
	for c in checks:
		counts[c["severity"]] += 1
	return {"success": True, "delivery_date": day, "farm": farm, "checks": checks, "counts": dict(counts)}


def _planned(model, soi):
	pl = model.planned.get(soi, {"real": 0.0, "expected": 0.0})
	return flt(pl["real"]) + flt(pl["expected"])


def _mixes(model, covered):
	"""Mixed boxes and bunches the plan has started that cannot be packed whole."""
	groups = defaultdict(list)
	for ln in model.lines:
		key = mix_key(ln)
		if key:
			groups[key].append(ln)
	out = []
	for members in groups.values():
		if not any(covered[m.name] > 0 for m in members):
			continue
		total = cint(members[0].custom_number_of_boxes) or 1
		boxes = {
			m.name: min(total, int(covered[m.name] / per_box(m) + 1e-9)) if per_box(m) else total
			for m in members
		}
		can = min(boxes.values())
		if can >= total:
			continue
		limiting = [m for m in members if boxes[m.name] == can]
		first = members[0]
		kind = _("mixed bunch") if cint(first.custom_mixed_bunch) else _("mixed box")
		parts = ", ".join(
			_("{0} {1} has {2} of {3}").format(
				m.item_code, m.custom_length or "", _n(covered[m.name]), _n(m.ordered_stems)
			)
			for m in limiting[:3]
		)
		out.append(
			_check(
				"mix_incomplete",
				"red",
				_("{0} · {1}: {2} of {3} boxes").format(
					first.custom_mix_name or kind, first.customer_name or first.customer, can, total
				),
				_("Only {0} of the {1} boxes of this {2} can be packed: {3}.").format(
					can, total, kind, parts
				),
				first,
				[_pool(limiting[0].item_code, limiting[0].custom_length or ""), _open(first)],
			)
		)
	return out


def _partial_boxes(model, covered):
	"""Straight lines covered for some boxes and part of the next: the part box waits for more stock."""
	out = []
	for ln in model.lines:
		if mix_key(ln):
			continue
		size, have, ordered = per_box(ln), covered[ln.name], flt(ln.ordered_stems)
		if not size or have <= 0 or have >= ordered - 0.001:
			continue
		boxes = int(have / size + 1e-9)
		extra = have - boxes * size
		if extra < 0.5:
			continue
		out.append(
			_check(
				"partial_box",
				"amber",
				_("{0} {1} · {2}").format(
					ln.item_code, ln.custom_length or "", ln.customer_name or ln.customer
				),
				_(
					"{0} stems fill {1} of {2} boxes and {3} stems of the next; that box needs {4} more."
				).format(_n(have), boxes, cint(ln.custom_number_of_boxes) or 1, _n(extra), _n(size - extra)),
				ln,
				[_pool(ln.item_code, ln.custom_length or "")],
			)
		)
	return out


def _downgrades(model, confs):
	"""Longer lengths confirmed: who they short, and whether allocation will ask for a reason."""
	out = []
	amber = pg.amber_from_days()
	held_age = _held_ages([c.name for c in confs if c.source == "Real"])
	mine = {x.name for x in model.lines}
	for c in confs:
		ln = model.by_soi.get(c.sales_order_item)
		if not ln or ln.name not in mine:
			continue
		ordered_len, length = ln.custom_length or "", c.length or ""
		if _cm(length) <= _cm(ordered_len):
			continue
		others = [
			o
			for o in model.lines
			if o.name != ln.name
			and o.item_code == c.variety
			and (o.custom_length or "") == length
			and model.left[o.name] > 0
		]
		need, cover = sum(model.left[o.name] for o in others), model.cover(c.variety, length)
		title = _("{0} {1} sent as {2} · {3}").format(
			c.variety, ordered_len, length, ln.customer_name or ln.customer
		)
		if need > cover + 0.5:
			out.append(
				_check(
					"downgrade",
					"red",
					title,
					_(
						"{0} who ordered {1} {2} are short by {3} stems while {4} of it is confirmed here."
					).format(
						model.customers(others),
						c.variety,
						length,
						_n(need - cover),
						_n(flt(c.stems) - flt(c.fulfilled_stems)),
					),
					ln,
					[_pool(c.variety, length), _open(ln)],
				)
			)
		ages = held_age.get(c.name) or []
		if c.source != "Real" or not ages or min(ages) < amber:
			out.append(
				_check(
					"downgrade",
					"amber",
					title,
					_(
						"Not past the amber time ({0} days): Sales Allocation asks for a downgrade reason."
					).format(cint(amber)),
					ln,
					[_pool(ln.item_code, ordered_len)],
				)
			)
	return out


def _held_ages(conf_names):
	"""{confirmation: [age in days of each held bucket]}."""
	out = defaultdict(list)
	if not conf_names:
		return out
	for conf, age in frappe.db.sql(
		"""SELECT br.confirmation,
		          DATEDIFF(CURDATE(), MIN(COALESCE(si.harvest_date, DATE(si.date_added))))
		   FROM `tabBucket Reservation` br
		   INNER JOIN `tabShelf Item` si ON si.bucket_id = br.bucket_id AND si.variety = br.item_code
		   WHERE br.confirmation IN %(c)s AND br.status = 'Active'
		   GROUP BY br.confirmation, br.bucket_id""",
		{"c": tuple(conf_names)},
	):
		out[conf].append(cint(age))
	return out


def _cut_stages(model, confs):
	"""Held buckets at a cut stage the order line does not take: Sales Allocation will not offer them."""
	real = [c for c in confs if c.source == "Real"]
	if not real:
		return []
	stage_of = dict(
		frappe.db.sql(
			"SELECT name, custom_cut_stage FROM `tabSales Order Item` WHERE name IN %(s)s",
			{"s": tuple({c.sales_order_item for c in real})},
		)
	)
	wrong = defaultdict(lambda: {"stems": 0.0, "stages": set()})
	for soi, bucket, stems, stage in frappe.db.sql(
		"""SELECT br.sales_order_item, br.bucket_id, br.stems, MIN(si.cut_stage)
		   FROM `tabBucket Reservation` br
		   INNER JOIN `tabShelf Item` si ON si.bucket_id = br.bucket_id AND si.variety = br.item_code
		   WHERE br.confirmation IN %(c)s AND br.status = 'Active'
		   GROUP BY br.name""",
		{"c": tuple(c.name for c in real)},
	):
		want = stage_of.get(soi)
		if want and stage and _norm_stage(stage) != _norm_stage(want):
			wrong[soi]["stems"] += flt(stems)
			wrong[soi]["stages"].add(stage)
	out = []
	for soi, w in wrong.items():
		ln = model.by_soi.get(soi)
		if not ln:
			continue
		out.append(
			_check(
				"cut_stage",
				"red",
				_("{0} {1} · {2}").format(
					ln.item_code, ln.custom_length or "", ln.customer_name or ln.customer
				),
				_(
					"{0} held stems are cut stage {1}; this order takes {2}, so Sales Allocation does not offer them."
				).format(_n(w["stems"]), ", ".join(sorted(w["stages"])), stage_of[soi]),
				ln,
				[_pool(ln.item_code, ln.custom_length or "")],
			)
		)
	return out


def _missing(model, confs):
	out = []
	for c in confs:
		ln = model.by_soi.get(c.sales_order_item)
		if c.status == "Broken" and ln:
			out.append(
				_check(
					"missing_bucket",
					"red",
					_("{0} {1} · {2}").format(c.variety, c.length or "", ln.customer_name or ln.customer),
					_("A bucket held for this line is no longer on the shelf."),
					ln,
					[_pool(c.variety, c.length or "")],
				)
			)
	return out


def _short_and_forecast(model):
	"""Varieties and lengths the day needs more of than can be covered, and confirmations that lean on the forecast."""
	need = defaultdict(list)
	for ln in model.lines:
		if model.left[ln.name] > 0:
			need[(ln.item_code, ln.custom_length or "")].append(ln)
	out = []
	for (v, length), lines in need.items():
		want, cover = sum(model.left[ln.name] for ln in lines), model.cover(v, length)
		if want > cover + 0.5:
			strict = [ln for ln in lines if model.strict(ln)]
			out.append(
				_check(
					"short",
					"red",
					_("{0} {1}: short by {2}").format(v, length, _n(want - cover)),
					_("{0} still to confirm for {1}; the shelf and forecast cover {2}.").format(
						_n(want), model.customers(strict or lines), _n(cover)
					),
					actions=[_pool(v, length)],
					variety=v,
					length=length,
				)
			)
	for (v, length), planned in model.expected_plan.items():
		if planned <= 0:
			continue
		_likely, sure = model.likely_sure(v, length)
		if planned > sure + 0.5:
			out.append(
				_check(
					"forecast",
					"amber",
					_("{0} {1}: {2} on the forecast").format(v, length, _n(planned)),
					_("{0} stems are confirmed from the forecast and we are sure of {1}.").format(
						_n(planned), _n(sure)
					),
					actions=[_pool(v, length)],
					variety=v,
					length=length,
				)
			)
	return out


def _squeezes(model, confs):
	"""A flexible customer holds a variety a strict customer is short of."""
	held = defaultdict(list)
	for c in confs:
		ln = model.by_soi.get(c.sales_order_item)
		if ln and not model.strict(ln):
			held[(c.variety, c.length or "")].append((ln, flt(c.stems) - flt(c.fulfilled_stems)))
	out = []
	for (v, length), holders in held.items():
		strict = [
			ln
			for ln in model.lines
			if ln.item_code == v
			and (ln.custom_length or "") == length
			and model.left[ln.name] > 0
			and model.strict(ln)
		]
		if not strict:
			continue
		short = sum(model.left[ln.name] for ln in strict) - max(0.0, model.cover(v, length))
		if short <= 0.5:
			continue
		for ln, stems in holders:
			alts = sorted(model.options(ln) - {v})
			out.append(
				_check(
					"squeeze",
					"amber",
					_("{0} {1} · {2}").format(v, length, ln.customer_name or ln.customer),
					_(
						"Holds {0} stems that {1} needs and has no other variety for; {2} also accepts {3}."
					).format(
						_n(stems),
						model.customers(strict),
						ln.customer_name or ln.customer,
						", ".join(alts[:3]),
					),
					ln,
					[_open(ln)],
				)
			)
	return out


def _non_moving(model):
	"""Stock past the amber time that open orders could take: the same length, a longer one, or an approved variety."""
	floor = max(1, cint(plans.setting("allocation_use_first_alert_stems")))
	out = []
	for (v, length), s in model.supply.items():
		old = flt(s.get("old"))
		if old < floor:
			continue
		users = {kind: nm._wanting(model, v, length, kind) for kind in ("exact", "longer", "substitute")}
		if not any(users.values()):
			continue
		parts = []
		for kind, label in (
			("exact", _("same length")),
			("longer", _("a shorter length")),
			("substitute", _("an approved variety")),
		):
			if users[kind]:
				parts.append(_("{0} ({1})").format(model.customers(users[kind]), label))
		out.append(
			_check(
				"non_moving",
				"amber",
				_("{0} {1}: {2} past the amber time").format(v, length, _n(old)),
				_("Could go to {0}.").format("; ".join(parts)),
				actions=[{"kind": "nonmoving", "label": _("Open Non-moving")}],
				variety=v,
				length=length,
			)
		)
	return out


def _open_options(model, day):
	"""For lines still to plan: a longer length of the same variety with stock to spare, a variety the
	specification approves with stock to spare, and stock that would be too old by the delivery day."""
	from frappe.utils import getdate, today

	from upande_packhouse.api.v2.core import stock

	discard = cint(stock._config()["discard_age"])
	days_out = (getdate(day) - getdate(today())).days
	need = defaultdict(float)
	for ln in model.lines:
		need[(ln.item_code, ln.custom_length or "")] += model.left[ln.name]

	def spare(v, length):
		p = model.pool(v, length)
		return max(0.0, flt(p["free"]) + flt(p["received"]) - need.get((v, length), 0.0))

	out, seen = [], set()
	for ln in model.lines:
		left = model.left[ln.name]
		if left <= 0:
			continue
		v, length = ln.item_code, ln.custom_length or ""
		short = left > 0 and model.cover(v, length) < need[(v, length)] - 0.5
		if short:
			longer = [
				sl for (sv, sl) in model.supply if sv == v and _cm(sl) > _cm(length) and spare(sv, sl) >= 1
			]
			if longer and ("longer", v, length) not in seen:
				seen.add(("longer", v, length))
				out.append(
					_check(
						"longer",
						"amber",
						_("{0} {1}: {2} has stock to spare").format(
							v, length, ", ".join(sorted(longer, key=_cm)[:2])
						),
						_("A longer stem can cover what {0} {1} is short of.").format(v, length),
						ln,
						[_pool(v, length)],
					)
				)
			alts = [
				a
				for a in sorted(model.options(ln) - {v})
				if any(spare(a, sl) >= 1 for (sv, sl) in model.supply if sv == a and _cm(sl) >= _cm(length))
			]
			if alts and ("sub", ln.name) not in seen:
				seen.add(("sub", ln.name))
				out.append(
					_check(
						"substitute",
						"amber",
						_("{0} {1} · {2}").format(v, length, ln.customer_name or ln.customer),
						_("Substitute {0} with {1}.").format(v, ", ".join(alts[:3])),
						ln,
						[_pool(v, length)],
					)
				)
		p = model.pool(v, length)
		if (
			flt(p["free"]) > 0
			and cint(p.get("oldest")) + days_out >= discard
			and ("delivery", v, length) not in seen
		):
			seen.add(("delivery", v, length))
			out.append(
				_check(
					"delivery",
					"amber",
					_("{0} {1}: {2} days old by delivery").format(
						v, length, cint(p.get("oldest")) + days_out
					),
					_(
						"The oldest stock on the shelf will be past the {0}-day discard age by the delivery day; confirm it on the forecast."
					).format(discard),
					ln,
					[_pool(v, length)],
				)
			)
	return out
