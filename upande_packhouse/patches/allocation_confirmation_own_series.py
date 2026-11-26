"""Allocation Confirmation names come from its own series, AL-#####.

`format:ALC-{#####}` drew on Frappe's one shared series, the same counter every `{#####}` name format uses --
Scouting Entry Metadata most of all (half a million names in three weeks), so confirmations went ALC-1109525 ...
ALC-1624983. A site override (Property Setter, ALC-26-.######) stood in for it on live; it goes, so the
doctype's own AL-.##### applies everywhere."""

import frappe


def execute():
	frappe.db.delete(
		"Property Setter",
		{"doc_type": "Allocation Confirmation", "property": ("in", ["autoname", "naming_rule"])},
	)
	frappe.clear_cache(doctype="Allocation Confirmation")
