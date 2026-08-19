"""Specifications lifecycle helpers.

Temporary specs carry an expiry date; once past it they must stop appearing in
the Sales Order spec picker. Rather than filter on dates everywhere, a daily job
flips expired Temporary specs to status = Inactive (history preserved).
"""

import frappe


def _ensure_bunch_uom(stems_per_bunch):
	"""A bunch UOM is named "Bunch (N)" where N stems make a bunch. The Sales Order
	spec-autofill sets each line's UOM to this, so the UOM record must exist or the
	order save fails with "Could not find Row #N: UOM: Bunch (N)". Match the existing
	"Bunch (10)": not whole-number, since qty (stems / factor) can be fractional."""
	n = int(stems_per_bunch or 0)
	if n <= 0:
		return None
	name = "Bunch ({0})".format(n)
	if not frappe.db.exists("UOM", name):
		frappe.get_doc({
			"doctype": "UOM",
			"uom_name": name,
			"must_be_whole_number": 0,
		}).insert(ignore_permissions=True)
	return name


def _ensure_packrate(stems_per_box):
	"""A Packrate is named by its stems-per-box number (autoname = prompt). Straight-box
	SO lines link to it, so provision it up front from the spec's pack rate."""
	n = int(stems_per_box or 0)
	if n <= 0:
		return None
	name = str(n)
	if not frappe.db.exists("Packrate", name):
		pr = frappe.new_doc("Packrate")
		pr.name = name
		pr.stems_per_box = n
		pr.insert(ignore_permissions=True)
	return name


def ensure_spec_uoms_and_packrates(doc, method=None):
	"""On every Specifications save, provision the bunch UOMs and packrates its box
	items imply, so downstream Sales Orders never hit a missing-UOM / missing-Packrate
	link error. Returns the (uoms, packrates) it touched (handy for bulk backfill)."""
	uoms, packrates = [], []
	for bi in (doc.box_items or []):
		u = _ensure_bunch_uom(bi.stems_per_bunch)
		if u:
			uoms.append(u)
		p = _ensure_packrate(bi.pack_rate)
		if p:
			packrates.append(p)
	return uoms, packrates


def expire_temporary_specs():
	"""Daily: deactivate Temporary specs whose expiry_date has passed."""
	today = frappe.utils.nowdate()
	names = frappe.get_all(
		"Specifications",
		filters={
			"spec_type": "Temporary",
			"status": "Active",
			"expiry_date": ["<", today],
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Specifications", name, "status", "Inactive", update_modified=False)
	if names:
		frappe.db.commit()
	return names
