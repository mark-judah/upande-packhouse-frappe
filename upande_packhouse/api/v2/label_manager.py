# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Label Print Manager v2 - the API behind www/label-print-manager-v2.html.

Prints QR labels for bunches, buckets, shelves, sea-freight shelves, trolleys
and staging locations. Independent of the doctype-based Label Print flow
(server_scripts/gen_label_id.py): it keeps its own registry (Label Code) and
batches (Label Batch), and writes the Bunch/Bucket/Shelf QR Code rows the
scanner apps look up, in the same transaction as the codes.

Writes are POST-only (a GET is rolled back by Frappe, and a write endpoint
must never be reachable by a link someone clicks).
"""

import json
import re

import frappe
from frappe import _
from frappe.utils import cint

from upande_packhouse.api.v2.label_print import batch as lp_batch
from upande_packhouse.api.v2.label_print import codes as lp_codes
from upande_packhouse.api.v2.label_print import render
from upande_packhouse.api.v2.label_print import types as lp_types

ROLES_HINT = ("System Manager", "Assistant Packhouse Manager", "Packing Supervisor")


def _check(ptype="read"):
	frappe.has_permission("Label Batch", ptype, throw=True)


def _loads(v, default):
	if v in (None, ""):
		return default
	return frappe.parse_json(v) if isinstance(v, str) else v


def _ensure_templates():
	"""One default template per type, created the first time anyone asks."""
	have = set(frappe.get_all("Label Template", pluck="label_type"))
	for t, meta in lp_types.TYPES.items():
		if t in have:
			continue
		frappe.get_doc(
			{
				"doctype": "Label Template",
				"template_name": f"Standard {meta['label'].lower()}",
				"label_type": t,
				"is_default": 1,
				"design": json.dumps(lp_types.DEFAULT_DESIGNS[t]),
			}
		).insert(ignore_permissions=True)


def _template_rows():
	rows = frappe.get_all(
		"Label Template",
		fields=["name", "template_name", "label_type", "is_default", "description", "design", "modified"],
		order_by="is_default desc, template_name asc",
	)
	for r in rows:
		r["design"] = render.normalize_design(json.loads(r["design"] or "{}"), r["label_type"])
		r["design"].pop("_unknown_fields", None)
	return rows


# ------------------------------------------------------------- bootstrap


@frappe.whitelist()
def bootstrap():
	_check()
	_ensure_templates()
	types = {
		k: {"label": v["label"], "kind": v["kind"], "fields": v["fields"], "sample": v["sample"], "default": lp_types.DEFAULT_DESIGNS[k]}
		for k, v in lp_types.TYPES.items()
	}
	farms = frappe.get_all("Farm", fields=["name", "farm_code"], order_by="name asc")
	return {
		"types": types,
		"templates": _template_rows(),
		"farms": farms,
		"presets": {k: {"w": v[0], "h": v[1]} for k, v in lp_types.PAGE_PRESETS.items()},
		"papers": {k: {"w": v[0], "h": v[1]} for k, v in render.PAPER.items()},
		"max_batch": lp_codes.MAX_BATCH,
		"inline_reprint_max": lp_batch.INLINE_REPRINT_MAX,
		"can_write": bool(frappe.has_permission("Label Batch", "create")),
	}


# ------------------------------------------------------------- bunch table


@frappe.whitelist()
def bunch_table(farm: str | None = None):
	"""The rows of the bunch label table for one farm: every spray-rose variety
	planted in its greenhouses, once per stem length, with the bunch size from
	the item and last week's daily average as a guide to how many to print."""
	_check()
	if not farm:
		frappe.throw(_("Choose a farm."))
	farm_code = frappe.db.get_value("Farm", farm, "farm_code")
	greenhouses = frappe.get_all("Greenhouse", {"farm": farm}, pluck="name")
	varieties = []
	if greenhouses:
		varieties = frappe.db.sql_list(
			"select distinct variety from `tabGreenhouse Variety` where parenttype='Greenhouse' and parent in %s and ifnull(variety,'')!=''",
			(greenhouses,),
		)
	items = []
	if varieties:
		filters = {"name": ["in", varieties]}
		root = frappe.db.get_value("Item Group", "Spray Roses", ["lft", "rgt"])
		if root:
			groups = frappe.db.sql_list("select name from `tabItem Group` where lft>=%s and rgt<=%s", root)
			filters["item_group"] = ["in", groups]
		items = frappe.get_all("Item", filters, ["name", "sales_uom"], order_by="name asc")
	lengths = frappe.get_all("Stem Length", pluck="length", order_by="length asc")
	avg = _prev_week(farm)
	rows = [
		{"variety": it.name, "bunch_size": it.sales_uom or "", "stem_length": sl, "prev_week_avg": avg.get(f"{it.name}|{sl}", 0), "qty": 0}
		for it in items
		for sl in lengths
	]
	return {
		"farm": farm,
		"farm_code": farm_code,
		"rows": rows,
		"varieties": len(items),
		"lengths": len(lengths),
		"note": None if items else _("No spray-rose varieties are planted in {0}.").format(farm),
	}


