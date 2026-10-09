# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""A key carries its own policy — groups, allow/deny, rate limits, geography, cap — and the wire
record the gateway reads is built from it.

A team id per test: IntegrationTestCase rolls back once when the class is done, not between
tests, so anything registered here is still there for the next one."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.access import limit_rows
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.model_provider.test_model_provider import our_model, provider, vendor_model
from grove.pathway.snapshot import effective_keys


def team(name, free=1, **fields):
	"""Free unless said otherwise: a prepaid team's key needs a cap cut from a funded balance."""
	if frappe.db.exists("Central Team", name):
		return name
	return frappe.get_doc({
		"doctype": "Central Team", "__newname": name, "email": f"{name}@example.com", "free": free, **fields,
	}).insert(ignore_permissions=True).name


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

	def test_membership_reaches_the_wire_as_one_sorted_comma_list(self):
		# The unit tests mock the query away, so this is the only thing standing between a wrong
		# parenttype/parentfield filter and every key projecting as ungrouped.
		for name in ("key-probe-zeta", "key-probe-acme"):
			if not frappe.db.exists("Model Group", name):
				frappe.get_doc({"doctype": "Model Group", "__newname": name}).insert()
		doc = key("key-probe-groups", model_groups=[{"model_group": "key-probe-zeta"}, {"model_group": "key-probe-acme"}])
		self.assertEqual(projected(doc.name)["group"], "key-probe-acme,key-probe-zeta")

	def test_the_teams_flags_ride_on_the_key(self):
		name = team("key-probe-flags", free=0)
		frappe.get_doc({"doctype": "Grove Credit", "team": name, "amount": 1}).insert(ignore_permissions=True)
		doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 1}).insert()
		record = projected(doc.name)
		self.assertEqual(
			(record["team"], record["prepaid"], record["budget"], record["limited"], record["log_payloads"]),
			(name, True, 1_000_000_000, False, True),
		)
		frappe.db.set_value("Central Team", name, {"free": 1, "log_payloads": 0})
		record = projected(doc.name)
		self.assertEqual((record["prepaid"], record["budget"], record["log_payloads"]), (False, 0, False))

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
		frappe.db.set_value("Model Group", {"is_default": 1}, "is_default", 0)
		cls.default = cls.group("key-default-one", is_default=1)

	@staticmethod
	def group(name, is_default=0):
		return frappe.get_doc({"doctype": "Model Group", "__newname": name, "is_default": is_default}).insert().name

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
		from grove import api

		api.provision_team("key-group-provisioned", "provisioned@example.com", free=True)
		minted = api.provision_key("key-group-provisioned")
		self.assertEqual(self.groups_of(frappe.get_doc("Grove API Key", minted["name"])), [self.default])

	def test_a_new_key_starts_in_its_own_geographys_default(self):
		away = make_test_geography("key-default-away")
		theirs = frappe.get_doc(
			{"doctype": "Model Group", "__newname": "key-default-away", "geography": away, "is_default": 1}
		).insert().name
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
			vendor = provider("allow-vendor", base_url="https://allow.test/v1", api_key="k", geography=geography).insert()
			cls.docs[geography] = vendor_model("allow-big", vendor.name).insert().name
		cls.ours = our_model("allow-ours-7b").insert().name

	def key(self, name, **fields):
		return frappe.get_doc({"doctype": "Grove API Key", "team": team(name), "geography": self.here, **fields})

	def test_a_vendor_model_of_another_geography_is_refused_and_ours_is_not(self):
		self.key("key-allow-here", allow=[{"model": self.docs[self.here]}, {"model": self.ours}]).insert()
		for field in ("allow", "deny"):
			with self.assertRaises(frappe.ValidationError):
				self.key(f"key-{field}-away", **{field: [{"model": self.docs[self.away]}]}).insert()

	def test_provisioning_by_key_allows_the_keys_geographys_doc(self):
		from grove import api

		api.provision_team("key-allow-keyed", "keyed@example.com", free=True)
		minted = api.provision_key("key-allow-keyed", geography=self.away, models=[self.KEY])
		allowed = frappe.get_all("Grove Model Row", filters={"parent": minted["name"], "parentfield": "allow"}, pluck="model")
		self.assertEqual(allowed, [self.docs[self.away]])


