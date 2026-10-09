# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Price Lists v2 (www/price-lists-v2.html).

One read endpoint, ``get_price_grid``, that returns the Variety x length grid
for a selling price list together with KPIs computed here over the full
filtered set (README rules 3, 7). Every filter on the page is applied before
the KPIs are counted, so Coverage / Priced / Missing / Varieties / Rate range
always describe exactly the rows the grid shows.

Scope (same as v1 ``api/price_list_management.getPriceTree``, which keeps
serving the v1 page unchanged): items under the Cut Flowers line / category
item groups (excluding "Cut Flowers - Legacy") and the six standard lengths.

"As at" date: an Item Price row counts for a cell on date D when
``valid_from <= D`` (or empty) and ``valid_upto >= D`` (or empty) -- the
standard ERPNext validity window, the same rule discards.py / downgrades.py
use to value stems. When several rows are valid for one cell the one with
the latest valid_from wins (then latest modified). Rates are edited in place
(no rate history is kept), so a past date shows which prices existed then at
their *current* rate.

Writes stay on v1 ``setItemPrice`` (unchanged); the page only allows editing
on today's view.

There is no farm notion on a price list (lists are per customer / currency),
so the region filter (README rule 6a) does not apply here.
"""

import datetime
import re

import frappe
from frappe.utils import today

from upande_packhouse.api.price_list_management import LENGTHS

ROOT = "Cut Flowers"
LEGACY = "Cut Flowers - Legacy"


def _taxonomy():
	"""[(line, [(category_group, display_name), ...]), ...] in name order."""
	if not frappe.db.exists("Item Group", ROOT):
		return []
	lines = [
		r.name
		for r in frappe.get_all(
			"Item Group", filters={"parent_item_group": ROOT}, fields=["name"], order_by="name"
		)
		if r.name != LEGACY
	]
	cats = {}
	if lines:
		for c in frappe.get_all(
			"Item Group",
			filters={"parent_item_group": ["in", lines]},
			fields=["name", "parent_item_group"],
			order_by="name",
		):
			cats.setdefault(c.parent_item_group, []).append(c.name)
	out = []
	for ln in lines:
		groups = cats.get(ln) or [ln]
		out.append((ln, [(g, g[len(ln) + 3 :] if g.startswith(ln + " - ") else g) for g in groups]))
	return out


def _rates(price_list, codes, as_at):
	"""{item_code: {length: rate}} for rows valid on as_at (latest valid_from wins)."""
	rates = {}
	if not codes:
		return rates
	for r in frappe.db.sql(
		"""
		SELECT item_code, custom_length, price_list_rate
		FROM `tabItem Price`
		WHERE selling = 1 AND price_list = %(pl)s
		  AND custom_length IN %(lengths)s AND item_code IN %(codes)s
		  AND (valid_from IS NULL OR valid_from <= %(d)s)
		  AND (valid_upto IS NULL OR valid_upto >= %(d)s)
		ORDER BY valid_from IS NULL DESC, valid_from, modified
		""",
		{"pl": price_list, "lengths": tuple(LENGTHS), "codes": tuple(codes), "d": as_at},
		as_dict=True,
	):
		rates.setdefault(r.item_code, {})[r.custom_length] = float(r.price_list_rate or 0)
	return rates


def _median(vals):
	if not vals:
		return None
	s = sorted(vals)
	n = len(s)
	return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


@frappe.whitelist()
def get_price_grid(
	price_list: str | None = None,
	as_at: str | None = None,
	line: str | None = None,
	category: str | None = None,
	length: str | None = None,
	status: str | None = None,
	show: str | None = None,
	q: str | None = None,
):
	"""Grid + KPIs for one selling price list.

	Filters (all optional except price_list):
	  as_at     date the prices must be valid on (default today)
	  line      Cut Flowers line item group
	  category  category item group (child of a line)
	  length    one of the six lengths; narrows the columns and every count
	  status    "active" | "inactive" (Item.disabled)
	  show      "all" | "gaps" (at least one missing rate in the shown lengths)
	            | "complete" (no missing rate in the shown lengths)
	  q         substring of variety name, item code, category or line
	"""
	if not price_list or not frappe.db.exists("Price List", price_list):
		return {"success": False, "error": "Unknown price list: " + str(price_list)}
	try:
		if as_at and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(as_at)):
			raise ValueError
		as_at = str(datetime.date.fromisoformat(as_at)) if as_at else today()
	except ValueError:
		return {"success": False, "error": "Invalid as-at date (use YYYY-MM-DD): " + str(as_at)}
	if length and length not in LENGTHS:
		return {"success": False, "error": "Unknown length: " + str(length)}
	status = (status or "").lower()
	show = (show or "all").lower()
	needle = (q or "").strip().lower()

	tax = _taxonomy()
	line_names = [ln for ln, _ in tax]
	categories = [
		{"value": g, "label": disp if (line or disp == ln) else ln + " · " + disp, "line": ln}
		for ln, groups in tax
		for g, disp in groups
	]
	group_line, group_disp = {}, {}
	for ln, groups in tax:
		for g, disp in groups:
			group_line[g] = ln
			group_disp[g] = disp
	all_groups = list(group_line)

	# Item scope after the item-level filters (line / category / status / search).
	groups = [
		g for g in all_groups if (not line or group_line[g] == line) and (not category or g == category)
	]
	cond = ["item_group IN %(groups)s"]
	params = {"groups": tuple(groups) or ("",)}
	if status == "active":
		cond.append("disabled = 0")
	elif status == "inactive":
		cond.append("disabled = 1")
	if needle:
		# A matching category or line name pulls in all of its items.
		hit_groups = [g for g in groups if needle in group_disp[g].lower() or needle in group_line[g].lower()]
		params["like"] = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
		params["hit_groups"] = tuple(hit_groups) or ("",)
		cond.append(
			"(LOWER(item_name) LIKE %(like)s OR LOWER(name) LIKE %(like)s OR item_group IN %(hit_groups)s)"
		)
	items = (
		frappe.db.sql(
			"SELECT name, item_name, item_group, disabled FROM `tabItem` WHERE "
			+ " AND ".join(cond)
			+ " ORDER BY item_name, name",
			params,
			as_dict=True,
		)
		if groups
		else []
	)

	shown_lengths = [length] if length else list(LENGTHS)
	rates = _rates(price_list, [it.name for it in items], as_at)

	by_group = {}
	priced = total = inactive = complete = 0
	rate_vals = []
	for it in items:
		r = rates.get(it.name, {})
		cells = {ln: r.get(ln) for ln in LENGTHS}
		miss = sum(1 for ln in shown_lengths if cells[ln] is None)
		if show == "gaps" and not miss:
			continue
		if show == "complete" and miss:
			continue
		total += len(shown_lengths)
		priced += len(shown_lengths) - miss
		rate_vals += [cells[ln] for ln in shown_lengths if cells[ln] is not None]
		if not miss:
			complete += 1
		if it.disabled:
			inactive += 1
		by_group.setdefault(it.item_group, []).append(
			{
				"n": it.item_name or it.name,
				"code": it.name,
				"s": "Inactive" if it.disabled else "Active",
				"lengths": cells,
				"missing": miss,
			}
		)

	out = []
	n_items = 0
	for ln, gs in tax:
		ocats = []
		for g, disp in gs:
			if by_group.get(g):
				ocats.append({"name": disp, "group": g, "items": by_group[g]})
				n_items += len(by_group[g])
		if ocats:
			out.append({"name": ln, "cats": ocats})

	# Whole-list figure for the subtitle (as at the same date, no other filter).
	list_total = list_priced = 0
	if all_groups:
		list_total = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabItem` WHERE item_group IN %(g)s", {"g": tuple(all_groups)}
		)[0][0] * len(LENGTHS)
		list_priced = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT ip.item_code, ip.custom_length)
			FROM `tabItem Price` ip JOIN `tabItem` i ON i.name = ip.item_code
			WHERE ip.selling = 1 AND ip.price_list = %(pl)s AND ip.custom_length IN %(lengths)s
			  AND i.item_group IN %(g)s
			  AND (ip.valid_from IS NULL OR ip.valid_from <= %(d)s)
			  AND (ip.valid_upto IS NULL OR ip.valid_upto >= %(d)s)
			""",
			{"pl": price_list, "lengths": tuple(LENGTHS), "g": tuple(all_groups), "d": as_at},
		)[0][0]

	return {
		"success": True,
		"price_list": price_list,
		"currency": frappe.db.get_value("Price List", price_list, "currency"),
		"as_at": as_at,
		"today": today(),
		"editable": as_at == today(),
		"lengths": shown_lengths,
		"all_lengths": list(LENGTHS),
		"lines": out,
		"line_options": line_names,
		"category_options": [c for c in categories if not line or c["line"] == line],
		"has_taxonomy": bool(all_groups),
		"kpis": {
			"total_cells": total,
			"priced_cells": priced,
			"missing_cells": total - priced,
			"coverage_pct": round(priced * 100.0 / total, 2) if total else 0,
			"varieties": n_items,
			"lines": len(out),
			"complete_varieties": complete,
			"inactive_varieties": inactive,
			"min_rate": min(rate_vals) if rate_vals else None,
			"max_rate": max(rate_vals) if rate_vals else None,
			"median_rate": _median(rate_vals),
		},
		"list_totals": {"total_cells": list_total, "priced_cells": list_priced},
	}