def _prev_week(farm):
	"""Daily average stems per (variety|stem length) over the last full Mon-Sun week."""
	data = frappe.db.sql(
		"""
		select sed.item_code v, se.custom_stem_length sl, coalesce(sum(sed.qty),0) q
		from `tabStock Entry` se join `tabStock Entry Detail` sed on sed.parent = se.name
		where se.docstatus = 1 and se.stock_entry_type = 'Harvesting' and se.custom_farm = %s
		  and se.posting_date between date_sub(date_sub(curdate(), interval weekday(curdate()) day), interval 7 day)
		  and date_sub(date_sub(curdate(), interval weekday(curdate()) day), interval 1 day)
		group by sed.item_code, se.custom_stem_length
		""",
		(farm,),
	)
	return {f"{v}|{sl}": int(round(float(q) / 7.0)) for v, sl, q in data}


# ------------------------------------------------------------- preview


@frappe.whitelist()
def preview(label_type: str, design=None, layout=None, spec=None):
	"""The label exactly as it will print (same renderer), for the designer."""
	_check()
	if label_type not in lp_types.TYPES:
		frappe.throw(_("Unknown label type."))
	design = render.normalize_design(_loads(design, None), label_type)
	layout = lp_batch.normalize_layout(_loads(layout, {}))
	ctxs = lp_batch.sample_ctxs(label_type, _loads(spec, {}))
	img, geo = render.preview_png(design, layout, ctxs, label_type)
	per = render.labels_per_page(design, layout)
	return {
		"image": img,
		"warnings": geo["warnings"],
		"unknown_fields": design["_unknown_fields"],
		"module_mm": round(geo["module_mm"], 3),
		"modules": geo["modules"],
		"font_pt": [round(s, 1) for s in geo["sizes"]],
		"qr_mm": round(geo["q"] / render.MM, 1),
		"per_page": per,
		"sample_count": len(ctxs),
		"label_mm": [design["page"]["w"], design["page"]["h"]],
	}


# ------------------------------------------------------------- batches


@frappe.whitelist(methods=["POST"])
def create(label_type: str, spec=None, design=None, layout=None, template: str | None = None, request_key: str | None = None):
	return lp_batch.create_batch(label_type, _loads(spec, {}), _loads(design, None), _loads(layout, {}), template, request_key)


@frappe.whitelist()
def batches(label_type: str | None = None, kind: str | None = None, limit: int = 60):
	_check()
	filters = {}
	if label_type:
		filters["label_type"] = label_type
	if kind:
		filters["kind"] = kind
	names = frappe.get_all("Label Batch", filters, pluck="name", order_by="creation desc", limit=min(cint(limit) or 60, 200))
	out = []
	for n in names:
		s = lp_batch.summary(n)
		s["spec"] = frappe.db.get_value("Label Batch", n, "spec")
		b = frappe._dict(s)
		s["stale"] = s["status"] in ("Queued", "Rendering") and lp_batch.is_stale(b)
		try:
			sp = json.loads(s.pop("spec") or "{}")
		except ValueError:
			sp = {}
		s["reason"] = sp.get("reason", "")
		out.append(s)
	return out


@frappe.whitelist()
def batch(name: str):
	_check()
	s = lp_batch.summary(name)
	s["stale"] = s["status"] in ("Queued", "Rendering") and lp_batch.is_stale(frappe._dict(s))
	return s


@frappe.whitelist(methods=["POST"])
def resume(name: str):
	return lp_batch.resume(name)


@frappe.whitelist(methods=["POST"])
def cancel(name: str):
	return lp_batch.cancel(name)


# ------------------------------------------------------------- find / reprint


_JSONISH = re.compile(r"^\s*\{.*\}\s*$", re.S)


