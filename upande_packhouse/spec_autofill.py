"""Server-owned Sales Order spec autofill.

Replaces the vibecoded 682-line client script. Two whitelisted calls:

  get_spec_fill_data(spec)  -> everything the popup needs: each colour line with
                               its customer-approved varieties annotated with LIVE
                               shelf availability (net of allocation + discards).
  build_spec_rows(spec, ..) -> the Sales Order Item rows to append, fully shaped
                               (qty, uom factor, packrate, mix/bunch groups,
                               spec detail payload, warehouse routing).

The client just renders a dialog and appends rows — no business logic, no
re-entrancy guards.
"""

import json

import frappe

from upande_packhouse.availability import variety_availability


# ----------------------------- helpers -----------------------------

def _as_list(v):
	if not v:
		return []
	if isinstance(v, str):
		try:
			v = json.loads(v)
		except Exception:
			v = [x.strip() for x in v.split(",") if x.strip()]
	return list(v) if isinstance(v, (list, tuple)) else [v]


def _approved_by_colour(doc):
	"""colour -> [approved variety, ...] from the spec's Approved Varieties table."""
	m = {}
	for r in (doc.approved_varieties or []):
		if r.colour and r.variety:
			m.setdefault(r.colour, []).append(r.variety)
	return m


def _approved_for(bi, approved_by_colour):
	"""Approved varieties for a Box Build line: the customer-allowed set for this
	line's colour. A colourless line offers the whole approved palette."""
	if bi.get("colour"):
		return list(approved_by_colour.get(bi.colour, []))
	out = []
	for vs in approved_by_colour.values():
		out.extend(vs)
	# de-dup preserving order
	seen = set()
	return [v for v in out if not (v in seen or seen.add(v))]


def _uom_for(stems_per_bunch):
	spb = int(stems_per_bunch or 0)
	return "Bunch ({0})".format(spb) if spb else ""


def _uom_factor(uom):
	if not uom:
		return 1
	import re
	m = re.search(r"\((\d+)\)", uom)
	return int(m.group(1)) if m else 1


def _match_sleeve(desc):
	d = (desc or "").lower()
	if "karen" in d:
		return "Karen Branded"
	if "clear" in d:
		return "Clear Sleeve"
	return ""


def _item_names(codes):
	codes = list({c for c in codes if c})
	if not codes:
		return {}
	rows = frappe.get_all("Item", filters={"name": ["in", codes]}, fields=["name", "item_name"])
	return {r.name: (r.item_name or r.name) for r in rows}


def _roses_map_sources():
	if not frappe.db.exists("SO Warehouse Mapping", "Roses-MAP"):
		return [], {}
	doc = frappe.get_doc("SO Warehouse Mapping", "Roses-MAP")
	mapping = {it.source_warehouse: it.delivery_warehouse for it in doc.items if it.source_warehouse}
	return list(mapping.keys()), mapping


def _detail_payload(doc):
	"""Order-side enrichment copied onto SO lines. Consumables now link to stock
	Items, so flower-food / sleeve / label are detected from the item name+group
	rather than a fixed Select value."""
	cons = [c for c in (doc.consumables or []) if c.get("item")]
	names = {}
	if cons:
		for r in frappe.get_all("Item", filters={"name": ["in", [c.item for c in cons]]},
								fields=["name", "item_name", "item_group"]):
			names[r.name] = ((r.item_name or "") + " " + (r.item_group or "")).lower()

	def _text(c):
		return names.get(c.item, "")

	p = {
		"custom_cut_stage": doc.cut_stage or "",
		"custom_defoliation_length": doc.defoliation_length or "",
		"custom_consumables_charge": 1 if doc.consumables_charge else 0,
		"custom_documentation_fee": 1 if doc.documentation_charge else 0,
		"custom_certificate_of_origin": 1 if doc.certificate_of_origin else 0,
		"custom_with_flower_food": 1 if any("flower" in _text(c) and "food" in _text(c) for c in cons) else 0,
	}
	for c in cons:
		t = _text(c)
		if "sleeve" in t and c.description:
			p["custom_sleeve_description"] = _match_sleeve(c.description) or c.description
		if "label" in t and c.description:
			p["custom_labels_description_on_sleeve"] = c.description
	return p


# ----------------------------- API -----------------------------

