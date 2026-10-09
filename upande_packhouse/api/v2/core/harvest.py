# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Harvested stems — one definition for Production, Variety Tree and Stem Movement.

    harvested = submitted "Harvesting" Stock Entries, Stock Entry Detail
                transfer_qty (stems in stock UOM), by posting_date

Stem length is the row's own length, else the entry header's (WF-21). Rose
type comes from the Item Group tree (rose.py). Location filters use
Farm.farm_location; farms without one are reported as "Unassigned" (WF-20).

Speed (WF-18, SO-26): the query is driven from the Stock Entry date index
(stock_entry_type, docstatus, posting_date) with STRAIGHT_JOIN, and the
item-group filter is a bound IN list, so MariaDB never starts from Item and
walks 3.36M historical detail rows (that plan took 57-83 s for one week).
"""

import frappe

from upande_packhouse.api.v2.core.rose import rose_sql

GROUPABLE = {
	"date": "se.posting_date",
	"item_code": "sed.item_code",
	"stem_length": "COALESCE(NULLIF(sed.custom_stem_length, ''), se.custom_stem_length)",
	"farm": "se.farm",
	"greenhouse": "se.custom_greenhouse",
}


def harvest(
	*,
	date_from,
	date_to,
	group_by=("item_code",),
	farms=None,
	location=None,
	rose=None,
	item_codes=None,
):
	"""Harvested stems grouped by any of: date, item_code, stem_length, farm, greenhouse.

	Returns a list of dicts with the group keys plus `stems` and `entries`."""
	unknown = set(group_by) - set(GROUPABLE)
	if unknown:
		frappe.throw("Unknown harvest grouping: {0}".format(", ".join(sorted(unknown))))
	params = {"f": date_from, "t": date_to}
	where = [
		"se.stock_entry_type = 'Harvesting'",
		"se.docstatus = 1",
		"se.posting_date BETWEEN %(f)s AND %(t)s",
	]
	# None = no farm filter; an empty list means "no such farm" and matches nothing.
	if farms is not None:
		where.append("se.farm IN %(farms)s")
		params["farms"] = tuple(farms) or ("",)
	if location == "__unassigned__":
		where.append(
			"(IFNULL(se.farm, '') = '' OR se.farm IN (SELECT name FROM `tabFarm` WHERE IFNULL(farm_location, '') = ''))"
		)
	elif location:
		where.append("se.farm IN (SELECT name FROM `tabFarm` WHERE farm_location = %(loc)s)")
		params["loc"] = location
	if item_codes:
		where.append("sed.item_code IN %(items)s")
		params["items"] = tuple(item_codes)
	rose_cond = ""
	if rose and rose != "all":
		# Bound IN list of item codes in the rose subtree: keeps the date index as the driver.
		frag = rose_sql("item_group", rose, params, key="ph2_hrose")
		rose_cond = " AND sed.item_code IN (SELECT name FROM `tabItem` WHERE 1=1{0})".format(frag)
	cols = [f"{GROUPABLE[g]} AS `{g}`" for g in group_by]
	group = ", ".join(f"`{g}`" for g in group_by) or "NULL"
	# nosemgrep: frappe-sql-format-injection -- holes are fixed SQL from GROUPABLE, values bound
	sql = f"""
		SELECT STRAIGHT_JOIN {", ".join(cols + ["SUM(sed.transfer_qty) AS stems", "COUNT(DISTINCT se.name) AS entries"])}
		FROM `tabStock Entry` se FORCE INDEX (stock_entry_type_docstatus_posting_date_index)
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE {" AND ".join(where)} {rose_cond}
		GROUP BY {group}
	"""
	return frappe.db.sql(sql, params, as_dict=True)
