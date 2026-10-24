"""OPTIONAL, NOT RUN, NOT TESTED (the user chose to keep the JTEST data on the local site).
Removes everything in jtest_manifest.json, then sweeps stragglers by bucket id 'JTEST-*'.
Run from sites/:  ../env/bin/python ../apps/upande_packhouse/docs/bucket-journey/jtest_cleanup.py --yes
Order: pick lists -> allocation status -> shelving/shelf items -> anomalies/replacements/events/requests -> stock entries
(cancel then delete; their ledger rows go with them) -> sales orders -> shelves -> QR codes -> Version/Comment/Error Log rows.
Stock Bin quantities of Aqua/Ever Red at the touched warehouses are restored by the cancel of the stock entries."""

import json
import os
import sys

import frappe

if "--yes" not in sys.argv:
	sys.exit("dry guard: pass --yes to really delete (site kaitet.local only)")
frappe.init(site="kaitet.local", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")
m = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "jtest_manifest.json")))
names = {}
for c in m["created"]:
	names.setdefault(c["doctype"], []).append(c["name"])
counts = {}


def count(dt):
	try:
		return frappe.db.count(dt)
	except Exception:
		return None


touched = sorted(names)
before = {d: count(d) for d in touched}


def kill(dt, nm, cancel=False):
	if not frappe.db.exists(dt, nm):
		return
	d = frappe.get_doc(dt, nm)
	if cancel and d.docstatus == 1:
		d.flags.ignore_links = True
		d.cancel()
	frappe.delete_doc(dt, nm, force=1, ignore_permissions=True)


ORDER = [
	"Order Pick List",
	"Bucket Allocation Status",
	"Shelving Log",
	"Shelf",
	"Bucket Reuse Anomaly",
	"Bucket Replacement",
	"Bucket Transfer Event",
	"Stem Replacement Log",
	"Discard Request",
	"Bucket Request Trip",
	"Quality Reporting",
	"Stock Entry",
	"Sales Order",
	"Bucket QR Code",
]
for dt in ORDER:
	for nm in reversed(names.get(dt, [])):
		try:
			kill(dt, nm, cancel=dt in ("Order Pick List", "Stock Entry", "Sales Order", "Discard Request"))
		except Exception as e:
			print("skip", dt, nm, e)
	frappe.db.commit()
for pat, dt, col in (
	("JTEST-%", "Shelf Item", "bucket_id"),
	("JTEST-%", "Shelving Log", "bucket_id"),
	("JTEST-%", "Bucket Reuse Anomaly", "bucket_id"),
	("JTEST-%", "Bucket Allocation Status", "bucket_id"),
):
	frappe.db.sql("delete from `tab%s` where %s like %%s" % (dt, col), pat)
for dt in ("Version", "Comment", "Error Log"):
	for nm in names.get(dt, []):
		frappe.db.delete(dt, {"name": nm})
frappe.db.commit()
for d in touched:
	print(d, before[d], "->", count(d))
