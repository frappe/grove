# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Usage aggregation. Pure — the rows are passed in, so no site needed."""

import unittest

from datetime import date
from unittest.mock import patch

from grove import api
from grove.api import _totals_by_model as totals_by_model


def row(model, requests=0, cost=0.0):
	return {"model": model, "requests": requests, "cost": cost}


class TestTotalsByModel(unittest.TestCase):
	def test_no_rows_is_no_summary(self):
		self.assertEqual(totals_by_model([]), [])

	def test_rows_for_one_model_are_summed_not_overwritten(self):
		# Two users on the same model arrive as two rows.
		summary = totals_by_model([row("qwen3-35b", requests=1, cost=0.5), row("qwen3-35b", requests=2, cost=0.25)])
		self.assertEqual(summary, [{"model": "qwen3-35b", "requests": 3, "cost": 0.75}])

	def test_costliest_comes_first_then_busiest(self):
		summary = totals_by_model([
			row("free-quiet", requests=1), row("big", requests=1, cost=9.0),
			row("free-busy", requests=50), row("mid", requests=900, cost=1.0),
		])
		self.assertEqual([t["model"] for t in summary], ["big", "mid", "free-busy", "free-quiet"])

	def test_missing_metrics_count_as_zero(self):
		summary = totals_by_model([{"model": "qwen3-35b", "requests": None}])
		self.assertEqual(summary, [{"model": "qwen3-35b", "requests": 0, "cost": 0}])


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
