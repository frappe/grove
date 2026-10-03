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
		self.assertEqual(api.usage(["nobody@example.com"])["model_summary"], [])

	def test_a_user_is_shown_only_the_models_they_may_call(self):
		def published_model(model_id):
			model = frappe.get_doc(
				{"doctype": "Model", "model_id": model_id, "modality": "text", "hf_repo": f"org/{model_id}"}
			).insert(ignore_permissions=True).name
			frappe.db.set_value("Model", model, "published", 1)
			return model

		grouped, denied, allowed, other = (published_model(f"probe-reach-{n}") for n in "abcd")
		frappe.get_doc(
			{"doctype": "Model Group", "__newname": "probe-reach", "models": [{"model": grouped}, {"model": denied}]}
		).insert(ignore_permissions=True)
		email = "probe-reach@example.com"
		grove_user = frappe.get_doc("Grove User", api._set_policy(email, "Probe Reach", None))
		grove_user.model_groups = []
		grove_user.save()
		self.assertEqual(api.available_models(email), [])
		self.assertEqual(api.available_models("nobody-reach@example.com"), [])

		grove_user.update(
			{"model_groups": [{"model_group": "probe-reach"}], "allow": [{"model": allowed}], "deny": [{"model": denied}]}
		)
		grove_user.save()
		# `name` on the wire is the key: what a caller sends, not a doc id.
		key = lambda doc: frappe.db.get_value("Model", doc, "model_key")  # noqa: E731
		self.assertEqual(sorted(row["name"] for row in api.available_models(email)), sorted([key(grouped), key(allowed)]))
		self.assertIn(key(other), [row["name"] for row in api.available_models()])

	def test_the_fleet_stays_out_of_reach(self):
		for doctype in WITHHELD:
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit=1)

	def test_provisioning_a_user_registers_the_login_it_names(self):
		email = "probe-person@example.com"
		geography = make_test_geography()
		self.assertEqual(api.provision_user("Probe Person", email, geography), {"geography": geography})
		self.assertEqual(frappe.db.get_value("User", email, "first_name"), "Probe Person")
		# Safe to repeat: the same user, left as they are.
		self.assertEqual(api.provision_user("Renamed", email), {"geography": geography})
		self.assertEqual(frappe.db.count("Grove User", {"user": email}), 1)
		self.assertEqual(frappe.db.count("Grove API Key", {"user": frappe.db.get_value("Grove User", {"user": email})}), 0)

	def test_a_key_is_minted_for_a_known_user_at_their_geography(self):
		email = "probe-keyed@example.com"
		with self.assertRaises(frappe.ValidationError, msg="a key does not create its user"):
			api.provision_key(email, title="laptop")

		geography = api.provision_user("Probe Keyed", email, make_test_geography())["geography"]
		result = api.provision_key(email, title="laptop")

		self.assertTrue(result["api_key"].startswith(KEY_PREFIX))
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(result["api_key"])}, "title"), "laptop")
		self.assertEqual(result["gateway_url"], f"https://{frappe.db.get_value('Geography', geography, 'endpoint')}")

	def pull_counter(self, email, full_name):
		"""A user to pull and their counter, cleared now and after: Redis is not rolled back."""
		grove_user = api._set_policy(email, full_name, None)
		key = frappe.cache.make_key(f"usage_pull:{grove_user}")
		frappe.cache.delete(key)
		self.addCleanup(frappe.cache.delete, key)
		return grove_user, key

	def test_the_control_role_pulls_a_user_on_demand_a_few_times_an_hour(self):
		email, other = "probe-pull@example.com", "probe-pull-other@example.com"
		grove_user, key = self.pull_counter(email, "Probe Pull")
		other_user, _ = self.pull_counter(other, "Probe Pull Other")

		with unittest.mock.patch("grove.pathway.usage.pull_all", return_value="PS-1") as pull_all:
			for _ in range(api.PULLS_PER_HOUR):
				self.assertEqual(api.pull_usage(email), {"sync": "PS-1"})
			pull_all.assert_called_with(trigger="Manual", wait=60, user=grove_user)
			with self.assertRaises(frappe.RateLimitExceededError):
				api.pull_usage(email)
			self.assertEqual(pull_all.call_count, api.PULLS_PER_HOUR)

			self.assertEqual(api.pull_usage(other), {"sync": "PS-1"}, "the count is per user")
			pull_all.assert_called_with(trigger="Manual", wait=60, user=other_user)
		self.assertGreater(frappe.cache.ttl(key), 0, "the counter expires")

		frappe.set_user("Guest")
		counted = int(frappe.cache.get(key))
		with self.assertRaises(frappe.PermissionError):
			api.pull_usage(email)
		self.assertEqual(int(frappe.cache.get(key)), counted, "a refused caller spends nothing")

	def test_the_control_role_can_post_a_credit(self):
		grove_user = api._set_policy("probe-credit@example.com", "Probe Credit", None)
		frappe.get_doc({"doctype": "Grove Credit", "grove_user": grove_user, "amount": 5}).insert()
		self.assertEqual(frappe.db.get_value("Grove User", grove_user, "balance"), 5)

	def test_add_credit_posts_to_the_ledger_and_returns_the_balance(self):
		email = "probe-topup@example.com"
		grove_user = api._set_policy(email, "Probe Topup", None)
		self.assertTrue(frappe.db.get_value("Grove User", grove_user, "credit_exhausted"))
		self.assertEqual(api.add_credit(email, 7)["balance"], 7)
		self.assertFalse(frappe.db.get_value("Grove User", grove_user, "credit_exhausted"))
		self.assertEqual(api.balance(email), {"balance": 7.0, "spent": 0.0, "is_free_user": False})
		with self.assertRaises(frappe.ValidationError):
			api.add_credit(email, 0)
		with self.assertRaises(frappe.ValidationError):
			api.add_credit("nobody-topup@example.com", 1)
		with self.assertRaises(frappe.ValidationError):
			api.balance("nobody-topup@example.com")

	def test_add_credit_repeated_with_a_reference_adds_nothing(self):
		email = "probe-retry@example.com"
		grove_user = api._set_policy(email, "Probe Retry", None)
		for _ in range(2):
			self.assertEqual(api.add_credit(email, 7, reference="ledger-1")["balance"], 7)
		self.assertEqual(frappe.db.count("Grove Credit", {"grove_user": grove_user}), 1)
		self.assertEqual(api.add_credit(email, 3, reference="ledger-2")["balance"], 10)
		# The id names one top-up: another amount, or another user, under it is refused.
		with self.assertRaises(frappe.ValidationError):
			api.add_credit(email, 8, reference="ledger-1")
		api._set_policy("probe-retry-other@example.com", "Probe Other", None)
		with self.assertRaises(frappe.ValidationError):
			api.add_credit("probe-retry-other@example.com", 7, reference="ledger-1")
		# No reference, no dedupe: two calls are two top-ups.
		for _ in range(2):
			api.add_credit(email, 1)
		self.assertEqual(api.balance(email)["balance"], 12.0)

	def test_a_reference_is_unique_on_the_ledger_itself(self):
		grove_user = api._set_policy("probe-unique@example.com", "Probe Unique", None)
		entry = {"doctype": "Grove Credit", "grove_user": grove_user, "amount": 5, "reference": "ledger-9"}
		credit = frappe.get_doc(entry).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc(entry).insert()
		credit.reference = "ledger-10"
		with self.assertRaisesRegex(frappe.ValidationError, "never edited"):
			credit.save(ignore_permissions=True)

