# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Backend for the web Specifications ledger (www/specifications.html) --
# CRUD over the real Specifications doctype (+ its Spec Box Item / Spec
# Approved Variety / Spec Consumable child tables), reshaping the raw
# bunch_id-grouped rows into the "bunches -> rows" shape the ledger UI
# renders, and back again on save.
#
# bunch_id (Spec Box Item / Spec Approved Variety) is a plain Data field --
# there is no rule anywhere that says rows must be numbered "1", "2", "3".
# The client is handed the LITERAL bunch_id value ("id" on each bunch), blank
# if that's what the doctype holds, and saveSpecification writes back exactly
# what the user typed -- it never invents a value. Two bunches sharing the
# same typed id merge into one group the next time the spec is opened; that
# is deliberate, not a bug.

import frappe


def _json_payload():
	"""The client sends complex payloads as a single `data` field, JSON-
	stringified before frappe.call() form-encodes the request (frappe.call
	is jQuery.ajax under the hood: a plain object body always goes out as
	application/x-www-form-urlencoded, never a raw JSON body) -- so
	frappe.request.get_json() is the wrong read here: it 415s because the
	actual Content-Type is never application/json. form_dict.data is the
	real carrier."""
	raw = frappe.form_dict.get("data")
	if raw is None:
		return {}
	if isinstance(raw, str):
		return frappe.parse_json(raw) or {}
	return raw


# Approved varieties are, by definition, cut flowers -- "Cut Flowers" is the
# real parent Item Group in this system's tree (see Item Group tree in the
# Desk). Scoping the picker to it (and everything nested under it) is a
# domain rule, not incidental master data, so it's a constant here rather
# than something fetched from a settings doctype.
CUT_FLOWERS_ITEM_GROUP = "Cut Flowers"


def _cut_flower_item_groups():
	"""This Item Group and every descendant in its tree (Spray Roses,
	Standard Roses, Alstroemeria, ... ), via the standard nested-set
	lft/rgt range -- the same technique Frappe's own tree Link queries use
	for "include children" filtering."""
	root = frappe.db.get_value("Item Group", CUT_FLOWERS_ITEM_GROUP, ["lft", "rgt"], as_dict=True)
	if not root:
		return [CUT_FLOWERS_ITEM_GROUP]
	return frappe.get_all(
		"Item Group",
		filters={"lft": [">=", root.lft], "rgt": ["<=", root.rgt]},
		pluck="name",
	)


def _item_fields(item_names):
	"""colour/headsize/budcount (Spec Approved Variety) and item_name (Spec
	Consumable) are declared fetch_from variety.custom_color / ... / item.
	item_name in the doctype JSON -- that only fires from the Desk form's
	own JS, so this API (which builds rows directly) replicates it
	server-side for both child tables in one query."""
	item_names = list({v for v in item_names if v})
	if not item_names:
		return {}
	rows = frappe.get_all(
		"Item",
		filters={"name": ["in", item_names]},
		fields=["name", "item_name", "custom_color", "custom_headsize_cm", "custom_budcount"],
	)
	return {r.name: r for r in rows}


