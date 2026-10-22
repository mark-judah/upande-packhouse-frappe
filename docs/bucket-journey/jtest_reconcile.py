"""Adds to the manifest every JTEST row that the per-step sweep could not see (child rows whose creation
is inherited from the parent, rows created by replace/transfer side effects). Reads + writes the manifest only."""
import sys; sys.path.insert(0, "../apps/upande_packhouse/docs/bucket-journey")
from jtest_lib import *
init()
m = load(); n0 = len(m["created"])
Q = [("Shelf Item", "bucket_id"), ("Shelving Log", "bucket_id"), ("Pick List Item", "bucket"), ("Bucket Allocation Status", "bucket_id"), ("Bucket Reuse Anomaly", "bucket_id"),
	("Bucket Replacement", "old_bucket"), ("Bucket Replacement", "new_bucket"), ("Bucket Transfer Event", "bucket"), ("Discard Request Bucket", "bucket_id"), ("Stem Replacement Log", "destination_bucket"),
	("Cold Store Stock Take Bucket", "bucket_id"), ("Bucket QR Code", "name"), ("Stock Entry", "custom_bucket_id")]
for dt, col in Q:
	for (name,) in frappe.db.sql("select name from `tab%s` where %s like 'JTEST-%%'" % (dt, col)):
		add(dt, name, "reconcile", "by bucket id", m)
for (name,) in frappe.db.sql("select distinct parent from `tabBucket Allocations` where parent in (select name from `tabBucket Allocation Status` where bucket_id like 'JTEST-%')"):
	pass
for (n,) in frappe.db.sql("select name from `tabBucket Allocations` where parent in (select name from `tabBucket Allocation Status` where bucket_id like 'JTEST-%')"):
	add("Bucket Allocations", n, "reconcile", "child of BAS", m)
for (n,) in frappe.db.sql("select distinct parent from `tabDiscard Request Bucket` where bucket_id like 'JTEST-%'"):
	add("Discard Request", n, "reconcile", "by bucket id", m)
for (n,) in frappe.db.sql("select name from `tabOrder Pick List` where name in (select parent from `tabPick List Item` where bucket like 'JTEST-%') or sales_order like 'JTEST-SO-%'"):
	add("Order Pick List", n, "reconcile", "by pick rows", m)
for (n,) in frappe.db.sql("select name from `tabShelf` where name like 'JTEST-SHELF-%'"):
	add("Shelf", n, "reconcile", "by name", m)
for (n,) in frappe.db.sql("select name from `tabSales Order` where name like 'JTEST-SO-%'"):
	add("Sales Order", n, "reconcile", "by name", m)
for (n,) in frappe.db.sql("select distinct parent from `tabPick List Item` where bucket like 'JTEST-%'"):
	pass
m = load(); print("manifest rows", n0, "->", len(m["created"]))
from collections import Counter
print(Counter(c["doctype"] for c in m["created"]))
