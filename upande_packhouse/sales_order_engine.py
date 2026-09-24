"""Sales Order math + pricing engine for the Roses flow (server-owned).

On every save of a Roses SO it:
  1. Prices each line length-aware — standard price lists ignore stem length, so we
     resolve the Item Price by (item_code, price_list, custom_length). The list is the
     line's selling_price_list, else the customer's default_price_list. Item Prices are
     per stem (uom Stems); the line rate is expressed in the line's own UOM.
  2. Recomputes qty / stock_qty / conversion_factor from the packrate so the box math
     always tallies (straight = custom_packrate x boxes; mixed = custom_packrate_mixed_box x boxes).

On submit it blocks the order when the customer has no price list configured, or when a
Roses line has no packrate — which is exactly what makes packing show "No packrate set".
"""

import re

import frappe
from frappe import _


def _uom_factor(uom):
	if not uom:
		return 1
	m = re.search(r"\((\d+)\)", uom)
	return int(m.group(1)) if m else 1


def _packrate_number(value):
	"""custom_packrate is a Link to Packrate whose name is the stems-per-box number."""
	if not value:
		return 0
	try:
		return int(float(str(value)))
	except (ValueError, TypeError):
		return 0


def _line_packrate(it):
	"""Stems-per-box for a line, for EVERY box type. Mixed box AND mixed bunch
	carry the per-variety stems-per-box in custom_packrate_mixed_box (an Int);
	a straight box links custom_packrate (a Packrate whose name is that number)."""
	if it.get("custom_mixed_box") or it.get("custom_mixed_bunch"):
		return int(it.get("custom_packrate_mixed_box") or 0)
	return _packrate_number(it.get("custom_packrate"))


def _line_stems(it):
	"""Total stems on a line = stems-per-box x number of boxes (all box types)."""
	return _line_packrate(it) * int(it.get("custom_number_of_boxes") or 0)


def _customer_default_pl(doc):
	return frappe.db.get_value("Customer", doc.customer, "default_price_list") if doc.customer else None


def _resolve_price_list(doc):
	"""The customer's default price list drives pricing; an explicit non-Standard
	choice on the order overrides it (the user may pick a specific May list)."""
	spl = doc.get("selling_price_list")
	if spl and spl != "Standard Selling":
		return spl
	return _customer_default_pl(doc)


def _price_list_fx(price_list, doc_currency):
	"""Exchange factor from the price list's currency to the order currency
	(1.0 when they match). Item Prices live in the price list's currency (the
	May lists are USD/EUR/GBP); when the order is booked in another currency the
	per-stem rate must be converted.

	Looks up a stored Currency Exchange record ONLY — deliberately does NOT fall
	back to ERPNext's get_exchange_rate() live-fetch path. That function's HTTP
	call (erpnext.setup.utils.get_exchange_rate -> requests.get(...)) is made
	with no timeout, so on a network path that can't reach the exchange-rate
	service (a sandboxed/firewalled server, the service being down) it blocks
	the save request indefinitely — the browser just sits frozen with no error,
	since nothing has actually failed yet. A missing rate here falls back to 1.0
	and logs instead, so the save always completes; add a Currency Exchange
	record for the pair to get the real conversion."""
	pl_currency = frappe.db.get_value("Price List", price_list, "currency")
	if not pl_currency or not doc_currency or pl_currency == doc_currency:
		return 1.0

	rate = frappe.db.get_value(
		"Currency Exchange",
		{"from_currency": pl_currency, "to_currency": doc_currency},
		"exchange_rate",
		order_by="date desc",
	)
	if rate:
		return float(rate)

	frappe.log_error(
		"No Currency Exchange record for {0} -> {1}; priced at 1.0. "
		"Add one (Setup > Currency Exchange) for the correct rate.".format(pl_currency, doc_currency),
		"sales_order_engine._price_list_fx",
	)
	return 1.0


