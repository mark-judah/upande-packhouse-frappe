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
			_(
				"Specification {0} is incomplete and cannot be used for autofill until it's fixed:<br>{1}"
			).format(frappe.bold(doc.name), "<br>".join(issues)),
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
	return list(v) if isinstance(v, list | tuple) else [v]


def _approved_by_colour(doc):
	"""colour -> [approved variety, ...] from the spec's Approved Varieties table."""
	m = {}
	for r in doc.approved_varieties or []:
		if r.colour and r.variety:
			m.setdefault(r.colour, []).append(r.variety)
	return m


def _box_idxs_by_variety(doc):
	"""Which Box Build rows belong to each Approved Variety, by POSITION.

	A flat spec's Approved Variety rows carry no bunch_id, so nothing links a
	variety to a pack shape explicitly -- but the two tables are written in
	step, and a spec that sells the same varieties at more than one length
	simply repeats the whole variety block once per length. Confirmed against
	this data:

	    EFLOWERS B-11ROSE SPRAY MIX 62/72CM   5 varieties, 10 box items
	                                          (the 5 at 62cm, then the same 5 at 72cm)
	    EFLOWERS B-ZUKOV FEMKE, DINARA MIX    2 varieties,  4 box items (62,62,72,72)
	    B-ATHENA 42/52/72CM                   1 variety,    3 box items
	    FATIM CHARLOTE CLASSIC/GARDEN MIX     7 of each, 1:1 -- and one of the
	                                          seven is 62cm where the rest are 52cm

	so box item j belongs to variety (j mod n). That last spec is why this
	matters even when there is only one box item per variety: the length is a
	property of the PAIR, not of the spec, and applying one length across a
	whole colour silently ships the wrong stem length.

	Only derived when len(box_items) is a whole multiple of len(varieties). On
	anything else the pairing would be guesswork, so an empty map is returned
	and the caller falls back to offering every box item for every variety.
	"""
	items = doc.box_items or []
	varieties = [r.variety for r in (doc.approved_varieties or []) if r.variety]
	n = len(varieties)
	if not items or not n or len(items) % n:
		return {}

	out = {}
	for j in range(len(items)):
		out.setdefault(varieties[j % n], []).append(j)
	return out


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


