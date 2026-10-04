# Copyright (c) 2026, Frappe and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase


class TestModality(IntegrationTestCase):
	def test_the_name_gets_a_capital_first_letter(self):
		doc = frappe.get_doc({"doctype": "Modality", "name": "speech", "is_output": 1}).insert()
		self.assertEqual(doc.name, "Speech")
