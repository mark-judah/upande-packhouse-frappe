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
from frappe import _

from upande_packhouse.availability import variety_availability


# ----------------------------- helpers -----------------------------

def _spec_issues(doc):
	"""Fields the autofill actually depends on (see build_spec_rows /
	_detail_payload / get_spec_fill_data below) -- same set the doctype's own
	`reqd` flags enforce on save, re-checked here at READ time too. A spec
	saved before those flags existed (or edited around them via the API) can
	still be missing one of these; without this check the autofill would
	silently hand the Sales Order blank/zero packing data instead of refusing
	to run. Returns a list of human-readable problem strings, empty if clean.
	"""
	issues = []
	if not doc.box_assortment:
		issues.append("Box Assortment is not set.")
	if not doc.cut_stage:
		issues.append("Cut Stage is not set.")
	if not doc.defoliation_length:
		issues.append("Defoliation Length is not set.")
	if not doc.approved_varieties:
		issues.append("No Approved Varieties are configured.")

	if not doc.box_items:
		issues.append("No Box Items are configured.")
	else:
		for i, bi in enumerate(doc.box_items):
			row = i + 1
			if not bi.stems_per_bunch:
				issues.append(f"Box Item row {row}: Stems/Bunch is not set.")
			if not bi.bunches_per_box:
				issues.append(f"Box Item row {row}: Bunches/Box is not set.")
			if not bi.length:
				issues.append(f"Box Item row {row}: Length is not set.")
			if not bi.box_type:
				issues.append(f"Box Item row {row}: Box Type is not set.")
			if not bi.pack_rate:
				issues.append(f"Box Item row {row}: Pack Rate could not be computed.")
	return issues


def _require_clean_spec(doc):
	issues = _spec_issues(doc)
	if issues:
		frappe.throw(
			_("Specification {0} is incomplete and cannot be used for autofill until it's fixed:"
			  "<br>{1}").format(frappe.bold(doc.name), "<br>".join(issues)),
			title=_("Incomplete Specification"),
		)


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