def _bunches_from_spec(doc):
	"""When every Approved Variety row carries a bunch_id, pair each bunch's
	Approved Variety row(s) with the Box Item row(s) sharing that same
	bunch_id -- matched positionally, but only WITHIN one bunch's own rows
	(the shared id already narrows this to just that recipe, not a guess
	across the whole spec the way box_options used to be offered).

	A bunch_id's rows are grouped into "slots" -- one per Box Item row:

	  - Mixed Bunch: every Approved Variety row is its own mandatory
	    component (several colours combined stem-by-stem into one physical
	    bunch), so row counts must match 1:1, in row order.
	  - Mono Bunch: rows are grouped by COLOUR instead. A colour can carry
	    several candidate varieties (the whole point of the original flat
	    picker -- "N varieties are approved for Red, pick whichever has
	    stock"), so a slot's `candidates` list can have more than one entry;
	    the operator picks at Sales Order time based on live availability.
	    Distinct-colour count must match Box Item row count, in row order.

	Returns (bunch_aware, bunches). bunch_aware is False -- and bunches []
	-- the moment any Approved Variety row lacks a bunch_id, so a spec that
	hasn't been touched since bunch_id was added behaves exactly as before;
	this is deliberately all-or-nothing rather than mixing the old flat
	picker with the new recipe view on one spec.

	Throws if a bunch_id's slot counts don't match 1:1 against its Box Item
	rows -- that's a genuinely broken recipe, not something to guess
	through silently.
	"""
	avs = doc.approved_varieties or []
	if not avs or not all(av.bunch_id for av in avs):
		return False, []

	varieties_by_bunch = {}
	for av in avs:
		varieties_by_bunch.setdefault(av.bunch_id, []).append(av)

	items_by_bunch = {}
	for bi in doc.box_items or []:
		items_by_bunch.setdefault(bi.bunch_id or "", []).append(bi)

	issues = []
	bunches = []
	for bunch_id, av_rows in varieties_by_bunch.items():
		bi_rows = items_by_bunch.get(bunch_id, [])
		# Whether this bunch is Mixed is the box item's OWN bunch_type, not
		# row counts -- several Mono Bunch rows can share a bunch_id too
		# (independent bunches still meant to fill the same box together),
		# and that must NOT read as "these combine stem-by-stem into one
		# bunch". A bunch_id can't legitimately mix bunch_types.
		types = {bi.bunch_type for bi in bi_rows}
		if len(types) > 1:
			issues.append(
				"Bunch '{0}': its Box Item rows disagree on Bunch Type ({1}) -- "
				"they must all be the same.".format(
					bunch_id, ", ".join(sorted(t or "(blank)" for t in types))
				)
			)
			continue
		is_mixed = types == {"Mixed Bunch"}

		if is_mixed:
			if len(bi_rows) != len(av_rows):
				issues.append(
					"Bunch '{0}': {1} Approved Variety row(s) but {2} Box Item row(s) -- a "
					"Mixed Bunch needs exactly one Box Item per component, matched 1:1.".format(
						bunch_id, len(av_rows), len(bi_rows)
					)
				)
				continue
			slots = [
				{"colour": av.colour or "", "box_item": bi, "candidates": [av]}
				for bi, av in zip(bi_rows, av_rows, strict=True)
			]
		else:
			by_colour = {}
			for av in av_rows:
				by_colour.setdefault(av.colour or "", []).append(av)
			if len(by_colour) != len(bi_rows):
				issues.append(
					"Bunch '{0}': {1} distinct colour(s) among its Approved Varieties but {2} "
					"Box Item row(s) carry this bunch_id -- they must match 1:1.".format(
						bunch_id, len(by_colour), len(bi_rows)
					)
				)
				continue
			slots = [
				{"colour": colour, "box_item": bi, "candidates": candidates}
				for bi, (colour, candidates) in zip(bi_rows, by_colour.items(), strict=True)
			]

		bunches.append({"bunch_id": bunch_id, "is_mixed": is_mixed, "slots": slots})

	if issues:
		frappe.throw(
			_(
				"This spec's bunch_id grouping doesn't line up and can't be used for autofill "
				"until it's fixed:<br>{0}"
			).format("<br>".join(issues)),
			title=_("Bunch ID Mismatch"),
		)

	# Preserve the spec's own row order rather than dict-iteration order.
	order = {}
	for av in avs:
		order.setdefault(av.bunch_id, len(order))
	bunches.sort(key=lambda b: order.get(b["bunch_id"], 0))
	return True, bunches


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
		for r in frappe.get_all(
			"Item",
			filters={"name": ["in", [c.item for c in cons]]},
			fields=["name", "item_name", "item_group"],
		):
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
def get_spec_fill_data(spec: str | None):
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

	bunch_aware, bunches = _bunches_from_spec(doc)
	if bunch_aware:
		return _spec_fill_data_by_bunch(doc, bunches)

	items = doc.box_items or []
	approved_by_colour = _approved_by_colour(doc)
	approved_varieties = _all_approved_varieties(approved_by_colour)

	all_lengths = [bi.length for bi in items if bi.length]

	avail = variety_availability(approved_varieties, list(set(all_lengths))) if approved_varieties else {}
	names = _item_names(approved_varieties)

	box_options = [
		{
			"idx": i,
			"bunch_type": bi.bunch_type or "",
			"is_mixed_bunch": bi.bunch_type == "Mixed Bunch",
			"length": bi.length or "",
			"stems_per_bunch": bi.stems_per_bunch or 0,
			"pack_rate": bi.pack_rate or 0,
			"box_type": bi.box_type or "",
		}
		for i, bi in enumerate(items)
	]

	# Which box items (and so which stem lengths) each variety is actually
	# specified at -- see _box_idxs_by_variety. Empty when the spec's two
	# tables don't line up, and the client then falls back to its per-colour
	# box selector.
	box_idxs = _box_idxs_by_variety(doc)

	lines = []
	for i, (colour, varieties) in enumerate(approved_by_colour.items()):
		approved = []
		for v in varieties:
			by_farm = avail.get(v, {})
			approved.append(
				{
					"variety": v,
					"item_name": names.get(v, v),
					"available": sum(by_farm.values()),
					"by_farm": by_farm,
					"box_idxs": box_idxs.get(v, []),
				}
			)
		lines.append(
			{
				"idx": i,
				"colour": colour or _("Colour {0}").format(i + 1),
				"approved": approved,
			}
		)

	sources = _roses_map_sources()[0]
	return {
		"spec": doc.name,
		"spec_name": doc.spec_name or doc.name,
		"box_assortment": doc.box_assortment or "",
		"is_mixed_box": doc.box_assortment == "Mixed Box",
		"ftnft": doc.ftnft or "",
		"bunch_aware": False,
		"lines": lines,
		"box_options": box_options,
		"sources": sources,
	}


