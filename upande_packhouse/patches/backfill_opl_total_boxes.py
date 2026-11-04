import frappe


def execute():
	"""Count the boxes on existing pick lists: packing guide box numbers, else pick row box ids."""
	frappe.db.sql(
		"""UPDATE `tabOrder Pick List` o SET o.total_boxes = COALESCE(
			NULLIF((SELECT COUNT(DISTINCT g.box_number) FROM `tabPacking Guide` g
				WHERE g.parent = o.name AND g.parenttype = 'Order Pick List' AND IFNULL(g.box_number, 0) != 0), 0),
			(SELECT COUNT(DISTINCT p.custom_box_id) FROM `tabPick List Item` p
				WHERE p.parent = o.name AND p.parenttype = 'Order Pick List' AND IFNULL(p.custom_box_id, '') != ''))"""
	)
