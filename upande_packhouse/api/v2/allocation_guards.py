# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Heads-ups for Allocation Planning v2 (phase 2 of docs/allocation-playground/spec.md).

Before a confirmation is saved, `check` looks at what it does to the other customers of the same day and says
so in plain words. Nothing here writes. The page shows a heads-up as a small popup with the next best step.

Heads-ups (code, severity):

  longer_length   red / amber / quiet  confirming a longer length than ordered while other orders for that
                                       length are not covered. Red when one of them has no other variety it
                                       would accept; quiet when the stock used is already getting old.
  squeeze         amber                confirming a variety to a customer who also accepts other varieties
                                       while a customer who accepts only that variety is not covered.
  mix_incomplete  red                  a mixed box or bunch that cannot be completed: one of its varieties and
                                       lengths is not covered.
  not_enough      red / amber          more than the shelf and the farm forecast can cover (red), or more than
                                       we are sure of (amber).

"Covered" for a variety and length is: free stems on the shelf (allocatable, so already net of age, cooling and
stems held for others) + the likely forecast for the cutting day, less what is already confirmed from it.
Customers who accept only one variety are the ones whose specification approves one variety or who have no
specification (treated as strict); the others are flexible. Ages come from Production Settings via core/stock.

Also here: `get_use_first` (stock that is getting old, matched to the lines that can use it).
"""

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_packhouse import allocation_plan as plans
from upande_packhouse import availability
from upande_packhouse.api.v2 import allocation_playground as pg

RED, AMBER, QUIET = "red", "amber", "quiet"
NAMES_SHOWN = 3


def _n(x):
	"""Stems as people read them: 12,345."""
	return "{:,}".format(int(round(x or 0)))


def _cm(length):
	return availability.length_cm(length)


def _mix_key(ln):
	if ln.custom_mixed_box or ln.custom_mixed_bunch:
		return "%s|%s" % (ln.parent, ln.custom_mix_group or ln.custom_bunch_group or "")
	return ""


def _spec_varieties(lines):
	"""{specification: {approved varieties}} for the specifications the lines use."""
	specs = {ln.custom_line for ln in lines if ln.custom_line}
	out = defaultdict(set)
	if specs:
		for spec, variety in frappe.db.sql(
			"""SELECT parent, variety FROM `tabSpec Approved Variety`
			   WHERE parenttype = 'Specifications' AND parent IN %(s)s AND IFNULL(variety, '') != ''""",
			{"s": tuple(specs)},
		):
			out[spec].add(variety)
	return out


class Model:
	"""One delivery day and farm: every line with what is left to confirm, and what covers each pool."""

	def __init__(self, day, farm, overrides=None):
		self.day, self.farm = day, farm
		self.lines = pg._lines(day)
		self.by_soi = {ln.name: ln for ln in self.lines}
		self.planned = overrides if overrides is not None else pg._planned_by_line(day, farm)
		self.supply = pg._stock_by_pool(farm, {ln.item_code for ln in self.lines}, day)
		self._error = {}
		self.specs = _spec_varieties(self.lines)
		self._likely = {}
		self.expected_plan = defaultdict(float)  # pool -> stems already confirmed from the forecast
		self.left = {}
		for ln in self.lines:
			pl = self.planned.get(ln.name, {"real": 0.0, "expected": 0.0})
			self.left[ln.name] = max(
				0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems) - pl["real"] - pl["expected"]
			)
			self.expected_plan[(ln.item_code, ln.custom_length or "")] += pl["expected"]

	def pool(self, variety, length):
		return {**pg.SUPPLY, **self.supply.get((variety, length or ""), {})}

	def likely_sure(self, variety, length):
		"""(likely, sure) stems still to come: the day's harvest not yet received plus what is still to be cut."""
		k = (variety, length or "")
		if k not in self._likely:
			self._likely[k] = pg.expected_for(variety, length or "", self.day, self.farm, self.pool(variety, length))
		return self._likely[k]

	def error_pct(self, variety):
		"""How far off the variety's forecast has been this year, in percent; None when there is nothing to judge by."""
		if variety not in self._error:
			err, _weeks = pg.forecast_error(variety, self.farm)
			self._error[variety] = None if err is None else round(100.0 * err)
		return self._error[variety]

	def cover(self, variety, length, sure=False):
		"""Stems that can still be confirmed for the variety and length: on the shelf, received and not
		shelved, and still to come."""
		likely, sure_v = self.likely_sure(variety, length)
		fc = (sure_v if sure else likely) - self.expected_plan.get((variety, length or ""), 0.0)
		p = self.pool(variety, length)
		return p["free"] + p["received"] + max(0.0, fc)

	def options(self, ln):
		"""Varieties this line's customer accepts (at least the one ordered)."""
		return (self.specs.get(ln.custom_line) or set()) | {ln.item_code}

	def strict(self, ln):
		return len(self.options(ln)) <= 1 or not ln.custom_line

	def customers(self, lines, n=NAMES_SHOWN):
		names = []
		for ln in sorted(lines, key=lambda x: -self.left[x.name]):
			c = ln.customer_name or ln.customer
			if c not in names:
				names.append(c)
		more = len(names) - n
		return ", ".join(names[:n]) + (_(" and {0} more").format(more) if more > 0 else "")


