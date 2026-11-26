# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""A team id per test: IntegrationTestCase rolls back once when the class is done, not between
tests, so anything registered here is still there for the next one."""

import unittest
import unittest.mock
from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase

from grove.api import keys
from grove.grove.doctype.grove_api_key import grove_api_key
from grove.grove.doctype.grove_api_key.grove_api_key import GroveAPIKey
from grove.grove.doctype.model_provider.test_model_provider import keyed_provider, our_model, vendor_model
from grove.infrastructure.doctype.geography.test_geography import make_test_geography
from grove.pathway.routes import utc_timestamp
from grove.pathway.snapshot import effective_keys, effective_teams


def team(name, **fields):
	"""A `spend_limit` given here goes on a Tier of the team's own; without one, no cap fits, and
	the keys stay at 0."""
	if frappe.db.exists("Grove Team", name):
		return name
	if "spend_limit" in fields:
		fields["tier"] = (
			frappe.get_doc(
				{"doctype": "Tier", "__newname": f"{name}-tier", "spend_limit": fields.pop("spend_limit")}
			)
			.insert()
			.name
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


def team_pushed(name):
	[record] = effective_teams([name])
	return record


def team_limits_pushed(name):
	return team_pushed(name)["limits"]


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
		groups = [{"model_group": name} for name in ("key-probe-zeta", "key-probe-acme")]
		doc = key(team("key-probe-groups", model_groups=groups))
		self.assertEqual(projected(doc.name)["group"], "key-probe-acme,key-probe-zeta")

	def test_the_teams_flags_ride_its_record_and_the_key_names_the_team(self):
		name = team("key-probe-flags", spend_limit=1, cap_message="trial over")
		doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 1}).insert()
		record = projected(doc.name)
		self.assertEqual((record["team"], record["budget"]), (name, 1_000_000_000))
		self.assertFalse({"limited", "log_payloads", "cap_message"} & set(record))
		self.assertEqual(
			team_pushed(name),
			{
				"team": name,
				"limited": False,
				"log_payloads": True,
				"cap_message": "trial over",
				"limits": "",
				"key_limits": "",
			},
		)
		frappe.db.set_value("Grove Team", name, {"log_payloads": 0})
		self.assertIs(team_pushed(name)["log_payloads"], False)

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
		index = frappe.db.sql(
			"show index from `tabGrove API Key` where key_name like 'unique%%title%%'", as_dict=True
		)
		self.assertEqual(
			[(row.Key_name, row.Column_name) for row in index],
			[("unique_title_team", "title"), ("unique_title_team", "team")],
		)

	def test_a_revoked_key_is_not_projected(self):
		doc = key("key-probe-revoked")
		frappe.db.set_value("Grove API Key", doc.name, "status", "revoked")
		self.assertEqual([k for k in effective_keys() if k["prefix"] == doc.name], [])

	def test_the_desk_button_revokes_a_key_however_fresh(self):
		"""The form posts the doc back as JSON."""
		doc = key("key-probe-desk-revoke")
		frappe.get_doc(frappe.parse_json(frappe.as_json(doc))).revoke()
		self.assertEqual(frappe.db.get_value("Grove API Key", doc.name, "status"), "revoked")


class IntegrationTestNewTeamsStartInTheDefaultGroups(IntegrationTestCase):
	"""What a key may call is decided by its team's groups, not sent by whoever provisions it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.here, cls.away = make_test_geography(), make_test_geography("key-default-away")
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		frappe.db.set_value("Model Group", {"is_default": 1}, "is_default", 0)
		cls.default = cls.group("key-default-one", is_default=1)
		cls.away_default = cls.group("key-default-away", is_default=1, geography=cls.away)

	@staticmethod
	def group(name, is_default=0, geography=None):
		doc = {"doctype": "Model Group", "__newname": name, "is_default": is_default}
		if geography:
			doc["geography"] = geography
		return frappe.get_doc(doc).insert().name

	def groups_of(self, name):
		return [row.model_group for row in frappe.get_doc("Grove Team", name).model_groups]

	def test_a_new_team_is_put_in_every_geographys_default_and_a_key_takes_its_own(self):
		name = team("key-group-new")
		self.assertEqual(self.groups_of(name), [self.away_default, self.default])
		here, away = key(name), key(name, geography=self.away)
		self.assertEqual(projected(here.name)["group"], self.default)
		self.assertEqual(projected(away.name)["group"], self.away_default)

	def test_groups_named_on_the_insert_are_kept(self):
		picked = self.group("key-default-picked")
		name = team("key-group-picked", model_groups=[{"model_group": picked}])
		self.assertEqual(self.groups_of(name), [picked])
		self.assertEqual(projected(key(name).name)["group"], picked)

	def test_a_team_taken_out_of_every_group_stays_out(self):
		doc = frappe.get_doc("Grove Team", team("key-group-out"))
		doc.model_groups = []
		doc.save()
		self.assertEqual(self.groups_of(doc.name), [])
		self.assertEqual(projected(key(doc.name).name)["group"], "")

	def test_provisioning_a_team_puts_it_in_the_defaults(self):
		from grove.api import teams

		teams.provision_team("key-group-provisioned")
		self.assertEqual(self.groups_of("key-group-provisioned"), [self.away_default, self.default])

	def test_a_key_adds_groups_of_its_own_geography_on_top_of_its_teams(self):
		own = self.group("key-default-own")
		doc = key(team("key-group-own"), model_groups=[{"model_group": own}])
		self.assertEqual(projected(doc.name)["group"], ",".join(sorted([self.default, own])))
		with self.assertRaisesRegex(frappe.ValidationError, "not this key's"):
			key("key-group-own", model_groups=[{"model_group": self.away_default}])


