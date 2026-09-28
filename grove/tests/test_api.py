# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Usage aggregation. Pure — the rows are passed in, so no site needed."""

import unittest

from datetime import date
from unittest.mock import patch

from grove import api
from grove.api import _token_totals as token_totals
from grove.api import _totals_by_model as totals_by_model

FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "request_count")


def row(model, prompt=0, completion=0, cached=0, requests=0):
	return {
		"model": model,
		"prompt_tokens": prompt,
		"completion_tokens": completion,
		"cached_tokens": cached,
		"request_count": requests,
	}


class TestTotalsByModel(unittest.TestCase):
	def test_no_rows_is_no_summary(self):
		self.assertEqual(totals_by_model([], FIELDS), [])

	def test_rows_for_one_model_are_summed_not_overwritten(self):
		# A user's keys each hold their own monthly record, so the same model arrives twice.
		summary = totals_by_model(
			[row("qwen3-35b", prompt=10, completion=20, requests=1),
			 row("qwen3-35b", prompt=5, completion=10, requests=2)],
			FIELDS,
		)
		self.assertEqual(len(summary), 1)
		self.assertEqual(summary[0]["prompt_tokens"], 15)
		self.assertEqual(summary[0]["completion_tokens"], 30)
		self.assertEqual(summary[0]["request_count"], 3)

	def test_biggest_consumer_comes_first(self):
		summary = totals_by_model(
			[row("small", prompt=10), row("big", prompt=900), row("mid", prompt=100)], FIELDS
		)
		self.assertEqual([t["model"] for t in summary], ["big", "mid", "small"])

	def test_the_model_is_named_in_each_entry(self):
		summary = totals_by_model([row("qwen3-35b", prompt=1)], FIELDS)
		self.assertEqual(summary[0]["model"], "qwen3-35b")

	def test_missing_metrics_count_as_zero(self):
		# get_all can hand back None for a column never written.
		summary = totals_by_model([{"model": "qwen3-35b", "prompt_tokens": None}], FIELDS)
		self.assertEqual(summary[0]["prompt_tokens"], 0)


class TestTokenTotals(unittest.TestCase):
	"""A grouped row folded into the token columns the endpoint has always returned."""

	def test_every_prompt_side_counter_is_a_prompt_token(self):
		totals = token_totals({
			"input_tokens": 60, "cached_tokens": 30, "cache_write_tokens": 10, "cache_write_1h_tokens": None,
			"completion_tokens": 5, "cost": 1.5,
		})
		self.assertEqual(totals, {"prompt_tokens": 100, "cached_tokens": 30, "completion_tokens": 5, "cost": 1.5})


class TestUsageWindow(unittest.TestCase):
	"""Every way of asking resolves to two UTC days."""

	def window(self, **kwargs):
		with patch.object(api, "utc_today", return_value=date(2026, 9, 28)):
			return api.usage_window(kwargs.get("from_date"), kwargs.get("to_date"), kwargs.get("period"), kwargs.get("month"))

	def test_the_named_periods(self):
		self.assertEqual(self.window(), (date(2026, 9, 1), date(2026, 9, 28)))
		self.assertEqual(self.window(period="Today"), (date(2026, 9, 28), date(2026, 9, 28)))
		self.assertEqual(self.window(period="Yesterday"), (date(2026, 9, 27), date(2026, 9, 27)))
		self.assertEqual(self.window(period="Last 7 Days"), (date(2026, 9, 22), date(2026, 9, 28)))
		self.assertEqual(self.window(period="Last 30 Days"), (date(2026, 8, 30), date(2026, 9, 28)))
		self.assertEqual(self.window(period="Last Month"), (date(2026, 8, 1), date(2026, 8, 31)))

	def test_a_month_and_an_explicit_range(self):
		self.assertEqual(self.window(month="2026-02"), (date(2026, 2, 1), date(2026, 2, 28)))
		self.assertEqual(self.window(from_date="2026-09-03", to_date="2026-09-05"), (date(2026, 9, 3), date(2026, 9, 5)))
		self.assertEqual(self.window(from_date="2026-09-03"), (date(2026, 9, 3), date(2026, 9, 3)))


if __name__ == "__main__":
	unittest.main()