def _all_approved_varieties(approved_by_colour):
	"""Every approved variety across every colour, deduped, order preserved.
	Box Build lines carry no colour of their own (that lives solely in
	Specifications.approved_varieties -- see _approved_by_colour above), so
	every line offers this same full palette; the operator picks the actual
	colour/variety per line at Sales Order time."""
	out = []
	for vs in approved_by_colour.values():
		out.extend(vs)
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
	"""Return the spec's approved COLOURS, each scoped to only the varieties
	approved under it (live shelf availability) -- one popup row per colour,
	not per box item. This is the actual point of the popup: the customer's
	spec says "5 varieties are approved for White, 10 for Red, ..."; for each
	colour the operator is choosing ONE variety, based on which one they have
	the best stock of right now. A box item describes the physical pack
	(bunch type, length, box type...) and is a SEPARATE axis from colour --
	most specs have exactly one, so it's applied to every colour row by
	default, but all of them are returned as `box_options` so the client can
	offer a choice on the rarer spec that defines more than one.
	"""
	doc = frappe.get_doc("Specifications", spec)
	_require_clean_spec(doc)
	items = doc.box_items or []
	approved_by_colour = _approved_by_colour(doc)
	approved_varieties = _all_approved_varieties(approved_by_colour)

	all_lengths = [bi.length for bi in items if bi.length]

	avail = variety_availability(approved_varieties, list(set(all_lengths))) if approved_varieties else {}
	names = _item_names(approved_varieties)

	box_options = [{
		"idx": i,
		"bunch_type": bi.bunch_type or "",
		"is_mixed_bunch": bi.bunch_type == "Mixed Bunch",
		"length": bi.length or "",
		"stems_per_bunch": bi.stems_per_bunch or 0,
		"pack_rate": bi.pack_rate or 0,
		"box_type": bi.box_type or "",
	} for i, bi in enumerate(items)]

	lines = []
	for i, (colour, varieties) in enumerate(approved_by_colour.items()):
		approved = []
		for v in varieties:
			by_farm = avail.get(v, {})
			approved.append({
				"variety": v,
				"item_name": names.get(v, v),
				"available": sum(by_farm.values()),
				"by_farm": by_farm,
			})
		lines.append({
			"idx": i,
			"colour": colour or _("Colour {0}").format(i + 1),
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
		"box_options": box_options,
		"sources": sources,
	}


@frappe.whitelist()
def build_spec_rows(spec, selections, next_mix_group=1, next_bunch_group=1, source_warehouse=None):
	"""Shape the chosen varieties into Sales Order Item rows.

	selections: [{line_idx, box_idx, variety, boxes, stems}] -- line_idx is
	which COLOUR was chosen from (informational only, the variety itself is
	what matters from here on), box_idx is which of the spec's box_items
	describes the physical pack for this selection (stems optional; defaults
	to that box item's own pack_rate).
	next_mix_group / next_bunch_group: current max+1 on the form (client supplies).
	"""
	doc = frappe.get_doc("Specifications", spec)
	_require_clean_spec(doc)
	selections = _as_list(selections)
	items = doc.box_items or []
	is_mixed_box = doc.box_assortment == "Mixed Box"
	next_mix_group = int(next_mix_group or 1)
	next_bunch_group = int(next_bunch_group or 1)

	detail = _detail_payload(doc)
	names = _item_names([s.get("variety") for s in selections])

	rows = []
	for s in selections:
		idx = int(s.get("box_idx") or 0)
		if idx < 0 or idx >= len(items):
			continue
		bi = items[idx]
		variety = s.get("variety")
		if not variety:
			continue

		boxes = int(s.get("boxes") or 1)
		stems_per_box = int(s.get("stems") or bi.pack_rate or 0)
		mixed_bunch = 1 if bi.bunch_type == "Mixed Bunch" else 0
		# A spec's box_assortment ("Mixed Box" vs Straight/Mono) is a coarser
		# categorisation than its box item's own bunch_type -- a spec can be
		# box_assortment="Mixed Box" while the chosen box item is itself a
		# Mixed Bunch (confirmed real data: SYSU-02490-20 52CM). Without this
		# bunch_type-takes-precedence guard, such a row got BOTH
		# custom_mixed_box=1 (stamping it into next_mix_group) AND
		# custom_mixed_bunch=1 (stamping it into next_bunch_group) -- if a
		# caller happens to pass the same counter value for both groups (as
		# the client naturally can, since each is its own independent
		# per-kind counter), the row silently joins a Mixed Box group it has
		# nothing to do with. opl_submit_blockers's mix_group completeness
		# query then waits on this unrelated bunch-group item forever,
		# leaving a fully-allocated Mixed Box OPL stuck in draft. bunch_type
		# is the more specific, physical signal (mirrors packing_guide.py's
		# own _box_kind, which already checks custom_mixed_bunch first), so
		# it wins here too -- a real Mixed Bunch line is never also tagged
		# into a mix_group.
		mixed_box = 1 if (is_mixed_box and not mixed_bunch) else 0
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
			# Sales Order Item.conversion_factor is a core mandatory field --
			# leaving it unset here means the grid's own client-side mandatory
			# check blocks Save before the request ever reaches the server, so
			# sales_order_engine.sales_order_before_validate (which recomputes
			# this from the same regex) never gets the chance to run. See the
			# identical fix in box_math.js's straight_calc for the manually
			# typed-item-code path.
			"conversion_factor": factor or 1,
		}

		if mixed_box or mixed_bunch:
			row["custom_packrate_mixed_box"] = stems_per_box
		else:
			pr = str(stems_per_box)
			if frappe.db.exists("Packrate", pr):
				row["custom_packrate"] = pr

		if source_warehouse:
			# `warehouse` carries the raw Receiving Cold Store for the farm
			# this line is sourced from -- NOT a mapped/resolved warehouse.
			# It used to be swapped for Roses-MAP's delivery (Graded Sold)
			# warehouse right here, which skipped the two real stock moves
			# stems must physically make on their way to a customer
			# (coldstore -> Ungraded Sold on issue, Ungraded Sold -> Graded
			# Sold on Farm Pack List submit -- see roses_warehouse_map.py).
			# Order Pick List / Pick List Item carries this same coldstore
			# value forward, and issueBucketToSaleOrderItem /
			# farm_pack_list.py / createOrUpdateDispatch each resolve the
			# next warehouse in the chain from it via Roses-MAP as needed.
			row["warehouse"] = source_warehouse

		row.update(detail)
		rows.append(row)

	return {"rows": rows}