class IntegrationTestATeamsGrantsReachTheKeysOfTheirGeography(IntegrationTestCase):
	"""An Allow or Deny row names a Model doc: a vendor's is one geography's and reaches the team's
	keys there; ours reaches every key."""

	KEY = "_T-allow-vendor/allow-big"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.here, cls.away = make_test_geography("allow-here"), make_test_geography("allow-away")
		cls.docs = {}
		for geography in (cls.here, cls.away):
			vendor = keyed_provider("_T-allow-vendor", base_url="https://allow.test/v1", geography=geography)
			cls.docs[geography] = vendor_model("allow-big", vendor.name).insert().name
		cls.ours = our_model("allow-ours-7b").insert().name
		cls.ours_key = frappe.db.get_value("Model", cls.ours, "model_key")

	def test_a_vendor_row_rides_on_its_geographys_keys_and_ours_on_every_key(self):
		from grove.access import get_reachable_models

		name = team(
			"key-allow-team",
			allow=[{"model": self.docs[self.here]}, {"model": self.ours}],
			deny=[{"model": self.docs[self.away]}],
		)
		here = projected(key(name, geography=self.here).name)
		away = projected(key(name, geography=self.away).name)
		both = ",".join(sorted([self.KEY, self.ours_key]))
		self.assertEqual((here["allow"], here["deny"]), (both, ""))
		self.assertEqual((away["allow"], away["deny"]), (self.ours_key, self.KEY))
		self.assertEqual(get_reachable_models(name, self.here), both.split(","))
		self.assertEqual(get_reachable_models(name, self.away), [self.ours_key])

	def test_a_model_in_both_allow_and_deny_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "both Allow and Deny"):
			team("key-allow-both", allow=[{"model": self.ours}], deny=[{"model": self.ours}])


