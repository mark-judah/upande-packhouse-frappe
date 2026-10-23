"""JTEST synthetic bucket data: helpers (HTTP calls to the LOCAL site only + manifest).
Run from sites/ with ../env/bin/python. Never points at any host except kaitet.local:8002."""
import json, os, time, datetime
import requests
import frappe

HOST = "http://kaitet.local:8002"
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "jtest_manifest.json")
TOKEN_FILE = "/tmp/claude-1000/-home-jk-Projects-upande-local-bench-v16/17d427cc-b5a7-41af-a956-596b17b9ecd7/scratchpad/local_admin_token"

# doctypes watched for new rows after every step (creation >= step start)
WATCH = ["Stock Entry", "Stock Ledger Entry", "GL Entry", "Shelving Log", "Bucket QR Code", "Shelf", "Bucket Allocation Status",
	"Order Pick List", "Bucket Reuse Anomaly", "Bucket Replacement", "Bucket Transfer Event", "Stem Replacement Log", "Discard Request",
	"Quality Reporting", "Bucket Request Trip", "Sales Order", "Comment", "Version", "Error Log", "Packing Reject Log", "Corrective Action Report"]
CHILD = {"Shelf Item": "Shelf", "Pick List Item": "Order Pick List"}


def init():
	if not getattr(frappe.local, "db", None):
		frappe.init(site="kaitet.local", sites_path="."); frappe.connect()


def now_db():
	return frappe.db.sql("select now(6)")[0][0]


def load():
	try:
		return json.load(open(MANIFEST))
	except Exception:
		return {"site": "kaitet.local", "created": [], "touched_existing": [], "steps": []}


def save(m):
	json.dump(m, open(MANIFEST + ".tmp", "w"), indent=1, default=str); os.replace(MANIFEST + ".tmp", MANIFEST)


def add(doctype, name, how, step, m=None):
	m = m or load()
	if any(c["doctype"] == doctype and c["name"] == name for c in m["created"]):
		return
	m["created"].append({"order": len(m["created"]) + 1, "doctype": doctype, "name": name, "how": how, "step": step})
	save(m)


def note_step(step, how):
	m = load(); m["steps"].append({"step": step, "how": how, "at": str(datetime.datetime.now())}); save(m)


def sweep(t0, step, how):
	"""Append every row created since t0 in WATCH doctypes + Shelf Item / Pick List Item children."""
	frappe.db.rollback()  # fresh snapshot
	m = load(); n = 0
	for dt in WATCH:
		rows = frappe.db.sql("select name from `tab%s` where creation >= %%s" % dt, (t0,))
		for (name,) in rows:
			if dt in ("Version", "Comment", "Error Log") :
				pass
			add(dt, name, how, step, m); n += 1
	for dt in ("Shelf Item", "Pick List Item", "Bucket Allocations", "Discard Request Bucket"):
		try:
			for (name,) in frappe.db.sql("select name from `tab%s` where creation >= %%s" % dt, (t0,)):
				add(dt, name, how, step, m); n += 1
		except Exception:
			pass
	return n


def _hdr():
	return {"Authorization": "token " + open(TOKEN_FILE).read().strip(), "Content-Type": "application/json"}


def call(method, payload=None, step="", http="POST", sleep=2.0, form=False):
	"""Call a whitelisted/Server-Script method on the local site, then sweep new rows into the manifest."""
	init()
	t0 = now_db()
	time.sleep(0.05)
	url = "%s/api/method/%s" % (HOST, method)
	assert "kaitet.local" in url
	if form:
		r = requests.request(http, url, headers={"Authorization": _hdr()["Authorization"]}, data=payload or {}, timeout=300)
	else:
		r = requests.request(http, url, headers=_hdr(), data=json.dumps(payload or {}), timeout=300)
	try:
		body = r.json()
	except Exception:
		body = {"raw": r.text[:500]}
	n = sweep(t0, step or method, "api:" + method)
	note_step(step or method, "api:%s %s -> %s" % (method, json.dumps(payload, default=str)[:300], r.status_code))
	print("[%s] %s -> %s, +%d rows" % (step, method, r.status_code, n))
	time.sleep(sleep)
	return r.status_code, body


