# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""grove.api runs under permission checks, not around them: the reads go through
frappe.get_list and the writes through a plain save, so the Grove Control role has to carry
exactly what those endpoints touch — and nothing more. Site-backed: DocPerms are the
behaviour under test."""

import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase

from grove.api import catalog, control, keys, teams, usage
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_api_key.grove_api_key import KEY_PREFIX, hash_secret
from grove.pathway.routes import utc_timestamp
from grove.utils import utc_today

PROBE = "control-probe@example.com"


def tier(name, **fields):
	return frappe.get_doc({"doctype": "Tier", "__newname": name, **fields}).insert().name
WITHHELD = ("Machine", "Inference Server", "Model Replica", "Monitoring Agent")


class TestTheControlRoleReachesOnlyWhatItServes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		"""One probe user for the class: IntegrationTestCase rolls back once at the end, not
		per test, so a per-test insert would collide with itself."""
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		frappe.get_doc(
			{
				"doctype": "User",
				"email": PROBE,
				"first_name": "Control Probe",
				"user_type": "Website User",
				"send_welcome_email": 0,
				"roles": [{"role": control.CONTROL_ROLE}],
			}
		).insert()

	def setUp(self):
		frappe.set_user(PROBE)
		self.addCleanup(frappe.set_user, "Administrator")

	def endpoint(self, geography):
		return f"https://{frappe.db.get_value('Geography', geography, 'endpoint')}"

	def test_the_catalogue_and_the_usage_report_are_readable(self):
		self.assertIsInstance(catalog.available_models(), list)
		self.assertEqual(usage.usage(["nobody"])["model_summary"], [])
		[geography] = [g for g in catalog.geographies() if g["name"] == make_test_geography()]
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
		teams.provision_team(team, free=True)
		key = frappe.get_doc("Grove API Key", keys.provision_key(team)["name"])
		key.model_groups = []
		key.save()
		self.assertEqual(catalog.available_models(team, key.name), [])
		with self.assertRaises(frappe.DoesNotExistError):
			catalog.available_models("nobody-reach", key.name)

		key.update(
			{"model_groups": [{"model_group": "probe-reach"}], "allow": [{"model": allowed}], "deny": [{"model": denied}]}
		)
		key.save()
		model_key = lambda doc: frappe.db.get_value("Model", doc, "model_key")  # noqa: E731
		reach = sorted([model_key(grouped), model_key(allowed)])
		rows = catalog.available_models(team, key.name)
		self.assertEqual(sorted(row["name"] for row in rows), reach)
		# The provider by its name, not its doc id: the key is that name and the model id.
		self.assertEqual({row["name"] for row in rows}, {f"{row['provider']}/{row['model_id']}" for row in rows})
		# Without a key: what a new key in the geography starts with, its default group's models.
		here = make_test_geography()
		frappe.get_doc({
			"doctype": "Model Group", "__newname": "probe-reach-default", "geography": here, "is_default": 1,
			"models": [{"model": other}, {"model": grouped}],
		}).insert(ignore_permissions=True)
		self.assertEqual(
			sorted(row["name"] for row in catalog.available_models(geography=here)), sorted([model_key(grouped), model_key(other)])
		)
		self.assertEqual(catalog.available_models(geography=make_test_geography("probe-reach-away")), [])

	def test_usage_is_read_off_the_replica_when_the_site_has_one(self):
		with (
			unittest.mock.patch.dict(frappe.local.conf, {"read_from_replica": 1}),
			unittest.mock.patch("frappe.connect_replica", return_value=False) as replica,
		):
			usage.usage(["nobody"])
		replica.assert_called_once()
		with unittest.mock.patch("frappe.connect_replica") as replica:
			usage.usage(["nobody"])
		replica.assert_not_called()

	def test_a_keys_rate_limits_are_readable(self):
		team = "probe-limits"
		teams.provision_team(team, free=True)
		name = keys.provision_key(team, limits=[{"metric": "requests", "window": "1m", "value": 200}])["name"]
		self.assertEqual(keys.limits(team, name), [{"metric": "requests", "window": "1m", "value": 200}])
		with self.assertRaises(frappe.DoesNotExistError):
			keys.limits("nobody-limits", name)

	def test_the_fleet_stays_out_of_reach(self):
		for doctype in WITHHELD:
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit=1)

	def test_provisioning_again_keeps_the_team_and_its_keys(self):
		team = "TEAM-PROBE"
		self.assertEqual(teams.provision_team(team, free=True), {"team": team, "max_keys": 0})
		key = keys.provision_key(team)["api_key"]
		self.assertEqual(teams.provision_team(team), {"team": team, "max_keys": 0})
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(key)}, "team"), team)
		self.assertEqual(frappe.db.get_value("Central Team", team, "free"), 1)
		with self.assertRaises(frappe.ValidationError):
			teams.provision_team("")

	def test_a_key_is_minted_for_a_known_team_in_a_geography(self):
		team = "probe-keyed"
		with self.assertRaises(frappe.ValidationError, msg="a key does not create its team"):
			keys.provision_key(team, title="laptop")

		teams.provision_team(team, free=True)
		here, away = make_test_geography(), make_test_geography("probe-away")
		result = keys.provision_key(team, title="laptop", geography=here)
		self.assertTrue(result["api_key"].startswith(KEY_PREFIX))
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(result["api_key"])}, "title"), "laptop")
		self.assertEqual((result["geography"], result["gateway_url"]), (here, self.endpoint(here)))
		abroad = keys.provision_key(team, title="eu", geography=away)
		self.assertEqual((abroad["geography"], abroad["gateway_url"]), (away, self.endpoint(away)))

	def test_caps_fit_the_spend_limit_and_a_revoke_frees_its_cap(self):
		team = "probe-cap"
		teams.provision_team(team)
		keys.provision_key(team, title="uncapped")
		self.assertEqual(keys.keys(team)[0]["cap"], 0)
		tier("probe-cap-tier", max_keys=5, spend_limit=10)
		self.assertEqual(
			teams.update_team(team, tier="probe-cap-tier"),
			{"team": team, "free": 0, "suspended": 0, "tier": "probe-cap-tier", "override_max_keys": 0, "max_keys": 5, "spend_limit": 10},
		)
		first = keys.provision_key(team, title="first", cap=6)["name"]
		with self.assertRaises(frappe.ValidationError):
			keys.provision_key(team, title="too much", cap=6)
		second = keys.provision_key(team, title="second", cap=4)["name"]
		self.assertEqual(usage.spend(team), {
			"spent": 0.0, "month_spent": 0.0, "capped": 10.0, "spend_limit": 10.0,
			"max_keys": 5, "suspended": False,
		})
		self.assertEqual(keys.update_key(team, second, cap=2)["cap"], 2)
		self.assertEqual(usage.spend(team)["capped"], 8.0)
		frappe.db.set_value("Grove API Key", first, "creation", frappe.utils.add_to_date(None, hours=-7), update_modified=False)
		keys.revoke_key(team, first)
		self.assertEqual(usage.spend(team)["capped"], 2.0)

	def test_caps_move_between_keys_in_one_call_or_not_at_all(self):
		team = "probe-caps"
		teams.provision_team(team)
		teams.update_team(team, tier=tier("probe-caps-tier", spend_limit=10))
		first = keys.provision_key(team, title="first", cap=6)["name"]
		second = keys.provision_key(team, title="second", cap=4)["name"]
		# 6/4 to 2/8: saved one by one in the order given, the raise would pass the limit first.
		rows = keys.update_caps(team, {second: 8, first: 2})
		self.assertEqual({row.name: row.cap for row in rows}, {first: 2, second: 8})
		with self.assertRaisesRegex(frappe.ValidationError, "spend limit of 10"):
			keys.update_caps(team, {second: 3, first: 9})
		self.assertEqual({row.name: row.cap for row in keys.keys(team)}, {first: 2, second: 8})
		teams.provision_team("probe-caps-other")
		stranger = keys.provision_key("probe-caps-other", title="theirs")["name"]
		with self.assertRaises(frappe.DoesNotExistError):
			keys.update_caps(team, {stranger: 1})

	def test_a_keys_month_spent_is_its_billed_records_this_month_revoked_or_not(self):
		team = "probe-month"
		teams.provision_team(team)
		key = keys.provision_key(team, title="monthly")["name"]
		frappe.db.set_value("Grove API Key", key, "spent", 5)
		today = utc_today()
		for day, cost in ((today, 2), (frappe.utils.add_months(today, -1), 3)):
			frappe.get_doc({
				"doctype": "Usage Record", "api_key": key, "team": team, "day": day, "drain_id": f"probe-month-{day}",
				"billed": 1, "request_count": 1, "cost": cost, "usage": "[]",
			}).insert(ignore_permissions=True)
		[listed] = keys.keys(team)
		self.assertEqual((listed["spent"], listed["month_spent"]), (5, 2))
		self.assertEqual(usage.spend(team)["month_spent"], 2)
		# Revoked, the key still spent it this month: Central invoices it.
		frappe.db.set_value("Grove API Key", key, "status", "revoked")
		self.assertEqual(usage.spend(team)["month_spent"], 2)
		self.assertTrue(teams.update_team(team, suspended=True)["suspended"])

	def test_central_sets_free_and_turning_it_off_resets_the_caps(self):
		team = "probe-free"
		teams.provision_team(team, free=True)
		teams.update_team(team, tier=tier("probe-free-tier", spend_limit=5))
		key = keys.provision_key(team, title="generous", cap=7)["name"]
		self.assertTrue(frappe.db.get_value("Central Team", team, "free"))
		self.assertEqual(teams.update_team(team, free=False)["free"], 0)
		self.assertEqual(frappe.db.get_value("Grove API Key", key, "cap"), 0)
		self.assertEqual(teams.update_team(team, free=True)["free"], 1)

	def test_a_teams_tier_sets_the_limits_of_its_unpinned_keys(self):
		team = "probe-tier"
		teams.provision_team(team)
		unpinned = keys.provision_key(team, title="inherits")["name"]
		pinned = keys.provision_key(team, title="pinned", limits=[{"metric": "requests", "window": "1m", "value": 5}])["name"]
		defaults = [
			{"metric": "requests", "window": "1m", "value": 20}, {"metric": "total_tokens", "window": "1m", "value": 100_000},
		]
		self.assertEqual(keys.limits(team, unpinned), defaults)
		growth = [{"metric": "requests", "window": "1m", "value": 60}]
		tier("probe-growth", max_keys=3, spend_limit=50, limits=[dict(r) for r in growth])
		limits = teams.update_team(team, tier="probe-growth")
		self.assertEqual((limits["tier"], limits["max_keys"], limits["spend_limit"]), ("probe-growth", 3, 50))
		listed = {row.name: row.limits for row in keys.keys(team)}
		self.assertEqual((listed[unpinned], listed[pinned]), (growth, [{"metric": "requests", "window": "1m", "value": 5}]))
		# A tier Central names before Grove has one: a copy of the default tier, never refused.
		frappe.db.set_single_value("Grove Settings", "default_tier", "probe-growth")
		self.addCleanup(frappe.db.set_single_value, "Grove Settings", "default_tier", "")
		self.assertEqual(teams.update_team(team, tier="probe-unseen")["max_keys"], 3)
		self.assertEqual([row.value for row in frappe.get_doc("Tier", "probe-unseen").limits], [60])
		# No tier: the default tier's. No default either: the hard-coded limits, and no cap allowed.
		self.assertEqual((teams.update_team(team, tier="")["spend_limit"], keys.limits(team, unpinned)), (50, growth))
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		self.assertEqual((usage.spend(team)["spend_limit"], keys.limits(team, unpinned)), (0, defaults))

	def test_a_minted_key_is_unique_by_its_hash(self):
		team = "probe-unique"
		teams.provision_team(team, free=True)
		same_bits = unittest.mock.patch(
			"grove.grove.doctype.grove_api_key.grove_api_key.secrets.token_hex", return_value="0" * 48
		)
		with same_bits:
			keys.provision_key(team, title="one")
			with self.assertRaises(frappe.UniqueValidationError):
				keys.provision_key(team, title="twin")

	def test_a_teams_keys_are_listed_without_their_secret(self):
		team = "probe-listed"
		teams.provision_team(team, free=True)
		minted = keys.provision_key(team, title="laptop", cap=0)

		[listed] = keys.keys(team)
		self.assertEqual((listed.name, listed.title, listed.status), (minted["name"], "laptop", "active"))
		self.assertEqual(listed.key_hash, hash_secret(minted["api_key"]))
		self.assertEqual((listed.geography, listed.gateway_url, listed.cap, listed.spent), (minted["geography"], minted["gateway_url"], 0, 0))
		self.assertEqual([row["metric"] for row in listed.limits], ["requests", "total_tokens"])
		self.assertEqual(
			listed.revocable_at, utc_timestamp(frappe.utils.add_to_date(listed.creation, hours=6))
		)
		self.assertEqual(listed.masked, minted["api_key"][:7] + "…")
		self.assertNotIn(minted["api_key"], str(listed))
		self.assertEqual(keys.keys("probe-nobody"), [])

	def test_a_key_is_revoked_by_name_only_for_the_team_holding_it(self):
		team, other = "probe-revoker", "probe-revoker-other"
		for each in (team, other):
			teams.provision_team(each, free=True)
		key = keys.provision_key(team, title="ci")["name"]

		with self.assertRaises(frappe.DoesNotExistError):
			keys.revoke_key(other, key)
		with self.assertRaises(frappe.DoesNotExistError):
			keys.update_key(other, key, cap=0)

		with self.assertRaises(frappe.ValidationError):
			keys.revoke_key(team, key)
		frappe.db.set_value(
			"Grove API Key", key, "creation", frappe.utils.add_to_date(None, hours=-7), update_modified=False
		)
		keys.revoke_key(team, key)
		self.assertEqual(frappe.db.get_value("Grove API Key", key, "status"), "revoked")

	def pull_counter(self, team):
		"""A team to pull and its counter, cleared now and after: Redis is not rolled back."""
		teams.provision_team(team)
		key = frappe.cache.make_key(f"usage_pull:{team}")
		frappe.cache.delete(key)
		self.addCleanup(frappe.cache.delete, key)
		return team, key

	def test_the_control_role_pulls_a_team_on_demand_a_few_times_an_hour(self):
		team, other = "probe-pull", "probe-pull-other"
		name, key = self.pull_counter(team)
		other_name, _ = self.pull_counter(other)

		with unittest.mock.patch("grove.pathway.usage.pull_all", return_value="PS-1") as pull_all:
			for _ in range(usage.PULLS_PER_HOUR):
				self.assertEqual(usage.pull_usage(team), {"sync": "PS-1"})
			pull_all.assert_called_with(trigger="Manual", wait=60, team=name)
			with self.assertRaises(frappe.RateLimitExceededError):
				usage.pull_usage(team)
			self.assertEqual(pull_all.call_count, usage.PULLS_PER_HOUR)

			self.assertEqual(usage.pull_usage(other), {"sync": "PS-1"}, "the count is per team")
			pull_all.assert_called_with(trigger="Manual", wait=60, team=other_name)
		self.assertGreater(frappe.cache.ttl(key), 0, "the counter expires")

		frappe.set_user("Guest")
		counted = int(frappe.cache.get(key))
		with self.assertRaises(frappe.PermissionError):
			usage.pull_usage(team)
		self.assertEqual(int(frappe.cache.get(key)), counted, "a refused caller spends nothing")
