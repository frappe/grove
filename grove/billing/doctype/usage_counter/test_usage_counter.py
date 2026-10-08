# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The counter table: what a row may say, that it never changes, and that the catalog seeds
today's twelve. The arithmetic over a table is `grove.tests.test_pricing`."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.billing.pricing import CounterTable


def counter(name, **fields):
	return frappe.get_doc({"doctype": "Usage Counter", "counter_name": name, **fields}).insert()


class TestUsageCounter(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_the_catalog_seeds_todays_twelve(self):
		table = CounterTable.load()
		self.assertEqual(len(table.names), 12)
		self.assertEqual(table.base("prompt_tokens_above_272k"), "prompt_tokens")
		self.assertEqual(table.parts("prompt_tokens_above_272k"), ["cached_tokens_above_272k", "cache_write_tokens_above_272k"])
		self.assertEqual(frappe.db.get_value("Usage Counter", "prompt_tokens_above_272k", "unit"), "Mtok")

	def test_a_derived_counter_takes_its_base_unit_and_names_a_threshold(self):
		doc = counter("prompt_tokens_above_500k", base_counter="prompt_tokens", min_prompt_tokens=500_000)
		self.assertEqual(doc.unit, "Mtok")
		with self.assertRaises(frappe.ValidationError):
			counter("completion_tokens_above_x", base_counter="completion_tokens")
		with self.assertRaises(frappe.ValidationError):
			counter("twice_derived", base_counter="prompt_tokens_above_272k", min_prompt_tokens=1)
		with self.assertRaises(frappe.ValidationError):
			counter("derived_part", base_counter="cached_tokens", min_prompt_tokens=1, part_of="prompt_tokens")

	def test_a_root_needs_a_unit_and_a_root_container(self):
		with self.assertRaises(frappe.ValidationError):
			counter("web_fetch_requests")
		with self.assertRaises(frappe.ValidationError):
			counter("stray", unit="Mtok", min_prompt_tokens=5)
		with self.assertRaises(frappe.ValidationError):
			counter("stray", unit="Mtok", part_of="prompt_tokens_above_272k")
		with self.assertRaises(frappe.ValidationError):
			counter("Web-Search", unit="request")
		doc = counter("web_search_requests", unit="request", label="Web searches")
		self.assertEqual(CounterTable.load().divisor(doc.name), 1)

	def test_a_counter_never_changes_once_it_exists(self):
		doc = counter("web_search_requests", unit="request")
		doc.label = "Searches"
		doc.save()
		doc.unit = "Mtok"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_a_counter_a_record_names_stays(self):
		doc = counter("web_search_requests", unit="request")
		record = frappe.get_doc({
			"doctype": "Usage Record", "api_key": "k", "team": "t", "day": "2026-10-02", "drain_id": "d",
			"billed": 1, "request_count": 1, "cost": 0, "gateway_cost": 0,
			"usage": '[{"model": "m", "web_search_requests": 2}]',
		})
		record.flags.ignore_links = True
		record.insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			doc.delete()

	def test_an_incomplete_bracket_is_refused_at_the_push_not_on_save(self):
		counter("cached_tokens_above_500k", base_counter="cached_tokens", min_prompt_tokens=500_000)
		with self.assertRaises(frappe.ValidationError):
			CounterTable.load().validate()
		counter("prompt_tokens_above_500k", base_counter="prompt_tokens", min_prompt_tokens=500_000)
		counter("cache_write_tokens_above_500k", base_counter="cache_write_tokens", min_prompt_tokens=500_000)
		table = CounterTable.load().validate()
		self.assertEqual(table.parts("prompt_tokens_above_500k"), ["cached_tokens_above_500k", "cache_write_tokens_above_500k"])