@frappe.whitelist()
def get_spec_fill_data(spec):
	"""Return the spec's colour lines with approved varieties + live availability."""
	doc = frappe.get_doc("Specifications", spec)
	items = doc.box_items or []
	approved_by_colour = _approved_by_colour(doc)

	all_varieties, all_lengths = [], []
	for bi in items:
		all_varieties.extend(_approved_for(bi, approved_by_colour))
		if bi.length:
			all_lengths.append(bi.length)

	avail = variety_availability(list(set(all_varieties)), list(set(all_lengths))) if all_varieties else {}
	names = _item_names(all_varieties)

	lines = []
	for i, bi in enumerate(items):
		approved = []
		for v in _approved_for(bi, approved_by_colour):
			by_farm = avail.get(v, {})
			approved.append({
				"variety": v,
				"item_name": names.get(v, v),
				"available": sum(by_farm.values()),
				"by_farm": by_farm,
			})
		lines.append({
			"idx": i,
			"colour": bi.colour or "",
			"bunch_type": bi.bunch_type or "",
			"is_mixed_bunch": bi.bunch_type == "Mixed Bunch",
			"length": bi.length or "",
			"stems_per_bunch": bi.stems_per_bunch or 0,
			"pack_rate": bi.pack_rate or 0,
			"box_type": bi.box_type or "",
			"approved": approved,
		})

	sources, _ = _roses_map_sources()
	return {
		"spec": doc.name,
		"spec_name": doc.spec_name or doc.name,
		"box_assortment": doc.box_assortment or "",
		"is_mixed_box": doc.box_assortment == "Mixed Box",
		"ftnft": doc.ftnft or "",
		"lines": lines,
		"sources": sources,
	}


@frappe.whitelist()
def build_spec_rows(spec, selections, next_mix_group=1, next_bunch_group=1, source_warehouse=None):
	"""Shape the chosen varieties into Sales Order Item rows.

	selections: [{line_idx, variety, boxes, stems}]  (stems optional; defaults to pack_rate)
	next_mix_group / next_bunch_group: current max+1 on the form (client supplies).
	"""
	doc = frappe.get_doc("Specifications", spec)
	selections = _as_list(selections)
	items = doc.box_items or []
	is_mixed_box = doc.box_assortment == "Mixed Box"
	next_mix_group = int(next_mix_group or 1)
	next_bunch_group = int(next_bunch_group or 1)

	detail = _detail_payload(doc)
	_, mapping = _roses_map_sources()
	delivery = mapping.get(source_warehouse) if source_warehouse else None
	names = _item_names([s.get("variety") for s in selections])

	rows = []
	for s in selections:
		idx = int(s.get("line_idx"))
		if idx < 0 or idx >= len(items):
			continue
		bi = items[idx]
		variety = s.get("variety")
		if not variety:
			continue

		boxes = int(s.get("boxes") or 1)
		stems_per_box = int(s.get("stems") or bi.pack_rate or 0)
		mixed_bunch = 1 if bi.bunch_type == "Mixed Bunch" else 0
		mixed_box = 1 if is_mixed_box else 0
		uom = _uom_for(bi.stems_per_bunch)
		total = stems_per_box * boxes
		factor = _uom_factor(uom)

		row = {
			"item_code": variety,
			"item_name": names.get(variety, variety),
			"uom": uom,
			"custom_line": doc.name,
			"custom_mixed_box": mixed_box,
			"custom_mix_group": next_mix_group if mixed_box else "",
			"custom_mixed_bunch": mixed_bunch,
			"custom_bunch_group": next_bunch_group if mixed_bunch else "",
			"custom_mix_name": doc.spec_name or "",
			"custom_number_of_boxes": boxes,
			"custom_length": bi.length,
			"custom_box_type": bi.box_type,
			"custom_ordered_quantity": total,
			"stock_qty": total,
			"qty": (total / factor) if factor else total,
		}

		if mixed_box or mixed_bunch:
			row["custom_packrate_mixed_box"] = stems_per_box
		else:
			pr = str(stems_per_box)
			if frappe.db.exists("Packrate", pr):
				row["custom_packrate"] = pr

		if source_warehouse:
			row["custom_source_warehouse"] = source_warehouse
			if delivery:
				row["warehouse"] = delivery

		row.update(detail)
		rows.append(row)

	return {"rows": rows}
