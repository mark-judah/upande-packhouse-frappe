import sys

sys.path.insert(0, "../apps/upande_packhouse/docs/bucket-journey")
from jtest_lib import *

init()
B2 = "JTEST-002"
B3 = "JTEST-003"
TRUCK = "KCF 700Z"
V_SIM = dict(
	farm="Simotwo",
	greenhouse="Simotwo GH 14 - KR",
	harvester="100374",
	stem_length="52cm",
	item_code="Aqua",
	cut_stage="1.5-2",
)
V_SIM_ER = dict(V_SIM, item_code="Ever Red", greenhouse="Simotwo GH 14 - KR")


def show(r):
	print("   ", r[0], str(r[1].get("message") or r[1].get("data") or r[1])[:700])


if __name__ == "__main__":
	# JTEST-003 journey 2 continues: the transfer of the order it replaced JTEST-002 on
	opl = "OPL-2026-00063"
	show(
		call(
			UQ + "saveTrolleyData",
			dict(data=dict(trolley_id="JTEST-TROLLEY-2", buckets=[dict(opl_name=opl, bucket_id=B3)])),
			"B3-J2 on trolley",
		)
	)
	show(
		call(
			UQ + "loadTrolleyInTruck",
			dict(data=dict(trolley_id="JTEST-TROLLEY-2", truck_id=TRUCK)),
			"B3-J2 loaded on truck",
		)
	)
	trip = frappe.db.get_value(
		"Bucket Request Trip",
		{"vehicle": TRUCK, "status": ["in", ["Scheduled", "Draft", "Requested"]]},
		"name",
		order_by="creation desc",
	)
	print(trip)
	show(
		call(
			"upande_packhouse.mobile.api.dispatchBucketTrip",
			dict(name=trip),
			"B3-J2 dispatch trip",
			form=True,
		)
	)
	show(shelve(B3, "JTEST-SHELF-11", farm="Kapkolia", step="B3-J2 shelved at the hub (arrival)"))
	pli = frappe.db.get_value("Pick List Item", {"bucket": B3, "parent": opl}, ["name", "sales_order_item"])
	print(pli)
	show(issue(B3, pli[1], opl, "B3-J2 issue"))
	# JTEST-003 journey 3: ends IN TRANSIT
	show(harvest(B3, 20, V_SIM, "B3-J3 harvest at Simotwo"))
	show(receive(B3, "JTBATCH-B3-3", "B3-J3 receive"))
	show(shelve(B3, "JTEST-SHELF-R2", farm="Simotwo", step="B3-J3 shelve Simotwo"))
	so, soi = make_so("B3-J3", [10])
	show(allocate(so, [(soi[0], 10)], B3, team="Team A", step="B3-J3 allocate (awaiting transfer)"))
	opl3 = frappe.db.get_value("Pick List Item", {"bucket": B3, "sales_order_item": soi[0]}, "parent")
	show(
		call(
			UQ + "saveTrolleyData",
			dict(data=dict(trolley_id="JTEST-TROLLEY-3", buckets=[dict(opl_name=opl3, bucket_id=B3)])),
			"B3-J3 on trolley",
		)
	)
	show(
		call(
			UQ + "loadTrolleyInTruck",
			dict(data=dict(trolley_id="JTEST-TROLLEY-3", truck_id=TRUCK)),
			"B3-J3 loaded on truck",
		)
	)
	trip = frappe.db.get_value(
		"Bucket Request Trip",
		{"vehicle": TRUCK, "status": ["in", ["Scheduled", "Draft", "Requested"]]},
		"name",
		order_by="creation desc",
	)
	show(
		call(
			"upande_packhouse.mobile.api.dispatchBucketTrip",
			dict(name=trip),
			"B3-J3 dispatch trip (left, stays in transit)",
			form=True,
		)
	)
	# JTEST-002 journey 5: Ever Red, awaiting transfer (open)
	show(harvest(B2, 10, V_SIM_ER, "B2-J5 harvest Ever Red at Simotwo"))
	show(receive(B2, "JTBATCH-B2-5", "B2-J5 receive"))
	show(shelve(B2, "JTEST-SHELF-R3", farm="Simotwo", step="B2-J5 shelve Simotwo"))
	so, soi = make_so("B2-J5", [10], item="Ever Red")
	show(
		allocate(
			so,
			[(soi[0], 10)],
			B2,
			item="Ever Red",
			team="Team A",
			step="B2-J5 allocate (awaiting transfer, stays open)",
		)
	)
