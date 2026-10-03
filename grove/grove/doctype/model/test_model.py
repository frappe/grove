# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""Published is the operator's tick: it needs a pricing and something serving the model, and
only ever comes off on its own."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.catalog import seed
from grove.grove.doctype.grove_user.grove_user import register_user
from grove.grove.doctype.model import model as model_module
from grove.billing.doctype.model_pricing.test_model_pricing import enabled_pricing


class TestPublishing(IntegrationTestCase):
	counter = 0

	def model(self):
		TestPublishing.counter += 1
		model_id = f"publish-{TestPublishing.counter}"
		return frappe.get_doc(
			{"doctype": "Model", "model_id": model_id, "modality": "text", "hf_repo": f"org/{model_id}"}
		).insert(ignore_permissions=True)

	def served(self, is_served=True):
		return patch.object(model_module, "has_vendor_front", return_value=is_served)

	def test_a_model_is_born_unpublished(self):
		self.assertEqual(self.model().published, 0)

	def test_publishing_refuses_an_unpriced_model(self):
		doc = self.model()
		doc.published = 1
		with self.served(), self.assertRaisesRegex(frappe.ValidationError, "no Enabled Model Pricing"):
			doc.save()

	def test_publishing_refuses_a_model_nothing_serves(self):
		doc = self.model()
		enabled_pricing(doc.name, completion_tokens=0)
		doc.published = 1
		with self.served(False), self.assertRaisesRegex(frappe.ValidationError, "nothing serving it"):
			doc.save()

	def test_a_priced_served_model_publishes_by_hand_only(self):
		doc = self.model()
		enabled_pricing(doc.name, completion_tokens=0)
		with self.served():
			model_module.sync_published(doc.name)
			self.assertEqual(frappe.db.get_value("Model", doc.name, "published"), 0)
			doc.published = 1
			doc.save()
		self.assertEqual(frappe.db.get_value("Model", doc.name, "published"), 1)

	def test_a_model_nothing_serves_any_more_is_unpublished(self):
		doc = self.model()
		frappe.db.set_value("Model", doc.name, "published", 1)
		with self.served(False):
			model_module.sync_published(doc.name)
		self.assertEqual(frappe.db.get_value("Model", doc.name, "published"), 0)


class TestGranted(IntegrationTestCase):
	"""A route is not a grant: a published model no Allow or peopled Model Group names reaches no key."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.model = frappe.get_doc(
			{"doctype": "Model", "model_id": "probe-granted", "modality": "text", "hf_repo": "org/probe-granted"}
		).insert(ignore_permissions=True).name
		cls.group = frappe.get_doc({"doctype": "Model Group", "__newname": "probe-granted-group"}).insert().name
		cls.user = frappe.get_doc(
			{"doctype": "Grove User", "user": register_user("probe-granted@example.com")}
		).insert(ignore_permissions=True).name

	def is_granted(self):
		return frappe.get_doc("Model", self.model).is_granted

	def test_a_grant_is_an_allow_or_a_group_with_someone_in_it(self):
		# One walk: the class shares its state between tests.
		self.assertFalse(self.is_granted())
		user = frappe.get_doc("Grove User", self.user)
		user.append("deny", {"model": self.model})
		user.save()
		self.assertFalse(self.is_granted(), "a deny is not a grant")
		group = frappe.get_doc("Model Group", self.group)
		group.append("models", {"model": self.model})
		group.save()
		self.assertFalse(self.is_granted(), "a group nobody is in grants nothing")
		user.append("model_groups", {"model_group": self.group})
		user.save()
		self.assertTrue(self.is_granted())
		user.model_groups, user.deny = [], []
		user.append("allow", {"model": self.model})
		user.save()
		self.assertTrue(self.is_granted(), "the user's own allow")


class TestLoadPricing(IntegrationTestCase):
	RATES = [{"counter": "prompt_tokens", "rate": 2.5}, {"counter": "completion_tokens", "rate": 15}]

	def model(self, model_id):
		return frappe.get_doc(
			{"doctype": "Model", "model_id": model_id, "modality": "text", "hf_repo": f"org/{model_id}"}
		).insert(ignore_permissions=True)

	def test_the_catalogs_rates_land_as_one_disabled_draft(self):
		doc = self.model("catalog-priced-7b")
		with patch.object(seed, "get_rates", return_value=self.RATES):
			self.assertTrue(doc.has_catalog_pricing)
			name = doc.load_pricing()
			self.assertIsNone(doc.load_pricing())
			self.assertFalse(doc.has_catalog_pricing)
		pricing = frappe.get_doc("Model Pricing", name)
		self.assertEqual(
			(pricing.status, [(row.counter, row.rate) for row in pricing.rates]),
			("Disabled", [("prompt_tokens", 2.5), ("completion_tokens", 15)]),
		)
		self.assertEqual(frappe.db.count("Model Pricing", {"model": doc.name}), 1)

	def test_a_model_the_catalog_does_not_price_throws(self):
		doc = self.model("catalog-unpriced-7b")
		self.assertFalse(doc.has_catalog_pricing)
		with self.assertRaisesRegex(frappe.ValidationError, "no pricing"):
			doc.load_pricing()
