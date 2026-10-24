# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""A key carries its own policy — groups, allow/deny, rate limits, geography, cap — and the wire
record the gateway reads is built from it.

A team id per test: IntegrationTestCase rolls back once when the class is done, not between
tests, so anything registered here is still there for the next one."""

import unittest
import unittest.mock
from datetime import timedelta
from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

from grove.access import limit_entries
from grove.api import keys
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_api_key import grove_api_key
from grove.grove.doctype.grove_api_key.grove_api_key import GroveAPIKey
from grove.grove.doctype.model_provider.test_model_provider import our_model, provider, vendor_model
from grove.pathway.routes import utc_timestamp
from grove.pathway.snapshot import effective_keys


def team(name, **fields):
	"""`max_keys` / `spend_limit` given here go on a Tier of the team's own; without one, no cap
	fits, and the keys stay at 0."""
	if frappe.db.exists("Grove Team", name):
		return name
	if "max_keys" in fields or "spend_limit" in fields:
		numbers = {k: fields.pop(k) for k in ("max_keys", "spend_limit") if k in fields}
		fields["tier"] = (
			frappe.get_doc({"doctype": "Tier", "__newname": f"{name}-tier", **numbers}).insert().name
		)
	return (
		frappe.get_doc(
			{
				"doctype": "Grove Team",
				"__newname": name,
				**fields,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def key(name, **fields):
	return frappe.get_doc({"doctype": "Grove API Key", "team": team(name), **fields}).insert()


def projected(name):
	[record] = [k for k in effective_keys() if k["prefix"] == name]
	return record


class IntegrationTestGroveAPIKey(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")

	def test_membership_reaches_the_wire_as_one_sorted_comma_list(self):
		for name in ("key-probe-zeta", "key-probe-acme"):
			if not frappe.db.exists("Model Group", name):
				frappe.get_doc({"doctype": "Model Group", "__newname": name}).insert()
		doc = key(
			"key-probe-groups",
			model_groups=[{"model_group": "key-probe-zeta"}, {"model_group": "key-probe-acme"}],
		)
		self.assertEqual(projected(doc.name)["group"], "key-probe-acme,key-probe-zeta")

	def test_the_teams_flags_ride_on_the_key(self):
		name = team("key-probe-flags", spend_limit=1)
		doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 1}).insert()
		record = projected(doc.name)
		self.assertEqual(
			(record["team"], record["budget"], record["limited"], record["log_payloads"]),
			(name, 1_000_000_000, False, True),
		)
		frappe.db.set_value("Grove Team", name, {"log_payloads": 0})
		record = projected(doc.name)
		self.assertEqual((record["budget"], record["log_payloads"]), (1_000_000_000, False))

	def test_the_teams_key_cap_window_rides_on_the_key_as_the_budget_window(self):
		name = team("key-probe-window", spend_limit=1)
		doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 1}).insert()
		self.assertEqual(projected(doc.name)["budget_window"], "month")
		frappe.db.set_value("Grove Team", name, {"key_cap_window": "Total"})
		self.assertEqual(projected(doc.name)["budget_window"], "total")

	def test_a_title_is_used_once_per_team_revoked_keys_included(self):
		name = team("key-probe-titles")
		first = frappe.get_doc({"doctype": "Grove API Key", "team": name, "title": "ci"}).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc({"doctype": "Grove API Key", "team": name, "title": "ci"}).insert()
		frappe.db.set_value("Grove API Key", first.name, "status", "revoked")
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc({"doctype": "Grove API Key", "team": name, "title": "ci"}).insert()
		frappe.get_doc(
			{"doctype": "Grove API Key", "team": team("key-probe-titles-2"), "title": "ci"}
		).insert()
		untitled = [
			frappe.get_doc({"doctype": "Grove API Key", "team": name, "title": blank}).insert()
			for blank in (None, "")
		]
		self.assertEqual([k.title for k in untitled], [None, None])
		first.reload()
		first.title = "ci-2"
		with self.assertRaises(frappe.CannotChangeConstantError):
			first.save()

	def test_a_revoked_key_is_not_projected(self):
		doc = key("key-probe-revoked")
		frappe.db.set_value("Grove API Key", doc.name, "status", "revoked")
		self.assertEqual([k for k in effective_keys() if k["prefix"] == doc.name], [])

	def test_the_desk_button_revokes_off_the_doc_it_sent_back(self):
		"""The form posts the doc as JSON, so `creation` arrives as a string, not a datetime."""
		doc = key("key-probe-desk-revoke")
		sent_back = frappe.get_doc(frappe.parse_json(frappe.as_json(doc)))
		self.assertIsInstance(sent_back.creation, str)
		with self.assertRaises(frappe.ValidationError):
			sent_back.revoke()


class IntegrationTestNewKeysStartInTheDefaultGroup(IntegrationTestCase):
	"""What a key may call is decided here, by group, not sent by whoever provisions it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		frappe.db.set_value("Model Group", {"is_default": 1}, "is_default", 0)
		cls.default = cls.group("key-default-one", is_default=1)

	@staticmethod
	def group(name, is_default=0):
		return (
			frappe.get_doc({"doctype": "Model Group", "__newname": name, "is_default": is_default})
			.insert()
			.name
		)

	def groups_of(self, doc):
		return [row.model_group for row in doc.model_groups]

	def test_a_new_key_is_put_in_the_default_group(self):
		doc = key("key-group-new")
		self.assertEqual(self.groups_of(doc), [self.default])
		self.assertEqual(projected(doc.name)["group"], self.default)

	def test_groups_named_on_the_insert_are_kept(self):
		picked = self.group("key-default-picked")
		doc = key("key-group-picked", model_groups=[{"model_group": picked}])
		self.assertEqual(self.groups_of(doc), [picked])

	def test_a_key_taken_out_of_every_group_stays_out(self):
		doc = key("key-group-out")
		doc.model_groups = []
		doc.save()
		self.assertEqual(self.groups_of(doc.reload()), [])

	def test_provisioning_a_key_puts_it_in_the_default_group(self):
		from grove.api import keys, teams

		teams.provision_team("key-group-provisioned")
		minted = keys.provision_key("key-group-provisioned")
		self.assertEqual(self.groups_of(frappe.get_doc("Grove API Key", minted["name"])), [self.default])

	def test_provisioning_into_a_named_group_skips_the_default(self):
		from grove.api import keys, teams

		picked = self.group("key-group-named")
		teams.provision_team("key-group-named")
		minted = keys.provision_key("key-group-named", model_group=picked)
		self.assertEqual(self.groups_of(frappe.get_doc("Grove API Key", minted["name"])), [picked])

	def test_a_group_of_another_geography_is_refused(self):
		away = make_test_geography("key-group-elsewhere")
		theirs = (
			frappe.get_doc({"doctype": "Model Group", "__newname": "key-group-elsewhere", "geography": away})
			.insert()
			.name
		)
		with self.assertRaisesRegex(frappe.ValidationError, "not this key's"):
			key("key-group-elsewhere", model_groups=[{"model_group": theirs}])

	def test_a_new_key_starts_in_its_own_geographys_default(self):
		away = make_test_geography("key-default-away")
		theirs = (
			frappe.get_doc(
				{
					"doctype": "Model Group",
					"__newname": "key-default-away",
					"geography": away,
					"is_default": 1,
				}
			)
			.insert()
			.name
		)
		doc = key("key-group-away", geography=away)
		self.assertEqual(self.groups_of(doc), [theirs])