def _set_order_summary(doc):
	"""Total Boxes / Total Stems shown below the items table — the authoritative,
	save-time tally (the client script mirrors this live for immediate feedback,
	but this is what actually lands on submit regardless of what the browser did).

	Stems are additive per line regardless of grouping (each colour's own stems
	genuinely add up), but Boxes are NOT: every row sharing one custom_bunch_group
	(a Mixed Bunch's colours) or custom_mix_group (several bunches sharing one
	Mixed Box) describes the SAME physical box count, just annotated once per
	colour -- summing all of them overcounts by the group size. Count each
	group's custom_number_of_boxes exactly once, same dedup approach api/
	dashboard.py's "Expected boxes per OPL" already uses for this same reason.

	A spec is a template for ONE box, and a single spec can define BOTH kinds
	of bunch at once -- confirmed real data: XPOL TOSCA_02721_10 has bunch_id
	1 as a Mixed Bunch (Ever Red + Snow Storm, custom_bunch_group) and bunch_ids
	2-4 as Mono Bunches feeding the same Mixed Box (custom_mix_group). Deduping
	bunch_group and mix_group as two separate dimensions reads that as 2 boxes
	(1 from each dimension) instead of 1 -- this function briefly regressed to
	that (checking the server-assigned groups first, "because they're the more
	specific box identity") and custom_total_boxes read 4 instead of 2 for
	exactly this spec. custom_line (+ custom_length, in case the SAME spec is
	genuinely filled at two different lengths -- a variety approved at both
	62 and 72 is two physically different boxes) is checked FIRST for exactly
	this reason: every row filled from the same spec fill, at the same length,
	counts toward its box total once, full stop, regardless of which internal
	tag each of its own bunch_ids happens to carry. Only a row with no spec at
	all (a manually typed line, or mixed_box_wizard.js's spec-less Mixed Box)
	falls back to the bunch_group/mix_group dedup, since there's no spec
	identity to key on there."""
	total_boxes, total_stems = 0, 0
	seen_groups = set()
	for it in doc.items:
		if not it.item_code:
			continue
		boxes = int(it.get("custom_number_of_boxes") or 0)
		total_stems += _line_packrate(it) * boxes

		group_key = None
		if it.get("custom_line"):
			group_key = ("spec", it.custom_line, it.get("custom_length") or "")
		elif it.get("custom_bunch_group"):
			group_key = ("bunch", it.custom_bunch_group)
		elif it.get("custom_mix_group"):
			group_key = ("mix", it.custom_mix_group)
		if group_key:
			if group_key in seen_groups:
				continue
			seen_groups.add(group_key)
		total_boxes += boxes
	doc.custom_total_boxes = total_boxes
	doc.custom_total_stems = total_stems


def sales_order_before_validate(doc, method=None):
	"""Quantity tally + point the order at the customer's price list, before
	ERPNext's qty>0 check and its own (length-blind) pricing run."""
	_set_order_summary(doc)
	if (doc.get("business_unit") or "") != "Roses":
		return
	pl = _resolve_price_list(doc)
	if pl and doc.get("selling_price_list") != pl:
		doc.selling_price_list = pl
	for it in doc.items:
		factor = _uom_factor(it.uom) or 1
		it.conversion_factor = factor
		stems = _line_stems(it)
		if stems:
			it.stock_qty = stems
			it.qty = stems / factor
			# custom_ordered_quantity is spec_autofill.py's own row-creation value
			# (packrate x boxes at the time the row was added) and never touched
			# again after that -- editing custom_number_of_boxes or a packrate
			# field directly on the grid updates stock_qty/qty above but silently
			# leaves this one stale. sales_allocation.py's _required_stems_for_so_item
			# (its own docstring: "single source of truth" for what allocation
			# targets) PREFERS this field over qty x conversion_factor, so a stale
			# value here quietly under-targets allocation while the picklist
			# generator (which recomputes packrate x boxes fresh) expects the
			# correct total -- exactly the "allocated X, needed Y" mismatch this
			# fixes. Keep it equal to stock_qty on every save, same as qty is.
			it.custom_ordered_quantity = stems


