"""Everything that would stop an imported Sales Order from saving, found up front.

The import wizard builds each order from a file, and a save then trips over one rule after another
(a missing order name, a consignee that does not exist, no Item Price for a variety and length ...). `collect_issues`
looks at the order the same way the save will and returns ALL of what is missing or wrong in one list, each with
what the person has to fill in, so the wizard can ask once instead of failing repeatedly.

It only reads. The rules mirror upande_packhouse.sales_order_engine (the Roses gates) and the mandatory fields of
Sales Order, and the wizard keeps a final safety net: whatever the real save still refuses is shown as it is.

An issue is a dict: {"id", "kind", "message", ...}. kinds and what they carry:

  field            header field with no value: fieldname, label, fieldtype, options
  missing_link     a value that is not a record: scope (header|row), doctype, value, fieldname, label, rows, can_create
  customer_price_list  the customer has no Default Price List: customer
  line_field       a line missing details: row, line, item_code, fields [{fieldname, label, fieldtype, options}]
  price            no Item Price for a variety and stem length: item_code, length, price_list, currency, rows
  sales_uom        an item with no Sales UOM: item_code
  disabled_item    a variety that is switched off: item_code
  info             a problem the person cannot fix in a form (packrate, bunch size, colours, box counts): message
"""

import re
from collections import defaultdict

import frappe

from upande_packhouse import sales_order_engine as engine

# Sales Order fields the importer or ERPNext fills in itself, so the person is never asked for them.
AUTOFILLED_HEADER = {
	"company",
	"naming_series",
	"order_type",
	"transaction_date",
	"currency",
	"conversion_rate",
	"selling_price_list",
	"price_list_currency",
	"plc_conversion_rate",
	"status",
	"items",
	"customer",
	"business_unit",
}
NOT_ASKABLE_TYPES = {
	"Table",
	"Table MultiSelect",
	"Section Break",
	"Column Break",
	"Tab Break",
	"HTML",
	"Button",
	"Heading",
	"Image",
	"Check",
	"Select",
	"Int",
	"Float",
	"Currency",
	"Percent",
}
# Doctypes the wizard can create a record for on the spot (kept in step with order_import.QUICK_ADD_DOCTYPES).
CREATABLE = {"Consignee", "Stem Length", "Delivery Point", "Shipping Agent", "Box Type"}

LINE_FIELDS = [
	("custom_length", "Stem length", "Link", "Stem Length"),
	("custom_box_type", "Box type", "Link", "Box Type"),
	("custom_number_of_boxes", "Number of boxes", "Int", ""),
]


def _eval_depends(expr, doc):
	if not expr:
		return False
	try:
		if expr.startswith("eval:"):
			from frappe.utils.safe_eval import safe_eval

			return bool(safe_eval(expr[5:], None, {"doc": doc}))
		return bool(doc.get(expr))
	except Exception:
		return False


def _exists(doctype, value, cache):
	key = (doctype, value)
	if key not in cache:
		cache[key] = bool(value) and bool(frappe.db.exists(doctype, value))
	return cache[key]


def _ref(refs, index):
	"""The wizard's own row index for the nth line of the doc (the doc lists spec rows first, then manual ones)."""
	return refs[index] if refs and index < len(refs) else index


