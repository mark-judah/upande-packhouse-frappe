# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Units for a Sales Order line: stems, bunches, stems per box.

Stems are the only unit the v2 backend adds up. Bunches are always derived
from stems with the line's own bunch size (the "N" in UOM "Bunch (N)"), never
with a fixed 10 (audit WF-4), and Box Label Item quantities, which are stored
in bunches, are never summed as stems (MV-4).

These delegate to sales_order_engine so the dashboards and the Sales Order
save hook can't drift apart.
"""

from upande_packhouse.sales_order_engine import _line_packrate, _uom_factor


def stems_per_bunch(uom):
	"""Stems in one bunch of `uom` ("Bunch (10)" -> 10, "Stems" -> 1)."""
	return _uom_factor(uom) or 1


def stems_per_box(line):
	"""Stems of THIS line's variety in one box (mixed lines: that variety's share)."""
	return _line_packrate(line)


def line_kind(line):
	"""'mixed_bunch' | 'mixed_box' | 'straight' (mixed bunch wins, as in packing_guide._box_kind)."""
	if line.get("custom_mixed_bunch"):
		return "mixed_bunch"
	if line.get("custom_mixed_box"):
		return "mixed_box"
	return "straight"


def ordered_stems(line):
	"""Ordered stems of a line.

	`stock_qty` is kept equal to stems-per-box x boxes by the engine on save and
	matches it on every submitted line (verified, audit OR / ST-9). A line with
	no stock_qty falls back to the same formula."""
	if line.get("stock_qty"):
		return float(line["stock_qty"])
	return float(stems_per_box(line) * int(line.get("custom_number_of_boxes") or 0))


def to_bunches(stems, uom):
	"""Bunches for a stem count at this UOM (may be fractional when a packrate
	isn't a whole number of bunches -- see audit SO-10)."""
	return float(stems or 0) / stems_per_bunch(uom)
