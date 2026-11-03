import datetime
import unittest

from upande_packhouse.api.order_import import (
	fill_po_number,
	guess_spec_by_code,
	parse_import_date,
	row_is_skipped,
	skip_rule,
	spec_candidates_by_code,
)

SPECS = [
	"XPOL TOSCA_02720_10",
	"XPOL TOSCA_02721_10",
	"COOP-04898-01 RED ROSE 10PK 52CM FT",
	"COOP-04898-01 PINK ROSE 10PK 52CM FT",
	"COOP-04898-01 BICOLOR PINK ROSE 10PK 52CM FT",
	"COOP-04898-01 WHITE ROSE 10PK 52CM FT",
	"SAWTMXRMXDO107 PASTEL MIX 52CM",
	"SAWTPASRED0001 RED 52CM",
]


class _Doc(dict):
	def get(self, key, default=None):
		return super().get(key, default)


class TestImportRules(unittest.TestCase):
	def test_skip_rule_reads_column_and_values_case_insensitively(self):
		col, values = skip_rule(
			_Doc(skip_rows_column=" Status ", skip_rows_values="CANCELLED\nvoid, Expired")
		)
		self.assertEqual(col, "Status")
		self.assertEqual(values, {"cancelled", "void", "expired"})
		self.assertTrue(row_is_skipped(" Cancelled ", values))
		self.assertFalse(row_is_skipped("CONFIRMED", values))
		self.assertFalse(row_is_skipped(None, values))

	def test_skip_rule_needs_both_column_and_values(self):
		self.assertEqual(skip_rule(_Doc(skip_rows_column="Status")), (None, set()))
		self.assertEqual(skip_rule(_Doc(skip_rows_values="CANCELLED")), (None, set()))
		self.assertEqual(skip_rule(_Doc()), (None, set()))

	def test_dates_are_day_first_unless_told_otherwise(self):
		self.assertEqual(parse_import_date("09/10/2026"), datetime.date(2026, 10, 9))
		self.assertEqual(parse_import_date("09/10/2026", day_first=False), datetime.date(2026, 9, 10))
		self.assertEqual(parse_import_date("14/09/2026"), datetime.date(2026, 9, 14))
		self.assertEqual(parse_import_date("09/14/2026", day_first=True), datetime.date(2026, 9, 14))
		self.assertEqual(parse_import_date("12/10/26"), datetime.date(2026, 10, 12))
		self.assertEqual(parse_import_date("2026-10-12"), datetime.date(2026, 10, 12))
		with self.assertRaises(ValueError):
			parse_import_date("")

	def test_po_number_template(self):
		self.assertEqual(
			fill_po_number("XPOL {delivery_date}", {"delivery_date": "2026-10-12"}), "XPOL 2026-10-12"
		)
		self.assertIsNone(fill_po_number("", {"delivery_date": "2026-10-12"}))
		self.assertIsNone(fill_po_number(None, {}))


class TestSpecGuessByCode(unittest.TestCase):
	def test_a_single_code_match_wins(self):
		self.assertEqual(
			guess_spec_by_code("TOSCA_02720_10", "Service for Roses RM 9pk", SPECS), "XPOL TOSCA_02720_10"
		)
		self.assertEqual(
			guess_spec_by_code("SAWTPASRED0001", "ROSE LONG STEM RED", SPECS), "SAWTPASRED0001 RED 52CM"
		)

	def test_letter_o_and_zero_are_the_same_in_a_code(self):
		self.assertEqual(
			guess_spec_by_code("SAWTMXRMXD0107", "PAS ROSE BQ PASTEL MIX A", SPECS),
			"SAWTMXRMXDO107 PASTEL MIX 52CM",
		)

	def test_description_words_choose_between_several(self):
		self.assertEqual(
			guess_spec_by_code("COOP_04898_01", "Rosa Red 10pk", SPECS), "COOP-04898-01 RED ROSE 10PK 52CM FT"
		)
		self.assertEqual(
			guess_spec_by_code("COOP_04898_01", "Rosa White 10pk", SPECS),
			"COOP-04898-01 WHITE ROSE 10PK 52CM FT",
		)

	def test_a_tie_or_no_clue_is_left_for_the_person(self):
		self.assertIsNone(
			guess_spec_by_code("COOP_04898_01", "Rosa Pink 10pk", SPECS)
		)  # PINK and BICOLOR PINK both contain it
		self.assertIsNone(guess_spec_by_code("COOP_04898_01", "10pk", SPECS))

	def test_unknown_or_missing_codes_match_nothing(self):
		self.assertIsNone(guess_spec_by_code("NOPE_99999", "Rosa Red", SPECS))
		self.assertIsNone(guess_spec_by_code("", "Rosa Red", SPECS))
		self.assertIsNone(guess_spec_by_code("AB", "Rosa Red", SPECS))


class TestSpecCandidates(unittest.TestCase):
	def test_every_spec_with_the_code_is_offered_best_description_match_first(self):
		out = spec_candidates_by_code("COOP_04898_01", "Rosa Pink 10pk", SPECS)
		self.assertEqual(len(out), 4)
		self.assertEqual(
			set(out[:2]),
			{"COOP-04898-01 PINK ROSE 10PK 52CM FT", "COOP-04898-01 BICOLOR PINK ROSE 10PK 52CM FT"},
		)
		self.assertNotIn("COOP-04898-01 RED ROSE 10PK 52CM FT", out[:2])

	def test_a_single_code_match_is_still_offered(self):
		self.assertEqual(spec_candidates_by_code("SAWTPASRED0001", "x", SPECS), ["SAWTPASRED0001 RED 52CM"])

	def test_nothing_without_a_usable_code(self):
		self.assertEqual(spec_candidates_by_code("", "Rosa Red", SPECS), [])
		self.assertEqual(spec_candidates_by_code("NOPE_99999", "Rosa Red", SPECS), [])
		self.assertEqual(spec_candidates_by_code("AB", "Rosa Red", SPECS), [])
