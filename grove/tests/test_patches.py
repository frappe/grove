"""What a data patch computes, off rows a test hands it: the site part is one UPDATE and one
DELETE, and the unique the patch clears the way for makes the pair it merges impossible to
insert on a migrated site."""

import unittest
from decimal import Decimal

import frappe

from grove.patches.v1_0.merge_free_usage_records import merged


class TestAFreeRowFoldsIntoItsChargedTwin(unittest.TestCase):
	def test_totals_sum_and_every_entry_is_kept(self):
		charged = frappe._dict(
			request_count=3,
			total_tokens=300,
			cost=Decimal("0.3"),
			gateway_cost=Decimal("0.3"),
			usage='[{"model": "m", "pricing": "p", "requests": 3, "grove_cost": 0.3}]',
		)
		free = frappe._dict(
			request_count=2,
			total_tokens=50,
			cost=Decimal("0.05"),
			gateway_cost=Decimal("0"),
			usage='[{"model": "m", "pricing": null, "requests": 2, "grove_cost": 0.05}]',
		)
		one = merged([charged, free])
		self.assertEqual(
			(one["request_count"], one["total_tokens"], one["cost"], one["gateway_cost"]),
			(5, 350, Decimal("0.35"), Decimal("0.3")),
		)
		self.assertEqual([entry["requests"] for entry in frappe.parse_json(one["usage"])], [3, 2])

	def test_a_row_with_no_detail_adds_nothing(self):
		bare = frappe._dict(request_count=1, total_tokens=0, cost=0, gateway_cost=0, usage=None)
		self.assertEqual(frappe.parse_json(merged([bare])["usage"]), [])
