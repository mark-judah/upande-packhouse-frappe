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
    per-stem rate must be converted. This is the v15 "Rate based on Length"
    exchange behaviour, made currency-correct: it uses the actual price-list ->
    order rate, fetched live via ERPNext (Currency Exchange record, else the
    configured exchange-rate service) when no stored rate exists."""
    pl_currency = frappe.db.get_value("Price List", price_list, "currency")
    if not pl_currency or not doc_currency or pl_currency == doc_currency:
        return 1.0
    try:
        from erpnext.setup.utils import get_exchange_rate

        return float(get_exchange_rate(pl_currency, doc_currency)) or 1.0
    except Exception:
        return 1.0


def sales_order_before_validate(doc, method=None):
    """Quantity tally + point the order at the customer's price list, before
    ERPNext's qty>0 check and its own (length-blind) pricing run."""
    if (doc.get("custom_business_unit") or "") != "Roses":
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


def sales_order_price(doc, method=None):
    """Length-aware pricing — runs on validate, AFTER ERPNext's (length-blind) standard
    pricing, so our per-length rate wins. Item Price is per stem → express per line UOM."""
    if (doc.get("custom_business_unit") or "") != "Roses":
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
            {"item_code": it.item_code, "price_list": price_list,
             "custom_length": it.custom_length, "selling": 1},
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
    if (doc.get("custom_business_unit") or "") != "Roses":
        return

    # 1. must have a price list configured (line-level or the customer's default)
    if not _customer_default_pl(doc):
        frappe.throw(
            "Customer <b>{0}</b> has no Default Price List configured. Set one on the Customer "
            "(or on this order) before saving a Roses order.".format(doc.customer),
            title="Missing Price List",
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
        frappe.throw("Complete these lines before saving: {0}.".format("; ".join(incomplete)),
                     title="Missing Details")

    # 2. every line must carry a packrate, else packing can't enforce box capacity
    missing = []
    for i, it in enumerate(doc.items, 1):
        if it.item_code and not _line_packrate(it):
            missing.append(str(i))
    if missing:
        frappe.throw("No packrate set on line(s) {0}. Set a Packrate (and number of boxes) so the "
                     "box math and packing capacity are defined.".format(", ".join(missing)))

    # 3. every line must be priced. sales_order_price zeroes the rate when there
    #    is no Item Price for this exact variety + stem length, so a zero rate here
    #    means "no length-specific price" — block so the user adds it.
    unpriced = []
    for it in doc.items:
        if it.item_code and float(it.get("rate") or 0) <= 0:
            unpriced.append("{0} {1}".format(it.item_code, it.get("custom_length") or "").strip())
    if unpriced:
        frappe.throw(
            "No price found for: <b>{0}</b>. Add an Item Price for that variety and stem "
            "length in the order's price list before saving.".format(", ".join(unpriced)),
            title="Missing Price",
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
            "No Sales UOM set on: <b>{0}</b>. Set a Sales UOM on the item so the order line "
            "has a unit of measure.".format(", ".join(sorted(set(no_sales_uom)))),
            title="Missing Sales UOM",
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
        limit = min(g["limits"])          # strictest spec on the box
        if len(g["colours"]) > limit:
            over.append("mix group {0} has {1} colours (max {2})".format(key, len(g["colours"]), limit))
    if over:
        frappe.throw(
            "A mixed box exceeds the allowed colours per box — {0}. Reduce the colours in the "
            "box, or raise the spec's <b>Max Colours Per Box</b>.".format("; ".join(over)),
            title="Too Many Colours in Mixed Box",
        )