def _requests(model, lines):
	"""[{ln, want, length}] for the lines being confirmed, validated against the model."""
	out = []
	for item in lines:
		ln = model.by_soi.get(item["sales_order_item"])
		if ln is None:
			frappe.throw(_("That order line is not open for planning on this day."))
		length = item.get("length") or ln.custom_length or ""
		if _cm(length) < _cm(ln.custom_length):
			frappe.throw(_("Only the ordered length or a longer one can be confirmed."))
		out.append({"ln": ln, "want": flt(item.get("stems")), "length": length})
	return out


def _current(model, soi):
	pl = model.planned.get(soi, {"real": 0.0, "expected": 0.0})
	return pl["real"] + pl["expected"]


def _stage_free(model, ln, length):
	"""Free shelf stems of the line's variety and length at its cut stage, or None when the line has no cut stage."""
	stage = frappe.db.get_value("Sales Order Item", ln.name, "custom_cut_stage")
	if not stage:
		return None
	from upande_packhouse.api.v2.core import stock
	from upande_packhouse.upande_packhouse.page.sales_allocation_v2.sales_allocation_v2 import _norm_cut_stage

	rows = stock.bucket_rows(farms=pg._stock_farms(model.farm), varieties=[ln.item_code], lengths=[length] if length else None)
	return sum(r.allocatable for r in rows if _norm_cut_stage(r.cut_stage) == _norm_cut_stage(stage))


def _item(code, severity, message, actions=None, customers=None):
	return {"code": code, "severity": severity, "message": message, "actions": actions or [], "customers": customers or ""}