def direct(step, fn, sleep=1.0):
	"""Run in-process code that inserts/updates test docs; fn() returns nothing; new rows are swept."""
	init()
	t0 = now_db()
	out = fn()
	frappe.db.commit()
	n = sweep(t0, step, "direct")
	note_step(step, "direct insert/update in-process")
	print("[%s] direct -> +%d rows" % (step, n))
	time.sleep(sleep)
	return out


SO_TEMPLATE = "SAL-ORD-2026-01738"  # read only: copied as a structure


def make_so(tag, lines, item="Aqua", length="52cm", farm="Kapkolia", step=""):
	"""Create + submit a NEW test Sales Order (copy of an existing one's shape, never touches it).
	lines: list of stems per order line. Adds Confirmed Stems rows for `farm`."""
	init()
	frappe.set_user("Administrator")
	src = frappe.get_doc("Sales Order", SO_TEMPLATE)
	t0 = now_db()
	so = frappe.copy_doc(src)
	so.docstatus = 0
	so.letter_head = None; so.amended_from = None; so.status = "Draft"; so.per_delivered = 0; so.per_billed = 0; so.billing_status = "Not Billed"; so.delivery_status = "Not Delivered"
	so.custom_order_name = "JTEST " + tag
	so.po_no = "JTEST-" + tag
	so.transaction_date = frappe.utils.nowdate()
	so.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 2)
	so.items = []
	so.payment_schedule = []
	so.taxes = [] if False else so.taxes
	so.custom_confirmed_stems_table = []
	tmpl = src.items[0]
	for stems in lines:
		row = frappe.copy_doc(tmpl)
		row.item_code = item
		row.item_name = item
		row.custom_length = length
		row.custom_packrate = str(stems)
		row.custom_number_of_boxes = 1
		row.uom = "Bunch (10)"; row.stock_uom = "Stems"; row.conversion_factor = 10
		row.qty = stems / 10.0; row.stock_qty = stems
		row.custom_ordered_quantity = stems
		row.delivery_date = so.delivery_date
		row.item_group = frappe.db.get_value("Item", item, "item_group")
		so.append("items", row)
	for f in ("custom_opl", "custom_fully_allocated", "custom_stock_available"):
		for r in so.items:
			r.set(f, None if f == "custom_opl" else 0)
	so.name = "JTEST-SO-" + tag  # explicit: the SAL-ORD series counter on this site lags behind existing names
	frappe.flags.in_import = True
	try:
		so.insert(ignore_permissions=True)
	finally:
		frappe.flags.in_import = False
	so.submit()
	for r in so.items:
		so.append("custom_confirmed_stems_table", {"farm": farm, "stems": r.stock_qty, "sales_order_item": r.name})
	for c in so.custom_confirmed_stems_table:
		c.db_insert()
	frappe.db.commit()
	n = sweep(t0, step or "SO " + tag, "direct")
	note_step(step or "SO " + tag, "direct: new Sales Order %s (lines %s) copied from structure of %s" % (so.name, lines, SO_TEMPLATE))
	print("SO", so.name, [r.name for r in so.items])
	return so.name, [r.name for r in so.items]


ALLOC = "upande_packhouse.upande_packhouse.page.sales_allocation.sales_allocation.allocate_stock_with_buckets"


def allocate(so, soi_stems, bucket, item="Aqua", length="52cm", team="Team A", step="allocate"):
	"""soi_stems: list of (sales_order_item, stems). Uses the real Sales Allocation function."""
	allocs = [dict(item_code=item, bucket_id=bucket, qty=q, sales_order_item=i, stem_length=length, warehouse="", uom="Bunch (10)",
		stock_uom="Stems", conversion_factor=10, length_status="exact") for i, q in soi_stems]
	return call(ALLOC, dict(sales_order=so, allocations=allocs, location="Kapkolia", teams={i: team for i, q in soi_stems}), step)


def issue(bucket, soi, opl, step):
	return call("upande_packhouse.mobile.api.issueBucketToSaleOrderItem", dict(bucket=bucket, sale_order_item=soi, opl_name=opl), step)


AQ = dict(farm="Kapkolia", greenhouse="Chepsito GH 12 - KR", harvester="201480", stem_length="52cm", item_code="Aqua", cut_stage="1.5-2.0")
ER = dict(farm="Kapkolia", greenhouse="Torongo GH 06 - KR", harvester="200670", stem_length="52cm", item_code="Ever Red", cut_stage="1.5-2.0")
UQ = "upande_quality.mobile.api."


