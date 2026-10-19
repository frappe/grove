# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The monthly spend limit holds against what the month's keys spent, not only their caps: each live
key's cap or its month's spend, whichever is more, plus what revoked keys spent, with what the
boxes hold this moment on top (`pricing.committed` over `usage.peek_team`). So moving a spent cap
to another key, or revoking and minting afresh, frees nothing. Site-backed; the boxes are a fake
that answers whatever a test says they hold."""

import unittest
import unittest.mock
from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase

from grove.api import keys, teams, usage
from grove.billing.pricing import committed, usd
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.tests.test_api_permissions import backdate, billed_record, tier
from grove.utils import utc_today

LAST_MONTH = frappe.utils.add_months(utc_today(), -1)


class SpendLimitCase(IntegrationTestCase):
	"""A team per test on a tier of its own, Administrator at the API, the boxes faked: `self.peek`
	is what `usage.peek_team` answers, {key: USD the boxes hold that no record has yet}."""

	counter = 0

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		cls.peek = cls.enterClassContext(
			unittest.mock.patch("grove.pathway.usage.peek_team", return_value={})
		)

	def setUp(self):
		self.peek.reset_mock()
		self.peek.return_value, self.peek.side_effect = {}, None

	def team(self, spend_limit=10):
		SpendLimitCase.counter += 1
		name = f"spend-{SpendLimitCase.counter}"
		teams.provision_team(name)
		teams.update_team(name, tier=tier(f"{name}-tier", spend_limit=spend_limit))
		return name

	def key(self, team, cap=0, title="key"):
		return keys.provision_key(team, title=title, cap=cap)["name"]

	def refused(self, fn, *words):
		with self.assertRaises(frappe.ValidationError) as caught:
			fn()
		for word in words:
			self.assertIn(word, str(caught.exception))


class TestWhatTheMonthSpentBindsTheLimit(SpendLimitCase):
	def test_a_lowered_cap_frees_only_what_the_key_did_not_spend(self):
		team = self.team(spend_limit=10)
		first, second = self.key(team, cap=10), self.key(team)
		billed_record(first, team, utc_today(), 6)
		self.assertEqual(keys.update_key(team, first, cap=0)["cap"], 0)
		self.assertEqual(usage.figures(team)["spend"]["committed"], 6.0)
		self.refused(lambda: keys.update_key(team, second, cap=5), "add up to 11 of a spend limit of 10")
		self.assertEqual(keys.update_key(team, second, cap=4)["cap"], 4)

	def test_a_revoked_key_frees_its_cap_and_never_its_spend(self):
		team = self.team(spend_limit=10)
		first, second = self.key(team, cap=10), self.key(team, cap=0)
		billed_record(first, team, utc_today(), 6)
		backdate(first)
		keys.revoke_key(team, first)
		self.assertEqual(usage.figures(team)["spend"]["capped"], 0.0)
		self.refused(lambda: self.key(team, cap=5), "add up to 11")
		self.assertEqual(keys.update_key(team, second, cap=4)["cap"], 4)
		self.refused(lambda: self.key(team, cap=1), "add up to 11")
		self.key(team, cap=0)
		self.assertEqual(usage.figures(team)["spend"]["committed"], 10.0)

	def test_a_key_counts_its_cap_or_its_spend_whichever_is_more(self):
		team = self.team(spend_limit=10)
		first, second = self.key(team, cap=3), self.key(team)
		billed_record(first, team, utc_today(), 5)  # over its own cap: in flight when the cap was hit
		self.assertEqual(committed(team), 5.0)
		self.refused(lambda: keys.update_key(team, second, cap=6), "add up to 11")
		self.assertEqual(keys.update_key(team, second, cap=5)["cap"], 5)

	def test_last_months_spend_is_last_months(self):
		team = self.team(spend_limit=10)
		first = self.key(team, cap=2)
		billed_record(first, team, LAST_MONTH, 9)
		billed_record(first, team, utc_today(), 1)
		self.assertEqual(committed(team), 2.0)
		self.assertEqual(keys.update_key(team, first, cap=10)["cap"], 10)

	def test_what_the_boxes_hold_counts_before_any_pull_lands_it(self):
		team = self.team(spend_limit=10)
		first, second = self.key(team, cap=10), self.key(team)
		keys.update_key(team, first, cap=0)
		self.peek.return_value = {first: Decimal("6")}
		self.refused(lambda: keys.update_key(team, second, cap=5), "add up to 11")
		self.assertEqual(keys.update_key(team, second, cap=4)["cap"], 4)
		backdate(first)
		keys.revoke_key(team, first)
		self.refused(lambda: self.key(team, cap=1), "add up to 11")

	def test_the_boxes_answer_stands_in_for_nothing_the_records_have(self):
		"""A spend both landed and still on the box would double; the peek skips a landed set-aside
		bucket itself (test_usage_pull), so here the sum is simply records plus what it answers."""
		team = self.team(spend_limit=10)
		first = self.key(team, cap=10)
		billed_record(first, team, utc_today(), 4)
		self.peek.return_value = {first: Decimal("2")}
		self.assertEqual(committed(team, pending=self.peek()), 10.0)
		self.assertEqual(committed(team, pending={first: Decimal("7")}), 11.0)


class TestWhereTheCheckRuns(SpendLimitCase):
	def test_a_mint_and_a_raise_ask_the_boxes_once_and_a_lower_never(self):
		team = self.team(spend_limit=10)
		first = self.key(team, cap=4)
		self.assertEqual(self.peek.call_count, 1)
		keys.update_key(team, first, cap=6)
		self.assertEqual(self.peek.call_count, 2)
		keys.update_key(team, first, cap=1)
		self.assertEqual(self.peek.call_count, 2)
		self.peek.assert_called_with(team)

	def test_a_batch_asks_once_refuses_whole_and_passes_its_answer_to_every_save(self):
		team = self.team(spend_limit=10)
		first, second = self.key(team, cap=6), self.key(team, cap=4)
		self.peek.reset_mock()
		self.peek.return_value = {first: Decimal("3")}
		keys.update_caps(team, {first: 2, second: 2})
		self.peek.assert_not_called()
		rows = keys.update_caps(team, {first: 3, second: 7})
		self.assertEqual({row.name: row.cap for row in rows}, {first: 3, second: 7})
		self.assertEqual(self.peek.call_count, 1)
		self.refused(
			lambda: keys.update_caps(team, {first: 3, second: 8}), "add up to 11 of a spend limit of 10"
		)
		self.assertEqual({row.name: row.cap for row in usage.figures(team)["keys"]}, {first: 3, second: 7})

	def test_spend_reports_the_figure_a_raise_is_checked_against(self):
		team = self.team(spend_limit=10)
		first, second = self.key(team, cap=3), self.key(team, cap=2)
		billed_record(first, team, utc_today(), 5)
		billed_record(second, team, utc_today(), 1)
		backdate(second)
		keys.revoke_key(team, second)
		spend = usage.figures(team)["spend"]
		figures = (spend["capped"], spend["month_spent"], spend["committed"], spend["spend_limit"])
		self.assertEqual(figures, (3.0, 6.0, 6.0, 10.0))


class TestTheMessage(unittest.TestCase):
	def test_amounts_read_as_themselves_to_the_nano(self):
		amounts = (10, 10.5, Decimal("0.0000465"), 0, 0.000000001)
		self.assertEqual([usd(x) for x in amounts], ["10", "10.5", "0.0000465", "0", "0.000000001"])
