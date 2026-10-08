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
from grove.pathway.routes import utc_timestamp

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

	def endpoint(self, geography):
		return f"https://{frappe.db.get_value('Geography', geography, 'endpoint')}"

	def test_the_catalogue_and_the_usage_report_are_readable(self):
		self.assertIsInstance(api.available_models(), list)
		self.assertEqual(api.usage(["nobody"])["model_summary"], [])
		[geography] = [g for g in api.geographies() if g["name"] == make_test_geography()]
		self.assertEqual(geography["endpoint"], self.endpoint(geography["name"]))

	def test_a_key_is_shown_only_the_models_it_may_call(self):
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
		team = "probe-reach"
		api.provision_team(team, ALERTS, free=True)
		key = frappe.get_doc("Grove API Key", api.provision_key(team)["name"])
		key.model_groups = []
		key.save()
		self.assertEqual(api.available_models(team, key.name), [])
		with self.assertRaises(frappe.DoesNotExistError):
			api.available_models("nobody-reach", key.name)

		key.update(
			{"model_groups": [{"model_group": "probe-reach"}], "allow": [{"model": allowed}], "deny": [{"model": denied}]}
		)
		key.save()
		# `name` on the wire is the key: what a caller sends, not a doc id.
		model_key = lambda doc: frappe.db.get_value("Model", doc, "model_key")  # noqa: E731
		reach = sorted([model_key(grouped), model_key(allowed)])
		self.assertEqual(sorted(row["name"] for row in api.available_models(team, key.name)), reach)
		# Without a key: what a new key in the geography starts with, its default group's models.
		here = make_test_geography()
		frappe.get_doc({
			"doctype": "Model Group", "__newname": "probe-reach-default", "geography": here, "is_default": 1,
			"models": [{"model": other}, {"model": grouped}],
		}).insert(ignore_permissions=True)
		self.assertEqual(
			sorted(row["name"] for row in api.available_models(geography=here)), sorted([model_key(grouped), model_key(other)])
		)
		self.assertEqual(api.available_models(geography=make_test_geography("probe-reach-away")), [])

	def test_a_keys_rate_limits_are_readable(self):
		team = "probe-limits"
		api.provision_team(team, ALERTS, free=True)
		name = api.provision_key(team, limits=[{"metric": "requests", "window": "1m", "value": 200}])["name"]
		self.assertEqual(api.limits(team, name), [{"metric": "requests", "window": "1m", "value": 200}])
		with self.assertRaises(frappe.DoesNotExistError):
			api.limits("nobody-limits", name)

	def test_the_fleet_stays_out_of_reach(self):
		for doctype in WITHHELD:
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit=1)

	def test_provisioning_again_keeps_the_team_and_moves_its_email(self):
		team = "TEAM-PROBE"
		self.assertEqual(api.provision_team(team, "first-owner@example.com", free=True), {"team": team, "max_keys": 10})
		self.assertEqual(frappe.db.count("Grove API Key", {"team": team}), 0)
		key = api.provision_key(team)["api_key"]
		# Safe to repeat, and how an owner change arrives: the same team and keys, a new address.
		self.assertEqual(api.provision_team(team, "next-owner@example.com"), {"team": team, "max_keys": 10})
		self.assertEqual(frappe.db.get_value("Central Team", team, "email"), "next-owner@example.com")
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(key)}, "team"), team)
		self.assertFalse(frappe.db.exists("User", "first-owner@example.com"), "an address is not a login")

	def test_one_address_may_sit_on_two_teams_and_a_blank_id_names_nobody(self):
		for team in ("TEAM-SHARED-1", "TEAM-SHARED-2"):
			api.provision_team(team, "shared-owner@example.com")
		self.assertEqual(frappe.db.count("Central Team", {"email": "shared-owner@example.com"}), 2)
		with self.assertRaises(frappe.ValidationError):
			api.provision_team("", "shared-owner@example.com")

	def test_a_key_is_minted_for_a_known_team_in_a_geography(self):
		team = "probe-keyed"
		with self.assertRaises(frappe.ValidationError, msg="a key does not create its team"):
			api.provision_key(team, title="laptop")

		api.provision_team(team, ALERTS, free=True)
		here, away = make_test_geography(), make_test_geography("probe-away")
		result = api.provision_key(team, title="laptop", geography=here)
		self.assertTrue(result["api_key"].startswith(KEY_PREFIX))
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(result["api_key"])}, "title"), "laptop")
		self.assertEqual((result["geography"], result["gateway_url"]), (here, self.endpoint(here)))
		# A second key of the same team may live elsewhere: that is how a team spans geographies.
		abroad = api.provision_key(team, title="eu", geography=away)
		self.assertEqual((abroad["geography"], abroad["gateway_url"]), (away, self.endpoint(away)))

	def test_a_cap_is_cut_from_the_balance_and_handed_back_by_a_revoke(self):
		team = "probe-cap"
		api.provision_team(team, ALERTS)
		with self.assertRaisesRegex(frappe.ValidationError, "cap above zero"):
			api.provision_key(team, title="uncapped")
		api.add_credit(team, 10)
		first = api.provision_key(team, title="first", cap=6)["name"]
		with self.assertRaises(frappe.ValidationError):
			api.provision_key(team, title="too much", cap=6)
		second = api.provision_key(team, title="second", cap=4)["name"]
		self.assertEqual(api.balance(team), {"balance": 10.0, "spent": 0.0, "unallocated": 0.0, "is_free_user": False})
		self.assertEqual(api.update_key(team, second, cap=2)["cap"], 2)
		self.assertEqual(api.balance(team)["unallocated"], 2.0)
		frappe.db.set_value("Grove API Key", first, "creation", frappe.utils.add_to_date(None, hours=-7), update_modified=False)
		api.revoke_key(team, first)
		self.assertEqual(api.balance(team)["unallocated"], 8.0)

	def test_a_minted_key_is_unique_by_its_hash(self):
		team = "probe-unique"
		api.provision_team(team, ALERTS, free=True)
		# 192 random bits never repeat in practice; the index is what makes sure of it.
		same_bits = unittest.mock.patch(
			"grove.grove.doctype.grove_api_key.grove_api_key.secrets.token_hex", return_value="0" * 48
		)
		with same_bits:
			api.provision_key(team, title="one")
			with self.assertRaises(frappe.UniqueValidationError):
				api.provision_key(team, title="twin")

	def test_a_teams_keys_are_listed_without_their_secret(self):
		team = "probe-listed"
		api.provision_team(team, ALERTS, free=True)
		minted = api.provision_key(team, title="laptop", cap=0)

		[listed] = api.keys(team)
		self.assertEqual((listed.name, listed.title, listed.status), (minted["name"], "laptop", "active"))
		self.assertEqual(listed.key_hash, hash_secret(minted["api_key"]))
		self.assertEqual((listed.geography, listed.gateway_url, listed.cap, listed.spent), (minted["geography"], minted["gateway_url"], 0, 0))
		self.assertEqual([row["metric"] for row in listed.limits], ["requests", "total_tokens"])
		# When revoke stops refusing it: six hours on, as UTC, so a caller can wait instead of asking.
		self.assertEqual(
			listed.revocable_at, utc_timestamp(frappe.utils.add_to_date(listed.creation, hours=6))
		)
		self.assertTrue(listed.masked.endswith(minted["api_key"][-4:]))
		self.assertNotIn(minted["api_key"], str(listed))
		self.assertEqual(api.keys("probe-nobody"), [])

	def test_a_key_is_revoked_by_name_only_for_the_team_holding_it(self):
		team, other = "probe-revoker", "probe-revoker-other"
		for each in (team, other):
			api.provision_team(each, ALERTS, free=True)
		key = api.provision_key(team, title="ci")["name"]

		with self.assertRaises(frappe.DoesNotExistError):
			api.revoke_key(other, key)
		with self.assertRaises(frappe.DoesNotExistError):
			api.update_key(other, key, cap=0)

		# A key younger than six hours cannot be revoked.
		with self.assertRaises(frappe.ValidationError):
			api.revoke_key(team, key)
		frappe.db.set_value(
			"Grove API Key", key, "creation", frappe.utils.add_to_date(None, hours=-7), update_modified=False
		)
		api.revoke_key(team, key)
		self.assertEqual(frappe.db.get_value("Grove API Key", key, "status"), "revoked")

	def pull_counter(self, team):
		"""A team to pull and its counter, cleared now and after: Redis is not rolled back."""
		api.provision_team(team, ALERTS)
		key = frappe.cache.make_key(f"usage_pull:{team}")
		frappe.cache.delete(key)
		self.addCleanup(frappe.cache.delete, key)
		return team, key

	def test_the_control_role_pulls_a_team_on_demand_a_few_times_an_hour(self):
		team, other = "probe-pull", "probe-pull-other"
		name, key = self.pull_counter(team)
		other_name, _ = self.pull_counter(other)

		with unittest.mock.patch("grove.pathway.usage.pull_all", return_value="PS-1") as pull_all:
			for _ in range(api.PULLS_PER_HOUR):
				self.assertEqual(api.pull_usage(team), {"sync": "PS-1"})
			pull_all.assert_called_with(trigger="Manual", wait=60, team=name)
			with self.assertRaises(frappe.RateLimitExceededError):
				api.pull_usage(team)
			self.assertEqual(pull_all.call_count, api.PULLS_PER_HOUR)

			self.assertEqual(api.pull_usage(other), {"sync": "PS-1"}, "the count is per team")
			pull_all.assert_called_with(trigger="Manual", wait=60, team=other_name)
		self.assertGreater(frappe.cache.ttl(key), 0, "the counter expires")

		frappe.set_user("Guest")
		counted = int(frappe.cache.get(key))
		with self.assertRaises(frappe.PermissionError):
			api.pull_usage(team)
		self.assertEqual(int(frappe.cache.get(key)), counted, "a refused caller spends nothing")

	def test_the_control_role_can_post_a_credit(self):
		api.provision_team("probe-credit", ALERTS)
		frappe.get_doc({"doctype": "Grove Credit", "team": "probe-credit", "amount": 5}).insert()
		self.assertEqual(frappe.db.get_value("Central Team", "probe-credit", "balance"), 5)

	def test_add_credit_posts_to_the_ledger_and_returns_the_balance(self):
		team = "probe-topup"
		api.provision_team(team, ALERTS)
		self.assertTrue(frappe.db.get_value("Central Team", team, "credit_exhausted"))
		self.assertEqual(api.add_credit(team, 7)["balance"], 7)
		self.assertFalse(frappe.db.get_value("Central Team", team, "credit_exhausted"))
		self.assertEqual(api.balance(team), {"balance": 7.0, "spent": 0.0, "unallocated": 7.0, "is_free_user": False})
		with self.assertRaises(frappe.ValidationError):
			api.add_credit(team, 0)
		with self.assertRaises(frappe.ValidationError):
			api.add_credit("nobody-topup", 1)
		with self.assertRaises(frappe.ValidationError):
			api.balance("nobody-topup")

	def test_add_credit_repeated_with_a_reference_adds_nothing(self):
		team = "probe-retry"
		api.provision_team(team, ALERTS)
		for _ in range(2):
			self.assertEqual(api.add_credit(team, 7, reference="ledger-1")["balance"], 7)
		self.assertEqual(frappe.db.count("Grove Credit", {"team": team}), 1)
		self.assertEqual(api.add_credit(team, 3, reference="ledger-2")["balance"], 10)
		# The id names one top-up: another amount, or another team, under it is refused.
		with self.assertRaises(frappe.ValidationError):
			api.add_credit(team, 8, reference="ledger-1")
		api.provision_team("probe-retry-other", ALERTS)
		with self.assertRaises(frappe.ValidationError):
			api.add_credit("probe-retry-other", 7, reference="ledger-1")
		# No reference, no dedupe: two calls are two top-ups.
		for _ in range(2):
			api.add_credit(team, 1)
		self.assertEqual(api.balance(team)["balance"], 12.0)

	def test_a_reference_is_unique_on_the_ledger_itself(self):
		api.provision_team("probe-unique-ledger", ALERTS)
		entry = {"doctype": "Grove Credit", "team": "probe-unique-ledger", "amount": 5, "reference": "ledger-9"}
		credit = frappe.get_doc(entry).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc(entry).insert()
		credit.reference = "ledger-10"
		with self.assertRaisesRegex(frappe.ValidationError, "never edited"):
			credit.save(ignore_permissions=True)