class IntegrationTestRateLimits(IntegrationTestCase):
	"""A key's limits are rows here and one sorted list on the wire."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()

	def test_a_new_key_starts_under_the_default_limits(self):
		doc = key("key-limit-default")
		self.assertEqual(projected(doc.name)["limits"], "requests:1m:20,total_tokens:1m:100000")

	def test_limits_reach_the_wire_sorted(self):
		doc = key("key-limit-wire", limits=[
			{"metric": "total_tokens", "window": "1M", "value": 50_000_000},
			{"metric": "requests", "window": "1m", "value": 200},
		])
		self.assertEqual(projected(doc.name)["limits"], "requests:1m:200,total_tokens:1M:50000000")

	def test_two_limits_on_one_metric_and_window_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			key("key-limit-twice", limits=[
				{"metric": "requests", "window": "1m", "value": 200},
				{"metric": "requests", "window": "1m", "value": 300},
			])

	def test_a_limit_must_be_above_zero(self):
		with self.assertRaises(frappe.ValidationError):
			key("key-limit-zero", limits=[{"metric": "requests", "window": "1m", "value": 0}])

	def test_a_window_the_gateway_does_not_know_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			key("key-limit-week", limits=[{"metric": "requests", "window": "1w", "value": 5}])

	def test_provisioning_replaces_the_limits_it_is_given_and_keeps_them_otherwise(self):
		from grove import api

		team_id = "key-limit-provisioned"
		api.provision_team(team_id, "limited@example.com", free=True)
		name = api.provision_key(team_id, limits=[{"metric": "requests", "window": "1m", "value": 5}])["name"]
		api.update_key(team_id, name)
		self.assertEqual(limit_rows()[name], ["requests:1m:5"])
		api.update_key(team_id, name, limits=[{"metric": "requests", "window": "1h", "value": 9}])
		self.assertEqual(limit_rows()[name], ["requests:1h:9"])
		api.update_key(team_id, name, limits=[])
		self.assertNotIn(name, limit_rows())


class IntegrationTestOneGeographyPerKey(IntegrationTestCase):
	"""A key's spend and counters land on one store, so every key is pinned to exactly one
	geography, for good."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		cls.default = frappe.db.get_value("Geography", {"is_default": 1})

	def other_geography(self, name):
		if not frappe.db.exists("Geography", name):
			frappe.get_doc({
				"doctype": "Geography", "__newname": name,
				"fleet_zone": f"{name}.grove.localhost", "endpoint": f"api.{name}.grove.localhost",
			}).insert(ignore_permissions=True)
		return name

	def test_a_key_saved_without_one_is_pinned_to_the_default(self):
		doc = key("key-geo-default")
		self.assertEqual((doc.geography, projected(doc.name)["geography"]), (self.default, self.default))

	def test_a_picked_geography_is_kept_and_never_changes(self):
		other = self.other_geography("geo-picked")
		doc = key("key-geo-picked", geography=other)
		self.assertEqual(doc.geography, other)
		doc.geography = self.default
		with self.assertRaisesRegex(frappe.ValidationError, "set once"):
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


class IntegrationTestCapsAreCutFromTheBalance(IntegrationTestCase):
	"""Across a team's live keys, Σ (cap - spent) never exceeds the balance: that is what lets
	every store gate its key exactly without the keys together outspending the team."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()

	def funded(self, team_id, amount):
		name = team(team_id, free=0)
		frappe.get_doc({"doctype": "Grove Credit", "team": name, "amount": amount}).insert(ignore_permissions=True)
		return name

	def test_caps_beyond_the_balance_are_refused_and_a_revoke_frees_its_share(self):
		from grove.billing.pricing import settle

		name = self.funded("key-cap-team", 10)
		first = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 6}).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "would hand out"):
			frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 6}).insert()
		second = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 4}).insert()
		self.assertEqual((projected(first.name)["budget"], projected(second.name)["budget"]), (6_000_000_000, 4_000_000_000))
		# Spend moves the balance and narrows what the key still claims alike: 6 cap with 5 spent
		# claims 1 of the 5 left, so the other key may claim 4 and no more.
		frappe.db.set_value("Grove API Key", first.name, "spent", 5)
		settle(name)
		second.cap = 5
		with self.assertRaises(frappe.ValidationError):
			second.save()
		# A revoked key hands its share back.
		frappe.db.set_value("Grove API Key", first.name, "status", "revoked")
		second.reload()
		second.cap = 5
		second.save()

	def test_a_prepaid_key_without_a_cap_is_minted_at_0_and_a_free_teams_caps_are_not_checked(self):
		name = self.funded("key-cap-needed", 5)
		for cap in (0, None):
			doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": cap}).insert()
			# Pushed prepaid with nothing to spend: the gateway refuses it until a limit is set.
			self.assertEqual((projected(doc.name)["prepaid"], projected(doc.name)["budget"]), (True, 0))
		free = team("key-cap-free")
		frappe.get_doc({"doctype": "Grove API Key", "team": free, "cap": 1000}).insert()
		frappe.get_doc({"doctype": "Grove API Key", "team": free, "cap": 0}).insert()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Grove API Key", "team": free, "cap": -1}).insert()

	def test_a_save_that_leaves_the_cap_alone_is_not_checked(self):
		# A box can overshoot a cap by what was in flight, which takes the balance under the caps
		# handed out; only a cap change has to fit what is left.
		from grove.billing.pricing import settle

		name = self.funded("key-cap-still", 5)
		doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 5}).insert()
		frappe.db.set_value("Grove API Key", doc.name, "spent", 6)
		settle(name)
		doc.reload()
		doc.title = "renamed"
		doc.save()
		doc.cap = 7
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_a_refund_past_the_caps_is_refused_and_turning_free_off_resets_them(self):
		name = self.funded("key-cap-refund", 5)
		doc = frappe.get_doc({"doctype": "Grove API Key", "team": name, "cap": 5}).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "Lower the keys' caps first"):
			frappe.get_doc({"doctype": "Grove Credit", "team": name, "amount": -3, "note": "refund"}).insert(ignore_permissions=True)
		doc.cap = 2
		doc.save()
		frappe.get_doc({"doctype": "Grove Credit", "team": name, "amount": -3, "note": "refund"}).insert(ignore_permissions=True)
		# A Free team's caps were never checked: they do not become budgets by flipping the flag.
		free = team("key-cap-flip")
		capped = frappe.get_doc({"doctype": "Grove API Key", "team": free, "cap": 1000}).insert()
		team_doc = frappe.get_doc("Central Team", free)
		team_doc.free = 0
		team_doc.save()
		self.assertEqual(frappe.db.get_value("Grove API Key", capped.name, "cap"), 0)
		self.assertEqual(projected(capped.name)["budget"], 0)
