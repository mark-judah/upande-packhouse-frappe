# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Rosenta -> local snapshots. TEMPORARY, with the rest of the Rosenta comparison.

The dashboards never call Rosenta. This module is the only thing that does, so Rosenta sees one reader,
one request at a time with a pause between them, however many people have the dashboard open.

One snapshot (Rosenta Snapshot) is one report for one delivery date. A report is read again only when it
can still change: today's every few minutes, yesterday's and the last week's invoices (payments arrive
late), everything older once.

  orders      Order Summary, per location          price, stems and status of every order line
  billing     Customer Billing                     invoices issued for the delivery date, paid or not
  sales       Sales Analysis                       invoiced lines with variety, price, country
  fill        Dispatch Analysis                    what was issued and dispatched, line by line
  greenhouse  Greenhouse Revenues, per location    revenue of the stems issued, by farm and greenhouse
  specs       Specifications (not per day)         every active specification: pack rate, bunches, varieties,
                                                   consumables. Pulled nightly, kept as the latest only.

Only read endpoints are called (see the inventory of the crawl). Credentials: Rosenta Connection.
"""

import hashlib
import json
import time

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, getdate, now_datetime, today

from upande_packhouse.api.v2 import rosenta_compare as rc

THROTTLE = 1.0  # seconds between two calls to Rosenta
LOCK_KEY = "rosenta_sync_running"
LOCK_TTL = 15 * 60

ORDERS_PATH = "/api/insights/get_allocations_summary_by_order_spec"
BILLING_PATH = "/api/financials/get_billing_by_delivery"
SALES_PATH = "/api/insights/get_sales_analysis_table"
FILL_PATH = "/api/insights/get_total_exports_table"
GREENHOUSE_PATH = "/api/insights/get_greenhouse_revenues_by_delivery"
SPECS_PATH = "/api/financials/get_specs"  # every specification, one call (about 11 seconds, 22,000 rows)

KEEP = {
	"orders": (
		"order_id", "spec_id", "account_name", "order_name", "order_status", "spec_name", "boxes_confirmed",
		"boxes_allocated", "boxes_staged", "boxes_loaded", "pack_rate", "stem_price", "currency",
		"location_name", "status", "order_delivery_date",
	),
	"billing": (
		"invoice_name", "invoice_id", "account_name", "account_no", "invoice_status", "invoice_paid",
		"invoice_currency", "invoice_amount", "paid_amount", "paid_date", "invoice_issued", "invoice_type",
		"order_ids", "total_boxes", "delivery_date", "invoice_due", "service_fees",
	),
	"sales": (
		"delivery_date", "invoice_date", "customer", "invoice_name", "line", "variety", "crop", "grade",
		"pack_rate", "unit_price", "boxes_loaded", "boxes_invoiced", "currency", "payment_status",
		"order_id", "location_name", "rejects", "shipping_country",
	),
	"fill": None,  # all of it, it is small
	"greenhouse": (
		"account_name", "order_id", "product_name", "stem_length", "currency", "stem_price", "stems_issued",
		"farm_name", "greenhouse_name", "revenue", "delivery_date",
	),
}
#: Every dataset, in the order they are pulled; `fast` ones are cheap enough to refresh every few minutes.
DATASETS = ("orders", "billing", "sales", "fill", "greenhouse")


def _dmy(d):
	return f"{d.day}/{d.month}/{d.year}"


def _slim(dataset, rows):
	keep = KEEP[dataset]
	if keep is None:
		return list(rows)
	return [{k: r.get(k) for k in keep} for r in rows]


def _flatten_orders(out):
	"""{order: {spec: line}} -> [line with its order id and spec id]."""
	rows = []
	for order_id, specs in (out or {}).items():
		for spec_id, line in (specs or {}).items():
			rows.append({"order_id": order_id, "spec_id": spec_id, **line})
	return rows


def fetch(client, dataset, day):
	"""One dataset for one delivery date, as a list of slim rows. Calls Rosenta, one request at a time."""
	dmy, iso = _dmy(day), str(day)
	rows = []
	if dataset in ("orders", "greenhouse"):
		for loc in rc.LOCATIONS.values():
			time.sleep(THROTTLE)
			if dataset == "orders":
				out = client.call(ORDERS_PATH, {"delivery_date": dmy, "location_id": loc})
				rows += _flatten_orders(out) if isinstance(out, dict) else []
			else:
				out = client.call(GREENHOUSE_PATH, {"delivery_date": dmy, "location_id": loc})
				rows += out if isinstance(out, list) else []
	elif dataset == "billing":
		time.sleep(THROTTLE)
		out = client.call(BILLING_PATH, {"delivery_date": dmy})
		rows = out if isinstance(out, list) else []
	elif dataset == "sales":
		time.sleep(THROTTLE)
		out = client.call(SALES_PATH, {"dates[]": [dmy]})
		rows = out if isinstance(out, list) else []
	elif dataset == "fill":
		time.sleep(THROTTLE)
		# Rosenta lists a line under the day it was dispatched, which is usually the day before the delivery.
		out = client.call(
			FILL_PATH, {"duration": "days", "start": str(add_days(day, -3)), "end": str(add_days(day, 1))}
		)
		rows = [r for r in out if r.get("delivery") == dmy] if isinstance(out, list) else []
	return _slim(dataset, rows)


def save(dataset, day, rows):
	"""Store one snapshot. A report that came back unchanged only gets a new fetched_at."""
	payload = json.dumps(rows, sort_keys=True, default=str)
	digest = hashlib.md5(payload.encode()).hexdigest()  # noqa: S324 (a change check, not security)
	name = f"{dataset}-{day}"
	now = now_datetime()
	if frappe.db.exists("Rosenta Snapshot", name):
		if frappe.db.get_value("Rosenta Snapshot", name, "data_hash") == digest:
			frappe.db.set_value("Rosenta Snapshot", name, "fetched_at", now, update_modified=False)
			return False
		doc = frappe.get_doc("Rosenta Snapshot", name)
		doc.update({"payload": payload, "rows": len(rows), "fetched_at": now, "data_hash": digest})
		doc.save(ignore_permissions=True)
		return True
	frappe.get_doc(
		{
			"doctype": "Rosenta Snapshot", "dataset": dataset, "day": day, "rows": len(rows),
			"fetched_at": now, "data_hash": digest, "payload": payload,
		}
	).insert(ignore_permissions=True)
	return True


def _json(x):
	try:
		return json.loads(x) if isinstance(x, str) else (x or {})
	except ValueError:
		return {}


def slim_spec(r):
	"""One Rosenta specification as the few fields the comparison needs."""
	box, cons = _json(r.get("box")), _json(r.get("consumables"))
	bunches = []
	for bn in (box.get("bunches") or {}).values():
		vs = [
			[v.get("product_name"), v.get("stem_length"), v.get("stem_count"), v.get("cut_stage")]
			for v in (bn.get("varieties") or {}).values()
		]
		bunches.append([bn.get("bunch_count"), vs])
	items = [
		[i.get("item_name"), i.get("item_type"), i.get("item_quantity")] for i in (cons.get("items") or {}).values()
	]
	return {
		"i": r.get("spec_id"), "n": r.get("spec_name"), "a": r.get("account_name"), "ft": r.get("spec_fairtrade"),
		"s": r.get("spec_sku"), "pr": box.get("pack_rate"), "g": box.get("grade"), "b": bunches, "c": items,
	}  # fmt: skip


def sync_specs(client=None):
	"""Pull every active specification and keep it as the latest snapshot (older ones are removed)."""
	if client is None:
		cfg = rc._settings()
		client = rc.RosentaOps(cfg["email"], cfg["password"])
	time.sleep(THROTTLE)
	out = client.call(SPECS_PATH, {})
	if not isinstance(out, list):
		frappe.throw(_("Rosenta did not return a list of specifications."))
	rows = [slim_spec(r) for r in out if r.get("status") == "active"]
	day = getdate(today())
	changed = save("specs", day, rows)
	for old in frappe.get_all("Rosenta Snapshot", filters={"dataset": "specs", "day": ["<", day]}, pluck="name"):
		frappe.delete_doc("Rosenta Snapshot", old, ignore_permissions=True, force=True)
	frappe.db.commit()
	return changed


def load_specs():
	"""(rows, fetched_at) of the latest specifications snapshot, or ([], None)."""
	r = frappe.get_all(
		"Rosenta Snapshot",
		filters={"dataset": "specs"},
		fields=["payload", "fetched_at"],
		order_by="day desc",
		limit=1,
	)
	return (json.loads(r[0].payload or "[]"), r[0].fetched_at) if r else ([], None)


def ask_for_specs():
	frappe.enqueue(
		"upande_packhouse.api.v2.rosenta_sync.sync_specs",
		queue="long",
		timeout=600,
		job_id="rosenta-sync-specs",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def sync_day(day, datasets=DATASETS, client=None):
	"""Pull the given datasets for one delivery date. A failing dataset is logged and skipped."""
	day = getdate(day)
	if client is None:
		cfg = rc._settings()
		client = rc.RosentaOps(cfg["email"], cfg["password"])
	done = {}
	for ds in datasets:
		try:
			done[ds] = save(ds, day, fetch(client, ds, day))
		except Exception:
			frappe.clear_messages()
			frappe.log_error(title=f"Rosenta sync {ds} {day}", message=frappe.get_traceback())
			done[ds] = None
	frappe.db.commit()  # noqa: E501 (keep what was pulled even if a later day fails)
	return done


def _guard():
	"""One run at a time: the lock is a cache key that ends by itself if a run dies."""
	if frappe.cache.get_value(LOCK_KEY):
		return False
	frappe.cache.set_value(LOCK_KEY, 1, expires_in_sec=LOCK_TTL)
	return True


def _configured():
	cfg = rc._settings()
	return bool(cfg["email"] and cfg["password"])


def sync_recent():
	"""Every few minutes: today in full, yesterday's invoices and fill, tomorrow's orders."""
	if not _configured() or not _guard():
		return
	try:
		cfg = rc._settings()
		client = rc.RosentaOps(cfg["email"], cfg["password"])
		t = getdate(today())
		sync_day(t, DATASETS, client)
		sync_day(add_days(t, -1), ("billing", "sales", "fill"), client)
		sync_day(add_days(t, 1), ("orders",), client)
	finally:
		frappe.cache.delete_value(LOCK_KEY)


def sync_nightly(days=14, max_new=6):
	"""Once a night: the invoices of the last week again (payments), and up to `max_new` older days that
	have no snapshot yet, newest first, so a first run catches up a few days at a time."""
	if not _configured() or not _guard():
		return
	try:
		cfg = rc._settings()
		client = rc.RosentaOps(cfg["email"], cfg["password"])
		t = getdate(today())
		for back in range(1, 8):
			sync_day(add_days(t, -back), ("billing",), client)
		try:
			sync_specs(client)
		except Exception:
			frappe.clear_messages()
			frappe.log_error(title="Rosenta sync specs", message=frappe.get_traceback())
		fresh = 0
		for back in range(2, days + 1):
			d = add_days(t, -back)
			if all(frappe.db.exists("Rosenta Snapshot", f"{ds}-{d}") for ds in DATASETS):
				continue
			sync_day(d, DATASETS, client)
			fresh += 1
			if fresh >= max_new:
				break
	finally:
		frappe.cache.delete_value(LOCK_KEY)


# ---------------------------------------------------------------- reading


def load(dataset, days):
	"""{iso day: rows} of the snapshots that exist, and {iso day: fetched_at} for them."""
	names = [f"{dataset}-{d}" for d in days]
	out, when = {}, {}
	for r in frappe.get_all(
		"Rosenta Snapshot",
		filters={"name": ["in", names]},
		fields=["day", "payload", "fetched_at"],
	):
		out[str(r.day)] = json.loads(r.payload or "[]")
		when[str(r.day)] = r.fetched_at
	return out, when


def missing(datasets, days):
	"""[(dataset, day)] with no snapshot yet."""
	have = {
		r.name
		for r in frappe.get_all(
			"Rosenta Snapshot",
			filters={"name": ["in", [f"{ds}-{d}" for ds in datasets for d in days]]},
			fields=["name"],
		)
	}
	return [(ds, d) for ds in datasets for d in days if f"{ds}-{d}" not in have]


def ask_for(datasets, days):
	"""Queue a pull of what is missing, once, so the next look has it. Never pulls inside the request."""
	for d in sorted({d for _ds, d in missing(datasets, days)}):
		frappe.enqueue(
			"upande_packhouse.api.v2.rosenta_sync.sync_day",
			queue="short",
			job_id=f"rosenta-sync-{d}",
			deduplicate=True,
			day=d,
			enqueue_after_commit=True,
		)


def freshness(when):
	"""The oldest pull among the given {day: fetched_at}, as text, for 'data as of'."""
	vals = [get_datetime(v) for v in when.values() if v]
	return str(min(vals)) if vals else None


def days_between(date_from, date_to, limit=45):
	a, b = getdate(date_from), getdate(date_to)
	if b < a:
		a, b = b, a
	n = min((b - a).days, limit - 1)
	return [str(add_days(a, i)) for i in range(n + 1)]
