# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Substitution options for an order line (the Substitute button on Allocation Planning v2).

A customer's specification lists the varieties it approves, per bunch: one is the PRIMARY (what the customer is
normally sent) and the others are APPROVED alternatives. Colour is a factor too: an alternative of another colour is
flagged, and in a mixed box it is checked against the specification's most colours per box.

For one order line this reads the specification and returns, for every approved alternative in the same bunch as the
ordered variety, what exists at the line's length from every source the page confirms against: on the shelf, received and
not shelved, harvested and not received, and the forecast; what is already taken by confirmations; and what other customers
still need of it. Nothing is written. A substitute is a different variety on the order line, so the page links to the order
to change it; the allocator allocates the variety that is on the line.
"""

import frappe
from frappe import _
from frappe.utils import flt

from upande_packhouse.api.v2 import allocation_guards as guards
from upande_packhouse.api.v2 import allocation_playground as pg


def pick_candidates(rows, ordered, colours):
	"""Approved alternatives to `ordered`, best first.

	`rows` are the specification's approved varieties ({bunch_id, variety, is_primary, colour}); `colours` maps a variety to its
	colour (the specification's own, else the item's). Only varieties in the same bunch as the ordered variety are offered
	(every approved variety when the ordered one is not in the list). Order: primary before approved, then the colour of the
	ordered variety before another colour. Each comes back as {variety, primary, colour, same_colour (True / False / None
	when a colour is not known)}."""
	bunches = {r["bunch_id"] or "" for r in rows if r["variety"] == ordered}
	pool = [r for r in rows if r["variety"] != ordered and (not bunches or (r["bunch_id"] or "") in bunches)]
	ref = colours.get(ordered)
	seen, out = {}, []
	for r in pool:
		v = r["variety"]
		if v in seen:
			seen[v]["primary"] = seen[v]["primary"] or bool(r["is_primary"])
			continue
		c = colours.get(v)
		item = {
			"variety": v,
			"primary": bool(r["is_primary"]),
			"colour": c or "",
			"same_colour": (c == ref) if (c and ref) else None,
		}
		seen[v] = item
		out.append(item)
	out.sort(key=lambda x: (not x["primary"], x["same_colour"] is False, x["variety"]))
	return out


def _colours(varieties, spec_rows):
	"""{variety: colour}: the specification's colour for the variety, else the item's colour."""
	out = {r["variety"]: r["colour"] for r in spec_rows if r.get("colour")}
	missing = [v for v in varieties if v not in out]
	if missing:
		for name, colour in frappe.db.sql(
			"SELECT name, custom_color FROM `tabItem` WHERE name IN %(v)s AND IFNULL(custom_color, '') != ''",
			{"v": tuple(missing)},
		):
			out[name] = colour
	return out


def _sources(model, variety, length):
	"""Everything that exists for a variety and length, by source, and what is left to confirm."""
	p = model.pool(variety, length)
	likely, sure = model.likely_sure(variety, length)
	taken_fc = model.expected_plan.get((variety, length or ""), 0.0)
	return {
		"variety": variety,
		"length": length or "",
		"shelved": p["free"],
		"received": p["received"],
		"harvested": p["on_the_way"],
		"forecast": max(0.0, likely - p["on_the_way"]),
		"forecast_sure": max(0.0, sure - p["on_the_way"]),
		"shelved_by_farm": [
			{"farm": f, "stems": v} for f, v in sorted(p["by_farm"].items(), key=lambda kv: -kv[1]) if v > 0
		],
		"taken": taken_fc,
		"left": model.cover(variety, length),
		"left_sure": model.cover(variety, length, sure=True),
		"error_pct": model.error_pct(variety),
	}


@frappe.whitelist()
def get_substitutions(
	delivery_date: str | None = None, farm: str | None = None, sales_order_item: str | None = None
):
	"""What an order line could be substituted with, by its specification, with every source for each option."""
	day = pg._day(delivery_date)
	farm = farm or pg._default_farm()
	ln = pg._line(sales_order_item)
	spec = ln.custom_line
	length = ln.custom_length or ""
	meta = (
		frappe.db.get_value(
			"Specifications",
			spec,
			["box_assortment", "min_colours_per_box", "max_colours_per_box"],
			as_dict=True,
		)
		if spec
		else None
	)
	out = {
		"success": True,
		"delivery_date": day,
		"farm": farm,
		"has_spec": bool(meta),
		"options": [],
		"line": {
			"soi": ln.name,
			"sales_order": ln.parent,
			"customer": ln.customer_name or ln.customer,
			"variety": ln.item_code,
			"length": length,
			"spec": spec or "",
			"assortment": (meta or {}).get("box_assortment") or "",
			"mixed": bool(ln.custom_mixed_box or ln.custom_mixed_bunch),
		},
	}
	if not meta:
		out["note"] = (
			_("This order line has no specification, so there are no approved varieties to substitute with.")
			if not spec
			else _("The specification of this order line was not found.")
		)
		return out
	rows = [
		dict(r)
		for r in frappe.get_all(
			"Spec Approved Variety",
			filters={"parent": spec, "parenttype": "Specifications"},
			fields=["bunch_id", "variety", "is_primary", "colour"],
			order_by="idx",
		)
	]
	names = {ln.item_code} | {r["variety"] for r in rows if r["variety"]}
	colours = _colours(names, rows)
	cands = pick_candidates(rows, ln.item_code, colours)
	model = guards.Model(day, farm)
	need = model.left.get(ln.name)
	if need is None:
		need = max(0.0, flt(ln.ordered_stems) - flt(ln.allocated_stems))
	own = _sources(model, ln.item_code, length)
	own["colour"] = colours.get(ln.item_code, "")
	out["ordered"] = {**own, "need": need, "short_by": max(0.0, need - own["left"])}
	out["need"] = need
	box_colours = set()
	if out["line"]["mixed"]:
		key = guards._mix_key(ln)
		box_colours = {
			colours.get(m.item_code) for m in model.lines if m.name != ln.name and guards._mix_key(m) == key
		} - {None, ""}
	max_colours = int((meta or {}).get("max_colours_per_box") or 0)
	for c in cands:
		src = _sources(model, c["variety"], length)
		others = [
			m
			for m in model.lines
			if m.item_code == c["variety"]
			and (m.custom_length or "") == length
			and m.name != ln.name
			and model.left[m.name] > 0
		]
		wanted = sum(model.left[m.name] for m in others)
		strict = [m for m in others if model.strict(m)]
		verdict = (
			"covers" if src["left"] > 0 and src["left"] >= need else "partly" if src["left"] > 0 else "none"
		)
		warn = []
		if c["same_colour"] is False:
			warn.append(_("A different colour from {0}.").format(ln.item_code))
		if max_colours and c["colour"] and len(box_colours | {c["colour"]}) > max_colours:
			warn.append(
				_("The box would have {0} colours; the specification allows {1}.").format(
					len(box_colours | {c["colour"]}), max_colours
				)
			)
		out["options"].append(
			{
				**c,
				**src,
				"verdict": verdict,
				"needed_by_others": wanted,
				"strict_customers": model.customers(strict) if strict else "",
				"warnings": warn,
			}
		)
	return out
