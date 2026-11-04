"""Jinja methods (hooks.py > jinja > methods): available in every template and
email, e.g. the Auto Repeat message of a standing order."""

from upande_packhouse import standing_orders


def standing_summary(doc):
	"""Dates, boxes and stems of a standing order's Sales Order, for its reminder email."""
	return standing_orders.summary(doc)


def invoice_hs_code(item_code, item_group=None):
	"""HS code for an invoice row: the item's own Customs Tariff Number, else its crop's default."""
	from upande_packhouse.item_groups import hs_code

	return hs_code(item_code, item_group)


def invoice_crop(item_group):
	"""Crop column of the invoice: Std Rose / Spr Rose, else the parent Item Group in full."""
	from upande_packhouse.item_groups import crop_name

	return crop_name(item_group)