def check(day, farm, lines, model=None):
	"""The heads-ups for confirming `lines` ([{sales_order_item, stems, length?}], totals per line)."""
	model = model or Model(day, farm)
	reqs = _requests(model, lines)
	asked = {r["ln"].name for r in reqs}
	# stems each pool loses to this request, and what is left of its cover afterwards
	take = defaultdict(float)
	for r in reqs:
		take[(r["ln"].item_code, r["length"])] += max(0.0, r["want"] - _current(model, r["ln"].name))
	cover_after = {k: model.cover(k[0], k[1]) - t for k, t in take.items()}
	items, seen = [], set()

	def add(it, key):
		if key not in seen:
			seen.add(key)
			items.append(it)

	for r in reqs:
		ln, length, want = r["ln"], r["length"], r["want"]
		if want <= _current(model, ln.name) + 0.0001:
			continue  # confirming less or the same never takes from anyone
		v, ordered_len = ln.item_code, ln.custom_length or ""
		pool_key = (v, length)
		delta = want - _current(model, ln.name)

		# --- longer length
		if _cm(length) > _cm(ordered_len):
			others = [
				o
				for o in model.lines
				if o.name not in asked and o.item_code == v and (o.custom_length or "") == length and model.left[o.name] > 0
			]
			need = sum(model.left[o.name] for o in others)
			short_by = max(0.0, need - max(cover_after[pool_key], 0.0))
			if short_by > 0 and model.pool(v, length)["old"] < delta:
				strict = [o for o in others if model.strict(o)]
				who = model.customers(strict or others)
				msg = _(
					"If you send {0} here, {1} who ordered {0} {2} will be short by {3} stems. Send {4} instead?"
				).format(length, who, v, _n(short_by), ordered_len)
				add(
					_item(
						"longer_length",
						RED if strict else AMBER,
						msg,
						[{"kind": "use_ordered_length", "label": _("Confirm {0} instead").format(ordered_len)}],
						who,
					),
					("longer", ln.name),
				)
			else:
				why = _("this stock is getting old") if model.pool(v, length)["old"] >= delta else _("nobody else needs {0}").format(length)
				add(_item("longer_length", QUIET, _("Longer than ordered, but {0}.").format(why)), ("longer", ln.name))
			# Sales Allocation asks for a downgrade reason unless the bucket is past the amber time
			if model.pool(v, length)["old"] < delta:
				add(
					_item(
						"downgrade_reason",
						AMBER,
						_("{0} {1} is not past the amber time, so Sales Allocation will ask for a downgrade reason for {2}.").format(
							v, length, ln.customer_name or ln.customer
						),
						[{"kind": "use_ordered_length", "label": _("Confirm {0} instead").format(ordered_len)}],
					),
					("reason", ln.name),
				)

		# --- the shelf stock at the cut stage this order takes (the only buckets Sales Allocation offers it)
		stage_free = _stage_free(model, ln, length)
		if stage_free is not None:
			shelf = model.pool(v, length)["free"]
			if stage_free < min(delta, shelf) - 0.5:
				add(
					_item(
						"cut_stage",
						AMBER,
						_("Only {0} of the {1} {2} {3} stems on the shelf are cut stage {4}, the stage this order takes; the rest of these {5} stems would come from the forecast.").format(
							_n(stage_free), _n(shelf), v, length, frappe.db.get_value("Sales Order Item", ln.name, "custom_cut_stage"), _n(delta)
						),
					),
					("stage", ln.name),
				)

		# --- whole boxes only: a part box waits for more stock before it can be packed
		from upande_packhouse.api.v2.allocation_checks import mix_key, per_box

		size = per_box(ln)
		if not mix_key(ln) and size and want < flt(ln.ordered_stems) - 0.001:
			have = flt(ln.allocated_stems) + want
			extra = have - int(have / size + 1e-9) * size
			if extra >= 0.5:
				add(
					_item(
						"partial_box",
						AMBER,
						_("{0} stems fill {1} boxes and {2} stems of the next; {3} more would complete it.").format(
							_n(have), int(have / size + 1e-9), _n(extra), _n(size - extra)
						),
					),
					("box", ln.name),
				)

		# --- another customer needs this variety
		if not model.strict(ln):
			peers = [
				o
				for o in model.lines
				if o.name not in asked and o.item_code == v and (o.custom_length or "") == length and model.left[o.name] > 0
			]
			strict_peers = [o for o in peers if model.strict(o)]
			peer_need = sum(model.left[o.name] for o in peers)
			if strict_peers and peer_need > max(cover_after[pool_key], 0.0):
				alts = sorted(model.options(ln) - {v})
				who = model.customers(strict_peers)
				msg = _("{0} needs {1} and has no other variety it accepts. {2} also accepts {3}.").format(
					who, v, ln.customer_name or ln.customer, ", ".join(alts[:3]) or _("other varieties")
				)
				add(
					_item(
						"squeeze",
						AMBER,
						msg,
						[{"kind": "open_order", "label": _("Open this order"), "order": ln.parent}],
						who,
					),
					("squeeze", ln.name),
				)

		# --- mixed box or bunch that cannot be completed
		mk = _mix_key(ln)
		if mk:
			for m in [o for o in model.lines if _mix_key(o) == mk]:
				req_m = next((x for x in reqs if x["ln"].name == m.name), None)
				confirmed_after = req_m["want"] if req_m else _current(model, m.name)
				remaining = max(0.0, flt(m.ordered_stems) - flt(m.allocated_stems) - confirmed_after)
				pool_m = (m.item_code, req_m["length"] if req_m else (m.custom_length or ""))
				covered = cover_after.get(pool_m, model.cover(*pool_m))
				short_by = remaining - max(covered, 0.0)
				if short_by > 0.0001:
					alts = sorted(model.options(m) - {m.item_code})
					msg = _("This mixed box needs {0} {1} too, and we are short of it by {2} stems, so the box could not be completed.").format(
						m.item_code, pool_m[1], _n(short_by)
					)
					if alts:
						msg += " " + _("It also accepts {0}.").format(", ".join(alts[:3]))
					add(_item("mix_incomplete", RED, msg, []), ("mix", mk, m.name))

		# --- stock past the amber time that this line could use instead (it is on its way to being thrown away)
		from upande_packhouse.api.v2 import allocation_nonmoving as nm

		old_notes = []
		for (sv, sl), sp in model.supply.items():
			if sp["old"] < 1 or (sv, sl) == pool_key:
				continue
			if sv == v and _cm(sl) > _cm(length):
				kind = "longer"
			elif sv != v and sv in model.options(ln) and _cm(sl) >= _cm(ordered_len):
				kind = "substitute"
			else:
				continue
			risk = nm.at_risk(model, sv, sl)
			if risk >= 1:
				old_notes.append((risk, kind, sv, sl))
		for risk, kind, sv, sl in sorted(old_notes, reverse=True)[:2]:
			if kind == "longer":
				msg = _("{0} stems of {1} {2} are past the amber time and nobody else has an order for them. This line could use them (a longer stem is fine for stock this old).").format(_n(risk), sv, sl)
				acts = [{"kind": "use_length", "label": _("Confirm {0} instead").format(sl), "length": sl}]
			else:
				msg = _("{0} stems of {1} {2} are past the amber time and nobody has an order for them. {3} also accepts {1}: change the order to use it before it is thrown away.").format(
					_n(risk), sv, sl, ln.customer_name or ln.customer
				)
				acts = [{"kind": "open_order", "label": _("Open this order"), "order": ln.parent}]
			add(_item("non_moving", AMBER, msg, acts), ("old", ln.name, sv, sl))

		# --- more than we have
		p = model.pool(v, length)
		cov = model.cover(v, length)
		cov_sure = model.cover(v, length, sure=True)
		pool_take = take[pool_key]
		if pool_take > cov + 0.0001:
			over = pool_take - cov
			parts = [_("{0} on the shelf").format(_n(p["free"]))]
			if p["received"]:
				parts.append(_("{0} received and not shelved yet").format(_n(p["received"])))
			parts.append(_("{0} still to come from the farms").format(_n(max(0.0, cov - p["free"] - p["received"]))))
			msg = _(
				"You are confirming {0} stems of {1} {2}, but we can only cover {3}: {4}. The other {5} would be short."
			).format(_n(pool_take), v, length, _n(cov), ", ".join(parts), _n(over))
			add(_item("not_enough", RED, msg), ("short", pool_key))
		elif pool_take > cov_sure + 0.0001:
			err = model.error_pct(v)
			why = (
				_("the forecast for {0} has been off by {1}% on average this year, so we count only {2} of it").format(v, err, _n(cov_sure))
				if err is not None
				else _("there are no finished weeks to judge the forecast for {0} by, so none of it counts as sure").format(v)
			)
			msg = _("You are confirming {0} stems of {1} {2}. We are sure of {3}: {4}. The other {5} depend on the forecast being right.").format(
				_n(pool_take), v, length, _n(cov_sure), why, _n(pool_take - cov_sure)
			)
			add(_item("not_enough", AMBER, msg), ("short", pool_key))

	loud = [i for i in items if i["severity"] in (RED, AMBER)]
	return {
		"ok": not loud,
		"heads_up": loud,
		"quiet": [i["message"] for i in items if i["severity"] == QUIET],
	}


