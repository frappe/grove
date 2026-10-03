# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Prices per counter. Pure — the rate rows are handed to the book, the counter table read off
the catalog, no site."""

import unittest
from decimal import Decimal

import frappe

from grove import pricing
from grove.catalog import seed
from grove.pricing import CounterTable, PriceBook

D = Decimal

ROOT = {"unit": "Mtok"}


def table(*extra):
	"""The catalog's twelve, plus `extra` rows."""
	return CounterTable([*seed.read()["counters"], *extra])


def book(models=(), pricings=None, counters=None):
	b = PriceBook()
	b.models = set(models)
	b.counters = counters or table()
	b.pricings = {
		name: frappe._dict(model=model, rates={c: D(r) for c, r in rates.items()})
		for name, (model, rates) in (pricings or {}).items()
	}
	return b


class TestPriceBook(unittest.TestCase):
	RATES = {"prompt_tokens": "3", "cached_tokens": "0.3", "cache_write_tokens": "3.75",
	         "cache_write_1h_tokens": "6", "completion_tokens": "15", "audio_tokens": "40"}

	def priced(self):
		return book(pricings={"p1": ("anthropic/claude", self.RATES)})

	def test_the_prompt_rate_charges_what_the_cache_counters_left(self):
		counts = {"prompt_tokens": 1_000_000, "cached_tokens": 400_000, "cache_write_tokens": 100_000,
		          "cache_write_1h_tokens": 100_000, "completion_tokens": 500_000}
		self.assertEqual(self.priced().pricing_cost("p1", counts), D("9.795"))
		# Parts past the prompt charge no prompt: only the cached 50 bill.
		self.assertEqual(self.priced().pricing_cost("p1", {"prompt_tokens": 10, "cached_tokens": 50}), D("0.000015"))

	def test_audio_output_comes_out_of_the_completion(self):
		counts = {"completion_tokens": 500_000, "completion_audio_tokens": 100_000}
		self.assertEqual(self.priced().pricing_cost("p1", counts), D("6"))
		priced = book(pricings={"p1": ("openai/gpt-audio", self.RATES | {"completion_audio_tokens": "80"})})
		self.assertEqual(priced.pricing_cost("p1", counts), D("14"))

	def test_a_drain_of_a_short_and_a_long_request_prices_as_the_two_apart(self):
		priced = book(pricings={"p1": ("anthropic/claude", self.RATES | {"prompt_tokens_above_272k": "6"})})
		short = {"prompt_tokens": 100_000, "cached_tokens": 60_000, "cache_write_1h_tokens": 10_000,
		         "completion_tokens": 1000}
		# 320k prompt: its 10k hour writes are counted with the base prompt, the rest above 272k.
		long = {"prompt_tokens": 10_000, "cache_write_1h_tokens": 10_000, "prompt_tokens_above_272k": 310_000,
		        "cached_tokens_above_272k": 280_000, "cache_write_tokens_above_272k": 20_000,
		        "completion_tokens_above_272k": 1000}
		drain = {counter: short.get(counter, 0) + long.get(counter, 0) for counter in short | long}
		self.assertEqual(priced.pricing_cost("p1", short), D("0.183"))
		self.assertEqual(priced.pricing_cost("p1", long), D("0.294"))
		self.assertEqual(priced.pricing_cost("p1", drain), D("0.477"))

	def test_audio_is_priced_per_mtok_and_a_counter_with_no_rate_bills_zero(self):
		counts = {"audio_tokens": 500_000, "audio_seconds": 90, "request_count": 4}
		self.assertEqual(self.priced().pricing_cost("p1", counts), D("20"))

	def test_amounts_charged_above_272k_are_priced_at_those_rates(self):
		rates = {"prompt_tokens": "2.5", "cached_tokens": "0.25", "completion_tokens": "15",
		         "prompt_tokens_above_272k": "5", "cached_tokens_above_272k": "0.5",
		         "cache_write_tokens_above_272k": "6.25", "completion_tokens_above_272k": "22.5"}
		counts = {"prompt_tokens_above_272k": 300_000, "cached_tokens_above_272k": 280_000,
		          "completion_tokens_above_272k": 1000}
		priced = book(pricings={"p1": ("openai/gpt", rates)})
		self.assertEqual(priced.pricing_cost("p1", counts), D("0.2625"))
		self.assertEqual(priced.pricing_cost("p1", {"cache_write_tokens_above_272k": 80_000}), D("0.5"))

	def test_an_above_272k_counter_with_no_rate_bills_at_its_base_rate(self):
		rates = {"prompt_tokens": "2.5", "completion_tokens": "15", "completion_tokens_above_272k": "0"}
		priced = book(pricings={"p1": ("openai/gpt", rates)})
		self.assertEqual(priced.pricing_cost("p1", {"prompt_tokens_above_272k": 1_000_000}), D("2.5"))
		self.assertEqual(priced.pricing_cost("p1", {"cached_tokens_above_272k": 1_000_000}), D("0"))
		# A rate of 0 is a rate: free on purpose, not a fallback.
		self.assertEqual(priced.pricing_cost("p1", {"completion_tokens_above_272k": 1_000_000}), D("0"))

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

	def test_tolerance_is_one_nano_per_root_a_request_can_move(self):
		self.assertEqual(table().tolerance(3), D("0.000000024"))


