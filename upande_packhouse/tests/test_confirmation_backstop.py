from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.doctype.allocation_confirmation import allocation_confirmation as ac

# Allocation Confirmation backstop: a line is packed by one team, and what a line has confirmed (not yet allocated)
# plus what it has allocated never passes what it ordered -- whatever path saves the confirmation. Mocked database.


def _conf(stems, team="Eldama A", fulfilled=0, name="NEW"):
	doc = frappe.get_doc(
		{
			"doctype": "Allocation Confirmation",
			"sales_order_item": "SOI-1",
			"status": "Planned",
			"stems": stems,
			"fulfilled_stems": fulfilled,
			"team": team,
		}
	)
	doc.name = name
	return doc


def _check(doc, others, ordered=200, allocated=0):
	with (
		patch.object(ac.frappe, "get_all", return_value=[SimpleNamespace(**o) for o in others]),
		patch.object(ac.frappe.db, "get_value", return_value=ordered),
		patch.object(ac.frappe.db, "sql", return_value=[(allocated,)]),
	):
		doc.validate_one_team_per_line()
		doc.validate_not_above_order()


class UnitTestConfirmationBackstop(UnitTestCase):
	def test_a_line_confirmed_up_to_its_order_passes(self):
		_check(_conf(120), [dict(name="C1", team="Eldama A", stems=80, fulfilled_stems=0)])

	def test_confirming_again_on_top_is_refused(self):
		# the 4x case: the same 200 confirmed a second time beside the first
		with self.assertRaises(frappe.ValidationError) as e:
			_check(_conf(200), [dict(name="C1", team="Eldama A", stems=200, fulfilled_stems=0)])
		self.assertIn("for an order line of 200", str(e.exception))

	def test_allocated_stems_count_against_the_order(self):
		with self.assertRaises(frappe.ValidationError):
			_check(_conf(100), [], allocated=150)
		_check(_conf(50), [], allocated=150)

	def test_a_second_team_on_the_same_line_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as e:
			_check(_conf(50, team="Jamafa"), [dict(name="C1", team="Eldama A", stems=50, fulfilled_stems=0)])
		self.assertIn("packed by one team", str(e.exception))

	def test_released_or_fulfilled_confirmations_do_not_count(self):
		# a revision: the earlier confirmation was released before this one is saved, so it is not live
		_check(_conf(200), [])