class IntegrationTestRateLimits(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")

	def test_a_new_team_starts_under_the_default_limits_and_its_key_carries_none(self):
		doc = key("key-limit-default")
		self.assertEqual(team_limits_pushed(doc.team), "requests:1m:20,total_tokens:1m:100000")
		self.assertFalse({"limits", "key_limits"} & set(projected(doc.name)))

	def test_a_teams_own_rows_reach_the_wire_sorted_with_the_model_last(self):
		served = our_model("key-limit-dear").insert()
		name = team(
			"key-limit-wire",
			limits=[
				{"metric": "total_tokens", "window": "1M", "value": 50_000_000},
				{"model": served.name, "metric": "requests", "window": "1m", "value": 5},
				{"metric": "requests", "window": "1m", "value": 200},
			],
		)
		self.assertEqual(
			team_limits_pushed(name),
			"requests:1m:200,total_tokens:1M:50000000,total_tokens:1m:100000,"
			f"requests:1m:5:{served.model_key}",
		)

	def test_a_team_refuses_two_limits_on_one_metric_and_window_and_one_below_zero(self):
		twice = [
			{"metric": "requests", "window": "1m", "value": 200},
			{"metric": "requests", "window": "1m", "value": 300},
		]
		for table, rows in (
			("limits", twice),
			("key_limits", twice),
			("limits", [{"metric": "requests", "window": "1m", "value": -1}]),
		):
			with self.subTest(table=table, rows=rows), self.assertRaises(frappe.ValidationError):
				team("key-limit-refused", **{table: rows})

	def test_a_teams_own_rows_are_laid_over_its_tiers_row_by_row(self):
		tier = frappe.get_doc(
			{
				"doctype": "Tier",
				"__newname": "key-tier-growth",
				"limits": [
					{"metric": "requests", "window": "1m", "value": 60},
					{"metric": "requests", "window": "1h", "value": 600},
				],
			}
		).insert()
		inherits = team("key-limit-tiered", tier=tier.name)
		pinned = team(
			"key-limit-pinned", tier=tier.name, limits=[{"metric": "requests", "window": "1m", "value": 5}]
		)
		lifted = team(
			"key-limit-lifted", tier=tier.name, limits=[{"metric": "requests", "window": "1h", "value": 0}]
		)
		self.assertEqual(
			[team_limits_pushed(name) for name in (inherits, pinned, lifted)],
			[
				"requests:1h:600,requests:1m:60",
				"requests:1h:600,requests:1m:5",
				"requests:1h:0,requests:1m:60",
			],
		)
		tier.limits = []
		tier.save()
		self.assertEqual((team_limits_pushed(inherits), team_limits_pushed(pinned)), ("", "requests:1m:5"))
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		self.assertEqual(
			team_limits_pushed(team("key-limit-untiered")), "requests:1m:20,total_tokens:1m:100000"
		)

	def test_a_tier_counting_per_key_sends_its_per_key_table_under_the_key(self):
		"""Each scope has its table, on the tier and on the team; the other one is not in force."""
		tables = {
			"limits": [{"metric": "requests", "window": "1m", "value": 600}],
			"key_limits": [{"metric": "requests", "window": "1m", "value": 6}],
		}
		own = {
			"limits": [{"metric": "requests", "window": "1m", "value": 300}],
			"key_limits": [{"metric": "requests", "window": "1m", "value": 2}],
		}
		pushed = {}
		for scope in ("Team", "Key"):
			tier = frappe.get_doc(
				{"doctype": "Tier", "__newname": f"key-tier-scope-{scope}", "limit_scope": scope, **tables}
			).insert()
			names = (
				team(f"key-limit-{scope}", tier=tier.name),
				team(f"key-limit-{scope}-own", tier=tier.name, **own),
			)
			self.assertEqual(frappe.get_doc("Grove Team", names[0]).limit_scope, scope)
			pushed[scope] = [(record["limits"], record["key_limits"]) for record in map(team_pushed, names)]
		self.assertEqual(
			pushed,
			{
				"Team": [("requests:1m:600", ""), ("requests:1m:300", "")],
				"Key": [("", "requests:1m:6"), ("", "requests:1m:2")],
			},
		)

	def test_a_tier_refuses_two_limits_on_one_model_metric_and_window(self):
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
		served = our_model("key-tier-dear").insert()
		tier = frappe.get_doc(
			{
				"doctype": "Tier",
				"__newname": "key-tier-per-model",
				"limits": [
					{"metric": "requests", "window": "1m", "value": 60},
					{"model": served.name, "metric": "requests", "window": "1m", "value": 6},
				],
			}
		).insert()
		dear = frappe.get_doc(
			{"doctype": "Grove API Key", "team": team("key-limit-per-model", tier=tier.name)}
		).insert()
		self.assertEqual(team_limits_pushed(dear.team), f"requests:1m:60,requests:1m:6:{served.model_key}")
		tier.append("limits", {"model": served.name, "metric": "requests", "window": "1m", "value": 7})
		with self.assertRaisesRegex(frappe.ValidationError, f"for {served.model_key}"):
			tier.save()

	def test_a_limit_row_holds_where_its_model_does(self):
		"""One key served in two geographies is two docs. A tier row linking one reaches that
		geography's team record alone, and a row linking a model of ours reaches both."""
		here, away = make_test_geography("limit-here"), make_test_geography("limit-away")
		docs = {}
		for geography in (here, away):
			vendor = keyed_provider("_T-limit-vendor", base_url="https://limit.test/v1", geography=geography)
			docs[geography] = vendor_model("limit-big", vendor.name).insert()
		ours, shared_key = our_model("limit-ours").insert(), docs[here].model_key
		tier = frappe.get_doc(
			{
				"doctype": "Tier",
				"__newname": "key-tier-by-geography",
				"limits": [
					{"model": docs[here].name, "metric": "requests", "window": "1m", "value": 3},
					{"model": docs[away].name, "metric": "requests", "window": "1m", "value": 9},
					{"model": ours.name, "metric": "requests", "window": "1m", "value": 5},
				],
			}
		).insert()
		name = team("key-limit-by-geography", tier=tier.name)
		pushed = {g: set(effective_teams([name], g)[0]["limits"].split(",")) for g in (here, away, "")}
		everywhere = f"requests:1m:5:{ours.model_key}"
		self.assertEqual(
			pushed,
			{
				here: {everywhere, f"requests:1m:3:{shared_key}"},
				away: {everywhere, f"requests:1m:9:{shared_key}"},
				"": {everywhere},
			},
		)

		by_key = {"model_key": shared_key, "metric": "requests", "window": "1m", "value": 2}
		with self.assertRaisesRegex(frappe.ValidationError, "link the model"):
			frappe.get_doc({"doctype": "Tier", "__newname": "key-tier-by-key", "limits": [by_key]}).insert()


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

	def test_a_picked_geography_is_kept(self):
		other = self.other_geography("geo-picked")
		doc = key("key-geo-picked", geography=other)
		self.assertEqual(doc.geography, other)

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
		with self.assertRaisesRegex(frappe.ValidationError, "would add up to"):
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
		doc.save()
		doc.cap = 9
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc.reload()
		doc.cap = 6
		doc.save()
		frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 0}).insert()


