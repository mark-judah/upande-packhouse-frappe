import sys, json
import frappe
frappe.init(site="kaitet.local", sites_path="."); frappe.connect(); frappe.set_user("Administrator")
from upande_packhouse.api.v2 import rt_journey as RJ
b = sys.argv[1]; only = sys.argv[2] if len(sys.argv) > 2 else None
r = RJ.get_bucket_journeys(b, limit=0, only=only)
print("BUCKET", r["bucket"]["id"], "journeys", r["bucket"]["journeys"], "| now:", r["now"]["label"], "-", r["now"]["detail"])
for j in r["journeys"]:
    print("== J%d [%s] %s | %s | stems %s alloc %s issued %s on_shelf %s q %s acc %s rej %s" % (j["number"], j["kind"], j["state"], j["variety"], j["stems"], j["allocated_stems"], j["issued_stems"], j["on_shelf_stems"], j["quarantined_stems"], j["accepted_stems"], j["rejected_stems"]))
    for f in j["flags"]: print("   FLAG", f["level"], f["text"])
    for s in j["steps"]: print("   %s %-16s %-45s | %s | %s %s" % (s["t"][11:19], s["ev"], s["title"][:45], s["where"][:30], s["line"][:70], ("["+";".join(s["flags"])+"]") if s["flags"] else ""))
    for s in j["ledger"]: print("   (ledger) %s %s %s" % (s["t"][11:19], s["title"], s["line"][:50]))
print("UNPLACED", [(u["ev"], u["title"], u.get("reason")) for u in r["unplaced"]])
print("FLOW", {f["key"]: f["count"] for f in r["flow"] if f["count"]})