class IntegrationTestOwnGrantsAreTheKeysGeographys(IntegrationTestCase):
	"""An Allow or Deny row names the vendor doc the key's own geography serves."""

	KEY = "allow-vendor/allow-big"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.here, cls.away = make_test_geography("allow-here"), make_test_geography("allow-away")
		cls.docs = {}
		for geography in (cls.here, cls.away):
			vendor = provider(
				"allow-vendor", base_url="https://allow.test/v1", api_key="k", geography=geography
			).insert()
			cls.docs[geography] = vendor_model("allow-big", vendor.name).insert().name
		cls.ours = our_model("allow-ours-7b").insert().name

	def key(self, name, **fields):
		return frappe.get_doc(
			{"doctype": "Grove API Key", "team": team(name), "geography": self.here, **fields}
		)

	def test_a_vendor_model_of_another_geography_is_refused_and_ours_is_not(self):
		self.key("key-allow-here", allow=[{"model": self.docs[self.here]}, {"model": self.ours}]).insert()
		for field in ("allow", "deny"):
			with self.assertRaises(frappe.ValidationError):
				self.key(f"key-{field}-away", **{field: [{"model": self.docs[self.away]}]}).insert()

	def test_provisioning_by_key_allows_the_keys_geographys_doc(self):
		from grove.api import keys, teams

		teams.provision_team("key-allow-keyed")
		minted = keys.provision_key("key-allow-keyed", geography=self.away, models=[self.KEY])
		allowed = frappe.get_all(
			"Grove Model Row", filters={"parent": minted["name"], "parentfield": "allow"}, pluck="model"
		)
		self.assertEqual(allowed, [self.docs[self.away]])


