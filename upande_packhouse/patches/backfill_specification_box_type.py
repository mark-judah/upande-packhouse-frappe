import frappe


def execute():
	"""Ships with the schema move of `box_type` from Spec Box Item up to
	Specifications' own header (see specifications.json / spec_box_item.json).

	The header field is `reqd`, and spec_autofill._spec_issues refuses to
	autofill a Specification whose `box_type` is blank ("Box Type is not
	set."). Every Specification saved before the move has a blank header --
	the value lives on its box rows -- so without this patch the move silently
	breaks autofill for the entire existing catalogue, on Desk and on the
	Sales Order dashboard alike, until somebody retypes Box Type on each spec.

	Frappe never drops a column when a field leaves a DocType's JSON, so the
	old `tabSpec Box Item`.`box_type` values are still there to read. Where a
	spec's rows disagree (a pre-move spec was free to mix box types across
	rows) the most-used value wins, and the rest are reported so a human can
	check them -- guessing silently would be worse than saying so.
	"""
	if not frappe.db.table_exists("Spec Box Item"):
		return

	column = frappe.db.sql("SHOW COLUMNS FROM `tabSpec Box Item` LIKE %s", "box_type")
	if not column:
		# Nothing to read from: either a fresh site (no pre-move data) or the
		# column has already been dropped by a later cleanup.
		return

	rows = frappe.db.sql(
		"""
		SELECT sbi.parent AS spec, sbi.box_type AS box_type, COUNT(*) AS n
		FROM `tabSpec Box Item` sbi
		INNER JOIN `tabSpecifications` s ON s.name = sbi.parent
		WHERE IFNULL(sbi.box_type, '') != ''
		  AND IFNULL(s.box_type, '') = ''
		GROUP BY sbi.parent, sbi.box_type
		ORDER BY sbi.parent, n DESC
		""",
		as_dict=True,
	)
	if not rows:
		return

	by_spec = {}
	for r in rows:
		by_spec.setdefault(r.spec, []).append(r)

	filled, ambiguous = 0, []
	for spec, candidates in by_spec.items():
		# candidates are already ordered most-used first for this spec
		winner = candidates[0].box_type
		if len(candidates) > 1:
			ambiguous.append((spec, [c.box_type for c in candidates]))
		if not frappe.db.exists("Box Type", winner):
			# A dangling link would fail the next save of the spec -- leave it
			# blank and report instead, so the spec fails loudly on edit
			# rather than carrying a broken Link.
			ambiguous.append((spec, [f"{winner} (no such Box Type)"]))
			continue
		frappe.db.set_value("Specifications", spec, "box_type", winner, update_modified=False)
		filled += 1

	frappe.db.commit()
	frappe.clear_cache(doctype="Specifications")

	print(f"backfill_specification_box_type: filled {filled} Specification(s) from Spec Box Item")
	if ambiguous:
		print(f"  {len(ambiguous)} spec(s) need a human look (rows disagreed, or the link is dangling):")
		for spec, options in ambiguous[:20]:
			print(f"    {spec}: {options}")

	still_blank = frappe.db.count("Specifications", {"box_type": ["in", ["", None]]})
	if still_blank:
		print(
			f"  {still_blank} Specification(s) still have no Box Type -- their box rows carried "
			f"none either. These cannot be used for autofill until Box Type is set by hand."
		)
