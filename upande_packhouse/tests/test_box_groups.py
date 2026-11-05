"""A spec's mono bunches and mixed bunches are one physical box (box_groups.box_keys)."""

import unittest

from upande_packhouse.box_groups import box_keys

SPEC = "XPOL COOP_00152_12 42CM"


def line(name, **kw):
	base = {
		"name": name,
		"parent": "SO-1",
		"custom_number_of_boxes": 21,
		"custom_length": "42cm",
		"custom_mixed_box": 0,
		"custom_mixed_bunch": 0,
		"custom_mix_group": "",
		"custom_bunch_group": "",
		"custom_mix_name": "",
		"custom_line": "",
	}
	base.update(kw)
	return base


def mono(name, group="1", spec=SPEC, boxes=21):
	return line(
		name,
		custom_mixed_box=1,
		custom_mix_group=group,
		custom_mix_name=spec,
		custom_line=spec,
		custom_number_of_boxes=boxes,
	)


def bunch(name, n, spec=SPEC, boxes=21):
	return line(
		name,
		custom_mixed_bunch=1,
		custom_bunch_group=f"{spec}::{n}",
		custom_mix_name=spec,
		custom_line=spec,
		custom_number_of_boxes=boxes,
	)


class TestBoxGroups(unittest.TestCase):
	def test_mono_and_mixed_bunches_of_a_spec_are_one_box(self):
		lines = [mono("m1"), mono("m2"), bunch("b6a", 6), bunch("b6b", 6), bunch("b7a", 7)]
		keys = box_keys(lines)
		self.assertEqual({keys[n] for n in keys}, {("box", "1")})

	def test_mixed_bunches_of_a_spec_without_mono_are_one_box(self):
		lines = [bunch("f1", 1, "FRAN", 2), bunch("f2", 1, "FRAN", 2), bunch("f3", 2, "FRAN", 2)]
		keys = box_keys(lines)
		self.assertEqual(len(set(keys.values())), 1)
		self.assertEqual(next(iter(keys.values()))[0], "bunch")

	def test_different_specs_and_straight_lines_stay_apart(self):
		lines = [
			bunch("a", 1, "PINK BQT", 3),
			bunch("b", 1, "RED BQT", 3),
			line("s1"),
			line("s2"),
		]
		keys = box_keys(lines)
		self.assertNotEqual(keys["a"], keys["b"])
		self.assertEqual(keys["s1"], ("line", "s1"))
		self.assertEqual(keys["s2"], ("line", "s2"))

	def test_a_box_count_or_length_apart_is_another_box(self):
		lines = [mono("m1", boxes=21), bunch("b", 6, boxes=5)]
		keys = box_keys(lines)
		self.assertEqual(keys["m1"], ("box", "1"))
		self.assertNotEqual(keys["b"], ("box", "1"))

	def test_split_mixes_keep_their_own_groups(self):
		# Add variety splits a mix into two mix groups of the same spec: each is its own box
		lines = [mono("k1", "1", boxes=1), mono("n1", "3", boxes=3)]
		keys = box_keys(lines)
		self.assertEqual(keys["k1"], ("box", "1"))
		self.assertEqual(keys["n1"], ("box", "3"))

	def test_orders_are_matched_apart(self):
		other = dict(bunch("x", 6), parent="SO-2")
		keys = box_keys([mono("m1"), other])
		self.assertNotEqual(keys["x"], ("box", "1"))
