# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""A row names one tool for one vendor, under the vendor's Provider Name spelling, and only once.

Site-backed: the name is the pair, and a second row of the same pair is the insert that refuses it.
"""

import frappe
from frappe.tests import IntegrationTestCase


def denied_tool(provider_name, tool):
	return frappe.get_doc({"doctype": "Denied Tool", "provider_name": provider_name, "tool": tool})


class TestADeniedToolIsOnePairUnderTheVendorsName(IntegrationTestCase):
	def test_the_pair_is_the_name_trimmed_and_listed_once(self):
		row = denied_tool(" catalog-vendor ", " web_search_20250305 ").insert()
		self.assertEqual(row.name, "catalog-vendor:web_search_20250305")
		with self.assertRaises(frappe.DuplicateEntryError):
			denied_tool("catalog-vendor", "web_search_20250305").insert()

	def test_a_name_no_provider_record_can_carry_is_refused(self):
		for name in ("Anthropic", "open ai", "", "a--b"):
			with self.subTest(name), self.assertRaises(frappe.ValidationError):
				denied_tool(name, "web_search_20250305").insert()

	def test_a_blank_tool_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			denied_tool("catalog-vendor", "  ").insert()
