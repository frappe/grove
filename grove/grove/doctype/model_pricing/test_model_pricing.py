# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""A pricing is scheduled, fires at its Activates At, and is never edited after: the requests it
charged are billed. A Scheduled one stays editable until the lead time before it fires."""

import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from grove.grove.doctype.model_pricing import model_pricing
from grove.pricing import PriceBook
from grove.utils import utc_today

LEAD = 10


def set_lead(minutes=LEAD):
	frappe.db.set_single_value("Grove Settings", "pricing_lead_minutes", minutes)


def scheduled_pricing(model, minutes=LEAD + 1, **rates):
	set_lead()
	return frappe.get_doc({
		"doctype": "Model Pricing", "model": model, "status": "Scheduled",
		"activates_at": add_to_date(now_datetime(), minutes=minutes),
		"rates": [{"counter": counter, "rate": rate} for counter, rate in rates.items()],
	}).insert(ignore_permissions=True)


def enabled_pricing(model, **rates):
	"""A pricing taken through its schedule to Enabled — the only way one gets there."""
	doc = scheduled_pricing(model, **rates)
	frappe.db.set_value("Model Pricing", doc.name, "activates_at", add_to_date(now_datetime(), minutes=-1))
	doc.reload()
	doc.status = "Enabled"
	doc.save(ignore_permissions=True)
	return doc


class TestModelPricing(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.model = frappe.get_doc(
			{"doctype": "Model", "model_id": "pricing-7b", "modality": "text", "hf_repo": "org/pricing-7b"}
		).insert(ignore_permissions=True).name

	def scheduled(self, minutes=LEAD + 1, **rates):
		"""One Scheduled doc per model, so each test's is cancelled behind it."""
		doc = scheduled_pricing(self.model, minutes=minutes, **rates)
		self.addCleanup(frappe.db.set_value, "Model Pricing", doc.name, "status", "Disabled")
		return doc

	def fire_due(self):
		with unittest.mock.patch.object(frappe.db, "commit"):
			model_pricing.enable_due()

	def test_a_pricing_is_never_enabled_directly(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Model Pricing", "model": self.model, "status": "Enabled",
				"rates": [{"counter": "completion_tokens", "rate": 1}],
			}).insert(ignore_permissions=True)

	def test_scheduling_needs_the_lead_time_and_a_lead_to_be_set(self):
		with self.assertRaises(frappe.ValidationError):
			self.scheduled(minutes=LEAD - 1, completion_tokens=1)
		set_lead(0)
		self.addCleanup(set_lead)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Model Pricing", "model": self.model, "status": "Scheduled",
				"activates_at": add_to_date(now_datetime(), days=1), "rates": [{"counter": "completion_tokens", "rate": 1}],
			}).insert(ignore_permissions=True)

	def test_one_scheduled_per_model(self):
		self.scheduled(completion_tokens=1)
		with self.assertRaises(frappe.ValidationError):
			self.scheduled(minutes=LEAD + 5, completion_tokens=2)

	def test_a_scheduled_pricing_is_editable_until_the_lead_before_it_fires(self):
		doc = self.scheduled(completion_tokens=1)
		doc.rates[0].rate = 5
		doc.save()
		frappe.db.set_value("Model Pricing", doc.name, "activates_at", add_to_date(now_datetime(), minutes=LEAD - 1))
		doc.reload()
		doc.rates[0].rate = 6
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_a_scheduled_pricing_is_in_the_book_by_id(self):
		doc = self.scheduled(completion_tokens=2)
		book = PriceBook.load()
		self.assertEqual(book.nano_rates(doc.name), {"completion_tokens": 2_000_000_000})

	def test_cancelling_a_schedule_clears_its_window(self):
		doc = self.scheduled(completion_tokens=1)
		doc.status = "Disabled"
		doc.save()
		self.assertIsNone(doc.activates_at)

	def test_a_due_schedule_fires_and_retires_the_predecessor(self):
		first = enabled_pricing(self.model, completion_tokens=1)
		doc = self.scheduled(completion_tokens=2)
		frappe.db.set_value("Model Pricing", doc.name, "activates_at", add_to_date(now_datetime(), minutes=-1))
		self.fire_due()
		doc.reload()
		first.reload()
		self.assertEqual((doc.status, doc.enabled_on, first.status), ("Enabled", utc_today(), "Disabled"))

	def test_a_schedule_not_yet_due_waits_and_cannot_be_enabled_by_hand(self):
		doc = self.scheduled(completion_tokens=1)
		self.fire_due()
		doc.reload()
		self.assertEqual(doc.status, "Scheduled")
		doc.status = "Enabled"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

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

	def test_a_counter_missing_from_the_pricing_is_unpriced_whatever_the_cost_card_says(self):
		provider = frappe.get_doc("Model", self.model).provider
		provider_doc = frappe.get_doc("Model Provider", provider)
		provider_doc.append("rate_card", {"provider_model_id": "pricing-7b", "counter": "input_tokens", "rate": 0.5})
		provider_doc.save(ignore_permissions=True)
		doc = enabled_pricing(self.model, completion_tokens=1)
		self.assertEqual(PriceBook.load().nano_rates(doc.name), {"completion_tokens": 1_000_000_000})