class IntegrationTestRateLimits(IntegrationTestCase):
	"""A key's limits are rows here and one sorted list on the wire."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")

	def test_a_new_key_starts_under_the_default_limits(self):
		doc = key("key-limit-default")
		self.assertEqual(projected(doc.name)["limits"], "requests:1m:20,total_tokens:1m:100000")

	def test_limits_reach_the_wire_sorted(self):
		doc = key(
			"key-limit-wire",
			limits=[
				{"metric": "total_tokens", "window": "1M", "value": 50_000_000},
				{"metric": "requests", "window": "1m", "value": 200},
			],
		)
		self.assertEqual(projected(doc.name)["limits"], "requests:1m:200,total_tokens:1M:50000000")

	def test_two_limits_on_one_metric_and_window_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			key(
				"key-limit-twice",
				limits=[
					{"metric": "requests", "window": "1m", "value": 200},
					{"metric": "requests", "window": "1m", "value": 300},
				],
			)

	def test_a_limit_must_be_above_zero(self):
		with self.assertRaises(frappe.ValidationError):
			key("key-limit-zero", limits=[{"metric": "requests", "window": "1m", "value": 0}])

	def test_a_window_the_gateway_does_not_know_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			key("key-limit-week", limits=[{"metric": "requests", "window": "1w", "value": 5}])

	def test_provisioning_replaces_the_limits_it_is_given_and_keeps_them_otherwise(self):
		from grove.api import keys, teams

		team_id = "key-limit-provisioned"
		teams.provision_team(team_id)
		name = keys.provision_key(team_id, limits=[{"metric": "requests", "window": "1m", "value": 5}])[
			"name"
		]
		keys.update_key(team_id, name)
		self.assertEqual(
			limit_entries("Grove API Key")[name], [{"metric": "requests", "window": "1m", "value": 5}]
		)
		keys.update_key(team_id, name, limits=[{"metric": "requests", "window": "1h", "value": 9}])
		self.assertEqual(
			limit_entries("Grove API Key")[name], [{"metric": "requests", "window": "1h", "value": 9}]
		)
		keys.update_key(team_id, name, limits=[])
		self.assertNotIn(name, limit_entries("Grove API Key"))
		self.assertEqual(projected(name)["limits"], "requests:1m:20,total_tokens:1m:100000")

	def test_a_key_with_no_rows_runs_under_its_teams_tier(self):
		tier = frappe.get_doc(
			{
				"doctype": "Tier",
				"__newname": "key-tier-growth",
				"limits": [{"metric": "requests", "window": "1m", "value": 60}],
			}
		).insert()
		name = team("key-limit-tiered", tier=tier.name)
		inherits = frappe.get_doc({"doctype": "Grove API Key", "team": name}).insert()
		pinned = frappe.get_doc(
			{
				"doctype": "Grove API Key",
				"team": name,
				"limits": [{"metric": "requests", "window": "1h", "value": 9}],
			}
		).insert()
		self.assertEqual(
			(projected(inherits.name)["limits"], projected(pinned.name)["limits"]),
			("requests:1m:60", "requests:1h:9"),
		)
		tier.limits = []
		tier.save()
		self.assertEqual(
			(projected(inherits.name)["limits"], projected(pinned.name)["limits"]), ("", "requests:1h:9")
		)
		with self.assertRaises(frappe.LinkExistsError):
			tier.delete()

	def test_a_keys_own_rows_replace_the_tiers_on_the_metrics_they_name(self):
		tier = frappe.get_doc(
			{
				"doctype": "Tier",
				"__newname": "key-tier-merge",
				"limits": [
					{"metric": "requests", "window": "1m", "value": 60},
					{"metric": "requests", "window": "1h", "value": 600},
					{"metric": "total_tokens", "window": "1m", "value": 100_000},
				],
			}
		).insert()
		name = team("key-limit-merged", tier=tier.name)
		pinned = frappe.get_doc(
			{
				"doctype": "Grove API Key",
				"team": name,
				"limits": [{"metric": "requests", "window": "1m", "value": 5}],
			}
		).insert()
		self.assertEqual(projected(pinned.name)["limits"], "requests:1m:5,total_tokens:1m:100000")
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		self.assertEqual(
			projected(
				frappe.get_doc(
					{
						"doctype": "Grove API Key",
						"team": team("key-limit-untiered"),
						"limits": [{"metric": "total_tokens", "window": "1d", "value": 7}],
					}
				)
				.insert()
				.name
			)["limits"],
			"requests:1m:20,total_tokens:1d:7",
		)

	def test_a_tier_refuses_two_limits_on_one_metric_and_window(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Tier",
					"__newname": "key-tier-twice",
					"limits": [
						{"metric": "requests", "window": "1m", "value": 1},
						{"metric": "requests", "window": "1m", "value": 2},
					],
				}
			).insert()


class IntegrationTestOneGeographyPerKey(IntegrationTestCase):
	"""A key's spend and counters land on one store, so every key is pinned to exactly one
	geography, for good."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		cls.default = frappe.db.get_value("Geography", {"is_default": 1})

	def other_geography(self, name):
		if not frappe.db.exists("Geography", name):
			frappe.get_doc(
				{
					"doctype": "Geography",
					"__newname": name,
					"fleet_zone": f"{name}.grove.localhost",
					"endpoint": f"api.{name}.grove.localhost",
				}
			).insert(ignore_permissions=True)
		return name

	def test_a_key_saved_without_one_is_pinned_to_the_default(self):
		doc = key("key-geo-default")
		self.assertEqual((doc.geography, projected(doc.name)["geography"]), (self.default, self.default))

	def test_a_picked_geography_is_kept_and_never_changes(self):
		other = self.other_geography("geo-picked")
		doc = key("key-geo-picked", geography=other)
		self.assertEqual(doc.geography, other)
		doc.geography = self.default
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()

	def test_with_no_default_a_key_without_one_is_refused(self):
		frappe.db.set_value("Geography", self.default, "is_default", 0)
		self.addCleanup(frappe.db.set_value, "Geography", self.default, "is_default", 1)
		with self.assertRaises(frappe.ValidationError):
			key("key-geo-none")

	def test_ticking_a_second_default_unticks_the_first(self):
		other = frappe.get_doc("Geography", self.other_geography("geo-second"))
		self.addCleanup(frappe.db.set_value, "Geography", self.default, "is_default", 1)
		self.addCleanup(frappe.db.set_value, "Geography", other.name, "is_default", 0)
		other.is_default = 1
		other.save(ignore_permissions=True)
		self.assertEqual(frappe.get_all("Geography", filters={"is_default": 1}, pluck="name"), [other.name])