class IntegrationTestExpiry(IntegrationTestCase):
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

	def test_one_key_the_guard_refuses_does_not_hold_the_rest_of_the_sweep_back(self):
		stuck, due = (key("key-probe-sweep-stuck", expiry="1 Day") for _ in range(2))
		yesterday = frappe.utils.add_days(None, -1)
		for doc in (stuck, due):
			frappe.db.set_value("Grove API Key", doc.name, "expires_at", yesterday, update_modified=False)
		revoke = GroveAPIKey.revoke

		def revoke_unless_stuck(self):
			if self.name == stuck.name:
				frappe.throw("probe")
			revoke(self)

		with (
			unittest.mock.patch.object(GroveAPIKey, "revoke", revoke_unless_stuck),
			unittest.mock.patch("frappe.log_error") as logged,
		):
			grove_api_key.revoke_expired()
		self.assertEqual(
			[frappe.db.get_value("Grove API Key", d.name, "status") for d in (stuck, due)],
			["active", "revoked"],
		)
		logged.assert_called_once_with(title=f"Expired key {stuck.name} could not be revoked")

	def test_a_key_minted_to_expire_from_first_use_gets_its_moment_at_its_first_record_once(self):
		name = team("key-probe-first-use")
		first_use = key(name, expiry="7 Days", expiry_from_first_use=1)
		ruled = key(name, expiry="1 Day")
		ruled.reload()
		self.assertEqual(
			(first_use.expiry, first_use.expiry_from_first_use, first_use.expires_at), ("7 Days", 1, None)
		)
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

	def test_never_takes_no_moment_at_first_use(self):
		doc = key("key-probe-first-use-never", expiry_from_first_use=1)
		grove_api_key.start_expiry([doc.name])
		self.assertIsNone(frappe.db.get_value("Grove API Key", doc.name, "expires_at"))

	def test_extending_moves_the_moment_by_days_from_the_later_of_it_and_now(self):
		ahead, lapsed = key("key-probe-extend", expiry="7 Days"), key("key-probe-extend", expiry="1 Day")
		yesterday = frappe.utils.add_days(frappe.utils.now_datetime(), -1)
		frappe.db.set_value("Grove API Key", lapsed.name, "expires_at", yesterday, update_modified=False)
		ahead.reload()
		lapsed.reload()
		moment, stamp = ahead.expires_at, ahead.modified

		ahead.extend_expiry(3)
		lapsed.extend_expiry(3)

		self.assertEqual(
			frappe.db.get_value("Grove API Key", ahead.name, "expires_at"), frappe.utils.add_days(moment, 3)
		)
		self.assertEqual(frappe.db.get_value("Grove API Key", ahead.name, "modified"), stamp)
		fresh = frappe.db.get_value("Grove API Key", lapsed.name, "expires_at")
		self.assertAlmostEqual(
			fresh, frappe.utils.add_days(frappe.utils.now_datetime(), 3), delta=timedelta(seconds=5)
		)

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