def _spec_fill_data_by_bunch(doc, bunches):
	"""get_spec_fill_data's bunch-aware branch: one popup card per recipe
	(bunch_id), each with its own slots (one per Box Item row). A Mixed
	Bunch's slots each carry exactly one mandatory variety; a Mono Bunch's
	slot carries every variety approved under that slot's colour, annotated
	with LIVE shelf availability, so the operator still picks whichever one
	has stock -- same as the original flat picker, just correctly scoped to
	this slot's own box shape instead of the spec's whole box_item palette."""
	all_varieties = sorted(
		{av.variety for b in bunches for slot in b["slots"] for av in slot["candidates"] if av.variety}
	)
	all_lengths = sorted(
		{slot["box_item"].length for b in bunches for slot in b["slots"] if slot["box_item"].length}
	)
	avail = variety_availability(all_varieties, all_lengths) if all_varieties else {}
	names = _item_names(all_varieties)

	bunch_payload = []
	for b in bunches:
		slots = []
		for slot in b["slots"]:
			bi = slot["box_item"]
			candidates = []
			for av in slot["candidates"]:
				by_farm = avail.get(av.variety, {})
				candidates.append(
					{
						"variety": av.variety,
						"item_name": names.get(av.variety, av.variety),
						"available": sum(by_farm.values()),
						"by_farm": by_farm,
					}
				)
			slots.append(
				{
					"colour": slot["colour"],
					"candidates": candidates,
					"stems_per_bunch": bi.stems_per_bunch or 0,
					"bunches_per_box": bi.bunches_per_box or 0,
					"pack_rate": bi.pack_rate or 0,
					"length": bi.length or "",
					"box_type": bi.box_type or "",
					"bunch_type": bi.bunch_type or "",
				}
			)
		bunch_payload.append({"bunch_id": b["bunch_id"], "is_mixed": b["is_mixed"], "slots": slots})

	return {
		"spec": doc.name,
		"spec_name": doc.spec_name or doc.name,
		"box_assortment": doc.box_assortment or "",
		"is_mixed_box": doc.box_assortment == "Mixed Box",
		"ftnft": doc.ftnft or "",
		"bunch_aware": True,
		"bunches": bunch_payload,
	}


