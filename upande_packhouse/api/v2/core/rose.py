# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Spray / Standard classification through the Item Group tree.

Varieties sit in sub-groups ("Spray Roses - Premium", "Standard Roses -
Intermediate" ...), so comparing `item_group` with a flat "Spray Roses"
string misses almost everything (audit WF-17, WF-22, WF-29: Sprays showed
7,140 harvested stems against a true 89,545). Every v2 rose filter goes
through here.
"""

import frappe

ROOTS = {"spray": "Spray Roses", "standard": "Standard Roses"}


def _subtree(root):
	bounds = frappe.db.get_value("Item Group", root, ["lft", "rgt"], as_dict=True)
	if not bounds:
		return []
	return frappe.get_all(
		"Item Group",
		filters={"lft": [">=", bounds.lft], "rgt": ["<=", bounds.rgt]},
		pluck="name",
	)


def rose_groups(kind):
	"""Item Groups under the Spray or Standard root (cached per request)."""
	cache = getattr(frappe.local, "ph2_rose_groups", None)
	if cache is None:
		cache = frappe.local.ph2_rose_groups = {}
	if kind not in cache:
		cache[kind] = _subtree(ROOTS[kind]) if kind in ROOTS else []
	return cache[kind]


def rose_type(item_group):
	"""'spray' | 'standard' | None for an Item Group name."""
	for kind in ROOTS:
		if item_group and item_group in rose_groups(kind):
			return kind
	return None


def normalize(rose):
	"""Accept the values pages send ('all', 'spray', 'standard', 'Spray Roses' ...)."""
	r = (rose or "all").strip().lower()
	if r.startswith("spray"):
		return "spray"
	if r.startswith("standard") or r == "std":
		return "standard"
	return "all"


def rose_sql(column, rose, params, key="ph2_rose"):
	"""SQL fragment limiting `column` (an item_group column) to a rose type.

	Adds the bound list to `params`; returns "" for 'all'. An empty subtree
	yields a condition that matches nothing rather than everything."""
	kind = normalize(rose)
	if kind == "all":
		return ""
	groups = rose_groups(kind)
	if not groups:
		return " AND 1=0"
	params[key] = tuple(groups)
	return " AND {0} IN %({1})s".format(column, key)
