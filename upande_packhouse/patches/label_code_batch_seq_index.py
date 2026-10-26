# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Composite index so a render chunk (batch + position range) is one index range scan."""

import frappe


def execute():
	frappe.reload_doc("upande_packhouse", "doctype", "label_code")
	frappe.db.add_index("Label Code", ["batch", "seq"], "batch_seq")
