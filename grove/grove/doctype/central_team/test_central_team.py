# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""One team per Central id, an email that is only an address, and a cap on how many keys it holds.

An id per test: IntegrationTestCase rolls back once when the class is done, not between tests, so
anything registered here is still there for the next one."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography


def team(name, **fields):
	return frappe.get_doc({
		"doctype": "Central Team", "__newname": name, "email": f"{name}@example.com", **fields,
	}).insert()


class IntegrationTestCentralTeam(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()

	def test_an_id_names_one_team(self):
		# The ledger is the team's, so a second team under one id is a second allowance.
		team("team-probe-twin")
		with self.assertRaises(frappe.DuplicateEntryError):
			team("team-probe-twin")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Central Team", "email": "team-probe-blank@example.com"}).insert()

	def test_an_email_is_an_address_not_a_login(self):
		# One owner may hold several teams.
		for reference in ("team-probe-shared-1", "team-probe-shared-2"):
			team(reference, email="shared@example.com")
		self.assertEqual(frappe.db.count("Central Team", {"email": "shared@example.com"}), 2)
		self.assertFalse(frappe.db.exists("User", "shared@example.com"))

	def test_a_team_holds_at_most_max_keys_live_keys(self):
		doc = team("team-probe-count", max_keys=2, free=1)
		keys = [frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert() for _ in range(2)]
		with self.assertRaisesRegex(frappe.ValidationError, "already holds 2 live keys"):
			frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		# A revoked key does not count.
		frappe.db.set_value("Grove API Key", keys[0].name, "status", "revoked")
		frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert()
		self.assertEqual(doc.reload().active_keys, 2)

	def test_max_keys_must_be_above_zero(self):
		with self.assertRaises(frappe.ValidationError):
			team("team-probe-zero", max_keys=0)

	def test_a_new_prepaid_team_is_exhausted_until_credited(self):
		doc = team("team-probe-exhausted")
		self.assertEqual(doc.credit_exhausted, 1)
		frappe.get_doc({"doctype": "Grove Credit", "team": doc.name, "amount": 3}).insert()
		self.assertEqual(frappe.db.get_value("Central Team", doc.name, ["credit_exhausted", "balance"]), (0, 3))
		free = team("team-probe-free", free=1)
		self.assertEqual(free.credit_exhausted, 0)

	def test_the_button_flips_free_and_hands_out_the_limits_in_one_go(self):
		doc = team("team-probe-flip", free=1)
		keys = [frappe.get_doc({"doctype": "Grove API Key", "team": doc.name}).insert().name for _ in range(2)]
		frappe.get_doc({"doctype": "Grove Credit", "team": doc.name, "amount": 10}).insert()
		# Refused up front: limits past the balance. Nothing moves.
		with self.assertRaisesRegex(frappe.ValidationError, "Top up first"):
			doc.set_free(0, caps=[{"key": keys[0], "cap": 6}, {"key": keys[1], "cap": 6}])
		self.assertEqual(frappe.db.get_value("Central Team", doc.name, "free"), 1)
		doc.set_free(0, caps=[{"key": keys[0], "cap": 6}, {"key": keys[1], "cap": 4}])
		self.assertEqual(frappe.db.get_value("Central Team", doc.name, "free"), 0)
		self.assertEqual([frappe.db.get_value("Grove API Key", k, "cap") for k in keys], [6, 4])
		doc.reload()
		doc.set_free(1)
		self.assertEqual(frappe.db.get_value("Central Team", doc.name, ["free", "credit_exhausted"]), (1, 0))
		# A key left out of the table starts at 0: refused until it gets a limit.
		doc.reload()
		doc.set_free(0, caps=[{"key": keys[0], "cap": 4}])
		self.assertEqual([frappe.db.get_value("Grove API Key", k, "cap") for k in keys], [4, 0])
