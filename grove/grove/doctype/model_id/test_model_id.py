# Copyright (c) 2026, Frappe and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.model_id.model_id import ensure_model_id


class TestModelID(IntegrationTestCase):
	def test_the_id_is_slugified_and_is_the_name(self):
		doc = frappe.get_doc({"doctype": "Model ID", "model_id": "Claude Sonnet 4.5"}).insert()
		self.assertEqual(("claude-sonnet-4.5", "claude-sonnet-4.5"), (doc.name, doc.model_id))

	def test_nothing_sluggable_and_a_slash_are_refused(self):
		for bad in ("   ", "Meta/Llama 3"):
			with self.subTest(bad), self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Model ID", "model_id": bad}).insert()

	def test_ensure_holds_one_record_per_id(self):
		name = ensure_model_id("Probe Ensured 7B")
		self.assertEqual("probe-ensured-7b", name)
		self.assertEqual(name, ensure_model_id("probe-ensured-7b"))
		self.assertEqual(1, frappe.db.count("Model ID", {"name": name}))

	def test_ensure_fills_a_blank_display_name_and_keeps_a_set_one(self):
		frappe.clear_messages()
		name = ensure_model_id("Probe Named 7B")
		self.assertEqual([], frappe.get_message_log())
		ensure_model_id("Probe Named 7B", "Probe Named")
		ensure_model_id("Probe Named 7B", "Renamed")
		self.assertEqual("Probe Named", frappe.db.get_value("Model ID", name, "display_name"))
