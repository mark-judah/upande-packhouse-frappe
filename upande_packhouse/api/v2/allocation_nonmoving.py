# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Non-moving stock: stock that has reached the amber time (Production Settings) and is on its way to becoming a discard.

The Non-moving tab of Allocation Planning v2 lists it by variety and length, against the customers who could use it:

  exact       an order line for the same variety and length that still needs stems,
  longer      an order line for the same variety that ordered a SHORTER length (a longer stem is fine for stock this old),
  substitute  an order line for another variety its specification approves (colour flagged): advice only, since the
              allocator packs the variety on the order line, so sales changes the order to use it.

`at_risk(model, variety, length)` is the part nobody has a line for: the stems the confirmations should warn about.
Confirming exact and longer matches goes through the normal confirm flow (heads-ups included).
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_packhouse import allocation_plan as plans
from upande_packhouse.api.v2 import allocation_guards as guards
from upande_packhouse.api.v2 import allocation_playground as pg
from upande_packhouse.api.v2.allocation_substitutes import _colours

_cm = guards._cm


def full_model(day, farm):
	"""The planning model, with stock of varieties nobody ordered added: those are the likeliest to be thrown away."""
	model = guards.Model(day, farm)
	for k, v in pg._stock_by_pool(farm, None, None, everything=True).items():
		model.supply.setdefault(k, v)
	return model


def _wanting(model, variety, length, mode):
	"""Lines still needing stems that could take stock of `variety` at `length`."""
	out = []
	for ln in model.lines:
		if model.left[ln.name] <= 0:
			continue
		ordered = ln.custom_length or ""
		if mode == "exact" and ln.item_code == variety and ordered == length:
			out.append(ln)
		elif mode == "longer" and ln.item_code == variety and 0 < _cm(ordered) < _cm(length):
			out.append(ln)
		elif (
			mode == "substitute"
			and ln.item_code != variety
			and variety in (model.specs.get(ln.custom_line) or ())
			and 0 < _cm(ordered) <= _cm(length)
		):
			out.append(ln)
	return out


def at_risk(model, variety, length):
	"""Stems of this pool that are past the amber time and that no exact line is waiting for."""
	old = model.pool(variety, length)["old"]
	if old <= 0:
		return 0.0
	wanted = sum(model.left[ln.name] for ln in _wanting(model, variety, length, "exact"))
	return max(0.0, old - wanted)


@frappe.whitelist()
def get_non_moving(delivery_date: str | None = None, farm: str | None = None):
	day, farm = pg._day(delivery_date), farm or pg._default_farm()
	model = full_model(day, farm)
	amber = pg.amber_from_days()
	items = []
	for (v, length), s in sorted(model.supply.items(), key=lambda kv: -kv[1]["old"]):
		if s["old"] < 1:
			continue
		left_old = s["old"]
		by_farm = s.get("old_by_farm") or {}
		old_farm = max(by_farm, key=by_farm.get) if by_farm else farm
		matches = []
		for kind in ("exact", "longer", "substitute"):
			group = sorted(_wanting(model, v, length, kind), key=lambda x: -model.left[x.name])
			colours = {}
			if kind == "substitute" and group:
				colours = _colours({v} | {g.item_code for g in group}, [])
			for ln in group:
				take = min(left_old, model.left[ln.name]) if left_old > 0 else 0.0
				if kind != "substitute" and take <= 0:
					break  # the old stock is all spoken for
				if kind != "substitute":
					left_old -= take
				m = {
					"kind": kind,
					"soi": ln.name,
					"customer": ln.customer_name or ln.customer,
					"order": ln.parent,
					"variety": ln.item_code,
					"ordered_length": ln.custom_length or "",
					"needs": model.left[ln.name],
					"take": take,
					"team": plans.team_for_line(ln.name) or plans.group_team(guards._mix_key(ln)) or "",
					"mixed": bool(ln.custom_mixed_box or ln.custom_mixed_bunch),
				}
				if kind == "substitute":
					m["colour_differs"] = bool(
						colours.get(v) and colours.get(ln.item_code) and colours[v] != colours[ln.item_code]
					)
					m["take"] = min(s["old"], model.left[ln.name])
				else:
					m["apply"] = {
						"sales_order_item": ln.name,
						"stock_farm": old_farm,
						"stems": guards._current(model, ln.name) + take,
						**({"length": length} if kind == "longer" else {}),
					}
				matches.append(m)
		items.append(
			{
				"variety": v,
				"length": length,
				"old_stems": s["old"],
				"oldest_days": s["oldest"],
				"farm": old_farm,
				"unused": max(0.0, left_old),
				"matches": matches,
			}
		)
	return {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"amber_from_days": amber,
		"total": sum(i["old_stems"] for i in items),
		"at_risk": sum(i["unused"] for i in items),
		"items": items,
	}