def harvest(bucket, qty, v=AQ, step="harvest"):
	return call("upande_agriculture.mobile.api.createHarvestStockEntry", dict(v, quantity=qty, bucket_id=bucket), step)


def receive(bucket, batch, step="receive"):
	return call(UQ + "createReceivingStockEntry", dict(bucket_id=bucket, custom_receiving_batch_id=batch), step)


def shelve(bucket, shelf, farm="Kapkolia", step="shelve"):
	return call(UQ + "createShelvingEntry", dict(shelf_id=shelf, bucket_id=bucket, farm=farm), step)


def qc(bucket, batch, stems, decision, rejected=0, v=AQ, step="qc"):
	b = dict(bucket_id=bucket, stems=stems, selected=True, item_code=v["item_code"], item_name=v["item_code"], warehouse="Kapkolia Receiving Cold Store - KR",
		greenhouse=v["greenhouse"], farm=v["farm"], rejected_stems=rejected, basic_rate=0, cost_center="Main - KR")
	return call(UQ + "submitBatchQuality", dict(batch_no=batch, control_point="Intake", batch_decision=decision, quarantine_scope="batch", farm=v["farm"],
		company="Karen Roses", buckets=[b], checked_stems_sampled=10), step)


def release(bucket, batch, action, stems, step="release"):
	return call(UQ + "releaseFromQuarantine", dict(bucket_id=bucket, action=action, stems_to_release=stems, batch_no=batch), step)


def remove_mine(doctype, name, why):
	"""Cancel + delete one of OUR OWN test docs (created by a mistaken step); logged in the manifest."""
	init(); frappe.set_user("Administrator")
	d = frappe.get_doc(doctype, name)
	if d.docstatus == 1:
		d.cancel()
	frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
	frappe.db.commit()
	m = load()
	m["created"] = [c for c in m["created"] if not (c["doctype"] == doctype and c["name"] == name)]
	m.setdefault("removed_mistakes", []).append({"doctype": doctype, "name": name, "why": why})
	save(m)


def discard_request(bucket, step):
	"""Direct insert of an Approved, submitted Discard Request listing ONLY our bucket, exactly as
	tasks.auto_discard_request builds it (that nightly job itself is NOT run: it scans every shelf)."""
	def go():
		frappe.set_user("Administrator")
		si = frappe.db.sql("select si.bucket_id, si.parent shelf, si.date_added, si.variety, si.stem_qty, si.stem_length, si.greenhouse, sh.farm from `tabShelf Item` si join tabShelf sh on sh.name=si.parent where si.bucket_id=%s", bucket, as_dict=True)
		assert si, "bucket not on a shelf"
		r = si[0]
		row = dict(bucket_id=bucket, farm=r.farm, greenhouse=r.greenhouse, shelf=r.shelf, is_shelved=1, harvest_date=r.date_added, age_days=5.0,
			age_hours=120.0, variety=r.variety, stem_qty=r.stem_qty, stem_length=r.stem_length)
		doc = frappe.get_doc({"doctype": "Discard Request", "age_threshold": 5, "age_unit": "Days", "requested_by": "Administrator",
			"requested_date": frappe.utils.today(), "farm": r.farm, "company": "Karen Roses", "workflow_state": "Draft", "buckets": [row]})
		doc.insert(ignore_permissions=True)
		doc.workflow_state = "Pending Approval"; doc.save(ignore_permissions=True)
		doc.workflow_state = "Approved"; doc.approved_by = "Administrator"; doc.approval_date = frappe.utils.today(); doc.submit()
		print("Discard Request", doc.name)
		return doc.name
	return direct(step, go)


def discard(bucket, farm="Kapkolia", step="discard"):
	return call(UQ + "createDiscardEntry", dict(bucket_id=bucket, from_discard_request=1, farm=farm), step)


def move_shelf(bucket, to_shelf, step="shelf move"):
	return call(UQ + "transferBucket", dict(bucket_id=bucket, to_shelf_id=to_shelf), step)


def make_shelf(name, farm="Kapkolia", step=None):
	def go():
		if not frappe.db.exists("Shelf", name):
			frappe.get_doc({"doctype": "Shelf", "name": name, "shelf_id": name, "farm": farm}).insert(ignore_permissions=True)
	return direct(step or "shelf " + name, go)
