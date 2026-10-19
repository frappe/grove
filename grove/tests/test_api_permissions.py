# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""grove.api runs under permission checks, not around them: the reads go through
frappe.get_list and the writes through a plain save, so the Grove Control role has to carry
exactly what those endpoints touch — and nothing more. Site-backed: DocPerms are the
behaviour under test."""

import base64
import hashlib
import hmac
import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase

from grove.api import catalog, control, keys, teams, usage
from grove.billing.pricing import settle
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_api_key.grove_api_key import KEY_PREFIX, hash_secret
from grove.pathway.routes import utc_timestamp
from grove.utils import utc_today

PROBE = "control-probe@example.com"


def tier(name, **fields):
	return frappe.get_doc({"doctype": "Tier", "__newname": name, **fields}).insert().name


def billed_record(key, team, day, cost, drain=""):
	"""A pulled Usage Record of `cost` served on `day`, settled as the pull settles: what
	month_spent reads."""
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
	settle(team)


def backdate(key):
	"""Past the six-hour revoke guard."""
	seven_hours_ago = frappe.utils.add_to_date(None, hours=-7)
	frappe.db.set_value("Grove API Key", key, "creation", seven_hours_ago, update_modified=False)


WITHHELD = ("Machine", "Inference Server", "Model Replica", "Monitoring Agent")


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
		self.assertIsInstance(catalog.available_models(), list)
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
				frappe.get_doc({"doctype": "Model", "model_id": model_id, "hf_repo": f"org/{model_id}"})
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
		key = frappe.get_doc("Grove API Key", keys.provision_key(team)["name"])
		key.model_groups = []
		key.save()
		self.assertEqual(catalog.available_models(team, key.name), [])
		with self.assertRaises(frappe.DoesNotExistError):
			catalog.available_models("nobody-reach", key.name)

		key.update(
			{
				"model_groups": [{"model_group": "probe-reach"}],
				"allow": [{"model": allowed}],
				"deny": [{"model": denied}],
			}
		)
		key.save()
		model_key = lambda doc: frappe.db.get_value("Model", doc, "model_key")  # noqa: E731
		reach = sorted([model_key(grouped), model_key(allowed)])
		rows = catalog.available_models(team, key.name)
		self.assertEqual(sorted(row["name"] for row in rows), reach)
		self.assertEqual(
			{row["name"] for row in rows}, {f"{row['provider']}/{row['model_id']}" for row in rows}
		)
		here = make_test_geography()
		frappe.get_doc(
			{
				"doctype": "Model Group",
				"__newname": "probe-reach-default",
				"geography": here,
				"is_default": 1,
				"models": [{"model": other}, {"model": grouped}],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(
			sorted(row["name"] for row in catalog.available_models(geography=here)),
			sorted([model_key(grouped), model_key(other)]),
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
		teams.provision_team(team)
		name = keys.provision_key(team, limits=[{"metric": "requests", "window": "1m", "value": 200}])["name"]
		self.assertEqual(
			keys.limits(team, name),
			[
				{"metric": "requests", "window": "1m", "value": 200},
				{"metric": "total_tokens", "window": "1m", "value": 100_000},
			],
		)
		with self.assertRaises(frappe.DoesNotExistError):
			keys.limits("nobody-limits", name)

	def test_the_fleet_stays_out_of_reach(self):
		for doctype in WITHHELD:
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_list(doctype, limit=1)

	def test_provisioning_again_keeps_the_team_and_its_keys(self):
		team = "TEAM-PROBE"
		self.assertEqual(teams.provision_team(team), {"team": team, "max_keys": 0})
		key = keys.provision_key(team)["api_key"]
		self.assertEqual(teams.provision_team(team), {"team": team, "max_keys": 0})
		self.assertEqual(frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(key)}, "team"), team)
		with self.assertRaises(frappe.ValidationError):
			teams.provision_team("")

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
		tier("probe-cap-tier", max_keys=5, spend_limit=10)
		self.assertEqual(
			teams.update_team(team, tier="probe-cap-tier"),
			{
				"team": team,
				"suspended": 0,
				"tier": "probe-cap-tier",
				"override_max_keys": 0,
				"max_keys": 5,
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
				"spent": 0.0,
				"month_spent": 0.0,
				"capped": 10.0,
				"committed": 10.0,
				"spend_limit": 10.0,
				"max_keys": 5,
				"suspended": False,
			},
		)
		self.assertEqual(keys.update_key(team, second, cap=2)["cap"], 2)
		self.assertEqual(usage.figures(team)["spend"]["capped"], 8.0)
		backdate(first)
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
		frappe.db.set_value("Grove API Key", key, "spent", 5)
		today = utc_today()
		for day, cost in ((today, 2), (frappe.utils.add_months(today, -1), 3)):
			billed_record(key, team, day, cost)
		[listed] = usage.figures(team)["keys"]
		self.assertEqual((listed["spent"], listed["month_spent"]), (5, 2))
		self.assertEqual(usage.figures(team)["spend"]["month_spent"], 2)
		frappe.db.set_value("Grove API Key", key, "status", "revoked")
		self.assertEqual(usage.figures(team)["spend"]["month_spent"], 2)
		self.assertTrue(teams.update_team(team, suspended=True)["suspended"])

	def test_a_teams_tier_sets_the_limits_of_its_unpinned_keys(self):
		team = "probe-tier"
		teams.provision_team(team)
		unpinned = keys.provision_key(team, title="inherits")["name"]
		pinned = keys.provision_key(
			team, title="pinned", limits=[{"metric": "requests", "window": "1m", "value": 5}]
		)["name"]
		defaults = [
			{"metric": "requests", "window": "1m", "value": 20},
			{"metric": "total_tokens", "window": "1m", "value": 100_000},
		]
		self.assertEqual(keys.limits(team, unpinned), defaults)
		growth = [{"metric": "requests", "window": "1m", "value": 60}]
		tier("probe-growth", max_keys=3, spend_limit=50, limits=[dict(r) for r in growth])
		limits = teams.update_team(team, tier="probe-growth")
		self.assertEqual((limits["tier"], limits["max_keys"], limits["spend_limit"]), ("probe-growth", 3, 50))
		listed = {row.name: row.limits for row in usage.figures(team)["keys"]}
		self.assertEqual(
			(listed[unpinned], listed[pinned]), (growth, [{"metric": "requests", "window": "1m", "value": 5}])
		)
		frappe.db.set_single_value("Grove Settings", "default_tier", "probe-growth")
		self.addCleanup(frappe.db.set_single_value, "Grove Settings", "default_tier", "")
		self.assertEqual(teams.update_team(team, tier="probe-unseen")["max_keys"], 3)
		self.assertEqual([row.value for row in frappe.get_doc("Tier", "probe-unseen").limits], [60])
		self.assertEqual(
			(teams.update_team(team, tier="")["spend_limit"], keys.limits(team, unpinned)), (50, growth)
		)
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		self.assertEqual(
			(usage.figures(team)["spend"]["spend_limit"], keys.limits(team, unpinned)), (0, defaults)
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
			(listed.geography, listed.gateway_url, listed.cap, listed.spent),
			(minted["geography"], minted["gateway_url"], 0, 0),
		)
		self.assertEqual([row["metric"] for row in listed.limits], ["requests", "total_tokens"])
		self.assertEqual(
			listed.revocable_at, utc_timestamp(frappe.utils.add_to_date(listed.creation, hours=6))
		)
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

		with self.assertRaises(frappe.ValidationError):
			keys.revoke_key(team, key)
		frappe.db.set_value(
			"Grove API Key", key, "creation", frappe.utils.add_to_date(None, hours=-7), update_modified=False
		)
		keys.revoke_key(team, key)
		self.assertEqual(frappe.db.get_value("Grove API Key", key, "status"), "revoked")

	def test_the_default_tier_moves_the_figures_of_a_team_that_names_none(self):
		from grove.billing.pricing import forget_figures

		# The tier and the settings are the operator's to edit; the cache is what is under test.
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
		self.assertEqual(usage.figures(team)["spend"]["spend_limit"], 37, "the default tier edited")

		settings = frappe.get_doc("Grove Settings")
		settings.default_tier = tier("probe-other-default", spend_limit=50)
		settings.save()
		self.assertEqual(usage.figures(team)["spend"]["spend_limit"], 50, "another tier made default")

	def test_the_figures_are_one_read_kept_until_something_moves_them(self):
		from grove.billing.pricing import forget_figures

		team = "probe-figures"
		teams.provision_team(team)
		teams.update_team(team, tier=tier("probe-figures-tier", spend_limit=20, max_keys=2))
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
		self.assertEqual((usage.figures(team)["keys"][0]["cap"], usage.figures(team)["spend"]["capped"]), (6, 6))
		teams.update_team(team, suspended=True)
		self.assertTrue(usage.figures(team)["spend"]["suspended"])

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
				self.assertEqual((answer["sync"], answer["figures"]["spend"]["spent"]), ("PS-1", 0))
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
			str(frappe.db.get_value("Central Team", team, "last_reported_at")), "2026-10-15 10:00:00.000001"
		)
		for stale in ("2026-10-15 10:00:00.000001", "2026-10-15 09:59:59"):
			replay = deliver(team=team, tier="probe-hook-b", observed_at=stale)
			self.assertEqual((replay["ignored"], replay["tier"]), ("not newer", "probe-hook-a"))
		self.assertEqual(
			deliver(team=team, tier="probe-hook-b", observed_at="2026-10-15 10:00:01")["tier"], "probe-hook-b"
		)
		frappe.set_user("Guest")
		self.assertIsNone(deliver(team="probe-hook-unknown", tier="probe-hook-a"))
		self.assertFalse(frappe.db.exists("Central Team", "probe-hook-unknown"))

		for forged in (
			{teams.WEBHOOK_SECRET_HEADER: "bm90IHRoZSBzaWduYXR1cmU="},
			{teams.CONTROL_KEY_HEADER: "not-a-key"},
			{teams.CONTROL_KEY_HEADER: None},
		):
			frappe.set_user("Guest")
			with self.assertRaises(frappe.AuthenticationError):
				deliver(forged, team=team, tier="probe-hook-a")
		self.assertEqual(frappe.db.get_value("Central Team", team, "tier"), "probe-hook-b")

	def test_a_renamed_tier_label_follows_at_grove(self):
		team = "probe-rename"
		teams.provision_team(team)
		rows = [{"metric": "requests", "window": "1m", "value": 7}]
		tier("probe-ren-a", max_keys=2, limits=[dict(r) for r in rows])
		teams.update_team(team, tier="probe-ren-a")
		key = keys.provision_key(team, title="follows")["name"]

		teams.rename_tier("probe-ren-a", "probe-ren-b")
		self.assertEqual(frappe.db.get_value("Central Team", team, "tier"), "probe-ren-b")
		self.assertFalse(frappe.db.exists("Tier", "probe-ren-a"))
		self.assertEqual((teams.update_team(team)["max_keys"], keys.limits(team, key)), (2, rows))
		self.assertIsNone(teams.rename_tier("probe-ren-unseen", "probe-ren-c"))
		self.assertFalse(frappe.db.exists("Tier", "probe-ren-c"))
		tier("probe-ren-c")
		with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
			teams.rename_tier("probe-ren-b", "probe-ren-c")
