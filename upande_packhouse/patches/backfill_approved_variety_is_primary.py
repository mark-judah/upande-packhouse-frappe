import frappe


def execute():
	"""Ships with `is_primary` arriving on Spec Approved Variety, and with
	Specifications.validate_approved_varieties enforcing exactly one primary
	per (bunch_id, colour) slot.

	The field was first added with `"default": "1"`, so schema sync created
	the column as DEFAULT 1 and every pre-existing Approved Variety row came
	back primary. Any spec carrying substitutes -- two or more varieties in
	one colour slot, which is the entire point of the feature -- then threw
	"only one variety can be marked Primary" on its next save and could not be
	edited at all. The JSON default is now 0; this reconciles the rows that
	were already written under the old default, and is safe to re-run.

	Rule per slot: keep exactly one primary. Where several are ticked the
	first row (lowest idx) wins, matching the grid order the operator sees.
	Where none is ticked -- the case after the default flips to 0 -- the first
	row is promoted, so an untouched spec stays saveable.
	"""
	if not frappe.db.table_exists("Spec Approved Variety"):
		return

	column = frappe.db.sql("SHOW COLUMNS FROM `tabSpec Approved Variety` LIKE %s", "is_primary")
	if not column:
		return

	rows = frappe.db.sql(
		"""
		SELECT name, parent, IFNULL(bunch_id, '') AS bunch_id, IFNULL(colour, '') AS colour,
		       idx, IFNULL(is_primary, 0) AS is_primary
		FROM `tabSpec Approved Variety`
		ORDER BY parent, bunch_id, colour, idx
		""",
		as_dict=True,
	)
	if not rows:
		return

	slots = {}
	for r in rows:
		slots.setdefault((r.parent, r.bunch_id, r.colour), []).append(r)

	promoted = demoted = 0
	for members in slots.values():
		primaries = [r for r in members if r.is_primary]
		if len(primaries) == 1:
			continue
		keep = primaries[0] if primaries else members[0]
		if not primaries:
			promoted += 1
		for r in members:
			want = 1 if r.name == keep.name else 0
			if r.is_primary != want:
				frappe.db.set_value(
					"Spec Approved Variety", r.name, "is_primary", want, update_modified=False
				)
				if want == 0:
					demoted += 1

	frappe.db.commit()
	frappe.clear_cache(doctype="Specifications")
	print(
		f"backfill_approved_variety_is_primary: {len(slots)} colour slot(s) reconciled "
		f"({promoted} promoted, {demoted} cleared)"
	)