@frappe.whitelist()
def preview_confirmation(delivery_date: str | None = None, farm: str | None = None, lines: str | list | None = None):
	"""What confirming these lines would do to the other customers. Writes nothing."""
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	if isinstance(lines, str):
		lines = json.loads(lines or "[]")
	return {"success": True, **check(day, farm, lines or [])}


@frappe.whitelist()
def get_use_first(delivery_date: str | None = None, farm: str | None = None):
	"""Stock that is getting old, matched to the lines that can use it now: the same variety and length first,
	then lines that ordered a shorter length (a longer stem is fine for stock that is getting old)."""
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	model = Model(day, farm)
	floor = cint(plans.setting("allocation_use_first_alert_stems"))
	out = []
	for (v, length), s in sorted(model.supply.items(), key=lambda kv: -kv[1]["old"]):
		if s["old"] < max(floor, 1):
			continue
		left_old = s["old"]
		by_farm = s.get("old_by_farm") or {}
		old_farm = max(by_farm, key=by_farm.get) if by_farm else farm  # the farm holding most of the old stock
		matches = []
		exact = [ln for ln in model.lines if ln.item_code == v and (ln.custom_length or "") == length and model.left[ln.name] > 0]
		shorter = [
			ln
			for ln in model.lines
			if ln.item_code == v and 0 < _cm(ln.custom_length) < _cm(length) and model.left[ln.name] > 0
		]
		for kind, group in (("exact", exact), ("longer", shorter)):
			for ln in sorted(group, key=lambda x: -model.left[x.name]):
				if left_old <= 0:
					break
				take = min(left_old, model.left[ln.name])
				left_old -= take
				matches.append(
					{
						"kind": kind,
						"soi": ln.name,
						"customer": ln.customer_name or ln.customer,
						"order": ln.parent,
						"ordered_length": ln.custom_length or "",
						"take": take,
						"team": plans.team_for_line(ln.name) or plans.group_team(_mix_key(ln)) or "",
						"apply": {
							"sales_order_item": ln.name,
							"stock_farm": old_farm,
							"stems": _current(model, ln.name) + take,
							**({"length": length} if kind == "longer" else {}),
						},
					}
				)
		if matches:
			out.append({"variety": v, "length": length, "old_stems": s["old"], "matches": matches, "unused": left_old})
	return {"success": True, "delivery_date": day, "farm": farm, "amber_from_days": pg.amber_from_days(), "items": out}