class TestCounterTable(unittest.TestCase):
	BRACKET = (
		{"counter_name": "prompt_tokens_above_500k", "base_counter": "prompt_tokens", "min_prompt_tokens": 500_000},
		{"counter_name": "cached_tokens_above_500k", "base_counter": "cached_tokens", "min_prompt_tokens": 500_000},
		{"counter_name": "cache_write_tokens_above_500k", "base_counter": "cache_write_tokens", "min_prompt_tokens": 500_000},
	)

	def test_the_catalog_is_todays_table(self):
		t = table()
		self.assertEqual(t.roots, ["prompt_tokens", "cached_tokens", "cache_write_tokens", "cache_write_1h_tokens",
		                           "audio_tokens", "completion_tokens", "completion_audio_tokens", "request_count"])
		self.assertEqual(t.parts("prompt_tokens"), ["cached_tokens", "cache_write_tokens", "cache_write_1h_tokens", "audio_tokens"])
		self.assertEqual(t.parts("prompt_tokens_above_272k"), ["cached_tokens_above_272k", "cache_write_tokens_above_272k"])
		self.assertEqual((t.divisor("prompt_tokens_above_272k"), t.divisor("request_count")), (1_000_000, 1))
		self.assertEqual(t.base("cached_tokens_above_272k"), "cached_tokens")
		self.assertIs(t.validate(), t)

	def test_a_second_bracket_prices_at_its_own_rates(self):
		rates = {"prompt_tokens": "2", "cached_tokens": "0.2", "prompt_tokens_above_272k": "4",
		         "cached_tokens_above_272k": "0.4", "prompt_tokens_above_500k": "8"}
		priced = book(pricings={"p1": ("m", rates)}, counters=table(*self.BRACKET))
		# 600k prompt, 100k of it cached: cached falls back to its base's rate, cached_tokens.
		counts = {"prompt_tokens_above_500k": 600_000, "cached_tokens_above_500k": 100_000}
		self.assertEqual(priced.pricing_cost("p1", counts), D("4") + D("0.02"))

	def test_a_part_bracketed_unlike_its_container_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			table(self.BRACKET[1]).validate()
		# Bracketed at the container's thresholds and no others: fine, whichever order the rows come.
		table(*reversed(self.BRACKET)).validate()

	def test_the_push_carries_only_what_is_set(self):
		rows = {row["name"]: row for row in table().published}
		self.assertEqual(rows["prompt_tokens"], {"name": "prompt_tokens", "divisor": 1_000_000})
		self.assertEqual(rows["cached_tokens"], {"name": "cached_tokens", "divisor": 1_000_000, "part_of": "prompt_tokens"})
		self.assertEqual(rows["cached_tokens_above_272k"], {
			"name": "cached_tokens_above_272k", "divisor": 1_000_000, "base": "cached_tokens", "min_prompt_tokens": 272_000,
		})
		self.assertEqual(rows["request_count"], {"name": "request_count", "divisor": 1})


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
