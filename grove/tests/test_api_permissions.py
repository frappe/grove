# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""grove.api runs under permission checks, not around them: the reads go through
frappe.get_list and the writes through a plain save, so the Grove Control role has to carry
exactly what those endpoints touch — and nothing more. The role is checked as well
(`for_control_clients`), and each gate holds with the other off. Site-backed: DocPerms are the
behaviour under test."""

import base64
import hashlib
import hmac
import unittest.mock
from pathlib import Path

import frappe
from frappe.auth import LoginAttemptTracker
from frappe.tests import IntegrationTestCase

from grove import auth
from grove.access import limits_in_force
from grove.api import catalog, control, keys, teams, usage
from grove.billing.pricing import forget_figures
from grove.grove.doctype.grove_api_key.grove_api_key import KEY_PREFIX, hash_secret
from grove.grove.doctype.model_provider.model_provider import self_hosted_provider
from grove.infrastructure.doctype.geography.test_geography import make_test_geography
from grove.utils import utc_today

PROBE = "control-probe@example.com"


def tier(name, **fields):
	return frappe.get_doc({"doctype": "Tier", "__newname": name, **fields}).insert().name


def probe_model_id(model_id):
	"""A Model ID the probe user could not make itself: the fixture is the test's, not the probe's."""
	return frappe.get_doc({"doctype": "Model ID", "model_id": model_id}).insert(ignore_permissions=True).name


def billed_record(key, team, day, cost, drain=""):
	"""A pulled Usage Record of `cost` served on `day`, the figures forgotten as the pull does:
	what month_spent reads."""
	frappe.get_doc(
		{
			"doctype": "Usage Record",
			"api_key": key,
			"team": team,
			"day": day,
			"drain_id": f"probe-{key}-{day}{drain}",
			"request_count": 1,
			"cost": cost,
			"usage": "[]",
		}
	).insert(ignore_permissions=True)
	forget_figures(team)


def other_user(email, *roles):
	"""A user holding `roles` alone, the test's fixture: the probe may not make a User."""
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "Probe",
			"send_welcome_email": 0,
			"roles": [{"role": role} for role in roles],
		}
	).insert(ignore_permissions=True)
	return email


WITHHELD = ("Machine", "Inference Server", "Model Replica", "Monitoring Agent")
# Reached without a session: a secret of their own is the gate.
GUEST_GATED = {"create_control_client", "team_webhook"}


