"""Item Price override: custom_length (Link -> Stem Length) is a real
differentiator for Roses pricing -- the same variety legitimately has a
different Item Price per stem length in the same Price List (see
sales_order_engine.sales_order_price, which resolves the rate by
(item_code, price_list, custom_length)). Stock ERPNext's own duplicate
check (erpnext.stock.doctype.item_price.item_price.ItemPrice.check_duplicates)
has no idea this field exists -- it matches only on uom / valid_from /
valid_upto / customer / supplier / batch_no / packing_unit, so two rows for
the same item + price list + UOM that differ ONLY in custom_length are
wrongly flagged as the same price entered twice ("Item Price appears
multiple times based on Price List, Supplier/Customer, Currency, Item,
Batch, UOM, Qty, and Dates."), blocking exactly the data this app needs.

This subclass re-runs the identical check with custom_length added to the
list of fields that must also match before two rows count as duplicates --
copied rather than wrapped/monkeypatched since the field list is inlined in
the core method, not exposed as an overridable attribute. If a future
ERPNext upgrade changes check_duplicates, re-diff this against
erpnext/stock/doctype/item_price/item_price.py and reapply the one
addition (the "custom_length" entry in data_fields).
"""

import frappe
from frappe import _
from frappe.query_builder import Criterion
from frappe.query_builder.functions import Cast_

from erpnext.stock.doctype.item_price.item_price import ItemPrice, ItemPriceDuplicateItem


class CustomItemPrice(ItemPrice):
    def check_duplicates(self):
        item_price = frappe.qb.DocType("Item Price")

        query = (
            frappe.qb.from_(item_price)
            .select(item_price.price_list_rate)
            .where(
                (item_price.item_code == self.item_code)
                & (item_price.price_list == self.price_list)
                & (item_price.name != self.name)
            )
        )
        data_fields = (
            "uom",
            "valid_from",
            "valid_upto",
            "customer",
            "supplier",
            "batch_no",
            "custom_length",  # the one addition over core -- see module docstring
        )

        number_fields = ["packing_unit"]

        for field in data_fields:
            if self.get(field):
                query = query.where(item_price[field] == self.get(field))
            else:
                query = query.where(
                    Criterion.any(
                        [
                            item_price[field].isnull(),
                            Cast_(item_price[field], "varchar") == "",
                        ]
                    )
                )

        for field in number_fields:
            if self.get(field):
                query = query.where(item_price[field] == self.get(field))
            else:
                query = query.where(
                    Criterion.any(
                        [
                            item_price[field].isnull(),
                            item_price[field] == 0,
                        ]
                    )
                )

        price_list_rate = query.run(as_dict=True)

        if price_list_rate:
            frappe.throw(
                _(
                    "Item Price appears multiple times based on Price List, Supplier/Customer, "
                    "Currency, Item, Batch, UOM, Length, Qty, and Dates."
                ),
                ItemPriceDuplicateItem,
            )
