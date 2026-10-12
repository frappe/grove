# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""One team per Central id, and a cap on how many keys it holds.

An id per test: IntegrationTestCase rolls back once when the class is done, not between tests, so
anything registered here is still there for the next one."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.access import team_limits
from grove.grove.doctype.geography.test_geography import make_test_geography


def team(name, **fields):
	"""Numbers given here are the team's own, not a tier's."""
	if "max_keys" in fields or "spend_limit" in fields:
		fields.setdefault("override_limits", 1)
	return frappe.get_doc({
		"doctype": "Central Team", "__newname": name, **fields,
	}).insert()


class IntegrationTestCentralTeam(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")

	def test_an_id_names_one_team(self):
		# The limits are the team's, so a second team under one id would be a second allowance.
		team("team-probe-twin")
		with self.assertRaises(frappe.DuplicateEntryError):
			team("team-probe-twin")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Central Team"}).insert()

	def test_a_team_holds_at_most_max_keys_live_keys(self):
		doc = team("team-probe-count", max_keys=2, free=1)
		keys = [frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert() for _ in range(2)]
		with self.assertRaisesRegex(frappe.ValidationError, "already holds 2 live keys"):
			frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		# A revoked key does not count.
		frappe.db.set_value("Grove API Key", keys[0].name, "status", "revoked")
		frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		self.assertEqual(doc.reload().active_keys, 2)

	def test_max_keys_0_is_no_limit_and_a_negative_one_is_refused(self):
		doc = team("team-probe-unbounded", max_keys=0, free=1)
		for _ in range(2):
			frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		with self.assertRaises(frappe.ValidationError):
			team("team-probe-negative-keys", max_keys=-1)

	def test_a_team_without_override_takes_its_tiers_caps_and_blank_takes_the_default(self):
		tier = frappe.get_doc({"doctype": "Tier", "__newname": "team-probe-tier", "max_keys": 1, "spend_limit": 5}).insert()
		doc = team("team-probe-tiered", tier=tier.name)
		self.assertEqual((team_limits(doc.name).max_keys, team_limits(doc.name).spend_limit), (1, 5))
		frappe.get_doc({"doctype": "Grove API Key", "team": doc.name, "cap": 5}).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "already holds 1 live keys"):
			frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		# No tier: the default tier's numbers; no default either: no key limit, and no cap allowed.
		untiered = team("team-probe-untiered")
		self.assertEqual((team_limits(untiered.name).max_keys, team_limits(untiered.name).spend_limit), (0, 0))
		frappe.db.set_single_value("Grove Settings", "default_tier", tier.name)
		self.addCleanup(frappe.db.set_single_value, "Grove Settings", "default_tier", "")
		self.assertEqual((team_limits(untiered.name).max_keys, team_limits(untiered.name).spend_limit), (1, 5))
		# Override Limits: the team's own, whatever its tier says.
		frappe.db.set_value("Central Team", doc.name, {"override_limits": 1, "max_keys": 0, "spend_limit": 1})
		self.assertEqual((team_limits(doc.name).max_keys, team_limits(doc.name).spend_limit), (0, 1))

	def test_a_spend_limit_cannot_be_negative(self):
		with self.assertRaises(frappe.ValidationError):
			team("team-probe-negative", spend_limit=-1)

	def test_the_button_flips_free_and_hands_out_the_caps_in_one_go(self):
		doc = team("team-probe-flip", free=1, spend_limit=10)
		keys = [frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert().name for _ in range(2)]
		# Refused up front: caps past the spend limit. Nothing moves.
		with self.assertRaisesRegex(frappe.ValidationError, "spend limit of 10"):
			doc.set_free(0, caps=[{"key": keys[0], "cap": 6}, {"key": keys[1], "cap": 6}])
		self.assertEqual(frappe.db.get_value("Central Team", doc.name, "free"), 1)
		doc.set_free(0, caps=[{"key": keys[0], "cap": 6}, {"key": keys[1], "cap": 4}])
		self.assertEqual(frappe.db.get_value("Central Team", doc.name, "free"), 0)
		self.assertEqual([frappe.db.get_value("Grove API Key", k, "cap") for k in keys], [6, 4])
		doc.reload()
		doc.set_free(1)
		self.assertEqual(frappe.db.get_value("Central Team", doc.name, "free"), 1)
		# A key left out of the table starts at 0: refused until it gets a cap.
		doc.reload()
		doc.set_free(0, caps=[{"key": keys[0], "cap": 4}])
		self.assertEqual([frappe.db.get_value("Grove API Key", k, "cap") for k in keys], [4, 0])
