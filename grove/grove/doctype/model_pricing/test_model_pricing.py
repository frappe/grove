# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Enabling a pricing retires its predecessor, and it is never edited after: the requests it
charged are billed."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.pricing import COUNTERS, PriceBook
from grove.utils import utc_today


def new_pricing(model, status="Disabled", **rates):
	return frappe.get_doc({
		"doctype": "Model Pricing", "model": model, "status": status,
		"rates": [{"counter": counter, "rate": rate} for counter, rate in rates.items()],
	}).insert(ignore_permissions=True)


def enabled_pricing(model, **rates):
	return new_pricing(model, status="Enabled", **rates)


class TestModelPricing(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.model = frappe.get_doc(
			{"doctype": "Model", "model_id": "pricing-7b", "modality": "text", "hf_repo": "org/pricing-7b"}
		).insert(ignore_permissions=True).name

	def test_a_draft_is_editable_until_it_is_enabled(self):
		doc = new_pricing(self.model, completion_tokens=1)
		doc.rates[0].rate = 5
		doc.save()
		self.assertIsNone(doc.enabled_on)
		doc.status = "Enabled"
		doc.save()
		self.assertEqual(doc.enabled_on, utc_today())

	def test_enabling_retires_the_predecessor(self):
		first = enabled_pricing(self.model, completion_tokens=1)
		doc = enabled_pricing(self.model, completion_tokens=2)
		first.reload()
		self.assertEqual((doc.status, doc.enabled_on, first.status), ("Enabled", utc_today(), "Disabled"))

	def test_zero_rates_are_a_pricing_and_never_publish_the_model(self):
		doc = enabled_pricing(self.model, **dict.fromkeys(COUNTERS, 0))
		self.assertEqual(PriceBook.load().nano_rates(doc.name), dict.fromkeys(COUNTERS, 0))
		self.assertEqual(frappe.db.get_value("Model", self.model, "published"), 0)

	def test_a_retired_pricing_stays_in_the_book_by_id(self):
		first = enabled_pricing(self.model, completion_tokens=2)
		enabled_pricing(self.model, completion_tokens=3)
		self.assertEqual(PriceBook.load().nano_rates(first.name), {"completion_tokens": 2_000_000_000})

	def test_an_enabled_pricing_is_frozen(self):
		doc = enabled_pricing(self.model, completion_tokens=1)
		doc.status = "Disabled"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		frappe.db.set_value("Model Pricing", doc.name, "status", "Disabled")
		doc.reload()
		doc.status = "Enabled"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc.reload()
		doc.rates[0].rate = 9
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_a_duplicate_is_a_disabled_draft_with_its_own_window(self):
		# What the form's Duplicate does: no_copy fields are left behind.
		first = enabled_pricing(self.model, completion_tokens=1)
		doc = frappe.copy_doc(first, ignore_no_copy=False).insert(ignore_permissions=True)
		first.reload()
		self.assertEqual((doc.status, doc.enabled_on, first.status), ("Disabled", None, "Enabled"))
		doc.status = "Enabled"
		doc.save()
		self.assertEqual(doc.enabled_on, utc_today())

	def test_a_counter_missing_from_the_pricing_is_unpriced_whatever_the_cost_card_says(self):
		provider = frappe.get_doc("Model", self.model).provider
		provider_doc = frappe.get_doc("Model Provider", provider)
		provider_doc.append("rate_card", {"provider_model_id": "pricing-7b", "counter": "input_tokens", "rate": 0.5})
		provider_doc.save(ignore_permissions=True)
		doc = enabled_pricing(self.model, completion_tokens=1)
		self.assertEqual(PriceBook.load().nano_rates(doc.name), {"completion_tokens": 1_000_000_000})