def _tokens(label_type, text):
	"""Codes typed, pasted or scanned: separated by spaces, commas or new lines;
	a scanned QR payload is unwrapped; `A..B` is a range (bunch numbers too)."""
	text = text or ""
	out = []
	for m in re.finditer(r"\{[^{}]*\}", text):
		try:
			obj = json.loads(m.group(0))
			tok = obj.get("bunch_id") or obj.get("shelf") or obj.get("location") or next(iter(obj))
			out.append(str(tok))
		except (ValueError, AttributeError, StopIteration):
			continue
	text = re.sub(r"\{[^{}]*\}", " ", text)
	for tok in re.split(r"[\s,;]+", text):
		tok = tok.strip()
		if not tok:
			continue
		if ".." in tok:
			a, b = [x.strip() for x in tok.split("..", 1)]
			m1, m2 = re.search(r"(\d+)$", a), re.search(r"(\d+)$", b)
			if m1 and m2:
				pre = a[: m1.start()] or ("BUNCH-" if label_type == "bunch" else "")
				lo, hi = int(m1.group(1)), int(m2.group(1))
				if hi - lo > 5000:
					frappe.throw(_("A range is limited to 5,000 labels."))
				out += [f"{pre}{n}" for n in range(lo, hi + 1)]
				continue
		out.append(tok)
	return list(dict.fromkeys(out))


@frappe.whitelist()
def lookup(label_type: str, text: str | None = None):
	"""What exists for the codes in `text` (read-only)."""
	_check()
	if label_type not in lp_types.TYPES:
		frappe.throw(_("Unknown label type."))
	toks = _tokens(label_type, text)[:5000]
	if not toks:
		return {"items": [], "missing": [], "suggestions": []}
	legacy = lp_types.TYPES[label_type]["legacy"]
	reg = {}
	for part in lp_codes._chunks(toks, 2000):
		for r in frappe.db.sql(
			"select code, status, print_count, last_printed, batch, meta from `tabLabel Code` where ref in %s",
			([f"{label_type}:{c}" for c in part],),
			as_dict=True,
		):
			reg[r.code.upper()] = r
	old = lp_codes.existing_names(legacy, toks) if legacy else set()
	old_u = {o.upper() for o in old}
	items, missing = [], []
	for c in toks:
		r = reg.get(c.upper())
		if r:
			items.append({"code": r.code, "status": r.status, "print_count": r.print_count, "last_printed": r.last_printed, "batch": r.batch, "source": "registry", "meta": json.loads(r.meta or "{}")})
		elif c.upper() in old_u:
			items.append({"code": c, "status": "Printed earlier", "print_count": None, "last_printed": None, "batch": None, "source": "legacy", "meta": {}})
		else:
			missing.append(c)
	suggestions = []
	if len(toks) == 1 and not items and len(toks[0]) >= 2:
		like = toks[0].replace("%", "").replace("_", r"\_") + "%"
		suggestions = frappe.db.sql_list(
			"select code from `tabLabel Code` where label_type=%s and code like %s order by code limit 12", (label_type, like)
		)
		if legacy and len(suggestions) < 12:
			suggestions += [s for s in frappe.db.sql_list(f"select name from `tab{legacy}` where name like %s order by name limit 12", (like,)) if s not in suggestions]  # noqa: S608
	return {"items": items[:500], "total": len(items), "missing": missing[:100], "missing_count": len(missing), "suggestions": suggestions[:12]}


@frappe.whitelist(methods=["POST"])
def reprint(label_type: str, codes=None, text: str | None = None, design=None, layout=None, template: str | None = None, request_key: str | None = None, reason: str | None = None):
	wanted = _loads(codes, None) or _tokens(label_type, text)
	return lp_batch.reprint(label_type, wanted, _loads(design, None), _loads(layout, {}), template, request_key, reason or "")


# ------------------------------------------------------------- templates


@frappe.whitelist(methods=["POST"])
def save_template(label_type: str, template_name: str, design=None, is_default=0, description: str | None = None):
	frappe.has_permission("Label Template", "write", throw=True)
	if label_type not in lp_types.TYPES:
		frappe.throw(_("Unknown label type."))
	template_name = (template_name or "").strip()
	if not template_name:
		frappe.throw(_("Give the template a name."))
	d = render.normalize_design(_loads(design, None), label_type)
	d.pop("_unknown_fields", None)
	if frappe.db.exists("Label Template", template_name):
		doc = frappe.get_doc("Label Template", template_name)
		if doc.label_type != label_type:
			frappe.throw(_("A template with that name exists for another label type."))
	else:
		doc = frappe.new_doc("Label Template")
		doc.template_name = template_name
		doc.label_type = label_type
	doc.design = json.dumps(d)
	doc.description = description or doc.description
	if cint(is_default):
		frappe.db.sql("update `tabLabel Template` set is_default=0 where label_type=%s", label_type)
		doc.is_default = 1
	doc.save(ignore_permissions=True)
	return _template_rows()


@frappe.whitelist(methods=["POST"])
def delete_template(name: str):
	frappe.has_permission("Label Template", "delete", throw=True)
	doc = frappe.get_doc("Label Template", name)
	if doc.is_default:
		frappe.throw(_("Make another template the default before deleting this one."))
	frappe.delete_doc("Label Template", name, ignore_permissions=True)
	return _template_rows()
