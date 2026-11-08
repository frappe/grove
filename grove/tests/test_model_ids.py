# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""A model id is `<provider>/<name>`, and that is the only id served.

The bare form is deliberately broken rather than kept as an alias: an id that does not say who serves
it is one nobody can read, and two spellings for one model would have to be carried by the route
table, the grant projection AND every engine's --served-model-name to stay consistent.

The models that predated this were renamed in place on the one control-plane site, so there is no
migration to test here — `autoname` runs at insert and is the whole rule.
"""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.model_id.model_id import ensure_model_id
from grove.grove.doctype.model_provider.model_provider import self_hosted_provider
from grove.grove.doctype.model_provider.test_model_provider import provider


class TestTheIdIsAlwaysPrefixed(IntegrationTestCase):
	"""Site-backed: autoname is the whole behaviour, so a real insert is the only honest check.
	IntegrationTestCase, not unittest.TestCase — it wraps each test in a transaction and rolls back,
	which is what keeps these probes out of the site."""

	def model(self, model_id, provider=None):
		doc = frappe.get_doc(
			{
				"doctype": "Model",
				"model_id": ensure_model_id(model_id),
				"hf_repo": "probe/Repo",
				"provider": provider,
			}
		)
		doc.insert()
		return doc

	def test_our_own_models_are_keyed_under_the_self_hosted_provider(self):
		doc = self.model("Ponytail Probe 7B")
		self.assertEqual(f"{self_hosted_provider()}/ponytail-probe-7b", doc.model_key)
		self.assertNotIn("/", doc.name)
		self.assertEqual("ponytail-probe-7b", doc.model_id)
		self.assertEqual(self_hosted_provider(), doc.provider)

	def test_a_third_party_model_is_keyed_under_its_vendor(self):
		vendor = provider("_T-probe-anthropic").insert()
		doc = self.model("Claude Sonnet 4.5", provider=vendor.name)
		self.assertEqual("_T-probe-anthropic/claude-sonnet-4.5", doc.model_key)
		self.assertEqual(vendor.geography, doc.geography)
		self.assertEqual("claude-sonnet-4.5", doc.model_id)

	def test_the_provider_name_is_read_off_the_provider_not_stored(self):
		vendor = provider("_T-probe-unstored").insert()
		doc = self.model("Unstored 7B", provider=vendor.name)
		self.assertFalse(frappe.db.has_column("Model", "provider_name"))
		self.assertEqual("_T-probe-unstored", frappe.get_doc("Model", doc.name).provider_name)

	def test_a_blank_provider_still_gets_a_prefix(self):
		doc = self.model("No Provider Named", provider="")
		self.assertTrue(doc.model_key.startswith(f"{self_hosted_provider()}/"))

	def test_the_id_cannot_be_edited_afterwards(self):
		doc = self.model("Before Rename 7B")
		doc.model_id = ensure_model_id("after-rename-7b")
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()

	def test_the_key_cannot_be_edited_afterwards(self):
		doc = self.model("Keyed 7B")
		doc.model_key = f"{self_hosted_provider()}/other-7b"
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()

	def test_one_key_is_a_doc_per_provider_record(self):
		first = provider("_T-probe-twice").insert()
		second = provider("_T-probe-twice", geography=make_test_geography("test-2")).insert()
		here = self.model("Twice 7B", provider=first.name)
		there = frappe.get_doc(
			{
				"doctype": "Model",
				"model_id": "twice-7b",
				"provider": second.name,
				"upstream_model_id": "twice-7b-eu",
			}
		).insert()
		self.assertEqual(here.model_key, there.model_key)
		self.assertNotEqual(here.name, there.name)
		self.assertEqual((here.geography, there.geography), (first.geography, second.geography))
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc({"doctype": "Model", "model_id": "twice-7b", "provider": second.name}).insert()


class TestAModelSaysWhereItsWeightsCome(IntegrationTestCase):
	def test_a_model_with_no_repo_is_refused(self):
		doc = frappe.get_doc({"doctype": "Model", "model_id": ensure_model_id("no-repo-7b")})
		with self.assertRaises(frappe.MandatoryError):
			doc.insert()


class TestProviderNames(IntegrationTestCase):
	"""The provider name is the prefix of every model id under it and the vendor type the gateway
	dials it as, so it is picked from the vendors pathway knows, never typed."""

	def test_a_name_pathway_does_not_know_is_refused(self):
		for bad in ("OpenAI", "open-ai", "vertex-ai", "Bad Name Inc"):
			with self.subTest(bad), self.assertRaises(frappe.ValidationError):
				provider(bad).insert()

	def test_a_known_vendor_is_fine(self):
		doc = provider("openai").insert()
		self.assertEqual("openai", doc.provider_name)

	def test_the_self_hosted_provider_ships_with_the_app(self):
		self.assertTrue(self_hosted_provider())


if __name__ == "__main__":
	unittest.main()