class IntegrationTestOneTeamPerKey(IntegrationTestCase):
	"""A key's cap and month's spend are counted against its team's limit; a move would carry them to a team
	whose count and limit never checked them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()

	def test_a_keys_team_is_set_once(self):
		doc = key("key-team-pinned")
		doc.team = team("team-probe-other")
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()


class IntegrationTestARevokedKeyStaysRevoked(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()

	def test_setting_it_active_again_is_refused(self):
		doc = key("key-revoked-for-good")
		frappe.db.set_value("Grove API Key", doc.name, "status", "revoked")
		doc.reload()
		doc.status = "active"
		with self.assertRaisesRegex(frappe.ValidationError, "stays revoked"):
			doc.save()


class IntegrationTestCapsFitTheSpendLimit(IntegrationTestCase):
	"""Across a team's live keys, Σ monthly caps never exceeds its spend limit: every store gates
	its key exactly, so the keys together never outspend the team's tier."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(unittest.mock.patch("grove.pathway.usage.peek_team", return_value={}))
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")

	def test_caps_past_the_limit_are_refused_and_a_revoke_frees_its_share(self):
		name = team("key-cap-team", spend_limit=10)
		first = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 6}).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "Lower another key's cap"):
			frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 6}).insert()
		second = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 4}).insert()
		self.assertEqual(
			(projected(first.name)["budget"], projected(second.name)["budget"]),
			(6_000_000_000, 4_000_000_000),
		)
		second.cap = 5
		with self.assertRaises(frappe.ValidationError):
			second.save()
		frappe.db.set_value("Grove API Key", first.name, "status", "revoked")
		second.reload()
		second.cap = 5
		second.save()

	def test_a_key_is_minted_at_0_whatever_the_limit_and_never_below(self):
		name = team("key-cap-needed")
		for cap in (0, None):
			doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": cap}).insert()
			self.assertEqual(projected(doc.name)["budget"], 0)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": -1}).insert()

	def test_a_lowered_limit_stops_no_key_and_refuses_only_a_raise(self):
		name = team("key-cap-demoted", spend_limit=10)
		doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 8}).insert()
		tier = frappe.get_doc("Tier", frappe.db.get_value("Grove Team", name, "tier"))
		tier.spend_limit = 5
		tier.save()
		self.assertEqual(projected(doc.name)["budget"], 8_000_000_000)
		doc.append("limits", {"metric": "requests", "window": "1m", "value": 5})
		doc.save()
		doc.cap = 9
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc.reload()
		doc.cap = 6
		doc.save()
		frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 0}).insert()


