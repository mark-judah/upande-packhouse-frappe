import importlib
import frappe
from werkzeug.test import EnvironBuilder

FARM = "Kapkolia"
HARVESTER = "500201"
GH = {
    "Odilia": "Kapkolia GH 18 - KR", "Alicia": "Kapkolia GH 18 - KR",
    "Mirabel": "Kapkolia GH 18 - KR", "Marisa": "Kapkolia GH 18 - KR",
    "Dinara": "Kapkolia GH 18 - KR",
}


def _call(method_path, payload):
    module_name, func_name = method_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    func = getattr(module, func_name)
    builder = EnvironBuilder(method="POST", json=payload)
    frappe.local.request = builder.get_request()
    frappe.response = frappe._dict()
    func()
    resp = frappe.response
    frappe.db.commit()
    return resp


def _receive_and_shelve(bucket_id, shelf_id):
    recv = _call("upande_quality.mobile.api.createReceivingStockEntry", {
        "bucket_id": bucket_id, "custom_receiving_batch_id": None, "confirm_receive": True,
    })
    shelf = _call("upande_quality.mobile.api.createShelvingEntry", {
        "farm": FARM, "shelf_id": shelf_id, "bucket_id": bucket_id,
    })
    ok = recv.get("status") == "received" and shelf.get("data", {}).get("status") == "success"
    return ok, recv, shelf


# (variety, cut_stage, length, num_buckets, bunches_per_bucket)
SPRAY_PLAN = [
    # 9, not 8: B05 ended up contaminated (blank cut_stage, 12 bunches
    # instead of 10 -- a duplicate-grading artifact from an earlier crashed
    # attempt) and can't be corrected post-receiving, so it's left as
    # harmless orphaned stock and a 9th bucket makes up the shortfall.
    ("Odilia", "2.0-2.5", "72cm", 9, 10),
    ("Odilia", "2.0-2.5", "52cm", 1, 10),
    ("Odilia", "1.5-2.0", "52cm", 1, 10),
    ("Alicia", "2.0-2.5", "72cm", 5, 10),
    ("Alicia", "2.0-2.5", "52cm", 3, 10),
    ("Mirabel", "2.0-2.5", "72cm", 5, 10),
    ("Mirabel", "2.0-2.5", "52cm", 2, 10),
    ("Marisa", "2.0-2.5", "72cm", 5, 10),
    ("Marisa", "2.0-2.5", "52cm", 2, 10),
    ("Dinara", "2.0-2.5", "52cm", 1, 10),
]

TAG = "KPK2B"
LABEL_DOCS = ["Bunch Label Table-02926", "Bunch Label Table-02927"]


def run():
    ok_count = 0
    fail_count = 0
    for variety, cut_stage, length, n_buckets, bunches_per_bucket in SPRAY_PLAN:
        gh = GH[variety]
        # Skip bunch_ids already consumed by the earlier (flawed) pass --
        # scoped to OUR OWN label batch, not a site-wide variety+length
        # match (this bench carries weeks of unrelated prior test data
        # under these same common variety/length combos, so a broad query
        # like "any Grading entry for this variety+length" both wildly
        # over-excludes candidates and can undercount how many bunches
        # remain -- confirmed empirically, see chat history).
        batch_bunch_ids = frappe.get_all(
            "Bunch QR Code",
            filters={"label_print_doc": ["in", LABEL_DOCS], "item_code": variety, "stem_length": length},
            pluck="name", order_by="name",
        )
        used = set(frappe.get_all(
            "Stock Entry",
            filters={"stock_entry_type": "Grading", "custom_bunch_id": ["in", batch_bunch_ids]},
            pluck="custom_bunch_id",
        ))
        available = [b for b in batch_bunch_ids if b not in used]
        idx = 0
        for b in range(1, n_buckets + 1):
            bucket_id = f"{TAG}-{variety[:3].upper()}-{cut_stage}-{length}-B{b:02d}"
            if frappe.db.exists("Shelf Item", {"bucket_id": bucket_id}):
                # No idx advance here: `available` already excludes every
                # bunch_id with a Grading entry (see `used` above), so an
                # already-shelved bucket's bunches are already absent from
                # this list -- advancing idx too would skip past that many
                # AGAIN, double-consuming the remaining pool and running out
                # early (confirmed: exactly this caused the previous retry's
                # IndexError on Odilia 72cm).
                continue
            harvest_entries_this_bucket = []
            for j in range(bunches_per_bucket):
                bunch_id = available[idx]
                idx += 1
                grade_resp = _call("upande_agriculture.mobile.api.createGradingStockEntry", {
                    "farm": FARM, "stock_entry_type": "Grading", "graded_by": HARVESTER,
                    "bunch_id": bunch_id, "qty": 1, "rose_type": "Spray Roses",
                    "source_warehouse": gh, "bucket_id": bucket_id,
                })
                # Reverse-lookup THIS scan's own harvest entry via the real,
                # persisted custom_grading_entry/custom_harvest_entry link
                # createGradingStockEntry itself sets -- not by "creation
                # desc" on the bucket. In a tight loop like this, several
                # inserts can land in the exact same `creation` instant, so
                # "desc" ties resolve arbitrarily and can silently keep
                # returning the SAME row for every scan -- confirmed: this
                # caused bucket B05 to end up with the same entry amended
                # repeatedly while its other 9 stayed blank.
                grading_entry = grade_resp.get("stock_entry")
                he = frappe.db.get_value(
                    "Stock Entry", {"custom_grading_entry": grading_entry}, "name",
                ) if grading_entry else None
                if he:
                    harvest_entries_this_bucket.append(he)

            # Amend EVERY bunch's harvest entry in this bucket -- amending
            # changes the entry's creation timestamp, so relying on "just
            # the first one" silently stops being first once amended (found
            # this the hard way: the first pass's amended entries ended up
            # LAST by creation time, so Receiving's "oldest unclaimed" pick
            # kept reading an un-amended, blank-cut_stage entry instead).
            for he in harvest_entries_this_bucket:
                amend = _call("upande_agriculture.mobile.api.amendHarvestEntry", {
                    "stock_entry_name": he, "cut_stage": cut_stage,
                })
                if amend.get("data", {}).get("error"):
                    print("  AMEND FAIL", bucket_id, he, amend)

            shelf_id = f"{TAG}-{variety[:3].upper()}-{cut_stage}-{length}-SH-{b:02d}"
            ok, recv, shelf = _receive_and_shelve(bucket_id, shelf_id)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                print("  FAIL", variety, cut_stage, length, bucket_id, recv, shelf)
        print(" ", variety, cut_stage, length, "->", n_buckets, "buckets done")

    print("OK buckets:", ok_count, "| FAILED buckets:", fail_count)