class IntegrationTestTransfer(IntegrationTestCase):
	"""A trial's key becomes the customer's at conversion: the same credential under the new team."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(unittest.mock.patch("grove.pathway.usage.peek_team", return_value={}))
		make_test_geography()
		frappe.db.set_single_value("Grove Settings", "default_tier", "")
		frappe.db.set_value("Model Group", {"is_default": 1}, "is_default", 0)
		cls.default = IntegrationTestNewTeamsStartInTheDefaultGroups.group(
			"key-handover-default", is_default=1
		)
		cls.trial_group = IntegrationTestNewTeamsStartInTheDefaultGroups.group("key-handover-trial-models")

	def trial_key(self, team_name, **fields):
		"""A key of a Total team, in the trial group on top of the team's default: a product's."""
		groups = [{"model_group": self.trial_group}]
		return key(team(team_name, key_cap_window="Total"), model_groups=groups, **fields)

	def test_team_and_title_move_and_the_trials_rules_go_in_one_save_the_log_and_the_wire_record(self):
		customer = team("key-handover-customer", spend_limit=10)
		doc = self.trial_key("key-handover-trial", cap=2, expiry="30 Days", expiry_from_first_use=1)
		grove_api_key.start_expiry([doc.name])
		doc.reload()
		trial, stamp = doc.team, doc.modified
		self.assertIsNotNone(doc.expires_at)
		self.assertEqual(projected(doc.name)["group"], ",".join(sorted([self.default, self.trial_group])))

		doc.transfer(customer, title="site.example.test")

		doc.reload()
		self.assertEqual((doc.team, doc.title), (customer, "site.example.test"))
		self.assertEqual((doc.expiry, doc.expiry_from_first_use, doc.expires_at), ("Never", 0, None))
		self.assertEqual(doc.model_groups, [])
		self.assertGreater(doc.modified, frappe.utils.get_datetime(stamp))
		record = projected(doc.name)
		self.assertEqual(
			(record["team"], record["budget_window"], record["budget"], record["group"]),
			(customer, "month", 2_000_000_000, self.default),
		)
		self.assertEqual(self.logged(doc.name), [(trial, customer, None)])
		# Already there: nothing moves, the title asked for is not taken.
		row = keys.transfer_key(trial, doc.name, customer, title="other")
		self.assertEqual(row.title, "site.example.test")
		self.assertEqual(frappe.db.get_value("Grove API Key", doc.name, "modified"), doc.modified)
		self.assertEqual(len(self.logged(doc.name)), 1)

	def logged(self, name):
		rows = frappe.get_all(
			"Key Transfer",
			filters={"api_key": name},
			fields=["from_team", "to_team", "from_title"],
			order_by="creation",
		)
		return [(row.from_team, row.to_team, row.from_title) for row in rows]

	def test_a_move_from_before_the_log_is_filled_in_from_the_version_it_left(self):
		from grove.patches.v1_0 import log_past_key_transfers

		trial = team("key-handover-past-trial", key_cap_window="Total")
		doc = key(team("key-handover-backfilled"), title="past.example.test")
		changed = [["title", "AITK-00012", doc.title], ["team", trial, doc.team]]
		version = frappe.get_doc(
			{
				"doctype": "Version",
				"ref_doctype": "Grove API Key",
				"docname": doc.name,
				"data": frappe.as_json({"changed": changed}),
			}
		).insert()
		version.db_set("creation", "2026-10-20 10:00:00", update_modified=False)

		log_past_key_transfers.execute()

		self.assertEqual(self.logged(doc.name), [(trial, doc.team, "AITK-00012")])
		moved_at = frappe.db.get_value("Key Transfer", {"api_key": doc.name}, "creation")
		self.assertEqual(str(moved_at), "2026-10-20 10:00:00")

	def test_a_title_twin_same_window_and_a_revoked_key_are_refused_and_nothing_moves(self):
		doc = self.trial_key("key-handover-refused")
		taken = team("key-handover-taken")
		key(taken, title="site.example.test")
		with self.assertRaises(frappe.UniqueValidationError):
			doc.transfer(taken, title="site.example.test")
		doc.reload()
		self.assertEqual((doc.team, doc.title), (team("key-handover-refused"), None))
		same = team("key-handover-same-window", key_cap_window="Total")
		with self.assertRaisesRegex(frappe.ValidationError, "would follow"):
			doc.transfer(same)
		self.assertEqual(frappe.db.get_value("Grove API Key", doc.name, "team"), team("key-handover-refused"))
		gone = self.trial_key("key-handover-refused")
		frappe.db.set_value("Grove API Key", gone.name, "status", "revoked")
		gone.reload()
		with self.assertRaisesRegex(frappe.ValidationError, "stays revoked"):
			gone.transfer(taken)
		self.assertEqual(self.logged(doc.name) + self.logged(gone.name), [])

	def test_a_titled_key_meets_the_title_twin_check_though_no_new_title_is_asked(self):
		taken = team("key-handover-taken-quiet")
		key(taken, title="site.example.test")
		doc = self.trial_key("key-handover-titled", title="site.example.test")
		with self.assertRaises(frappe.UniqueValidationError):
			doc.transfer(taken)
		self.assertEqual(frappe.db.get_value("Grove API Key", doc.name, "team"), team("key-handover-titled"))

	def test_the_cap_must_fit_a_monthly_teams_spend_limit(self):
		tight = team("key-handover-tight", spend_limit=5)
		frappe.get_doc({"doctype": "Grove API Key", "team": tight, "cap": 4}).insert()
		doc = self.trial_key("key-handover-capped", cap=2)
		with self.assertRaisesRegex(frappe.ValidationError, "spend limit"):
			doc.transfer(tight)
		self.assertEqual(frappe.db.get_value("Grove API Key", doc.name, "team"), team("key-handover-capped"))