class TestTwoMintsAtTheLimitSerialize(unittest.TestCase):
	def test_the_team_row_is_locked_and_the_count_is_a_locking_read(self):
		"""A plain count answers from the transaction's snapshot, so two mints at max_keys - 1 both
		passed; the lock on the team row holds the second until the first commits, and FOR UPDATE
		makes its count see that commit."""
		queries = []
		doc = SimpleNamespace(team="TEAM-00001")
		with (
			unittest.mock.patch.object(
				grove_api_key, "team_limits", return_value=frappe._dict(max_keys=2)
			) as limits,
			unittest.mock.patch.object(
				frappe, "db", SimpleNamespace(sql=lambda query, values: queries.append(query) or [[2]])
			),
			unittest.mock.patch("frappe.throw", side_effect=frappe.ValidationError),
			self.assertRaises(frappe.ValidationError),
		):
			GroveAPIKey.check_key_count(doc)
		self.assertEqual(limits.call_args, unittest.mock.call("TEAM-00001", for_update=True))
		self.assertEqual(len(queries), 1)
		self.assertIn("for update", queries[0].lower())

	def test_no_limit_takes_no_count(self):
		with (
			unittest.mock.patch.object(grove_api_key, "team_limits", return_value=frappe._dict(max_keys=0)),
			unittest.mock.patch.object(frappe, "db", SimpleNamespace()),
		):
			GroveAPIKey.check_key_count(SimpleNamespace(team="TEAM-00001"))


