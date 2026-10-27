# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Label batches: create (idempotent), render in resumable chunks, reprint.

A batch is created in ONE transaction -- counter bump, registry rows, scanner
rows and the batch record -- so it either exists whole or not at all, and the
same `request_key` always returns the same batch (a double-click, a retry after
a dropped connection, or two open tabs cannot make a second set of codes).

Rendering is separate and resumable: the codes are already fixed, labels are
drawn `CHUNK_LABELS` at a time into numbered chunk files, and the batch records
how many chunks are finished. A worker that dies is picked up at the next
unfinished chunk with the same codes; nothing is re-reserved.
"""

import base64
import json
import math
import os
import re
import shutil

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from upande_packhouse.api.v2.label_print import codes as lp_codes
from upande_packhouse.api.v2.label_print import render
from upande_packhouse.api.v2.label_print import types as lp_types

CHUNK_LABELS = 250
INLINE_REPRINT_MAX = 200
STALE_SECONDS = 120
JOB_PREFIX = "label_batch::"


# ------------------------------------------------------------- helpers


def normalize_layout(layout):
	layout = layout or {}
	if layout.get("mode") != "sheet":
		return {"mode": "single"}
	sh = layout.get("sheet") or {}
	return {
		"mode": "sheet",
		"sheet": {
			"paper": sh.get("paper") if sh.get("paper") in render.PAPER else "A4",
			"orientation": "landscape" if sh.get("orientation") == "landscape" else "portrait",
			"margin": render._num(sh.get("margin"), 0, 40, 8),
			"gap": render._num(sh.get("gap"), 0, 20, 2),
			"guides": bool(sh.get("guides")),
		},
	}


def ctx_of(code, meta):
	ctx = {"code": code}
	if isinstance(meta, str):
		try:
			meta = json.loads(meta or "{}")
		except ValueError:
			meta = {}
	ctx.update(meta or {})
	return ctx


_SHELF = re.compile(r"^(?:([A-Z0-9]+)-)?([A-Z]+)(\d+)([A-Z])$")
_TROLLEY = re.compile(r"^([A-Z0-9]+)-TROLLEY-(\d+)$")


def meta_from_code(label_type, code):
	"""What a label needs, recovered from the code itself (shelf, trolley)."""
	if label_type in ("shelf", "sf_shelf"):
		m = _SHELF.match(code.upper())
		if m:
			prefix, row, pos, lv = m.groups()
			prefix = prefix or ""
			return {"prefix": prefix, "main": f"{row}{pos}{lv}", "row": row, "pos": pos, "level": lv, "row_id": f"{prefix}-{row}" if prefix else row}
		return {"prefix": "", "main": code, "row": "", "pos": "", "level": ""}
	if label_type == "trolley":
		m = _TROLLEY.match(code.upper())
		if m:
			return {"prefix": m.group(1), "number": m.group(2)}
		return {"prefix": "", "number": code}
	return {}


def sample_ctxs(label_type, spec):
	"""Real-looking labels for the preview, without reserving anything."""
	try:
		if label_type == "bunch":
			rows = [r for r in (spec or {}).get("rows") or [] if cint(r.get("qty")) > 0][:300]
			out = []
			for r in rows:
				base = dict(lp_types.TYPES["bunch"]["sample"])
				base.update({k: r.get(k) or base[k] for k in ("variety", "bunch_size", "stem_length", "farm")})
				out.append(base)
			return out or [dict(lp_types.TYPES["bunch"]["sample"])]
		if label_type == "shelf" or label_type == "sf_shelf":
			return [ctx_of(i["code"], i["meta"]) for i in lp_codes._shelf_items(spec, label_type)][:2000]
		if label_type == "trolley":
			return [ctx_of(i["code"], i["meta"]) for i in lp_codes._trolley_items(spec)][:2000]
		if label_type == "staging":
			return [ctx_of(i["code"], i["meta"]) for i in lp_codes._staging_items(spec)][:2000]
	except frappe.ValidationError:
		frappe.local.message_log = []
	return [dict(lp_types.TYPES[label_type]["sample"])]


def _find_batch(request_key):
	return frappe.db.get_value("Label Batch", {"request_key": request_key}, "name")


def summary(name, duplicate=False):
	d = frappe.db.get_value(
		"Label Batch",
		name,
		["name", "kind", "label_type", "status", "total", "rendered", "first_code", "last_code", "parts", "error", "creation", "owner", "finished", "heartbeat"],
		as_dict=True,
	)
	d["parts"] = json.loads(d["parts"] or "[]")
	d["duplicate"] = duplicate
	return d


def _new_batch_doc(kind, label_type, request_key, template, design, layout, spec, items, geo):
	return frappe.get_doc(
		{
			"doctype": "Label Batch",
			"request_key": request_key,
			"kind": kind,
			"label_type": label_type,
			"status": "Queued",
			"template": template or None,
			"total": len(items),
			"rendered": 0,
			"chunk_size": 0,
			"chunks_done": 0,
			"first_code": items[0]["code"] if items else "",
			"last_code": items[-1]["code"] if items else "",
			"design": json.dumps(design, separators=(",", ":")),
			"layout": json.dumps(dict(layout, _geo=geo), separators=(",", ":")),
			"spec": json.dumps(spec, separators=(",", ":")),
		}
	)


def _chunk_size(design, layout):
	per = render.labels_per_page(design, layout)
	return per * max(1, math.ceil(CHUNK_LABELS / per))


# ------------------------------------------------------------- create


def create_batch(label_type, spec, design, layout, template, request_key):
	frappe.has_permission("Label Batch", "create", throw=True)
	if label_type not in lp_types.TYPES:
		frappe.throw(_("Unknown label type."))
	if not request_key or len(request_key) < 8:
		frappe.throw(_("Missing request key."))
	if (name := _find_batch(request_key)):
		return summary(name, duplicate=True)

	design = render.normalize_design(design, label_type)
	if design["_unknown_fields"]:
		frappe.throw(_("Unknown field(s) in the design: {0}").format(", ".join(design["_unknown_fields"])))
	layout = normalize_layout(layout)

	try:
		items = lp_codes.build_items(label_type, spec or {})
		if lp_types.TYPES[label_type]["kind"] == "fixed":
			# identities, possibly already registered by an earlier batch: render
			# from this batch's own list, not from rows tagged with another batch
			spec = dict(spec or {}, codes=[i["code"] for i in items])
		ctxs = [ctx_of(i["code"], i["meta"]) for i in items]
		geo = render.solve(design, ctxs, label_type)
		doc = _new_batch_doc("New", label_type, request_key, template, design, layout, spec, items, geo)
		doc.chunk_size = _chunk_size(design, layout)
		doc.insert(ignore_permissions=True)
		lp_codes.persist(label_type, items, doc.name, frappe.session.user)
	except frappe.UniqueValidationError:
		# a parallel request with the same key won the race
		frappe.db.rollback()
		if (name := _find_batch(request_key)):
			return summary(name, duplicate=True)
		raise
	_enqueue(doc.name)
	return summary(doc.name)


def _enqueue(name):
	frappe.enqueue(
		"upande_packhouse.api.v2.label_print.batch.run_batch",
		queue="long",
		timeout=3600,
		job_id=JOB_PREFIX + name,
		deduplicate=True,
		enqueue_after_commit=True,
		batch=name,
	)


# ------------------------------------------------------------- rendering job


def _chunk_dir(name):
	return frappe.get_site_path("private", "files", "label_print", name)


def _labels_for(b, start, end):
	"""Contexts for label positions start..end (1-based, inclusive)."""
	listed = json.loads(b.spec or "{}").get("codes")
	if b.kind == "New" and not listed:
		rows = frappe.db.sql(
			"select code, meta from `tabLabel Code` where batch=%s and seq between %s and %s order by seq",
			(b.name, start, end),
		)
		return [ctx_of(c, m) for c, m in rows]
	codes = json.loads(b.spec)["codes"][start - 1 : end]
	by = {r[0].upper(): r for r in frappe.db.sql("select code, meta from `tabLabel Code` where ref in %s", ([f"{b.label_type}:{c}" for c in codes],))}
	return [ctx_of(by[c.upper()][0], by[c.upper()][1]) for c in codes if c.upper() in by]


def _set(name, **vals):
	frappe.db.set_value("Label Batch", name, vals, update_modified=False)


def run_batch(batch):
	"""Background job. Safe to run again at any time: continues at the first
	unfinished chunk, and does nothing for a batch that is Done/Cancelled."""
	b = frappe.get_doc("Label Batch", batch)
	if b.status in ("Done", "Cancelled"):
		return
	_set(batch, status="Rendering", heartbeat=now_datetime(), error="", started=b.started or now_datetime())
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	try:
		design = json.loads(b.design)
		layout = json.loads(b.layout)
		geo = layout.pop("_geo")
		size = cint(b.chunk_size) or _chunk_size(design, layout)
		n_chunks = math.ceil(b.total / size)
		os.makedirs(_chunk_dir(batch), exist_ok=True)
		for idx in range(cint(b.chunks_done), n_chunks):
			start, end = idx * size + 1, min((idx + 1) * size, b.total)
			ctxs = _labels_for(b, start, end)
			if len(ctxs) != end - start + 1:
				frappe.throw(_("Chunk {0} is missing {1} label(s).").format(idx + 1, end - start + 1 - len(ctxs)))
			data = render.render_pdf(design, layout, ctxs, b.label_type, geo)
			path = os.path.join(_chunk_dir(batch), f"c{idx:05d}.pdf")
			with open(path + ".tmp", "wb") as f:
				f.write(data)
			os.replace(path + ".tmp", path)
			_set(batch, chunks_done=idx + 1, rendered=end, heartbeat=now_datetime())
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
		_finalize(b, design, layout)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"Label batch {batch} failed")
		_set(batch, status="Failed", error=frappe.get_traceback(with_context=False)[-400:].strip().splitlines()[-1][:300])
		frappe.db.commit()  # nosemgrep: frappe-manual-commit


def _finalize(b, design, layout):
	from frappe.core.api.file import get_max_file_size
	from pypdf import PdfWriter

	files = sorted(os.listdir(_chunk_dir(b.name)))
	files = [f for f in files if f.endswith(".pdf")]
	limit = int(get_max_file_size() * 0.8)
	for old in frappe.get_all("File", {"attached_to_doctype": "Label Batch", "attached_to_name": b.name}, pluck="name"):
		frappe.delete_doc("File", old, ignore_permissions=True, force=True)

	groups, cur, size = [], [], 0
	for f in files:
		sz = os.path.getsize(os.path.join(_chunk_dir(b.name), f))
		if cur and size + sz > limit:
			groups.append(cur)
			cur, size = [], 0
		cur.append(f)
		size += sz
	groups.append(cur)

	parts, done = [], 0
	for i, grp in enumerate(groups, start=1):
		w = PdfWriter()
		for f in grp:
			w.append(os.path.join(_chunk_dir(b.name), f))
		import io

		buf = io.BytesIO()
		w.write(buf)
		w.close()
		labels = min(len(grp) * cint(b.chunk_size), b.total - done) if b.chunk_size else b.total
		name = f"{b.name}.pdf" if len(groups) == 1 else f"{b.name}_part{i}_of_{len(groups)}.pdf"
		fd = frappe.get_doc(
			{"doctype": "File", "file_name": name, "attached_to_doctype": "Label Batch", "attached_to_name": b.name, "is_private": 1, "content": buf.getvalue()}
		).insert(ignore_permissions=True)
		parts.append({"url": fd.file_url, "name": name, "labels": labels, "bytes": len(buf.getvalue())})
		done += labels

	ts = now_datetime()
	listed = json.loads(b.spec or "{}").get("codes")
	if b.kind == "New" and not listed:
		frappe.db.sql(
			"update `tabLabel Code` set status='Rendered', print_count=print_count+1, last_printed=%s where batch=%s and status='Reserved'",
			(ts, b.name),
		)
	else:
		_mark_printed(b.label_type, listed, ts)
	_set(b.name, status="Done", parts=json.dumps(parts), finished=ts, heartbeat=ts, rendered=b.total, error="")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	shutil.rmtree(_chunk_dir(b.name), ignore_errors=True)


def _mark_printed(label_type, codes, ts):
	for part in lp_codes._chunks(codes):
		frappe.db.sql(
			"update `tabLabel Code` set status=if(status='Voided','Voided','Rendered'), print_count=print_count+1, last_printed=%s where ref in %s",
			(ts, [f"{label_type}:{c}" for c in part]),
		)


# ------------------------------------------------------------- resume / cancel


def is_stale(b):
	hb = get_datetime(b.heartbeat) if b.heartbeat else get_datetime(b.creation)
	return hb < add_to_date(now_datetime(), seconds=-STALE_SECONDS)


def resume(name):
	frappe.has_permission("Label Batch", "write", throw=True)
	b = frappe.get_doc("Label Batch", name)
	if b.status in ("Done", "Cancelled"):
		frappe.throw(_("This batch is already {0}.").format(b.status.lower()))
	if b.status in ("Queued", "Rendering") and not is_stale(b):
		frappe.throw(_("This batch is still running."))
	_set(name, status="Queued", error="", heartbeat=now_datetime())
	_enqueue(name)
	return summary(name)


def cancel(name):
	frappe.has_permission("Label Batch", "write", throw=True)
	b = frappe.get_doc("Label Batch", name)
	if b.status == "Done":
		frappe.throw(_("A finished batch cannot be cancelled. Void its codes instead."))
	if b.status in ("Queued", "Rendering") and not is_stale(b):
		frappe.throw(_("This batch is still running."))
	_set(name, status="Cancelled")
	if b.kind == "New":
		frappe.db.sql("update `tabLabel Code` set status='Voided' where batch=%s and status='Reserved'", name)
	shutil.rmtree(_chunk_dir(name), ignore_errors=True)
	return summary(name)


# ------------------------------------------------------------- reprint


def resolve(label_type, wanted):
	"""Registry rows for `wanted` codes, adopting codes that exist only in the
	older doctypes (printed before this manager existed). Returns
	(found: list of {code, meta, status}, missing: list)."""
	wanted = list(dict.fromkeys(c.strip() for c in wanted if c and c.strip()))
	refs = {c: f"{label_type}:{c}" for c in wanted}
	rows = {}
	for part in lp_codes._chunks(wanted, 2000):
		for r in frappe.db.sql(
			"select code, meta, status, print_count, last_printed from `tabLabel Code` where ref in %s",
			([refs[c] for c in part],),
			as_dict=True,
		):
			rows[r.code.upper()] = r
	missing = [c for c in wanted if c.upper() not in rows]
	legacy = lp_types.TYPES[label_type]["legacy"]
	adopt = []
	if legacy and missing:
		have = lp_codes.existing_names(legacy, missing)
		for c in missing:
			if c in have:
				adopt.append(c)
	if adopt:
		_adopt(label_type, legacy, adopt)
		for part in lp_codes._chunks(adopt, 2000):
			for r in frappe.db.sql(
				"select code, meta, status, print_count, last_printed from `tabLabel Code` where ref in %s",
				([refs[c] for c in part],),
				as_dict=True,
			):
				rows[r.code.upper()] = r
		missing = [c for c in wanted if c.upper() not in rows]
	found = [dict(code=rows[c.upper()].code, meta=json.loads(rows[c.upper()].meta or "{}"), status=rows[c.upper()].status, print_count=rows[c.upper()].print_count, last_printed=rows[c.upper()].last_printed) for c in wanted if c.upper() in rows]
	return found, missing


def _adopt(label_type, legacy, codes):
	"""Create registry rows for codes that were printed by the older flow."""
	ts = now_datetime()
	std = lp_codes._std(frappe.session.user, ts)
	info = {}
	if label_type == "bunch":
		for r in frappe.get_all(legacy, {"name": ["in", codes]}, ["name", "item_code", "bunch_size", "stem_length", "farm", "farm_code"]):
			info[r.name] = {"variety": r.item_code, "bunch_size": r.bunch_size, "stem_length": r.stem_length, "farm": r.farm, "farm_code": r.farm_code, "number": r.name.split("-", 1)[-1]}
	fields = ["name", "ref", "label_type", "code", "status", "batch", "seq", "print_count", "meta", *std]
	values = []
	for c in codes:
		meta = info.get(c) or meta_from_code(label_type, c)
		values.append([f"{label_type}:{c}", f"{label_type}:{c}", label_type, c, "Rendered", None, 0, 0, json.dumps(meta, separators=(",", ":")), *std.values()])
	frappe.db.bulk_insert("Label Code", fields=fields, values=values, ignore_duplicates=True)


def reprint(label_type, wanted, design, layout, template, request_key, reason=""):
	"""Reprint existing codes. Never reserves a number. <= INLINE_REPRINT_MAX
	labels come back immediately as a PDF; larger runs become a batch."""
	frappe.has_permission("Label Batch", "create", throw=True)
	if (name := _find_batch(request_key)):
		s = summary(name, duplicate=True)
		return {"batch": s, "pdf": None}
	design = render.normalize_design(design, label_type)
	layout = normalize_layout(layout)
	found, missing = resolve(label_type, wanted)
	if missing:
		frappe.throw(_("Not found: {0}").format(", ".join(missing[:20]) + (" ..." if len(missing) > 20 else "")))
	if not found:
		frappe.throw(_("Nothing to print."))
	if len(found) > lp_codes.MAX_BATCH:
		frappe.throw(_("Too many labels in one reprint."))
	items = [{"code": f["code"]} for f in found]
	ctxs = [ctx_of(f["code"], f["meta"]) for f in found]
	geo = render.solve(design, ctxs, label_type)
	spec = {"codes": [f["code"] for f in found], "reason": (reason or "")[:140]}
	doc = _new_batch_doc("Reprint", label_type, request_key, template, design, layout, spec, items, geo)
	doc.chunk_size = _chunk_size(design, layout)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		frappe.db.rollback()
		if (name := _find_batch(request_key)):
			return {"batch": summary(name, duplicate=True), "pdf": None}
		raise
	if len(found) <= INLINE_REPRINT_MAX:
		data = render.render_pdf(design, layout, ctxs, label_type, geo)
		ts = now_datetime()
		_mark_printed(label_type, spec["codes"], ts)
		_set(doc.name, status="Done", rendered=len(found), chunks_done=1, started=ts, finished=ts, heartbeat=ts)
		return {"batch": summary(doc.name), "pdf": base64.b64encode(data).decode()}
	_enqueue(doc.name)
	return {"batch": summary(doc.name), "pdf": None}
