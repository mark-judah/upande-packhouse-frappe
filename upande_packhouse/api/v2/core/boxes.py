# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""How many physical boxes an order holds — the one rule every v2 page uses.

Stems add up line by line; boxes don't. Every line of one mixed box (or
mixed bunch) describes the SAME physical boxes, once per colour, so a mixed
group's `custom_number_of_boxes` is counted once.

The key, per line:
  * mixed line (mixed box or mixed bunch)
      - filled from a spec  -> (spec, length): a spec is one box, and one spec
        can carry a mixed bunch AND mono bunches feeding the same box
        (XPOL TOSCA_02721_10), so both bunch types share the key;
      - else its bunch group, else its mix group;
  * straight line -> the line itself.

This differs from sales_order_engine._set_order_summary in ONE place, on
purpose (audit SO-3): the engine also merges STRAIGHT lines filled from the
same multi-variety Mono-Box spec, but allocation creates one Order Pick List
per straight line, so each of those lines is its own boxes
(SAL-ORD-2026-00382 is 2 boxes of 720, not 1 box of 1,440). v2 never reads
the stored `custom_total_boxes`, which is stale on 76 orders anyway (SO-2).

A group whose lines disagree on their box count (the engine blocks this on
save since rule 6, older orders can still have it) counts the LARGEST, since
that many physical boxes exist for at least one colour.
"""

from upande_packhouse.api.v2.core.units import line_kind


def box_key(line):
	"""Stable string key; lines sharing it share physical boxes within one order."""
	if line_kind(line) != "straight":
		if line.get("custom_line"):
			return "spec::{0}::{1}".format(line["custom_line"], line.get("custom_length") or "")
		if line.get("custom_bunch_group"):
			return "bunch::{0}".format(line["custom_bunch_group"])
		if line.get("custom_mix_group"):
			return "mix::{0}".format(line["custom_mix_group"])
	return "line::{0}".format(line.get("name") or id(line))


def order_boxes(lines):
	"""Physical boxes ordered across `lines` of ONE Sales Order."""
	per_key = {}
	for ln in lines:
		if not ln.get("item_code"):
			continue
		k = box_key(ln)
		per_key[k] = max(per_key.get(k, 0), int(ln.get("custom_number_of_boxes") or 0))
	return sum(per_key.values())


def boxes_by_order(lines, order_field="parent"):
	"""{sales_order: boxes} for lines of many orders (keys are per order)."""
	grouped = {}
	for ln in lines:
		grouped.setdefault(ln.get(order_field), []).append(ln)
	return {so: order_boxes(rows) for so, rows in grouped.items()}
