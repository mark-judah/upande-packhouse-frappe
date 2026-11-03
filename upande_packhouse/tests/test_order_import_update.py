from frappe.tests import UnitTestCase

from upande_packhouse.api.order_import import diff_orders, order_payload


class _Row(dict):
	def get(self, key, default=None):
		return super().get(key, default)


class _Doc:
	def __init__(self, items, **header):
		self.items = [_Row(i) for i in items]
		self._h = header

	def get(self, key, default=None):
		return self._h.get(key, default)


def _line(item, boxes, **kw):
	return {
		"item_code": item,
		"custom_length": "52cm",
		"custom_box_type": "Quarter",
		"custom_line": "S",
		"custom_number_of_boxes": boxes,
		"custom_packrate": "20",
		"uom": "Bunch (10)",
		**kw,
	}


class TestOrderUpdateDiff(UnitTestCase):
	def test_changed_added_and_removed_lines(self):
		old = _Doc([_line("A", 5), _line("B", 3), _line("C", 2)], delivery_date="2026-10-12")
		new = _Doc([_line("A", 8), _line("C", 2), _line("D", 4)], delivery_date="2026-10-15")
		diff = diff_orders(old, new, ["delivery_date"])
		types = {(x["type"], x["item_code"]) for x in diff["lines"]}
		self.assertEqual(types, {("changed", "A"), ("removed", "B"), ("added", "D")})
		changed = next(x for x in diff["lines"] if x["type"] == "changed")
		self.assertEqual(
			changed["fields"], [{"field": "custom_number_of_boxes", "label": "Boxes", "old": "5", "new": "8"}]
		)
		self.assertEqual(diff["header"][0]["field"], "delivery_date")
		self.assertEqual((diff["header"][0]["old"], diff["header"][0]["new"]), ("2026-10-12", "2026-10-15"))

	def test_identical_orders_have_no_differences_and_zero_equals_empty(self):
		old = _Doc([_line("A", 5, custom_packrate_mixed_box=0)], delivery_date="2026-10-12")
		new = _Doc([_line("A", 5, custom_packrate_mixed_box=None)], delivery_date="2026-10-12")
		self.assertEqual(diff_orders(old, new, ["delivery_date"]), {"header": [], "lines": []})

	def test_matching_fields_and_derived_ones_are_never_changes(self):
		old = _Doc([], po_no="P1", transaction_date="2026-10-01", currency="EUR")
		new = _Doc([], po_no="P2", transaction_date="2026-10-20", currency="GBP")
		self.assertEqual(diff_orders(old, new, ["po_no", "transaction_date", "currency"])["header"], [])

	def test_the_same_variety_twice_is_compared_line_by_line(self):
		old = _Doc([_line("A", 5), _line("A", 5)])
		new = _Doc([_line("A", 5), _line("A", 9)])
		diff = diff_orders(old, new, [])
		self.assertEqual([(x["type"], x["fields"][0]["new"]) for x in diff["lines"]], [("changed", "9")])

	def test_order_payload_splits_spec_and_manual_rows(self):
		order = {
			"header": {"po_no": "P1"},
			"rows": [
				{
					"item_code": "A",
					"custom_line": "S",
					"_is_spec_row": True,
					"_source_row": 3,
					"custom_number_of_boxes": 5,
				},
				{
					"item_code": "B",
					"_stems_per_bunch": 10,
					"_bunches_per_box": 2,
					"custom_number_of_boxes": 4,
				},
			],
		}
		p = order_payload(order)
		self.assertEqual(
			p["spec_rows"], [{"item_code": "A", "custom_line": "S", "custom_number_of_boxes": 5}]
		)
		self.assertEqual(p["manual_rows"][0]["boxes"], 4)
		self.assertEqual(p["row_refs"], [0, 1])