@frappe.whitelist()
def getSpecificationOptions():
	"""Master lists for the ledger's dropdowns -- real doctype values, never
	hardcoded, so a length/box type/cut stage added later just shows up."""
	try:
		stem_lengths = frappe.get_all("Stem Length", pluck="name", order_by="name asc")
		box_types = frappe.get_all("Box Type", pluck="name", order_by="name asc")
		cut_stages = frappe.get_all("Cut Stage", pluck="name", order_by="name asc")
		frappe.response["message"] = {
			"success": True,
			"stem_lengths": stem_lengths,
			"box_types": box_types,
			"cut_stages": cut_stages,
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getSpecificationOptions error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def searchCustomers():
	try:
		query = frappe.form_dict.get("query") or ""
		rows = frappe.get_all(
			"Customer",
			filters={"customer_name": ["like", "%" + query + "%"]},
			fields=["name", "customer_name"],
			order_by="customer_name asc",
			limit_page_length=20,
		)
		frappe.response["message"] = {"success": True, "customers": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="searchCustomers error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "customers": []}


@frappe.whitelist()
def searchVarieties():
	"""Item search for the Approved Varieties picker -- scoped to the Cut
	Flowers item group tree, since a variety is always a cut flower."""
	try:
		query = frappe.form_dict.get("query") or ""
		rows = frappe.get_all(
			"Item",
			filters={
				"name": ["like", "%" + query + "%"],
				"disabled": 0,
				"item_group": ["in", _cut_flower_item_groups()],
			},
			fields=["name", "item_group", "custom_color"],
			order_by="name asc",
			limit_page_length=20,
		)
		frappe.response["message"] = {"success": True, "varieties": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="searchVarieties error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "varieties": []}


@frappe.whitelist()
def searchItems():
	"""Item search for the Consumables picker (Spec Consumable.item is a
	plain Link to Item, same as variety -- no domain restriction either)."""
	try:
		query = frappe.form_dict.get("query") or ""
		rows = frappe.get_all(
			"Item",
			filters={"name": ["like", "%" + query + "%"], "disabled": 0},
			fields=["name", "item_group", "item_name"],
			order_by="name asc",
			limit_page_length=20,
		)
		frappe.response["message"] = {"success": True, "items": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="searchItems error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "items": []}


@frappe.whitelist()
def listSpecificationCustomers():
	"""Distinct customers that actually have a specification -- for the list
	view's customer filter, so it never shows a customer with nothing to
	find, and never needs updating when a new customer gets a spec."""
	try:
		rows = frappe.get_all(
			"Specifications",
			filters={"customer": ["is", "set"]},
			fields=["customer"],
			distinct=True,
			order_by="customer asc",
		)
		frappe.response["message"] = {"success": True, "customers": [r.customer for r in rows]}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listSpecificationCustomers error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "customers": []}


@frappe.whitelist()
def listSpecifications():
	try:
		query = frappe.form_dict.get("query") or ""
		customer = frappe.form_dict.get("customer") or ""
		status = frappe.form_dict.get("status") or ""
		spec_type = frappe.form_dict.get("spec_type") or ""
		filters = {}
		if query:
			filters["spec_name"] = ["like", "%" + query + "%"]
		if customer:
			filters["customer"] = customer
		if status:
			filters["status"] = status
		if spec_type:
			filters["spec_type"] = spec_type
		rows = frappe.get_all(
			"Specifications",
			filters=filters,
			fields=[
				"name",
				"spec_name",
				"customer",
				"box_assortment",
				"status",
				"spec_type",
				"modified",
			],
			order_by="modified desc",
			limit_page_length=200,
		)
		frappe.response["message"] = {"success": True, "specifications": rows}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="listSpecifications error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e), "specifications": []}


def _group_into_bunches(doc):
	"""Box Item / Approved Variety rows -> [{id, type, length,
	bunches_per_box, rows:[{varieties, colour, stems_per_bunch}]}], grouped
	by bunch_id, in first-seen order. box_type isn't part of this shape --
	a spec is one box, so it's a header field (doc.box_type), the same for
	every bunch. A spec saved before bunch_id existed
	(or edited around it) has every row with bunch_id="" -- give each of
	those its OWN single-row bunch instead of collapsing them all into one
	(they don't necessarily share a length/bunches_per_box, so
	merging them would be guessing). Blank-bunch_id Box Items and blank-
	bunch_id Approved Varieties are paired up positionally -- the Nth blank
	row in one table with the Nth blank row in the other -- the same 1:1
	positional rule the Mixed Bunch case below already uses once two rows
	share a real bunch_id, just applied before grouping instead of after.
	It's the only reconstruction possible with no bunch_id to key on, and it
	matches how this data was entered in the first place (row for row).
	The returned "id" for a blank-bunch_id group is always "" -- never a
	made-up number. The client shows that as a blank, user-editable field:
	nothing here decides which rows are "bunch one" or "bunch two" on the
	user's behalf.
	"""
	av_by_bunch = {}
	av_order = []
	blank_av_n = 0
	for av in doc.approved_varieties or []:
		if av.bunch_id:
			key = av.bunch_id
		else:
			key = "__blank__" + str(blank_av_n)
			blank_av_n += 1
		if key not in av_by_bunch:
			av_by_bunch[key] = []
			av_order.append(key)
		av_by_bunch[key].append(av)

	bi_by_bunch = {}
	bi_order = []
	blank_bi_n = 0
	for bi in doc.box_items or []:
		if bi.bunch_id:
			key = bi.bunch_id
		else:
			key = "__blank__" + str(blank_bi_n)
			blank_bi_n += 1
		if key not in bi_by_bunch:
			bi_by_bunch[key] = []
			bi_order.append(key)
		bi_by_bunch[key].append(bi)

	# Order bunches by whichever table introduces the key first (Box Items
	# usually lead; Approved Variety keys not seen there are appended after).
	order = list(bi_order)
	for k in av_order:
		if k not in order:
			order.append(k)

	bunches = []
	for key in order:
		bi_rows = bi_by_bunch.get(key, [])
		av_rows = av_by_bunch.get(key, [])
		length = bi_rows[0].length if bi_rows else None
		bunches_per_box = bi_rows[0].bunches_per_box if bi_rows else None

		# Group by colour regardless of bunch_type: substitution (several
		# varieties for one colour, one marked primary) is a real thing for
		# both a Mono bunch's single colour and a Mixed bunch's several --
		# it was never a Mixed-only restriction, that was a bug. Each colour
		# becomes one "row"; is_primary decides which variety leads it (the
		# rest, in whatever order they were saved, are its substitutes).
		by_colour = {}
		colour_order = []
		for av in av_rows:
			c = av.colour or ""
			if c not in by_colour:
				by_colour[c] = []
				colour_order.append(c)
			by_colour[c].append(av)

		rows = []
		for i, c in enumerate(colour_order):
			avs = sorted(by_colour[c], key=lambda a: 0 if a.is_primary else 1)
			bi_row = bi_rows[i] if i < len(bi_rows) else (bi_rows[0] if bi_rows else None)
			rows.append(
				{
					"varieties": [a.variety for a in avs if a.variety],
					"colour": c,
					"stems_per_bunch": bi_row.stems_per_bunch if bi_row else None,
				}
			)
		if not rows and bi_rows:
			# Box Item row(s) exist with no matching Approved Variety yet.
			rows = [
				{"varieties": [], "colour": "", "stems_per_bunch": bi.stems_per_bunch}
				for bi in bi_rows
			]

		bunches.append(
			{
				"id": "" if key.startswith("__blank__") else key,
				"type": "Mixed" if len(colour_order) > 1 else "Mono",
				"length": length,
				"bunches_per_box": bunches_per_box,
				"rows": rows,
			}
		)
	return bunches


@frappe.whitelist()
def getSpecification():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Specifications", name):
			frappe.response["message"] = {"success": False, "error": "Specification not found"}
			return
		doc = frappe.get_doc("Specifications", name)
		frappe.response["message"] = {
			"success": True,
			"specification": {
				"name": doc.name,
				"spec_name": doc.spec_name,
				"customer": doc.customer,
				"ftnft": doc.ftnft,
				"spec_type": doc.spec_type,
				"status": doc.status,
				"valid_from": str(doc.valid_from or ""),
				"expiry_date": str(doc.expiry_date or ""),
				"category_code": doc.category_code,
				"box_assortment": doc.box_assortment,
				"box_type": doc.box_type,
				"cut_stage": doc.cut_stage,
				"defoliation_length": doc.defoliation_length,
				"rubber_band_type": doc.rubber_band_type,
				"rubber_band_distance_1": doc.rubber_band_distance_1,
				"rubber_band_distance_2": doc.rubber_band_distance_2,
				"min_colours_per_box": doc.min_colours_per_box,
				"max_colours_per_box": doc.max_colours_per_box,
				"consumables_charge": doc.consumables_charge,
				"documentation_charge": doc.documentation_charge,
				"certificate_of_origin": doc.certificate_of_origin,
				"bunches": _group_into_bunches(doc),
				"consumables": [
					{
						"consumable_type": c.consumable_type,
						"item": c.item,
						"item_name": c.item_name,
						"application_level": c.application_level,
						"description": c.description,
						"qty_per_bunch": c.qty_per_bunch,
						"qty_per_box": c.qty_per_box,
						"price_inclusive": c.price_inclusive,
					}
					for c in (doc.consumables or [])
				],
			},
		}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.log_error(title="getSpecification error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def saveSpecification():
	try:
		data = _json_payload()
		name = data.get("name")

		if name and frappe.db.exists("Specifications", name):
			doc = frappe.get_doc("Specifications", name)
		else:
			doc = frappe.new_doc("Specifications")

		header_fields = [
			"spec_name",
			"customer",
			"ftnft",
			"spec_type",
			"status",
			"valid_from",
			"expiry_date",
			"category_code",
			"box_assortment",
			"box_type",
			"cut_stage",
			"defoliation_length",
			"rubber_band_type",
			"rubber_band_distance_1",
			"rubber_band_distance_2",
			"min_colours_per_box",
			"max_colours_per_box",
			"consumables_charge",
			"documentation_charge",
			"certificate_of_origin",
		]
		for f in header_fields:
			if f in data:
				doc.set(f, data.get(f))

		bunches = data.get("bunches") or []
		consumables = data.get("consumables") or []

		# Every Item referenced anywhere on the spec, in one query, to fill
		# colour/headsize/budcount and item_name the same way the Desk
		# form's fetch_from would.
		all_varieties = [v for b in bunches for r in b.get("rows", []) for v in (r.get("varieties") or [])]
		all_consumable_items = [c.get("item") for c in consumables if c.get("item")]
		item_info = _item_fields(all_varieties + all_consumable_items)

		doc.set("box_items", [])
		doc.set("approved_varieties", [])

		for i, b in enumerate(bunches):
			# Literal, user-typed bunch id -- blank stays blank, never
			# renumbered positionally.
			bunch_id = (b.get("id") or "").strip()
			rows = b.get("rows") or []
			# bunch_type is informational only now (Desk list views, reports) --
			# it's derived from how many colour rows the bunch actually has,
			# never a separate client choice that could disagree with them.
			bunch_type = "Mixed Bunch" if len(rows) > 1 else "Mono Bunch"

			for r in rows:
				doc.append(
					"box_items",
					{
						"bunch_id": bunch_id,
						"bunch_type": bunch_type,
						"stems_per_bunch": r.get("stems_per_bunch") or 0,
						"length": b.get("length"),
						"bunches_per_box": b.get("bunches_per_box") or 0,
					},
				)
				# Within a colour row, the first variety is the primary pick;
				# any after it are approved substitutes for that same colour
				# (see Spec Approved Variety.is_primary) -- never additional
				# stems of their own.
				for vi, variety in enumerate(r.get("varieties") or []):
					info = item_info.get(variety)
					doc.append(
						"approved_varieties",
						{
							"bunch_id": bunch_id,
							"variety": variety,
							"is_primary": 1 if vi == 0 else 0,
							"colour": (info.custom_color if info else None) or r.get("colour") or "",
							"headsize_cm": info.custom_headsize_cm if info else None,
							"budcount": info.custom_budcount if info else None,
						},
					)

		doc.set("consumables", [])
		for c in consumables:
			item = c.get("item")
			info = item_info.get(item) if item else None
			doc.append(
				"consumables",
				{
					"consumable_type": c.get("consumable_type"),
					"item": item,
					"item_name": info.item_name if info else None,
					"application_level": c.get("application_level"),
					"description": c.get("description"),
					"qty_per_bunch": c.get("qty_per_bunch") or 0,
					"qty_per_box": c.get("qty_per_box") or 0,
					"price_inclusive": c.get("price_inclusive") or 0,
				},
			)

		if doc.is_new():
			doc.insert()
		else:
			doc.save()

		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True, "name": doc.name}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="saveSpecification error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}


@frappe.whitelist()
def deleteSpecification():
	try:
		name = frappe.form_dict.get("name")
		if not name or not frappe.db.exists("Specifications", name):
			frappe.response["message"] = {"success": False, "error": "Specification not found"}
			return
		frappe.delete_doc("Specifications", name, ignore_permissions=False)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		frappe.response["message"] = {"success": True}
	except Exception as e:
		frappe.clear_messages()  # discard any frappe.throw() message_log entry the caught exception left behind
		frappe.db.rollback()
		frappe.log_error(title="deleteSpecification error", message=str(e))
		frappe.response["message"] = {"success": False, "error": str(e)}
