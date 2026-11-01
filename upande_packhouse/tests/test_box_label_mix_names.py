"""A box's mix name comes from its own pick list's Sales Order lines."""

from unittest import TestCase
from unittest.mock import patch

from frappe import _dict

from upande_packhouse import box_label

# SAL-ORD-2026-03285: Dominica sits in the 60cm mix (OPL-A) and the 70cm mix (OPL-B).
LINES = [
	_dict(item_code="Dominica", custom_mix_name="MIX (60)", custom_opl="OPL-A"),
	_dict(item_code="Reflex", custom_mix_name="", custom_opl=None),
	_dict(item_code="Rosanella", custom_mix_name="MIX (70)", custom_opl="OPL-B"),
	_dict(item_code="Reflex", custom_mix_name="MIX (70)", custom_opl="OPL-B"),
	_dict(item_code="Dominica", custom_mix_name="MIX (70)", custom_opl="OPL-B"),
]


class TestBoxLabelMixNames(TestCase):
	def mix(self, opl, varieties):
		with patch.object(box_label.frappe, "get_all", return_value=[_dict(r) for r in LINES]):
			mixes = box_label.mix_names("SO", opl)
		return box_label.label_filters("ORDER", [(v, 40) for v in varieties], mixes)["mix_name"]

	def test_box_gets_only_its_pick_lists_mix(self):
		self.assertEqual(self.mix("OPL-B", ["Reflex", "Dominica", "Rosanella"]), "MIX (70)")

	def test_other_pick_list_keeps_its_own_mix(self):
		self.assertEqual(self.mix("OPL-A", ["Dominica"]), "MIX (60)")

	def test_without_pick_list_first_line_wins(self):
		self.assertEqual(self.mix(None, ["Dominica"]), "MIX (60)")


SPEC_LINES = [
	_dict(
		item_code="Reflex",
		custom_opl="OPL-B",
		custom_mix_group=2,
		custom_mix_name="MIX (70)",
		custom_line="",
	),
	_dict(
		item_code="Dominica",
		custom_opl="OPL-B",
		custom_mix_group=2,
		custom_mix_name="MIX (70)",
		custom_line="",
	),
	_dict(
		item_code="Dominica",
		custom_opl="OPL-A",
		custom_mix_group=1,
		custom_mix_name="MIX (60)",
		custom_line="",
	),
	_dict(item_code="Aqua", custom_opl="OPL-C", custom_line="COOP PINK BQT 42CM", custom_mix_name=""),
	_dict(item_code="Giselle", custom_opl="OPL-D", custom_line="", custom_mix_name=""),
	_dict(
		item_code="Nikita",
		custom_opl="OPL-E",
		custom_mix_group=3,
		custom_mix_name="MIX 3",
		custom_line="SPEC 3",
	),
]


class TestBoxLabelSpecification(TestCase):
	def spec(self, opl, varieties):
		return box_label.specification(SPEC_LINES, opl, varieties, "ORDER-1")

	def test_mix_uses_its_own_mix_name(self):
		self.assertEqual(self.spec("OPL-B", ["Reflex", "Dominica"]), "MIX (70)")

	def test_mix_prefers_specification(self):
		self.assertEqual(self.spec("OPL-E", ["Nikita"]), "SPEC 3")

	def test_single_variety_uses_specification(self):
		self.assertEqual(self.spec("OPL-C", ["Aqua"]), "COOP PINK BQT 42CM")

	def test_without_specification_falls_back_to_order_name(self):
		self.assertEqual(self.spec("OPL-D", ["Giselle"]), "ORDER-1")

	def test_purchase_order_blank_markers(self):
		self.assertEqual(box_label.purchase_order(_dict(po_no=" X ")), "")
		self.assertEqual(box_label.purchase_order(_dict(po_no="775949")), "775949")
