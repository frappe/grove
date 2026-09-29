# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Prices per counter. Pure — the rate rows are handed to the book, no site."""

import unittest
from decimal import Decimal

import frappe

from grove import pricing
from grove.pricing import PriceBook

D = Decimal


def book(models=(), pricings=None):
	b = PriceBook()
	b.models = set(models)
	b.pricings = {
		name: frappe._dict(model=model, rates={c: D(r) for c, r in rates.items()})
		for name, (model, rates) in (pricings or {}).items()
	}
	return b


class TestPriceBook(unittest.TestCase):
	RATES = {"input_tokens": "3", "cached_tokens": "0.3", "cache_write_tokens": "3.75",
	         "cache_write_1h_tokens": "6", "completion_tokens": "15", "audio_tokens": "40"}

	def priced(self):
		return book(pricings={"p1": ("anthropic/claude", self.RATES)})

	def test_a_million_prompt_tokens_split_across_the_cache_counters(self):
		counts = {"input_tokens": 400_000, "cached_tokens": 400_000, "cache_write_tokens": 100_000,
		          "cache_write_1h_tokens": 100_000, "completion_tokens": 500_000}
		self.assertEqual(self.priced().pricing_cost("p1", counts), D("9.795"))

	def test_audio_is_priced_per_mtok_and_a_counter_with_no_rate_bills_zero(self):
		counts = {"audio_tokens": 500_000, "audio_seconds": 90, "request_count": 4}
		self.assertEqual(self.priced().pricing_cost("p1", counts), D("20"))

	def test_rates_for_the_push_are_whole_nano_usd(self):
		b = book(pricings={"p1": ("m", {"completion_tokens": "0.3", "request_count": "0"})})
		self.assertEqual(b.nano_rates("p1"), {"completion_tokens": 300_000_000, "request_count": 0})

	def test_decimal_arithmetic_lands_exactly_on_zero(self):
		b = book(pricings={"p1": ("m", {"completion_tokens": "0.075"})})
		self.assertEqual(D("225") - b.pricing_cost("p1", {"completion_tokens": 3_000_000_000}), D("0"))

	def test_an_unknown_pricing_raises(self):
		with self.assertRaises(frappe.ValidationError):
			self.priced().pricing_cost("p2", {"completion_tokens": 1})


class TestMoneyUnits(unittest.TestCase):
	def test_nano_reads_a_float_as_its_decimal_text(self):
		self.assertEqual(pricing.nano(0.3), 300_000_000)
		self.assertEqual(pricing.nano(D("1.000000001")), 1_000_000_001)

	def test_tolerance_is_one_nano_per_counter_per_request(self):
		self.assertEqual(pricing.tolerance(3), D("0.000000021"))


class TestValidatePriceRows(unittest.TestCase):
	def rows(self, *specs):
		return [frappe._dict(idx=i + 1, counter=c, rate=r) for i, (c, r) in enumerate(specs)]

	def test_a_duplicate_counter_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			pricing.validate_price_rows(self.rows(("completion_tokens", 1), ("completion_tokens", 2)), key=lambda r: r.counter)

	def test_a_negative_rate_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			pricing.validate_price_rows(self.rows(("completion_tokens", -1)), key=lambda r: r.counter)

	def test_a_blank_rate_is_refused_but_zero_is_a_price(self):
		with self.assertRaises(frappe.ValidationError):
			pricing.validate_price_rows(self.rows(("completion_tokens", None)), key=lambda r: r.counter)
		pricing.validate_price_rows(self.rows(("completion_tokens", 0)), key=lambda r: r.counter)


if __name__ == "__main__":
	unittest.main()