class TestTheControlRoleReachesOnlyWhatItServes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		"""One probe user for the class: IntegrationTestCase rolls back once at the end, not
		per test, so a per-test insert would collide with itself."""
		super().setUpClass()
		cls.enterClassContext(unittest.mock.patch("grove.pathway.usage.peek_team", return_value={}))
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
		self.assertIsInstance(catalog.available_models("nobody"), list)
		self.assertEqual(usage.usage(["nobody"])["model_summary"], [])
		[geography] = [g for g in catalog.geographies() if g["name"] == make_test_geography()]
		self.assertEqual(geography["endpoint"], self.endpoint(geography["name"]))
		host = frappe.db.get_value("Geography", geography["name"], "endpoint")
		self.addCleanup(frappe.db.set_value, "Geography", geography["name"], "endpoint", host)
		frappe.db.set_value("Geography", geography["name"], "endpoint", "")
		[bare] = [g for g in catalog.geographies() if g["name"] == geography["name"]]
		self.assertIsNone(bare["endpoint"])

	def test_a_key_is_shown_only_the_models_it_may_call(self):
		def published_model(model_id):
			model = (
				frappe.get_doc(
					{"doctype": "Model", "model_id": probe_model_id(model_id), "hf_repo": f"org/{model_id}"}
				)
				.insert(ignore_permissions=True)
				.name
			)
			frappe.db.set_value("Model", model, "published", 1)
			return model

		grouped, denied, allowed, other = (published_model(f"probe-reach-{n}") for n in "abcd")
		frappe.get_doc(
			{
				"doctype": "Model Group",
				"__newname": "probe-reach",
				"models": [{"model": grouped}, {"model": denied}],
			}
		).insert(ignore_permissions=True)
		team = "probe-reach"
		teams.provision_team(team)
		policy = frappe.get_doc("Grove Team", team)
		policy.model_groups = []
		policy.save()
		self.assertEqual(catalog.available_models(team), [])

		policy.update(
			{
				"model_groups": [{"model_group": "probe-reach"}],
				"allow": [{"model": allowed}],
				"deny": [{"model": denied}],
			}
		)
		policy.save()
		model_key = lambda doc: frappe.db.get_value("Model", doc, "model_key")  # noqa: E731
		reach = sorted([model_key(grouped), model_key(allowed)])
		frappe.db.set_value("Model ID", "probe-reach-a", "display_name", "Probe Reach A")
		frappe.db.set_value("Model Provider", self_hosted_provider(), "display_name", "Our Engines")
		rows = catalog.available_models(team)
		# The team's own reach, never the default group's or another team's.
		self.assertEqual(sorted(row["name"] for row in rows), reach)
		self.assertEqual(
			{row["name"] for row in rows}, {f"{row['provider']}/{row['model_id']}" for row in rows}
		)
		# The Model ID's display name, else the id; the provider's, else its name.
		self.assertEqual({row["display_name"] for row in rows}, {"Probe Reach A", "probe-reach-c"})
		self.assertEqual({row["provider_display_name"] for row in rows}, {"Our Engines"})
		# A key's own group (a trial's) is that key's alone: the team's catalog does not show it.
		frappe.get_doc(
			{"doctype": "Model Group", "__newname": "probe-reach-own", "models": [{"model": other}]}
		).insert(ignore_permissions=True)
		keys.provision_key(team, model_groups=["probe-reach-own"])
		self.assertEqual(sorted(row["name"] for row in catalog.available_models(team)), reach)
		here = make_test_geography()
		keeper = frappe.get_doc(
			{"doctype": "Model Provider", "provider_name": "_T-probe-keeper", "geography": here}
		).insert(ignore_permissions=True)
		kept = (
			frappe.get_doc(
				{"doctype": "Model", "model_id": probe_model_id("probe-reach-kept"), "provider": keeper.name}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.set_value("Model", kept, "published", 1)
		frappe.get_doc(
			{
				"doctype": "Model Group",
				"__newname": "probe-reach-default",
				"geography": here,
				"is_default": 1,
				"models": [{"model": other}, {"model": grouped}, {"model": kept}],
			}
		).insert(ignore_permissions=True)
		# Elsewhere: its Allow of a model of ours reaches there, that geography's default group does not.
		self.assertEqual(
			[row["name"] for row in catalog.available_models(team, geography=here)], [model_key(allowed)]
		)
		policy.append("model_groups", {"model_group": "probe-reach-default"})
		policy.save()
		rows = catalog.available_models(team, geography=here)
		self.assertEqual(
			sorted(row["name"] for row in rows),
			sorted([model_key(allowed), model_key(grouped), model_key(other), model_key(kept)]),
		)
		# Ours keep nothing; a vendor says so itself.
		self.assertEqual(
			{row["name"]: row["zero_data_retention"] for row in rows},
			{
				model_key(allowed): True,
				model_key(grouped): True,
				model_key(other): True,
				model_key(kept): False,
			},
		)

	def test_the_fleet_stays_out_of_reach(self):
		for doctype in WITHHELD:
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit=1)

	def test_provisioning_again_keeps_the_team_and_its_keys(self):
		team = "TEAM-PROBE"
		frappe.clear_messages()
		self.assertEqual(teams.provision_team(team), {"team": team})
		self.assertEqual([], frappe.get_message_log())
		key = keys.provision_key(team)["api_key"]
		self.assertEqual(teams.provision_team(team), {"team": team})
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(key)}, "team"), team)

	def test_a_total_team_is_not_the_control_roles_to_make(self):
		"""The role inserts teams, so a plain REST insert could name the window itself."""
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": "Grove Team", "__newname": "probe-total", "key_cap_window": "Total"}
			).insert()

	def test_a_key_is_minted_for_a_known_team_in_a_geography(self):
		team = "probe-keyed"
		with self.assertRaises(frappe.ValidationError, msg="a key does not create its team"):
			keys.provision_key(team, title="laptop")

		teams.provision_team(team)
		here, away = make_test_geography(), make_test_geography("probe-away")
		result = keys.provision_key(team, title="laptop", geography=here)
		self.assertTrue(result["api_key"].startswith(KEY_PREFIX))
		self.assertEqual(
			frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(result["api_key"])}, "title"),
			"laptop",
		)
		self.assertEqual((result["geography"], result["gateway_url"]), (here, self.endpoint(here)))
		abroad = keys.provision_key(team, title="eu", geography=away)
		self.assertEqual((abroad["geography"], abroad["gateway_url"]), (away, self.endpoint(away)))

	def test_caps_fit_the_spend_limit_and_a_revoke_frees_its_cap(self):
		team = "probe-cap"
		teams.provision_team(team)
		keys.provision_key(team, title="uncapped")
		self.assertEqual(usage.figures(team)["keys"][0]["cap"], 0)
		tier("probe-cap-tier", spend_limit=10)
		self.assertEqual(
			teams.update_team(team, tier="probe-cap-tier"),
			{
				"team": team,
				"suspended": 0,
				"log_payloads": 1,
				"key_cap_window": "Monthly",
				"tier": "probe-cap-tier",
				"spend_limit": 10,
			},
		)
		first = keys.provision_key(team, title="first", cap=6)["name"]
		with self.assertRaises(frappe.ValidationError):
			keys.provision_key(team, title="too much", cap=6)
		second = keys.provision_key(team, title="second", cap=4)["name"]
		self.assertEqual(
			usage.figures(team)["spend"],
			{
				"month_spent": 0.0,
				"capped": 10.0,
				"committed": 10.0,
				"spend_limit": 10.0,
				"limits": [],
				"suspended": False,
				"log_payloads": True,
				"key_cap_window": "Monthly",
			},
		)
		self.assertEqual(keys.update_key(team, second, cap=2)["cap"], 2)
		self.assertEqual(usage.figures(team)["spend"]["capped"], 8.0)
		keys.revoke_key(team, first)
		self.assertEqual(
			(usage.figures(team)["spend"]["capped"], usage.figures(team)["spend"]["committed"]), (2.0, 2.0)
		)

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
		self.assertEqual({row.name: row.cap for row in usage.figures(team)["keys"]}, {first: 2, second: 8})
		teams.provision_team("probe-caps-other")
		stranger = keys.provision_key("probe-caps-other", title="theirs")["name"]
		with self.assertRaises(frappe.DoesNotExistError):
			keys.update_caps(team, {stranger: 1})

	def test_a_keys_month_spent_is_its_records_this_month_revoked_or_not(self):
		team = "probe-month"
		teams.provision_team(team)
		key = keys.provision_key(team, title="monthly")["name"]
		today = utc_today()
		for day, cost in ((today, 2), (frappe.utils.add_months(today, -1), 3)):
			billed_record(key, team, day, cost)
		[listed] = usage.figures(team)["keys"]
		self.assertEqual(listed["month_spent"], 2)
		self.assertEqual(usage.figures(team)["spend"]["month_spent"], 2)
		frappe.db.set_value("Grove API Key", key, "status", "revoked")
		self.assertEqual(usage.figures(team)["spend"]["month_spent"], 2)
		self.assertTrue(teams.update_team(team, suspended=True)["suspended"])

	def test_a_revoked_key_lists_after_every_live_one(self):
		team = "probe-order"
		teams.provision_team(team)
		older = keys.provision_key(team, title="older")["name"]
		newer = keys.provision_key(team, title="newer")["name"]
		self.assertEqual([row.name for row in keys.key_rows([older, newer])], [newer, older])
		frappe.db.set_value("Grove API Key", newer, "status", "revoked")
		self.assertEqual([row.name for row in keys.key_rows([older, newer])], [older, newer])

	def test_a_teams_tier_sets_its_limits_and_its_own_rows_stand_in(self):
		team = "probe-tier"
		teams.provision_team(team)
		self.addCleanup(forget_figures, team)
		defaults = [
			{"model_key": "", "geography": "", "metric": "requests", "window": "1m", "value": 20},
			{"model_key": "", "geography": "", "metric": "total_tokens", "window": "1m", "value": 100_000},
		]
		self.assertEqual(usage.figures(team)["spend"]["limits"], defaults)
		growth = [{"model_key": "", "geography": "", "metric": "requests", "window": "1m", "value": 60}]
		tier("probe-growth", spend_limit=50, limits=[dict(r) for r in growth])
		limits = teams.update_team(team, tier="probe-growth")
		self.assertEqual((limits["tier"], limits["spend_limit"]), ("probe-growth", 50))
		self.assertEqual(usage.figures(team)["spend"]["limits"], growth)
		doc = frappe.get_doc("Grove Team", team)
		doc.append("limits", {"metric": "requests", "window": "1m", "value": 5})
		doc.save()
		self.assertEqual(usage.figures(team)["spend"]["limits"], [{**growth[0], "value": 5}])
		doc.set("limits", [])
		doc.save()
		frappe.db.set_single_value("Grove Settings", "default_tier", "probe-growth")
		self.addCleanup(frappe.db.set_single_value, "Grove Settings", "default_tier", "")
		self.assertEqual(teams.update_team(team, tier="probe-unseen")["spend_limit"], 50)
		self.assertEqual([row.value for row in frappe.get_doc("Tier", "probe-unseen").limits], [60])
		self.assertEqual(
			(teams.update_team(team, tier="")["spend_limit"], usage.figures(team)["spend"]["limits"]),
			(50, growth),
		)
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		forget_figures(team)
		self.assertEqual(
			(usage.figures(team)["spend"]["spend_limit"], usage.figures(team)["spend"]["limits"]),
			(0, defaults),
		)

	def test_a_minted_key_is_unique_by_its_hash(self):
		team = "probe-unique"
		teams.provision_team(team)
		same_bits = unittest.mock.patch(
			"grove.grove.doctype.grove_api_key.grove_api_key.secrets.token_hex", return_value="0" * 48
		)
		with same_bits:
			keys.provision_key(team, title="one")
			with self.assertRaises(frappe.UniqueValidationError):
				keys.provision_key(team, title="twin")

	def test_a_teams_keys_are_listed_without_their_secret(self):
		team = "probe-listed"
		teams.provision_team(team)
		minted = keys.provision_key(team, title="laptop", cap=0)

		[listed] = usage.figures(team)["keys"]
		self.assertEqual((listed.name, listed.title, listed.status), (minted["name"], "laptop", "active"))
		self.assertEqual(listed.key_hash, hash_secret(minted["api_key"]))
		self.assertEqual(
			(listed.geography, listed.gateway_url, listed.cap, listed.month_spent),
			(minted["geography"], minted["gateway_url"], 0, 0),
		)
		self.assertEqual(listed.limits, usage.figures(team)["spend"]["limits"])
		self.assertIsInstance(listed.limits, list)
		self.assertEqual(listed.masked, minted["api_key"][:7] + "…")
		self.assertNotIn(minted["api_key"], str(listed))
		with self.assertRaises(frappe.ValidationError):
			usage.figures("probe-nobody")

	def test_a_key_is_revoked_by_name_only_for_the_team_holding_it(self):
		team, other = "probe-revoker", "probe-revoker-other"
		for each in (team, other):
			teams.provision_team(each)
		key = keys.provision_key(team, title="ci")["name"]

		with self.assertRaises(frappe.DoesNotExistError):
			keys.revoke_key(other, key)
		with self.assertRaises(frappe.DoesNotExistError):
			keys.update_key(other, key, cap=0)

		keys.revoke_key(team, key)
		self.assertEqual(frappe.db.get_value("Grove API Key", key, "status"), "revoked")

	def test_a_bulk_revoke_is_refused_whole_for_a_key_of_another_team(self):
		team, other = "probe-revoker-bulk", "probe-revoker-bulk-other"
		for each in (team, other):
			teams.provision_team(each)
		names = [keys.provision_key(team, title=f"k{i}")["name"] for i in range(2)]
		theirs = keys.provision_key(other, title="theirs")["name"]

		with self.assertRaises(frappe.DoesNotExistError):
			keys.revoke_keys(team, [names[0], theirs])
		self.assertEqual(frappe.db.get_value("Grove API Key", names[0], "status"), "active")
		self.assertEqual(keys.revoke_keys(team, names), names)
		self.assertEqual(
			frappe.get_all("Grove API Key", filters={"name": ("in", names)}, pluck="status"),
			["revoked", "revoked"],
		)

	def test_a_suspended_teams_keys_change_only_by_its_archive(self):
		team = "probe-suspended"
		teams.provision_team(team)
		teams.update_team(team, tier=tier("probe-suspended-tier", spend_limit=10))
		first = keys.provision_key(team, title="first", cap=2)["name"]
		second = keys.provision_key(team, title="second", cap=2)["name"]
		teams.update_team(team, suspended=True)
		for change in (
			lambda: keys.provision_key(team, title="third"),
			lambda: keys.update_key(team, first, cap=1),
			lambda: keys.update_caps(team, {first: 1}),
			lambda: keys.revoke_key(team, first),
			lambda: keys.revoke_keys(team, [first]),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "team is suspended"):
				change()
		self.assertEqual(len(usage.figures(team)["keys"]), 2, "reads stay open")

		# Closing the team is Grove's own doing: it goes through a suspension.
		self.assertCountEqual(teams.archive_team(team), [first, second])
		self.assertEqual(frappe.db.count("Grove API Key", {"team": team, "status": "active"}), 0)
		teams.update_team(team, suspended=False)
		keys.provision_key(team, title="after")

	def test_a_key_is_handed_over_by_name_only_for_the_team_holding_it(self):
		team, other, third = "probe-handover", "probe-handover-to", "probe-handover-third"
		for each in (team, other, third):
			teams.provision_team(each)
		# A hand-over crosses cap windows: the sending team counts a key's spend for life.
		frappe.db.set_value("Grove Team", team, "key_cap_window", "Total")
		key = keys.provision_key(team, title="ci")["name"]

		with self.assertRaises(frappe.DoesNotExistError):
			keys.transfer_key(third, key, other)
		row = keys.transfer_key(team, key, other, title="site.example.test")
		self.assertEqual((row.name, row.title), (key, "site.example.test"))
		self.assertEqual(frappe.db.get_value("Grove API Key", key, "team"), other)
		# The sender may ask again after a lost reply: the receiving team holds it, nothing moves.
		again = keys.transfer_key(team, key, other, title="site.example.test")
		self.assertEqual(again.title, "site.example.test")
		with self.assertRaises(frappe.DoesNotExistError):
			keys.transfer_key(team, key, third)

	def test_a_handed_over_keys_month_spent_is_its_records_under_its_team_now(self):
		trial, customer = "probe-handover-spent", "probe-handover-spent-to"
		for each in (trial, customer):
			teams.provision_team(each)
		frappe.db.set_value("Grove Team", trial, "key_cap_window", "Total")
		key = keys.provision_key(trial, title="trial")["name"]
		billed_record(key, trial, utc_today(), 2)
		keys.transfer_key(trial, key, customer)
		billed_record(key, customer, utc_today(), 1, drain="-paid")
		[listed] = usage.figures(customer)["keys"]
		self.assertEqual((listed["month_spent"], usage.figures(customer)["spend"]["month_spent"]), (1, 1))
		self.assertEqual(usage.figures(trial)["spend"]["month_spent"], 2)

	def test_a_tier_edit_shows_at_once_and_a_default_tier_switch_waits_out_the_cache(self):
		from grove.billing.pricing import forget_figures

		# A raised spend limit must show at once, or the console keeps refusing a cap that now fits.
		# Which tier is the default is switched too rarely to walk every team for.
		frappe.set_user("Administrator")
		was = frappe.db.get_single_value("Grove Settings", "default_tier")
		self.addCleanup(frappe.db.set_single_value, "Grove Settings", "default_tier", was)
		default = frappe.get_doc("Tier", tier("probe-default", spend_limit=30))
		frappe.db.set_single_value("Grove Settings", "default_tier", default.name)
		team = "probe-default-tier"
		teams.provision_team(team)
		self.addCleanup(forget_figures, team)
		self.assertEqual(usage.figures(team)["spend"]["spend_limit"], 30)

		default.spend_limit = 37
		default.save()
		self.assertEqual(usage.figures(team)["spend"]["spend_limit"], 37, "the tier's save forgot them")
		settings = frappe.get_doc("Grove Settings")
		settings.default_tier = tier("probe-other-default", spend_limit=50)
		settings.save()
		self.assertEqual(usage.figures(team)["spend"]["spend_limit"], 37, "kept through the switch")
		forget_figures(team)
		self.assertEqual(usage.figures(team)["spend"]["spend_limit"], 50, "read afresh")

	def test_the_figures_are_one_read_kept_until_something_moves_them(self):
		from grove.billing.pricing import forget_figures

		team = "probe-figures"
		teams.provision_team(team)
		teams.update_team(team, tier=tier("probe-figures-tier", spend_limit=20))
		self.addCleanup(forget_figures, team)
		key = keys.provision_key(team, title="kept")["name"]
		billed_record(key, team, utc_today(), 2)

		first = usage.figures(team)
		self.assertEqual(
			(first["spend"]["month_spent"], first["keys"][0]["month_spent"], first["usage"]["teams"]),
			(2, 2, [team]),
		)
		with unittest.mock.patch("grove.api.usage.spend_of", side_effect=AssertionError("read again")):
			self.assertEqual(usage.figures(team), first, "the three come off one entry")

		billed_record(key, team, utc_today(), 3, drain="-later")
		self.assertEqual(usage.figures(team)["spend"]["month_spent"], 5)
		keys.update_caps(team, {key: 6})
		self.assertEqual(
			(usage.figures(team)["keys"][0]["cap"], usage.figures(team)["spend"]["capped"]), (6, 6)
		)
		teams.update_team(team, suspended=True)
		self.assertTrue(usage.figures(team)["spend"]["suspended"])
		self.assertFalse(teams.update_team(team, log_payloads=False)["log_payloads"])
		self.assertFalse(usage.figures(team)["spend"]["log_payloads"])

		tomorrow = frappe.utils.add_days(utc_today(), 1)
		with (
			unittest.mock.patch("grove.billing.pricing.utc_today", return_value=tomorrow),
			unittest.mock.patch("grove.api.usage.spend_of", return_value={"stub": 1}),
		):
			self.assertEqual(usage.figures(team)["spend"], {"stub": 1})

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
				answer = usage.pull_usage(team)
				self.assertEqual((answer["sync"], answer["figures"]["spend"]["month_spent"]), ("PS-1", 0))
			pull_all.assert_called_with(trigger="Manual", wait=60, team=name)
			with self.assertRaises(frappe.RateLimitExceededError):
				usage.pull_usage(team)
			self.assertEqual(pull_all.call_count, usage.PULLS_PER_HOUR)

			self.assertEqual(usage.pull_usage(other)["sync"], "PS-1", "the count is per team")
			pull_all.assert_called_with(trigger="Manual", wait=60, team=other_name)
		self.assertGreater(frappe.cache.ttl(key), 0, "the counter expires")

		frappe.set_user("Guest")
		counted = int(frappe.cache.get(key))
		with self.assertRaises(frappe.PermissionError):
			usage.pull_usage(team)
		self.assertEqual(int(frappe.cache.get(key)), counted, "a refused caller spends nothing")

	def test_centrals_webhook_lands_signed_and_a_replay_is_ignored(self):
		team = "probe-hook"
		teams.provision_team(team)
		credential = control.create_control_client_key()
		tier("probe-hook-a")
		tier("probe-hook-b")
		original = getattr(frappe.local, "request", None)
		self.addCleanup(setattr, frappe.local, "request", original)
		self.addCleanup(setattr, frappe.local, "request_ip", frappe.local.request_ip)
		frappe.local.request_ip = "203.0.113.9"
		self.addCleanup(LoginAttemptTracker("webhook:203.0.113.9").add_success_attempt)
		frappe.set_user("Guest")

		def deliver(headers=None, **body):
			raw = frappe.as_json(body).encode()
			signature = base64.b64encode(
				hmac.new(credential["api_secret"].encode(), raw, hashlib.sha256).digest()
			)
			frappe.local.request = frappe._dict(
				headers={
					teams.CONTROL_KEY_HEADER: credential["api_key"],
					teams.WEBHOOK_SECRET_HEADER: signature.decode(),
					**(headers or {}),
				},
				get_data=lambda: raw,
			)
			return teams.team_webhook(**body)

		applied = deliver(team=team, tier="probe-hook-a", observed_at="2026-10-15 10:00:00.000001")
		self.assertEqual((applied["tier"], frappe.session.user), ("probe-hook-a", PROBE))
		self.assertEqual(
			str(frappe.db.get_value("Grove Team", team, "last_reported_at")), "2026-10-15 10:00:00.000001"
		)
		for stale in ("2026-10-15 10:00:00.000001", "2026-10-15 09:59:59"):
			replay = deliver(team=team, tier="probe-hook-b", observed_at=stale)
			self.assertEqual((replay["ignored"], replay["tier"]), ("not newer", "probe-hook-a"))
		self.assertEqual(
			deliver(team=team, tier="probe-hook-b", observed_at="2026-10-15 10:00:01")["tier"], "probe-hook-b"
		)
		frappe.set_user("Guest")
		self.assertIsNone(deliver(team="probe-hook-unknown", tier="probe-hook-a"))
		self.assertFalse(frappe.db.exists("Grove Team", "probe-hook-unknown"))

		for forged in (
			{teams.WEBHOOK_SECRET_HEADER: "bm90IHRoZSBzaWduYXR1cmU="},
			{teams.CONTROL_KEY_HEADER: "not-a-key"},
			{teams.CONTROL_KEY_HEADER: None},
		):
			frappe.set_user("Guest")
			with self.assertRaises(frappe.AuthenticationError):
				deliver(forged, team=team, tier="probe-hook-a")
		self.assertEqual(frappe.db.get_value("Grove Team", team, "tier"), "probe-hook-b")
		# Three bad signatures lock the address, a good one included; another address is untouched.
		with self.assertRaises(frappe.TooManyRequestsError):
			deliver(team=team, tier="probe-hook-a", observed_at="2026-10-15 10:00:02")
		frappe.local.request_ip = "203.0.113.10"
		self.assertEqual(
			deliver(team=team, tier="probe-hook-a", observed_at="2026-10-15 10:00:02")["tier"], "probe-hook-a"
		)

	def test_a_renamed_tier_label_follows_at_grove(self):
		team = "probe-rename"
		teams.provision_team(team)
		rows = [{"model_key": "", "geography": "", "metric": "requests", "window": "1m", "value": 7}]
		tier("probe-ren-a", limits=[dict(r) for r in rows])
		teams.update_team(team, tier="probe-ren-a")
		keys.provision_key(team, title="follows")

		teams.rename_tier("probe-ren-a", "probe-ren-b")
		self.assertEqual(frappe.db.get_value("Grove Team", team, "tier"), "probe-ren-b")
		self.assertFalse(frappe.db.exists("Tier", "probe-ren-a"))
		self.assertEqual(
			(teams.update_team(team)["tier"], limits_in_force({team: "probe-ren-b"})[team]),
			("probe-ren-b", rows),
		)
		self.assertIsNone(teams.rename_tier("probe-ren-unseen", "probe-ren-c"))
		self.assertFalse(frappe.db.exists("Tier", "probe-ren-c"))
		# A name Grove already has is left alone, not merged.
		tier("probe-ren-c")
		self.assertIsNone(teams.rename_tier("probe-ren-b", "probe-ren-c"))
		self.assertEqual(frappe.db.get_value("Grove Team", team, "tier"), "probe-ren-b")

	def team_with_a_key(self, team):
		"""A team holding all an endpoint could hand out or move: a key that expires, a report on
		record, a tier label, its figures remembered. → (team, key)"""
		for each in (team, f"{team}-to"):
			teams.provision_team(each)
		tier(f"{team}-tier")
		key = keys.provision_key(team, title="ci", expiry="7 Days")["name"]
		teams.update_team(team, observed_at="2026-01-01 00:00:00")
		self.addCleanup(frappe.cache.delete_value, f"figures:{team}")
		usage.figures(team)
		return team, key

	def control_calls(self, team, key):
		"""Every endpoint of the control clients, asked as one of them would."""
		return {
			"geographies": catalog.geographies,
			"available_models": lambda: catalog.available_models(team),
			"create_control_client_key": control.create_control_client_key,
			"provision_key": lambda: keys.provision_key(team, title="more"),
			"update_key": lambda: keys.update_key(team, key, cap=2),
			"update_caps": lambda: keys.update_caps(team, {key: 2}),
			"revoke_key": lambda: keys.revoke_key(team, key),
			"revoke_keys": lambda: keys.revoke_keys(team, [key]),
			"transfer_key": lambda: keys.transfer_key(team, key, f"{team}-to"),
			"provision_team": lambda: teams.provision_team(team),
			# Older than the report on record: answered without a save.
			"update_team": lambda: teams.update_team(team, suspended=True, observed_at="2020-01-01 00:00:00"),
			"archive_team": lambda: teams.archive_team(team),
			"rename_tier": lambda: teams.rename_tier(f"{team}-tier", f"{team}-renamed"),
			"usage": lambda: usage.usage([team]),
			"figures": lambda: usage.figures(team),
			"pull_usage": lambda: usage.pull_usage(team),
		}

	def test_an_operator_without_the_role_is_refused_by_every_endpoint(self):
		"""The role alone: a System Manager holds every doc permission these touch."""
		calls = self.control_calls(*self.team_with_a_key("probe-role-gate"))
		listed = {fn.__name__ for fn in frappe.whitelisted if fn.__module__.startswith("grove.api.")}
		self.assertEqual(set(calls) | GUEST_GATED, listed, "an endpoint is not asked here")
		frappe.set_user(other_user("probe-operator@example.com", "System Manager"))
		for name, call in calls.items():
			with self.assertRaises(frappe.PermissionError, msg=name):
				call()

	def test_a_caller_without_the_doc_permissions_is_refused_before_anything_moves(self):
		"""The doc permissions alone, the role check switched off: what a role handed out too
		widely would meet. Refused before a remembered answer, a store or an unsaved write."""
		team, key = self.team_with_a_key("probe-doc-gate")
		# Suspended, which is not said to a caller who cannot read the team; remembered again after.
		teams.update_team(team, suspended=True)
		usage.figures(team)
		calls = self.control_calls(team, key)
		# It touches the caller's own User alone, which no doc permission covers: the role is its gate.
		del calls["create_control_client_key"]
		# Read off the projection, which checks nothing: the role is its gate.
		del calls["available_models"]
		expires_at = frappe.db.get_value("Grove API Key", key, "expires_at")
		frappe.set_user(other_user("probe-no-role@example.com"))
		with (
			unittest.mock.patch("frappe.only_for"),
			unittest.mock.patch("grove.pathway.usage.pull_all") as pull_all,
			unittest.mock.patch("grove.pathway.usage.peek_team") as peek_team,
			unittest.mock.patch("grove.api.usage._count_pull") as count_pull,
		):
			for name, call in calls.items():
				with self.assertRaises(frappe.PermissionError, msg=name):
					call()
			for reached in (pull_all, peek_team, count_pull):
				reached.assert_not_called()
		self.assertEqual(frappe.db.get_value("Grove API Key", key, "expires_at"), expires_at)

	def test_only_an_operator_reaches_past_the_api(self):
		"""The control role writes Grove Team and Grove API Key so that its endpoints run under
		permission checks. Over REST, `frappe.client` or a Desk button that same write would reach
		the operator's field (`suspended_at_grove`). And any signed-in user may call whatever is
		whitelisted: everyone signed in but an operator is kept to `grove.api`."""
		self.assertIn("grove.auth.hook", frappe.get_hooks("auth_hooks"))
		self.addCleanup(setattr, frappe.local, "request", getattr(frappe.local, "request", None))

		def ask(path, **form):
			frappe.local.request = frappe._dict(path=path)
			with unittest.mock.patch.dict(frappe.local.form_dict, form, clear=True):
				auth.hook()

		own = "/api/method/grove.api.teams.update_team"
		elsewhere = (
			"/api/resource/Grove Team/probe",
			"/api/v2/document/Grove Team/probe",
			"/api/method/frappe.client.set_value",
			"/api/method/run_doc_method",
			"/api/method/grove.grove.page.usage.usage.peek",
			"/api/method/grove.monitoring.targets",
			"/app/grove-team",
		)
		for user in (PROBE, other_user("probe-roleless@example.com")):
			frappe.set_user(user)
			ask(own)
			for path in elsewhere:
				with self.assertRaises(frappe.PermissionError, msg=f"{user}: {path}"):
					ask(path)
			with self.assertRaises(frappe.PermissionError, msg="cmd names what runs, whatever the path"):
				ask(own, cmd="frappe.client.set_value")
		# What that prefix reaches is Grove's own: nothing whitelisted is borrowed into the modules.
		borrowed = [
			f"{module.__name__}.{name}"
			for module in (catalog, control, keys, teams, usage)
			for name, value in vars(module).items()
			if callable(value)
			and value in frappe.whitelisted
			and not value.__module__.startswith("grove.api.")
		]
		self.assertEqual(borrowed, [])

		# A guest is Frappe's to keep out: the login page and its calls have to answer.
		operator = other_user("probe-both@example.com", control.CONTROL_ROLE, "System Manager")
		for user in ("Guest", "Administrator", operator):
			frappe.set_user(user)
			for path in ("/app", "/login", "/api/method/login", elsewhere[0]):
				ask(path)

	def test_a_guest_can_call_nothing_of_groves_past_the_api_but_the_scrape_list(self):
		"""The hook leaves a guest to Frappe, which serves one only what is whitelisted for
		guests. Outside `grove.api` that is `monitoring.targets`, which checks its own token."""
		root = Path(auth.__file__).parent
		past_the_api = {}
		for path in root.rglob("*.py"):
			name = str(path.relative_to(root))
			if "tests" in path.parts or path.name.startswith("test_") or name.startswith("api/"):
				continue
			if count := path.read_text().count("allow_guest=True"):
				past_the_api[name] = count
		self.assertEqual(past_the_api, {"monitoring.py": 1})

	def test_a_reader_of_a_team_and_its_key_changes_neither(self):
		"""Read (Desk's Share gives it) is not write, nor read of the usage: checked at the load,
		since a raised cap asks the stores first and an older report is answered unsaved."""
		team, key = self.team_with_a_key("probe-reader")
		reader = other_user("probe-reader@example.com")
		for doctype, name in (("Grove Team", team), ("Grove API Key", key)):
			frappe.share.add_docshare(doctype, name, reader, read=1, flags={"ignore_share_permission": True})
		refused = (
			lambda: keys.update_caps(team, {key: 2}),
			lambda: keys.revoke_key(team, key),
			lambda: teams.update_team(team, suspended=True, observed_at="2020-01-01 00:00:00"),
			lambda: usage.usage([team]),
		)
		frappe.set_user(reader)
		with (
			unittest.mock.patch("frappe.only_for"),
			unittest.mock.patch("grove.pathway.usage.peek_team") as peek_team,
		):
			for call in refused:
				with self.assertRaises(frappe.PermissionError):
					call()
			peek_team.assert_not_called()
