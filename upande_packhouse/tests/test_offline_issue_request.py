from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api import offline_issue

# Issuing from the cold store (incl. after a bucket replace) calls the issuing scan's
# endpoint with a stand-in request. The stock posting it makes runs Server Scripts,
# which read frappe.request.path: the stand-in must carry the real request's path.


class UnitTestOfflineIssueRequest(UnitTestCase):
	def test_stand_in_keeps_the_real_request(self):
		real = SimpleNamespace(path="/api/method/x", method="POST")
		req = offline_issue._Request({"a": 1}, real)
		self.assertEqual(req.get_json(), {"a": 1})
		self.assertEqual(req.path, "/api/method/x")
		self.assertEqual(req.method, "POST")

	def test_stand_in_without_a_request_has_an_empty_path(self):
		self.assertEqual(offline_issue._Request({}).path, "")

	def test_endpoint_sees_the_body_and_the_path(self):
		seen = {}

		def endpoint():
			seen["body"] = frappe.local.request.get_json()
			seen["path"] = frappe.local.request.path
			frappe.local.response["message"] = "ok"

		real = SimpleNamespace(path="/api/method/mark_issued_offline")
		with patch.object(frappe.local, "request", real, create=True):
			status, message, _data = offline_issue._call(endpoint, {"bucket": "B1"})
			self.assertIs(frappe.local.request, real)
		self.assertEqual((status, message), (200, "ok"))
		self.assertEqual(seen, {"body": {"bucket": "B1"}, "path": "/api/method/mark_issued_offline"})
