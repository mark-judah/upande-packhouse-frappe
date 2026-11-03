"""A channel order keeps the per-stem price its buyer agreed on."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

from frappe import _dict

from upande_packhouse import sales_order_engine


def _order(**line):
	doc = MagicMock()
	doc.get.side_effect = lambda key, default=None: {"business_unit": "Roses"}.get(key, default)
	doc.items = [_dict(item_code="Madam Red", custom_length="62cm", uom="Bunch (10)", **line)]
	return doc


class TestSalesOrderChannelRate(TestCase):
	def price(self, doc, item_price=None):
		with (
			patch.object(sales_order_engine, "_resolve_price_list", return_value="EUR Price List"),
			patch.object(sales_order_engine, "_price_list_fx", return_value=1),
			patch.object(sales_order_engine.frappe.db, "get_value", return_value=item_price),
		):
			sales_order_engine.sales_order_price(doc)
		return doc.items[0]

	def test_floriday_rate_is_kept_without_an_item_price(self):
		line = self.price(_order(custom_floriday_rate=0.2))
		self.assertAlmostEqual(line.rate, 2.0)
		self.assertAlmostEqual(line.price_list_rate, 2.0)

	def test_biflorica_rate_is_kept_over_the_item_price(self):
		line = self.price(_order(custom_biflorica_rate=0.15), item_price=0.3)
		self.assertAlmostEqual(line.rate, 1.5)

	def test_line_without_a_channel_rate_is_priced_from_item_price(self):
		line = self.price(_order(), item_price=0.3)
		self.assertAlmostEqual(line.rate, 3.0)

	def test_line_without_a_channel_rate_or_item_price_is_zeroed(self):
		line = self.price(_order())
		self.assertEqual(line.rate, 0)


class TestSNumberRequired(TestCase):
	def required(self, customer, business_unit="Roses", floriday="Royal FloraHolland"):
		doc = _dict(customer=customer, business_unit=business_unit)
		with patch.object(sales_order_engine, "floriday_customer", return_value=floriday):
			return sales_order_engine.s_number_required(doc)

	def test_required_on_a_roses_order(self):
		self.assertTrue(self.required("FLAMINGO UK"))

	def test_not_required_for_the_floriday_customer(self):
		self.assertFalse(self.required("Royal FloraHolland"))

	def test_required_when_floriday_is_not_set_up(self):
		self.assertTrue(self.required("Royal FloraHolland", floriday=None))

	def test_not_required_outside_roses(self):
		self.assertFalse(self.required("FLAMINGO UK", business_unit="Coffee"))
