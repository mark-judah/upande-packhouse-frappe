"""Seed the four HS codes the Roses invoice uses as Customs Tariff Numbers and give every
cut-flower Item that has none its crop's default (Item > Customs Tariff Number).

The code is per ITEM because dried and dyed stems (060390) share Item Groups with fresh ones;
only items with no code yet are filled, so anything set by hand is never touched. Dried or dyed
items must be set to 060390 on the Item. Safe to run again.
"""

import frappe

from upande_packhouse.item_groups import HS_DEFAULT_BY_GROUP, HS_DESCRIPTIONS


def execute():
	for number, description in HS_DESCRIPTIONS.items():
		if not frappe.db.exists("Customs Tariff Number", number):
			frappe.get_doc({"doctype": "Customs Tariff Number", "tariff_number": number, "description": description}).insert(
				ignore_permissions=True
			)
	for group, code in HS_DEFAULT_BY_GROUP.items():
		if not frappe.db.exists("Item Group", group):
			continue
		g = frappe.db.get_value("Item Group", group, ["lft", "rgt"], as_dict=True)
		groups = frappe.get_all("Item Group", filters={"lft": [">=", g.lft], "rgt": ["<=", g.rgt]}, pluck="name")
		frappe.db.sql(
			"""UPDATE `tabItem` SET customs_tariff_number = %s
			   WHERE item_group IN %s AND IFNULL(customs_tariff_number, '') = ''""",
			(code, tuple(groups)),
		)
