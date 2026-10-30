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
