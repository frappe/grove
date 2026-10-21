# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""One team per Central id, and a cap on how many keys it holds.

An id per test: IntegrationTestCase rolls back once when the class is done, not between tests, so
anything registered here is still there for the next one."""

import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase

from grove.access import team_limits
from grove.grove.doctype.geography.test_geography import make_test_geography


def team(name, **fields):
	"""`max_keys` / `spend_limit` given here go on a Tier of the team's own."""
	if "max_keys" in fields or "spend_limit" in fields:
		numbers = {k: fields.pop(k) for k in ("max_keys", "spend_limit") if k in fields}
		fields["tier"] = (
			frappe.get_doc({"doctype": "Tier", "__newname": f"{name}-tier", **numbers}).insert().name
		)
	return frappe.get_doc(
		{
			"doctype": "Grove Team",
			"__newname": name,
			**fields,
		}
	).insert()


class IntegrationTestGroveTeam(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(unittest.mock.patch("grove.pathway.usage.peek_team", return_value={}))
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")

	def test_an_id_names_one_team(self):
		team("team-probe-twin")
		with self.assertRaises(frappe.DuplicateEntryError):
			team("team-probe-twin")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Grove Team"}).insert()

	def test_a_team_holds_at_most_max_keys_live_keys(self):
		doc = team("team-probe-count", max_keys=2)
		keys = [frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert() for _ in range(2)]
		with self.assertRaisesRegex(frappe.ValidationError, "already holds 2 live keys"):
			frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		frappe.db.set_value("Grove API Key", keys[0].name, "status", "revoked")
		frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		self.assertEqual(doc.reload().active_keys, 2)

	def test_max_keys_0_is_no_limit_and_a_negative_one_is_refused(self):
		doc = team("team-probe-unbounded", max_keys=0)
		for _ in range(2):
			frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		with self.assertRaises(frappe.ValidationError):
			team("team-probe-negative-keys", max_keys=-1)

	def test_a_team_takes_its_tiers_caps_and_blank_takes_the_default(self):
		tier = frappe.get_doc(
			{"doctype": "Tier", "__newname": "team-probe-tier", "max_keys": 1, "spend_limit": 5}
		).insert()
		doc = team("team-probe-tiered", tier=tier.name)
		self.assertEqual((team_limits(doc.name).max_keys, team_limits(doc.name).spend_limit), (1, 5))
		frappe.get_doc({"doctype": "Grove API Key", "team": doc.name, "cap": 5}).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "already holds 1 live keys"):
			frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		untiered = team("team-probe-untiered")
		self.assertEqual(
			(team_limits(untiered.name).max_keys, team_limits(untiered.name).spend_limit), (0, 0)
		)
		frappe.db.set_single_value("Grove Settings", "default_tier", tier.name)
		self.addCleanup(frappe.db.set_single_value, "Grove Settings", "default_tier", "")
		self.assertEqual(
			(team_limits(untiered.name).max_keys, team_limits(untiered.name).spend_limit), (1, 5)
		)
		frappe.db.set_value("Grove Team", doc.name, {"override_max_keys": 1, "max_keys": 0})
		self.assertEqual((team_limits(doc.name).max_keys, team_limits(doc.name).spend_limit), (0, 5))
		frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()

	def test_a_spend_limit_cannot_be_negative(self):
		with self.assertRaises(frappe.ValidationError):
			team("team-probe-negative", spend_limit=-1)

	def test_the_key_cap_window_is_monthly_or_total_and_figures_report_it(self):
		from grove.api import usage

		self.assertEqual(usage.figures(team("team-probe-window").name)["spend"]["key_cap_window"], "Monthly")
		doc = team("team-probe-window-total", key_cap_window="Total")
		self.assertEqual(usage.figures(doc.name)["spend"]["key_cap_window"], "Total")
		doc.key_cap_window = "Monthly"
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()
		with self.assertRaises(frappe.ValidationError):
			team("team-probe-weekly", key_cap_window="Weekly")

	def test_a_total_team_has_no_spend_limit(self):
		from grove.api import keys, usage

		doc = team("team-probe-total-limit", spend_limit=1, key_cap_window="Total")
		first = keys.provision_key(doc.name, title="a", cap=5)["name"]
		second = keys.provision_key(doc.name, title="b", cap=5)["name"]
		keys.update_key(doc.name, first, cap=50)
		keys.update_caps(doc.name, {first: 60, second: 60})
		spend = usage.figures(doc.name)["spend"]
		self.assertEqual(
			(spend["spend_limit"], spend["capped"], spend["key_cap_window"]), (None, 120.0, "Total")
		)
		monthly = team("team-probe-monthly-limit", spend_limit=1)
		with self.assertRaisesRegex(frappe.ValidationError, "spend limit of 1"):
			keys.provision_key(monthly.name, title="a", cap=5)