def sales_order_price(doc, method=None):
	"""Length-aware pricing — runs on validate, AFTER ERPNext's (length-blind) standard
	pricing, so our per-length rate wins. Item Price is per stem → express per line UOM."""
	if (doc.get("business_unit") or "") != "Roses":
		return
	price_list = _resolve_price_list(doc)
	if not price_list:
		return
	fx = _price_list_fx(price_list, doc.get("currency"))
	for it in doc.items:
		if not it.get("custom_length"):
			continue
		per_stem = frappe.db.get_value(
			"Item Price",
			{
				"item_code": it.item_code,
				"price_list": price_list,
				"custom_length": it.custom_length,
				"selling": 1,
			},
			"price_list_rate",
		)
		# Length-specific price only. If none exists for this variety + length,
		# zero the rate (no length-blind fallback) so the save-time check blocks it.
		rate = float(per_stem) * (_uom_factor(it.uom) or 1) * fx if per_stem is not None else 0
		it.price_list_rate = rate
		it.discount_percentage = 0
		it.discount_amount = 0
		it.rate = rate
	doc.calculate_taxes_and_totals()


def sales_order_validate(doc, method=None):
	"""Data-integrity gates for Roses orders — enforced on every SAVE (not just
	submit), so an invalid draft can't be saved. Runs after sales_order_price."""
	if (doc.get("business_unit") or "") != "Roses":
		return

	# 1. must have a price list configured (line-level or the customer's default)
	if not _customer_default_pl(doc):
		frappe.throw(
			_(
				"Customer <b>{0}</b> has no Default Price List configured. Set one on the Customer "
				"(or on this order) before saving a Roses order."
			).format(doc.customer),
			title=_("Missing Price List"),
		)

	# 2a. box type, stem length and number of boxes are required on Roses lines
	#     (shown as mandatory in the form via mandatory_depends_on; enforced here
	#     on save too, and this also catches number of boxes = 0).
	incomplete = []
	for i, it in enumerate(doc.items, 1):
		if not it.item_code:
			continue
		gaps = []
		if not it.get("custom_length"):
			gaps.append("stem length")
		if not it.get("custom_box_type"):
			gaps.append("box type")
		if not it.get("custom_number_of_boxes"):
			gaps.append("number of boxes")
		if gaps:
			incomplete.append("line {0} ({1})".format(i, ", ".join(gaps)))
	if incomplete:
		frappe.throw(
			_("Complete these lines before saving: {0}.").format("; ".join(incomplete)),
			title=_("Missing Details"),
		)

	# 2. every line must carry a packrate, else packing can't enforce box capacity
	missing = []
	for i, it in enumerate(doc.items, 1):
		if it.item_code and not _line_packrate(it):
			missing.append(str(i))
	if missing:
		frappe.throw(
			_(
				"No packrate set on line(s) {0}. Set a Packrate (and number of boxes) so the "
				"box math and packing capacity are defined."
			).format(", ".join(missing))
		)

	# 3. every line must be priced. sales_order_price zeroes the rate when there
	#    is no Item Price for this exact variety + stem length, so a zero rate here
	#    means "no length-specific price" — block so the user adds it.
	unpriced = []
	for it in doc.items:
		if it.item_code and float(it.get("rate") or 0) <= 0:
			unpriced.append("{0} {1}".format(it.item_code, it.get("custom_length") or "").strip())
	if unpriced:
		frappe.throw(
			_(
				"No price found for: <b>{0}</b>. Add an Item Price for that variety and stem "
				"length in the order's price list before saving."
			).format(", ".join(unpriced)),
			title=_("Missing Price"),
		)

	# 4. Every line needs a UOM the packing app can read. A line filled from a
	#    specification takes its UOM from the spec; a line added directly takes it
	#    from the item's Sales UOM. Block when a directly-added line's item has no
	#    Sales UOM, so the user sets one.
	no_sales_uom = []
	for it in doc.items:
		if not it.item_code or it.get("custom_line"):
			continue
		if not frappe.db.get_value("Item", it.item_code, "sales_uom"):
			no_sales_uom.append(it.item_code)
	if no_sales_uom:
		frappe.throw(
			_(
				"No Sales UOM set on: <b>{0}</b>. Set a Sales UOM on the item so the order line "
				"has a unit of measure."
			).format(", ".join(sorted(set(no_sales_uom)))),
			title=_("Missing Sales UOM"),
		)

	# 4b. A directly-added line's own UOM must match the item's real Sales
	#     UOM. Roses varieties are graded at one fixed physical bunch size
	#     (Item.sales_uom) -- there's no mechanism for a farm to grade a
	#     special one-off bunch size for a single order, so a line that
	#     free-types a different "Bunch (N)" (e.g. via the web ledger's
	#     mixed-box card, which derives it from a hand-typed stems-per-bunch
	#     with no cross-check -- see api/sales_order.py's _shape_manual_row)
	#     will never be matched by any real graded bunch during packing. The
	#     mobile app then rejects every scan for that line with "Size
	#     (Bunch (N)) invalid for <variety>" -- confirmed root cause of a
	#     real packing incident (SAL-ORD-2026-00306, Giselle, mix_group 3).
	#     A spec-derived line (custom_line set) is exempt: its UOM comes
	#     from the spec's own Box Item, a deliberate choice already
	#     validated when the spec was built, which can legitimately differ
	#     from the item's default Sales UOM.
	wrong_uom = []
	for it in doc.items:
		if not it.item_code or it.get("custom_line") or not it.get("uom"):
			continue
		item_sales_uom = frappe.db.get_value("Item", it.item_code, "sales_uom")
		if item_sales_uom and it.uom != item_sales_uom:
			wrong_uom.append("{0} (line has {1}, item's Sales UOM is {2})".format(it.item_code, it.uom, item_sales_uom))
	if wrong_uom:
		frappe.throw(
			_(
				"These lines don't match the item's real Sales UOM, so packing will never find a "
				"matching graded bunch: <b>{0}</b>. Fix the line's bunch size (or the item's Sales "
				"UOM, if that's actually the wrong one) before saving."
			).format("; ".join(wrong_uom)),
			title=_("Wrong Bunch Size"),
		)

	# 5. Mixed-box colour limit — a mixed box may not contain more distinct colours
	#    than the applicable spec's "Max Colours Per Box". Colours come from the
	#    variety's Item.custom_color; the spec is the line's custom_line.
	from collections import defaultdict

	groups = defaultdict(lambda: {"colours": set(), "limits": set()})
	for it in doc.items:
		if not it.item_code or not it.get("custom_mixed_box"):
			continue
		key = it.get("custom_mix_group") or "-"
		colour = frappe.db.get_value("Item", it.item_code, "custom_color")
		if colour:
			groups[key]["colours"].add(colour)
		if it.get("custom_line"):
			mx = frappe.db.get_value("Specifications", it.get("custom_line"), "max_colours_per_box")
			if mx:
				groups[key]["limits"].add(int(mx))
	over = []
	for key, g in groups.items():
		if not g["limits"]:
			continue
		limit = min(g["limits"])  # strictest spec on the box
		if len(g["colours"]) > limit:
			over.append("mix group {0} has {1} colours (max {2})".format(key, len(g["colours"]), limit))
	if over:
		frappe.throw(
			_(
				"A mixed box exceeds the allowed colours per box — {0}. Reduce the colours in the "
				"box, or raise the spec's <b>Max Colours Per Box</b>."
			).format("; ".join(over)),
			title=_("Too Many Colours in Mixed Box"),
		)

	# 6. Every colour sharing one mix_group (or bunch_group, for Mixed Bunch) must
	#    book the SAME Number of Boxes. Packing later numbers boxes 1..N once per
	#    group and expects every colour's box N to be the same physical box --
	#    if one colour says 5 boxes and another says 4, "box 5" only exists for
	#    one of them and the packing guide can't be built consistently. This used
	#    to go unchecked entirely; three different places downstream (allocation,
	#    packing-guide building) each just guessed at "the" box count for a group
	#    by taking whichever colour's value they happened to see first.
	box_groups = defaultdict(set)
	for it in doc.items:
		if not it.item_code:
			continue
		if it.get("custom_mixed_bunch"):
			key = ("bunch", it.get("custom_bunch_group") or it.get("custom_line") or it.name)
		elif it.get("custom_mixed_box"):
			key = ("mix", it.get("custom_mix_group") or it.name)
		else:
			continue
		box_groups[key].add(int(it.get("custom_number_of_boxes") or 0))
	mismatched = [
		"{0} group {1}".format("Mixed Bunch" if kind == "bunch" else "Mixed Box", group)
		for (kind, group), counts in box_groups.items()
		if len(counts) > 1
	]
	if mismatched:
		frappe.throw(
			_(
				"Every colour in the same group must book the same Number of Boxes — {0} has "
				"colours that disagree. Fix Number of Boxes on each line before saving."
			).format("; ".join(mismatched)),
			title=_("Inconsistent Box Count"),
		)
