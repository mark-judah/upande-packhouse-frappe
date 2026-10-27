# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Code allocation. Everything here runs inside the caller's database
transaction: if anything raises, the counter bump, the registry rows and the
scanner-side rows (Bunch/Bucket/Shelf QR Code) roll back together, so a failed
or retried request never leaves a half-made or duplicated range behind.

Three guards against duplicates, in order:
  1. the counter row is locked (`SELECT ... FOR UPDATE`) while a range is taken,
     so two requests can never be handed the same numbers;
  2. every candidate is checked against what already exists -- the counter on a
     live site can sit far behind the real highest BUNCH-n -- and taken numbers
     are skipped (and burnt, never reused);
  3. the registry's primary key is `type:code`, so even a bug in 1 and 2 ends in
     a rolled-back transaction instead of a duplicate label.
"""

import json
import re
import secrets

import frappe
from frappe import _
from frappe.utils import cint, now

from upande_packhouse.api.v2.label_print import types as lp_types

MAX_BATCH = 100_000
RANDOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: read aloud or typed by hand
IN_CHUNK = 5000


def _chunks(seq, n=IN_CHUNK):
	seq = list(seq)
	for i in range(0, len(seq), n):
		yield seq[i : i + n]


def existing_names(doctype, names):
	"""Which of `names` already exist as `doctype` records."""
	found = set()
	for part in _chunks(names):
		found.update(frappe.db.sql_list(f"select name from `tab{doctype}` where name in %(n)s", {"n": part}))
	return found


def _std(owner, ts):
	return {"owner": owner, "creation": ts, "modified": ts, "modified_by": owner, "docstatus": 0, "idx": 0}


# ------------------------------------------------------------- spec -> items


def _sequence_start(field):
	"""Lock and read the shared counter (the same one the doctype-based flow
	uses, so both flows keep moving forward together)."""
	row = frappe.db.sql(
		"select value from tabSingles where doctype='QR Sequence' and field=%s for update", (field,)
	)
	if not row:
		frappe.db.sql(
			"insert into tabSingles (doctype, field, value) values ('QR Sequence', %s, '0')", (field,)
		)
		return 0
	return cint(row[0][0])


def _sequence_store(field, value):
	frappe.db.sql(
		"update tabSingles set value=%s where doctype='QR Sequence' and field=%s", (str(value), field)
	)
	frappe.clear_document_cache("QR Sequence", "QR Sequence")


def take_bunch_numbers(n):
	"""n unused bunch numbers, skipping any that already exist."""
	cur = _sequence_start("bunch_counter")
	nums = []
	while len(nums) < n:
		want = n - len(nums)
		cand = list(range(cur + 1, cur + want + 1))
		names = [f"BUNCH-{c}" for c in cand]
		used = existing_names("Bunch QR Code", names) | existing_names(
			"Label Code", [f"bunch:{x}" for x in names]
		)
		used = {u.replace("bunch:", "") for u in used}
		nums += [c for c, nm in zip(cand, names, strict=False) if nm not in used]
		cur += want
	_sequence_store("bunch_counter", cur)
	return nums


def _bunch_items(spec):
	rows = [r for r in (spec.get("rows") or []) if cint(r.get("qty")) > 0]
	if not rows:
		frappe.throw(_("Enter a quantity on at least one row."))
	total = sum(cint(r["qty"]) for r in rows)
	if total > MAX_BATCH:
		frappe.throw(
			_("A batch is limited to {0} labels; this one has {1}.").format(f"{MAX_BATCH:,}", f"{total:,}")
		)

	farms = sorted({r.get("farm") for r in rows})
	codes = dict(
		frappe.db.get_all("Farm", filters={"name": ["in", farms]}, fields=["name", "farm_code"], as_list=True)
	)
	missing = [f for f in farms if not codes.get(f)]
	if missing:
		frappe.throw(
			_(
				"These farms have no Farm Code on their Farm record: {0}. Set it there first - nothing was reserved."
			).format(", ".join(frappe.bold(m) for m in missing))
		)
	for dt, key in (("Item", "variety"), ("UOM", "bunch_size"), ("Stem Length", "stem_length")):
		wanted = {r.get(key) for r in rows}
		bad = sorted(w for w in wanted if not w) + sorted(w for w in wanted - existing_names(dt, wanted) if w)
		if bad:
			frappe.throw(_("{0} not found: {1}").format(_(dt), ", ".join(bad)))

	nums = take_bunch_numbers(total)
	items, i = [], 0
	for r in rows:
		meta = {
			"variety": r["variety"],
			"bunch_size": r["bunch_size"],
			"stem_length": r["stem_length"],
			"farm": r["farm"],
			"farm_code": codes[r["farm"]],
		}
		for _n in range(cint(r["qty"])):
			items.append({"code": f"BUNCH-{nums[i]}", "meta": dict(meta, number=str(nums[i])), "new": True})
			i += 1
	return items


def _bucket_items(spec):
	n = cint(spec.get("qty"))
	if n < 1:
		frappe.throw(_("Enter how many bucket labels you need."))
	if n > MAX_BATCH:
		frappe.throw(_("A batch is limited to {0} labels.").format(f"{MAX_BATCH:,}"))
	codes, seen = [], set()
	for _attempt in range(30):
		need = n - len(codes)
		if need <= 0:
			break
		cand = []
		for _i in range(int(need * 1.1) + 10):
			c = "".join(secrets.choice(RANDOM_ALPHABET) for _ in range(6))
			if c not in seen:
				seen.add(c)
				cand.append(c)
		taken = set()
		for dt in ("Bucket QR Code", "Bunch QR Code", "Box Label"):
			taken |= {t.upper() for t in existing_names(dt, cand)}
		taken |= {
			t.split(":", 1)[1].upper() for t in existing_names("Label Code", [f"bucket:{c}" for c in cand])
		}
		codes += [c for c in cand if c not in taken][:need]
	if len(codes) < n:
		frappe.throw(_("Could not find enough free bucket codes. Try again."))
	return [{"code": c, "meta": {}, "new": True} for c in codes]


def _id(v, what, maxlen=8):
	v = (v or "").strip().upper()
	if not v or len(v) > maxlen or not re.fullmatch(r"[A-Z0-9]+", v):
		frappe.throw(_("{0} must be letters and numbers only (got '{1}').").format(what, v))
	return v


def _range(r):
	a, b = cint(r.get("from")), cint(r.get("to"))
	if a < 1 or b < a or b > 1000:
		frappe.throw(_("Positions must run from 1 up, for example 1 to 21 (got {0} to {1}).").format(a, b))
	return a, b


def _shelf_items(spec, label_type):
	levels = [x for x in (spec.get("levels") or ["T", "M", "B"]) if x in ("T", "M", "B")]
	if not levels:
		frappe.throw(_("Pick at least one shelf level."))
	rows = spec.get("rows") or []
	if not rows:
		frappe.throw(_("Add at least one shelf row."))
	items, seen = [], set()
	for r in rows:
		prefix = "SF" if label_type == "sf_shelf" else _id(r.get("prefix"), _("Farm prefix"), 6)
		row = _id(r.get("row"), _("Row letter"), 3)
		a, b = _range(r)
		for pos in range(a, b + 1):
			for lv in levels:
				code = f"{prefix}-{row}{pos}{lv}"
				if code in seen:
					continue
				seen.add(code)
				items.append(
					{
						"code": code,
						"meta": {
							"prefix": prefix,
							"main": f"{row}{pos}{lv}",
							"row": row,
							"pos": str(pos),
							"level": lv,
							"row_id": f"{prefix}-{row}",
						},
						"new": True,
					}
				)
	return items


def _trolley_items(spec):
	items, seen = [], set()
	for r in spec.get("rows") or []:
		prefix = _id(r.get("prefix"), _("Farm prefix"), 6)
		a, b = _range(r)
		for n in range(a, b + 1):
			code = f"{prefix}-TROLLEY-{n}"
			if code not in seen:
				seen.add(code)
				items.append({"code": code, "meta": {"prefix": prefix, "number": str(n)}, "new": True})
	if not items:
		frappe.throw(_("Add at least one trolley range."))
	return items


def _staging_items(spec):
	items, seen = [], set()
	for name in spec.get("names") or []:
		name = re.sub(r"\s+", " ", str(name or "")).strip()
		if not name:
			continue
		if len(name) > 60 or re.search(r"[\x00-\x1f\"\\]", name):
			frappe.throw(_("'{0}' is not a valid location name (max 60 characters, no quotes).").format(name))
		if name.upper() in seen:
			continue
		seen.add(name.upper())
		items.append({"code": name, "meta": {}, "new": True})
	if not items:
		frappe.throw(_("Enter at least one location name."))
	return items


def build_items(label_type, spec):
	"""Allocate the codes a request asks for (not yet written)."""
	if label_type == "bunch":
		return _bunch_items(spec)
	if label_type == "bucket":
		return _bucket_items(spec)
	if label_type in ("shelf", "sf_shelf"):
		items = _shelf_items(spec, label_type)
	elif label_type == "trolley":
		items = _trolley_items(spec)
	elif label_type == "staging":
		items = _staging_items(spec)
	else:
		frappe.throw(_("Unknown label type."))
	if len(items) > MAX_BATCH:
		frappe.throw(_("A batch is limited to {0} labels.").format(f"{MAX_BATCH:,}"))
	return items


# ------------------------------------------------------------- persistence


def persist(label_type, items, batch, owner):
	"""Write registry rows (and the scanner-side rows) for `items`. Existing
	registry rows are kept as they are: fixed codes (shelf, trolley, staging)
	are identities, so asking for one that exists is a reprint, not a clash.
	Returns how many codes were new."""
	ts = now()
	refs = {it["code"]: f"{label_type}:{it['code']}" for it in items}
	have = existing_names("Label Code", refs.values())
	fresh = [it for it in items if refs[it["code"]] not in have]

	std = _std(owner, ts)
	fields = ["name", "ref", "label_type", "code", "status", "batch", "seq", "print_count", "meta", *std]
	values = []
	for seq, it in enumerate(items, start=1):
		it["seq"] = seq
	for it in fresh:
		values.append(
			[
				refs[it["code"]],
				refs[it["code"]],
				label_type,
				it["code"],
				"Reserved",
				batch,
				it["seq"],
				0,
				json.dumps(it["meta"], separators=(",", ":")),
				*std.values(),
			]
		)
	if values:
		frappe.db.bulk_insert("Label Code", fields=fields, values=values, ignore_duplicates=False)

	legacy = lp_types.TYPES[label_type]["legacy"]
	if legacy:
		_persist_legacy(label_type, legacy, items, std)
	return len(fresh)


def _persist_legacy(label_type, legacy, items, std):
	have = existing_names(legacy, [x["code"] for x in items])
	missing = [it for it in items if it["code"] not in have]
	if not missing:
		return
	if label_type == "bunch":
		f = ["name", "id", "item_code", "bunch_size", "stem_length", "farm", "farm_code", *std]
		v = [
			[
				i["code"],
				i["code"],
				i["meta"]["variety"],
				i["meta"]["bunch_size"],
				i["meta"]["stem_length"],
				i["meta"]["farm"],
				i["meta"]["farm_code"],
				*std.values(),
			]
			for i in missing
		]
	elif label_type == "bucket":
		f = ["name", "id", "status", *std]
		v = [[i["code"], i["code"], "Available", *std.values()] for i in missing]
	else:  # shelf / sf_shelf
		f = ["name", "shelf_id", "row_id", "position", *std]
		v = [
			[i["code"], i["code"], i["meta"]["row_id"], cint(i["meta"]["pos"]), *std.values()]
			for i in missing
		]
	frappe.db.bulk_insert(legacy, fields=f, values=v, ignore_duplicates=False)
