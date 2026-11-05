# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""One record per id, slugified once at insert; a Model links one and cannot name an id the site
has no record of."""

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

	def test_a_model_cannot_name_an_id_the_site_has_no_record_of(self):
		model = {"doctype": "Model", "model_id": "never-made-7b", "hf_repo": "probe/Repo"}
		with self.assertRaises(frappe.LinkValidationError):
			frappe.get_doc(model).insert()
