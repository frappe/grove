# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""A row names one tool for one vendor, and the pair is its name."""

import frappe
from frappe.tests import IntegrationTestCase


def denied_tool(provider_name, tool):
	return frappe.get_doc({"doctype": "Denied Tool", "provider_name": provider_name, "tool": tool})


class TestADeniedToolIsOnePairUnderTheVendorsName(IntegrationTestCase):
	def test_the_pair_is_the_name_trimmed(self):
		row = denied_tool(" _T-catalog-vendor ", " web_search_20250305 ").insert()
		self.assertEqual(row.name, "_T-catalog-vendor:web_search_20250305")

	def test_a_blank_tool_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			denied_tool("anthropic", "  ").insert()