def collect_issues(doc, row_refs=None):
	"""All problems with this (unsaved) Sales Order doc, as a list of issue dicts. See the module docstring."""
	issues = []
	exists_cache = {}
	meta = frappe.get_meta("Sales Order")

	def add(kind, message, **extra):
		issues.append({"id": f"{kind}-{len(issues)}", "kind": kind, "message": message, **extra})

	# 1. header fields that must be filled and are not
	for df in meta.fields:
		if df.fieldname in AUTOFILLED_HEADER or df.fieldtype in NOT_ASKABLE_TYPES:
			continue
		if df.hidden or df.read_only:
			continue
		if not (df.reqd or _eval_depends(df.mandatory_depends_on, doc)):
			continue
		if doc.get(df.fieldname):
			continue
		add(
			"field",
			f"{df.label} is required.",
			fieldname=df.fieldname,
			label=df.label,
			fieldtype=df.fieldtype,
			options=df.options or "",
		)

	# 1b. Set Warehouse: Roses lines need a warehouse, which comes from the Sales Settings default or from this field.
	# Only asked for when neither supplied one, so a site with the default never sees it.
	if not doc.get("set_warehouse") and any(it.item_code and not it.get("warehouse") for it in doc.items):
		add(
			"field",
			"Set Warehouse is required.",
			fieldname="set_warehouse",
			label="Set Warehouse",
			fieldtype="Link",
			options="Warehouse",
		)

	# 2. header values that are not records (a consignee the file names that does not exist ...)
	for df in meta.fields:
		if df.fieldtype != "Link" or df.fieldname in {"customer", "company"} or not df.options:
			continue
		value = doc.get(df.fieldname)
		if value and not _exists(df.options, value, exists_cache):
			add(
				"missing_link",
				f"{df.label} “{value}” does not exist.",
				scope="header",
				doctype=df.options,
				value=value,
				fieldname=df.fieldname,
				label=df.label,
				rows=[],
				can_create=df.options in CREATABLE,
			)

	# 2b. line values that are not records
	by_value = defaultdict(list)
	for i, it in enumerate(doc.items):
		for fieldname, label, doctype in (
			("item_code", "Item", "Item"),
			("uom", "UOM", "UOM"),
			("custom_length", "Stem length", "Stem Length"),
			("custom_box_type", "Box type", "Box Type"),
		):
			value = it.get(fieldname)
			if value and not _exists(doctype, value, exists_cache):
				by_value[(fieldname, label, doctype, value)].append(_ref(row_refs, i))
	for (fieldname, label, doctype, value), rows in by_value.items():
		add(
			"missing_link",
			f"{label} “{value}” does not exist.",
			scope="row",
			doctype=doctype,
			value=value,
			fieldname=fieldname,
			label=label,
			rows=rows,
			can_create=doctype in CREATABLE,
		)

	# 3. no price list for the customer (and none chosen on the order)
	order_list = doc.get("selling_price_list")
	customer_list = engine._customer_default_pl(doc)
	if not customer_list and (not order_list or order_list == "Standard Selling"):
		add(
			"customer_price_list",
			f"{doc.customer} has no Default Price List, so lines cannot be priced.",
			customer=doc.customer,
		)

	# 4. lines missing stem length, box type or number of boxes
	for i, it in enumerate(doc.items):
		if not it.item_code:
			continue
		missing = [
			{"fieldname": f, "label": label, "fieldtype": ft, "options": opts}
			for f, label, ft, opts in LINE_FIELDS
			if not it.get(f)
		]
		if missing:
			add(
				"line_field",
				f"Line {i + 1} ({it.item_code}) is missing "
				+ ", ".join(m["label"].lower() for m in missing)
				+ ".",
				row=_ref(row_refs, i),
				line=i + 1,
				item_code=it.item_code,
				fields=missing,
			)

	# 5. lines that have no price for their variety and stem length in the order's price list
	price_list = engine._resolve_price_list(doc)
	if price_list:
		currency = frappe.db.get_value("Price List", price_list, "currency")
		fx = engine._price_list_fx(price_list, doc.get("currency"))
		unpriced = defaultdict(list)
		for i, it in enumerate(doc.items):
			if not (it.item_code and it.get("custom_length")):
				continue
			per_stem = frappe.db.get_value(
				"Item Price",
				{
					"item_code": it.item_code,
					"price_list": price_list,
					"custom_length": it.custom_length,
					"selling": 1,
				},
				"price_list_rate",
			)
			rate = float(per_stem) * (engine._uom_factor(it.uom) or 1) * fx if per_stem is not None else 0
			if rate <= 0:
				unpriced[(it.item_code, it.custom_length)].append(_ref(row_refs, i))
		for (item_code, length), rows in unpriced.items():
			add(
				"price",
				f"No price for {item_code} {length} in {price_list}.",
				item_code=item_code,
				length=length,
				price_list=price_list,
				currency=currency,
				rows=rows,
			)

	# 5b. varieties that are switched off (ERPNext refuses a disabled item on an order)
	disabled = {
		it.item_code
		for it in doc.items
		if it.item_code
		and _exists("Item", it.item_code, exists_cache)
		and frappe.db.get_value("Item", it.item_code, "disabled")
	}
	for item_code in sorted(disabled):
		add("disabled_item", f"Item {item_code} is disabled.", item_code=item_code)

	# 6. directly-added lines whose item has no Sales UOM
	missing_uom = set()
	for it in doc.items:
		if it.item_code and not it.get("custom_line") and _exists("Item", it.item_code, exists_cache):
			if not frappe.db.get_value("Item", it.item_code, "sales_uom"):
				missing_uom.add(it.item_code)
	for item_code in sorted(missing_uom):
		add("sales_uom", f"{item_code} has no Sales UOM.", item_code=item_code)

	# 7. things the person cannot fix in a form
	no_packrate = [
		str(i + 1) for i, it in enumerate(doc.items) if it.item_code and not engine._line_packrate(it)
	]
	if no_packrate:
		add(
			"info",
			"No packrate on line(s) " + ", ".join(no_packrate) + ". Use Review & fix to set it.",
		)
	for i, it in enumerate(doc.items):
		if not it.item_code or it.get("custom_line") or not it.get("uom"):
			continue
		sales_uom = frappe.db.get_value("Item", it.item_code, "sales_uom")
		if sales_uom and it.uom != sales_uom:
			add(
				"info",
				f"Line {i + 1} ({it.item_code}) has bunch size {it.uom} but the item's Sales UOM is {sales_uom}. "
				"Use Review & fix.",
			)

	box_counts = defaultdict(set)
	for it in doc.items:
		if not it.item_code:
			continue
		if it.get("custom_mixed_bunch"):
			key = ("Mixed Bunch", it.get("custom_bunch_group") or it.get("custom_line") or it.name)
		elif it.get("custom_mixed_box"):
			key = ("Mixed Box", it.get("custom_mix_group") or it.name)
		else:
			continue
		box_counts[key].add(int(it.get("custom_number_of_boxes") or 0))
	for (kind, group), counts in box_counts.items():
		if len(counts) > 1:
			add(
				"info",
				f"{kind} {group}: its colours do not all book the same Number of Boxes. Use Review & fix.",
			)

	return issues


def price_list_summary(doc):
	"""The price list this order will be priced from, and where it comes from, for the wizard's dialogs."""
	price_list = engine._resolve_price_list(doc)
	return {
		"customer": doc.customer,
		"customer_price_list": engine._customer_default_pl(doc),
		"price_list": price_list,
		"price_list_currency": frappe.db.get_value("Price List", price_list, "currency")
		if price_list
		else None,
		"order_currency": doc.get("currency"),
		"set_warehouse": doc.get("set_warehouse"),
	}


_TAG = re.compile(r"<[^>]+>")


def plain(message):
	"""An error message from a save, without its HTML."""
	return _TAG.sub("", message or "")
