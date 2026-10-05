# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""grove.api runs under permission checks, not around them: the reads go through
frappe.get_list and the writes through a plain save, so the Grove Control role has to carry
exactly what those endpoints touch — and nothing more. Site-backed: DocPerms are the
behaviour under test."""

import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase

from grove import api
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_api_key.grove_api_key import KEY_PREFIX, hash_secret

PROBE = "control-probe@example.com"
ALERTS = "owner@example.com"
WITHHELD = ("Machine", "Inference Server", "Model Replica", "Monitoring Agent")


class TestTheControlRoleReachesOnlyWhatItServes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		"""One probe user for the class: IntegrationTestCase rolls back once at the end, not
		per test, so a per-test insert would collide with itself."""
		super().setUpClass()
		make_test_geography()
		frappe.get_doc(
			{
				"doctype": "User",
				"email": PROBE,
				"first_name": "Control Probe",
				"user_type": "Website User",
				"send_welcome_email": 0,
				"roles": [{"role": api.CONTROL_ROLE}],
			}
		).insert()

	def setUp(self):
		frappe.set_user(PROBE)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_the_catalogue_and_the_usage_report_are_readable(self):
		self.assertIsInstance(api.available_models(), list)
		self.assertEqual(api.usage(["nobody"])["model_summary"], [])

	def test_a_user_is_shown_only_the_models_they_may_call(self):
		def published_model(model_id):
			model = frappe.get_doc(
				{"doctype": "Model", "model_id": model_id, "hf_repo": f"org/{model_id}"}
			).insert(ignore_permissions=True).name
			frappe.db.set_value("Model", model, "published", 1)
			return model

		grouped, denied, allowed, other = (published_model(f"probe-reach-{n}") for n in "abcd")
		frappe.get_doc(
			{"doctype": "Model Group", "__newname": "probe-reach", "models": [{"model": grouped}, {"model": denied}]}
		).insert(ignore_permissions=True)
		user = "probe-reach"
		grove_user = frappe.get_doc("Grove User", api._set_policy(user, ALERTS, None))
		grove_user.model_groups = []
		grove_user.save()
		self.assertEqual(api.available_models(user), [])
		self.assertEqual(api.available_models("nobody-reach"), [])

		grove_user.update(
			{"model_groups": [{"model_group": "probe-reach"}], "allow": [{"model": allowed}], "deny": [{"model": denied}]}
		)
		grove_user.save()
		# `name` on the wire is the key: what a caller sends, not a doc id.
		key = lambda doc: frappe.db.get_value("Model", doc, "model_key")  # noqa: E731
		self.assertEqual(sorted(row["name"] for row in api.available_models(user)), sorted([key(grouped), key(allowed)]))
		self.assertIn(key(other), [row["name"] for row in api.available_models()])

	def test_a_users_rate_limits_are_readable(self):
		user = "probe-limits"
		api._set_policy(user, ALERTS, None, limits=[{"metric": "requests", "window": "1m", "value": 200}])
		self.assertEqual(api.limits(user), [{"metric": "requests", "window": "1m", "value": 200}])
		self.assertEqual(api.limits("nobody-limits"), [])

	def test_the_fleet_stays_out_of_reach(self):
		for doctype in WITHHELD:
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit=1)

	def test_provisioning_again_keeps_the_user_and_moves_its_email(self):
		user, geography = "TEAM-PROBE", make_test_geography()
		self.assertEqual(api.provision_user(user, "first-owner@example.com", geography), {"geography": geography})
		grove_user = frappe.db.get_value("Grove User", {"reference": user})
		self.assertEqual(frappe.db.count("Grove API Key", {"user": grove_user}), 0)
		key = api.provision_key(user)["api_key"]
		# Safe to repeat, and how an owner change arrives: the same user and keys, a new address.
		self.assertEqual(api.provision_user(user, "next-owner@example.com"), {"geography": geography})
		self.assertEqual(
			frappe.db.get_value("Grove User", {"reference": user}, ["name", "email"]),
			(grove_user, "next-owner@example.com"),
		)
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(key)}, "user"), grove_user)
		self.assertFalse(frappe.db.exists("User", "first-owner@example.com"), "an address is not a login")

	def test_one_address_may_sit_on_two_users_and_a_blank_reference_names_nobody(self):
		for user in ("TEAM-SHARED-1", "TEAM-SHARED-2"):
			api.provision_user(user, "shared-owner@example.com")
		self.assertEqual(frappe.db.count("Grove User", {"email": "shared-owner@example.com"}), 2)
		with self.assertRaises(frappe.ValidationError):
			api.provision_user("", "shared-owner@example.com")

	def test_a_key_is_minted_for_a_known_user_at_their_geography(self):
		user = "probe-keyed"
		with self.assertRaises(frappe.ValidationError, msg="a key does not create its user"):
			api.provision_key(user, title="laptop")

		geography = api.provision_user(user, ALERTS, make_test_geography())["geography"]
		result = api.provision_key(user, title="laptop")

		self.assertTrue(result["api_key"].startswith(KEY_PREFIX))
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(result["api_key"])}, "title"), "laptop")
		self.assertEqual(result["gateway_url"], f"https://{frappe.db.get_value('Geography', geography, 'endpoint')}")

	def pull_counter(self, user):
		"""A user to pull and their counter, cleared now and after: Redis is not rolled back."""
		grove_user = api._set_policy(user, ALERTS, None)
		key = frappe.cache.make_key(f"usage_pull:{grove_user}")
		frappe.cache.delete(key)
		self.addCleanup(frappe.cache.delete, key)
		return grove_user, key

	def test_the_control_role_pulls_a_user_on_demand_a_few_times_an_hour(self):
		user, other = "probe-pull", "probe-pull-other"
		grove_user, key = self.pull_counter(user)
		other_user, _ = self.pull_counter(other)

		with unittest.mock.patch("grove.pathway.usage.pull_all", return_value="PS-1") as pull_all:
			for _ in range(api.PULLS_PER_HOUR):
				self.assertEqual(api.pull_usage(user), {"sync": "PS-1"})
			pull_all.assert_called_with(trigger="Manual", wait=60, user=grove_user)
			with self.assertRaises(frappe.RateLimitExceededError):
				api.pull_usage(user)
			self.assertEqual(pull_all.call_count, api.PULLS_PER_HOUR)

			self.assertEqual(api.pull_usage(other), {"sync": "PS-1"}, "the count is per user")
			pull_all.assert_called_with(trigger="Manual", wait=60, user=other_user)
		self.assertGreater(frappe.cache.ttl(key), 0, "the counter expires")

		frappe.set_user("Guest")
		counted = int(frappe.cache.get(key))
		with self.assertRaises(frappe.PermissionError):
			api.pull_usage(user)
		self.assertEqual(int(frappe.cache.get(key)), counted, "a refused caller spends nothing")

	def test_the_control_role_can_post_a_credit(self):
		grove_user = api._set_policy("probe-credit", ALERTS, None)
		frappe.get_doc({"doctype": "Grove Credit", "grove_user": grove_user, "amount": 5}).insert()
		self.assertEqual(frappe.db.get_value("Grove User", grove_user, "balance"), 5)

	def test_add_credit_posts_to_the_ledger_and_returns_the_balance(self):
		user = "probe-topup"
		grove_user = api._set_policy(user, ALERTS, None)
		self.assertTrue(frappe.db.get_value("Grove User", grove_user, "credit_exhausted"))
		self.assertEqual(api.add_credit(user, 7)["balance"], 7)
		self.assertFalse(frappe.db.get_value("Grove User", grove_user, "credit_exhausted"))
		self.assertEqual(api.balance(user), {"balance": 7.0, "spent": 0.0, "is_free_user": False})
		with self.assertRaises(frappe.ValidationError):
			api.add_credit(user, 0)
		with self.assertRaises(frappe.ValidationError):
			api.add_credit("nobody-topup", 1)
		with self.assertRaises(frappe.ValidationError):
			api.balance("nobody-topup")

	def test_add_credit_repeated_with_a_reference_adds_nothing(self):
		user = "probe-retry"
		grove_user = api._set_policy(user, ALERTS, None)
		for _ in range(2):
			self.assertEqual(api.add_credit(user, 7, reference="ledger-1")["balance"], 7)
		self.assertEqual(frappe.db.count("Grove Credit", {"grove_user": grove_user}), 1)
		self.assertEqual(api.add_credit(user, 3, reference="ledger-2")["balance"], 10)
		# The id names one top-up: another amount, or another user, under it is refused.
		with self.assertRaises(frappe.ValidationError):
			api.add_credit(user, 8, reference="ledger-1")
		api._set_policy("probe-retry-other", ALERTS, None)
		with self.assertRaises(frappe.ValidationError):
			api.add_credit("probe-retry-other", 7, reference="ledger-1")
		# No reference, no dedupe: two calls are two top-ups.
		for _ in range(2):
			api.add_credit(user, 1)
		self.assertEqual(api.balance(user)["balance"], 12.0)

	def test_a_reference_is_unique_on_the_ledger_itself(self):
		grove_user = api._set_policy("probe-unique", ALERTS, None)
		entry = {"doctype": "Grove Credit", "grove_user": grove_user, "amount": 5, "reference": "ledger-9"}
		credit = frappe.get_doc(entry).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc(entry).insert()
		credit.reference = "ledger-10"
		with self.assertRaisesRegex(frappe.ValidationError, "never edited"):
			credit.save(ignore_permissions=True)