class IntegrationTestExpiry(IntegrationTestCase):
	"""A key retires on its own: a rule at the mint, the moment it names on the key, the hourly sweep."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")

	def test_the_rule_at_the_mint_names_the_moment_and_never_is_the_default(self):
		doc = key("key-probe-expiry", expiry="7 Days")
		doc.reload()
		self.assertEqual(doc.expires_at, frappe.utils.add_days(doc.creation, 7))
		forever = key("key-probe-expiry")
		self.assertEqual((forever.expiry, forever.expires_at), ("Never", None))
		row = keys.key_rows([doc.name])[0]
		self.assertEqual((row.expiry, row.expires_at), ("7 Days", utc_timestamp(doc.expires_at)))
		with self.assertRaises(frappe.ValidationError):
			key("key-probe-expiry", expiry="2 Days")
		doc.expiry = "Never"
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()

	def test_the_sweep_revokes_every_key_past_its_moment_and_no_other(self):
		due, later, forever = (key("key-probe-sweep", expiry=rule) for rule in ("1 Day", "30 Days", "Never"))
		two_days_ago = frappe.utils.add_days(None, -2)
		frappe.db.set_value(
			"Grove API Key",
			due.name,
			{"creation": two_days_ago, "expires_at": frappe.utils.add_days(two_days_ago, 1)},
			update_modified=False,
		)
		grove_api_key.revoke_expired()
		self.assertEqual(
			[frappe.db.get_value("Grove API Key", d.name, "status") for d in (due, later, forever)],
			["revoked", "active", "active"],
		)
		self.assertEqual([k for k in effective_keys() if k["prefix"] == due.name], [])

	def test_a_key_minted_to_expire_from_first_use_gets_its_moment_at_its_first_record_once(self):
		name = team("key-probe-first-use")
		minted = keys.provision_key(name, expiry="7 Days", expiry_from_first_use=True)
		first_use = frappe.get_doc("Grove API Key", minted["name"])
		ruled = key(name, expiry="1 Day")
		ruled.reload()
		self.assertEqual((first_use.expiry, first_use.expiry_from_first_use, first_use.expires_at), ("7 Days", 1, None))
		row = keys.key_rows([first_use.name])[0]
		self.assertEqual((row.expiry_from_first_use, row.expires_at), (True, None))

		grove_api_key.start_expiry([first_use.name, ruled.name])

		moment = frappe.db.get_value("Grove API Key", first_use.name, "expires_at")
		self.assertAlmostEqual(
			moment, frappe.utils.add_days(frappe.utils.now_datetime(), 7), delta=timedelta(seconds=5)
		)
		# A key counting from the mint keeps its moment; a second record moves nothing.
		self.assertEqual(frappe.db.get_value("Grove API Key", ruled.name, "expires_at"), ruled.expires_at)
		grove_api_key.start_expiry([first_use.name])
		self.assertEqual(frappe.db.get_value("Grove API Key", first_use.name, "expires_at"), moment)

	def test_never_takes_no_moment_at_first_use_and_the_flag_is_fixed(self):
		doc = key("key-probe-first-use-never", expiry_from_first_use=1)
		grove_api_key.start_expiry([doc.name])
		self.assertIsNone(frappe.db.get_value("Grove API Key", doc.name, "expires_at"))
		doc.expiry_from_first_use = 0
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()

	def test_extending_moves_the_moment_by_days_from_the_later_of_it_and_now(self):
		ahead, lapsed = key("key-probe-extend", expiry="7 Days"), key("key-probe-extend", expiry="1 Day")
		yesterday = frappe.utils.add_days(frappe.utils.now_datetime(), -1)
		frappe.db.set_value("Grove API Key", lapsed.name, "expires_at", yesterday, update_modified=False)
		ahead.reload()
		moment, stamp = ahead.expires_at, ahead.modified

		ahead.extend_expiry(3)
		row = keys.extend_key(ahead.team, lapsed.name, 3)

		self.assertEqual(
			frappe.db.get_value("Grove API Key", ahead.name, "expires_at"), frappe.utils.add_days(moment, 3)
		)
		self.assertEqual(frappe.db.get_value("Grove API Key", ahead.name, "modified"), stamp)
		fresh = frappe.db.get_value("Grove API Key", lapsed.name, "expires_at")
		self.assertAlmostEqual(
			fresh, frappe.utils.add_days(frappe.utils.now_datetime(), 3), delta=timedelta(seconds=5)
		)
		self.assertEqual(row.expires_at, utc_timestamp(fresh))

	def test_a_key_without_a_moment_is_not_extended_and_a_revoked_one_refuses(self):
		unused = key("key-probe-extend-none", expiry="7 Days", expiry_from_first_use=1)
		forever = key("key-probe-extend-none")
		for doc in (unused, forever):
			doc.extend_expiry(3)
			self.assertIsNone(frappe.db.get_value("Grove API Key", doc.name, "expires_at"))
		gone = key("key-probe-extend-none", expiry="7 Days")
		frappe.db.set_value("Grove API Key", gone.name, "status", "revoked")
		gone.reload()
		with self.assertRaisesRegex(frappe.ValidationError, "revoked"):
			gone.extend_expiry(3)
		with self.assertRaises(frappe.ValidationError):
			forever.extend_expiry(0)