def _build_spec_rows_by_bunch(doc, bunches, selections, next_mix_group, detail, source_warehouse):
	"""build_spec_rows's bunch-aware branch. `selections` here is
	[{bunch_id, boxes, picks}] -- `picks` maps a slot's colour to the
	variety the operator chose for it (only meaningful when that slot has
	more than one approved candidate; falls back to the slot's only/first
	candidate otherwise, so a client that omits `picks` entirely still
	works for the common single-candidate case).

	custom_bunch_group is derived straight from (spec, bunch_id) -- always
	the same value for the same recipe, so two separate "Add to Order"
	calls (or two calls on two different days) land in the same group
	instead of the old shared per-click counter merging unrelated recipes
	together.

	custom_mix_group needs the same guarantee for Mono Bunch groups, but
	has to stay a small plain integer (mixed_box_wizard.js's "Edit Mixed
	Boxes" does cint() on it, and a non-numeric value there silently
	collapses every group into "0"), so it can't just be derived the same
	way custom_bunch_group is. A single spec can describe more than one
	physical box -- confirmed real data: XPOL TOSCA_02720_10 has bunch_id
	"1" (Furiosa+Athena+Aqua, 288 stems/box) and bunch_id "2"
	(Moonwalk+High & Magic+Tropical Amazon, 252 stems/box) as two DISTINCT
	Mixed Box recipes, not one combined box -- so every distinct bunch_id
	selected in this call gets its OWN mix_group, counting up from
	next_mix_group; only the slots WITHIN one bunch_id share a group. The
	caller (spec_autofill.js) supplies a starting value high enough that it
	can't collide with any mix_group already on the form.
	"""
	by_bunch_id = {b["bunch_id"]: b for b in bunches}
	is_mixed_box = doc.box_assortment == "Mixed Box"
	next_group_value = int(next_mix_group or 1)
	mix_group_by_bunch = {}

	rows = []
	for s in selections:
		bunch = by_bunch_id.get(s.get("bunch_id"))
		if not bunch:
			continue
		boxes = int(s.get("boxes") or 0)
		if boxes <= 0:
			continue

		picks = s.get("picks") or {}
		slot_choices = []
		for slot in bunch["slots"]:
			candidates = {av.variety: av for av in slot["candidates"]}
			chosen = candidates.get(picks.get(slot["colour"]))
			if not chosen and slot["candidates"]:
				chosen = slot["candidates"][0]
			if chosen:
				slot_choices.append((slot["box_item"], chosen))
		if not slot_choices:
			continue

		names = _item_names([av.variety for _bi, av in slot_choices])
		mixed_bunch = 1 if bunch["is_mixed"] else 0
		# Same precedence as the legacy path: a bunch that's genuinely mixed
		# (several colours combined stem-by-stem) is never also tagged into
		# a mix_group, even when the spec's own box_assortment is Mixed Box --
		# see build_spec_rows's own comment on why that guard exists.
		mixed_box = 1 if (is_mixed_box and not mixed_bunch) else 0
		mix_group = None
		if mixed_box:
			if bunch["bunch_id"] not in mix_group_by_bunch:
				mix_group_by_bunch[bunch["bunch_id"]] = next_group_value
				next_group_value += 1
			mix_group = mix_group_by_bunch[bunch["bunch_id"]]

		for bi, av in slot_choices:
			stems_per_box = bi.pack_rate or 0
			total = stems_per_box * boxes
			uom = _uom_for(bi.stems_per_bunch)
			factor = _uom_factor(uom)

			row = {
				"item_code": av.variety,
				"item_name": names.get(av.variety, av.variety),
				"uom": uom,
				"custom_line": doc.name,
				"custom_mixed_box": mixed_box,
				"custom_mix_group": mix_group if mixed_box else "",
				"custom_mixed_bunch": mixed_bunch,
				"custom_bunch_group": "{0}::{1}".format(doc.name, bunch["bunch_id"]) if mixed_bunch else "",
				"custom_mix_name": doc.spec_name or "",
				"custom_number_of_boxes": boxes,
				"custom_length": bi.length,
				"custom_box_type": bi.box_type,
				"custom_ordered_quantity": total,
				"stock_qty": total,
				"qty": (total / factor) if factor else total,
				"conversion_factor": factor or 1,
			}

			if mixed_box or mixed_bunch:
				row["custom_packrate_mixed_box"] = stems_per_box
			else:
				pr = str(stems_per_box)
				if frappe.db.exists("Packrate", pr):
					row["custom_packrate"] = pr

			if source_warehouse:
				row["warehouse"] = source_warehouse

			row.update(detail)
			rows.append(row)

	return {"rows": rows}


@frappe.whitelist()
def build_spec_rows(
	spec: str | None,
	selections: str | None,
	next_mix_group: str | None = 1,
	next_bunch_group: str | None = 1,
	source_warehouse: str | None = None,
):
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
	detail = _detail_payload(doc)

	bunch_aware, bunches = _bunches_from_spec(doc)
	if bunch_aware:
		return _build_spec_rows_by_bunch(doc, bunches, selections, next_mix_group, detail, source_warehouse)

	items = doc.box_items or []
	is_mixed_box = doc.box_assortment == "Mixed Box"
	next_mix_group = int(next_mix_group or 1)
	next_bunch_group = int(next_bunch_group or 1)

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
			# warehouse right here, which skipped the real stock moves stems
			# must physically make on their way to a customer (the Sold leg
			# when a bucket is issued, then Packing / Dispatch / Loading --
			# see stock_movement.STAGES).
			# Order Pick List / Pick List Item carries this same coldstore
			# value forward, and issueBucketToSaleOrderItem /
			# farm_pack_list.py / createOrUpdateDispatch each resolve the
			# next warehouse in the chain from it via Roses-MAP as needed.
			row["warehouse"] = source_warehouse

		row.update(detail)
		rows.append(row)

	return {"rows": rows}
