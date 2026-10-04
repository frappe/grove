# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""One policy per login, and a login for every policy.

An email per test: IntegrationTestCase rolls back once when the class is done, not between
tests, so anything registered here is still there for the next one."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.access import limit_rows
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_user.grove_user import GROVE_USER_ROLE, register_user
from grove.grove.doctype.model_provider.test_model_provider import our_model, provider, vendor_model
from grove.pathway.snapshot import effective_users


class IntegrationTestGroveUser(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()

	def test_a_policy_registers_the_login_it_names(self):
		# Provisioning is by email, for someone who may never have signed in.
		email = "grove-probe-new@example.com"
		self.assertFalse(frappe.db.exists("User", email))
		frappe.get_doc({"doctype": "Grove User", "user": register_user(email, "Probe Person")}).insert()
		self.assertEqual(frappe.db.get_value("User", email, "first_name"), "Probe Person")
		# Carries the Grove User role: an identity to scope later, no perms now.
		self.assertIn(GROVE_USER_ROLE, frappe.get_roles(email))

	def test_registering_twice_leaves_the_login_alone(self):
		email = "grove-probe-twice@example.com"
		register_user(email, "Probe Person")
		register_user(email, "Renamed")
		self.assertEqual(frappe.db.get_value("User", email, "first_name"), "Probe Person")

	def test_a_login_cannot_hold_two_policies(self):
		# The budget is the person's, so a second policy for one login is a second allowance.
		email = "grove-probe-twin@example.com"
		frappe.get_doc({"doctype": "Grove User", "user": register_user(email)}).insert()
		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc({"doctype": "Grove User", "user": email}).insert()

	def test_membership_reaches_the_wire_as_one_sorted_comma_list(self):
		# The unit tests mock the query away, so this is the only thing standing between a wrong
		# parenttype/parentfield filter and every user projecting as ungrouped.
		email = "grove-probe-groups@example.com"
		for name in ("grove-probe-zeta", "grove-probe-acme"):
			if not frappe.db.exists("Model Group", name):
				frappe.get_doc({"doctype": "Model Group", "__newname": name}).insert()
		grove_user = frappe.get_doc(
			{
				"doctype": "Grove User",
				"user": register_user(email),
				"model_groups": [
					{"model_group": "grove-probe-zeta"},
					{"model_group": "grove-probe-acme"},
				],
			}
		).insert()
		[record] = [u for u in effective_users() if u["name"] == grove_user.name]
		self.assertEqual(record["group"], "grove-probe-acme,grove-probe-zeta")


class IntegrationTestNewUsersStartInTheDefaultGroup(IntegrationTestCase):
	"""What a user may call is decided here, by group, not sent by whoever provisions them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		frappe.db.set_value("Model Group", {"is_default": 1}, "is_default", 0)
		cls.default = cls.group("grove-default-one", is_default=1)

	@staticmethod
	def group(name, is_default=0):
		return frappe.get_doc({"doctype": "Model Group", "__newname": name, "is_default": is_default}).insert().name

	def groups_of(self, grove_user):
		return [row.model_group for row in grove_user.model_groups]

	def test_a_new_user_is_put_in_the_default_group(self):
		doc = frappe.get_doc({"doctype": "Grove User", "user": register_user("grove-group-new@example.com")}).insert()
		self.assertEqual(self.groups_of(doc), [self.default])
		[record] = [u for u in effective_users() if u["name"] == doc.name]
		self.assertEqual(record["group"], self.default)

	def test_groups_named_on_the_insert_are_kept(self):
		picked = self.group("grove-default-picked")
		doc = frappe.get_doc({
			"doctype": "Grove User", "user": register_user("grove-group-picked@example.com"),
			"model_groups": [{"model_group": picked}],
		}).insert()
		self.assertEqual(self.groups_of(doc), [picked])

	def test_a_user_taken_out_of_every_group_stays_out(self):
		doc = frappe.get_doc({"doctype": "Grove User", "user": register_user("grove-group-out@example.com")}).insert()
		doc.model_groups = []
		doc.save()
		self.assertEqual(self.groups_of(doc.reload()), [])

	def test_provisioning_a_key_puts_the_new_user_in_the_default_group(self):
		from grove import api

		grove_user = api._set_policy("grove-group-provisioned@example.com", "Provisioned", None)
		self.assertEqual(self.groups_of(frappe.get_doc("Grove User", grove_user)), [self.default])

	def test_ticking_a_second_default_unticks_the_first(self):
		self.addCleanup(frappe.db.set_value, "Model Group", self.default, "is_default", 1)
		other = self.group("grove-default-two", is_default=1)
		self.addCleanup(frappe.db.set_value, "Model Group", other, "is_default", 0)
		here = frappe.db.get_value("Model Group", other, "geography")
		ticked = frappe.get_all("Model Group", filters={"is_default": 1, "geography": here}, pluck="name")
		self.assertEqual(ticked, [other])

	def test_a_new_user_starts_in_their_own_geographys_default(self):
		away = make_test_geography("grove-default-away")
		theirs = frappe.get_doc(
			{"doctype": "Model Group", "__newname": "grove-default-away", "geography": away, "is_default": 1}
		).insert().name
		user = register_user("grove-group-away@example.com")
		doc = frappe.get_doc({"doctype": "Grove User", "user": user, "geography": away}).insert()
		self.assertEqual(self.groups_of(doc), [theirs])


class IntegrationTestOwnGrantsAreTheUsersGeographys(IntegrationTestCase):
	"""An Allow or Deny row names the vendor doc the user's own geography serves."""

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

	def user(self, email, **fields):
		return frappe.get_doc({"doctype": "Grove User", "user": register_user(email), "geography": self.here, **fields})

	def test_a_vendor_model_of_another_geography_is_refused_and_ours_is_not(self):
		self.user("grove-allow-here@example.com", allow=[{"model": self.docs[self.here]}, {"model": self.ours}]).insert()
		for field in ("allow", "deny"):
			with self.assertRaises(frappe.ValidationError):
				self.user(f"grove-{field}-away@example.com", **{field: [{"model": self.docs[self.away]}]}).insert()

	def test_provisioning_by_key_allows_the_users_geographys_doc(self):
		from grove import api

		grove_user = api._set_policy("grove-allow-keyed@example.com", "Keyed", [self.KEY], geography=self.away)
		allowed = frappe.get_all("Grove Model Row", filters={"parent": grove_user, "parentfield": "allow"}, pluck="model")
		self.assertEqual(allowed, [self.docs[self.away]])


class IntegrationTestRateLimits(IntegrationTestCase):
	"""A user's limits are rows here and one sorted list on the wire."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()

	def user(self, email, limits):
		return frappe.get_doc({"doctype": "Grove User", "user": register_user(email), "limits": limits})

	def test_a_new_user_starts_under_the_default_limits(self):
		doc = self.user("grove-limit-default@example.com", []).insert()
		[record] = [u for u in effective_users() if u["name"] == doc.name]
		self.assertEqual(record["limits"], "requests:1m:20,total_tokens:1m:100000")

	def test_limits_reach_the_wire_sorted(self):
		doc = self.user("grove-limit-wire@example.com", [
			{"metric": "total_tokens", "window": "1M", "value": 50_000_000},
			{"metric": "requests", "window": "1m", "value": 200},
		]).insert()
		[record] = [u for u in effective_users() if u["name"] == doc.name]
		self.assertEqual(record["limits"], "requests:1m:200,total_tokens:1M:50000000")

	def test_two_limits_on_one_metric_and_window_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.user("grove-limit-twice@example.com", [
				{"metric": "requests", "window": "1m", "value": 200},
				{"metric": "requests", "window": "1m", "value": 300},
			]).insert()

	def test_a_limit_must_be_above_zero(self):
		with self.assertRaises(frappe.ValidationError):
			self.user("grove-limit-zero@example.com", [{"metric": "requests", "window": "1m", "value": 0}]).insert()

	def test_a_window_the_gateway_does_not_know_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.user("grove-limit-week@example.com", [{"metric": "requests", "window": "1w", "value": 5}]).insert()

	def test_provisioning_replaces_the_limits_it_is_given_and_keeps_them_otherwise(self):
		from grove import api

		email = "grove-limit-provisioned@example.com"
		grove_user = api._set_policy(email, "Limited", None, limits=[{"metric": "requests", "window": "1m", "value": 5}])
		api._set_policy(email, "Limited", None)
		self.assertEqual(limit_rows()[grove_user], ["requests:1m:5"])
		api._set_policy(email, "Limited", None, limits=[{"metric": "requests", "window": "1h", "value": 9}])
		self.assertEqual(limit_rows()[grove_user], ["requests:1h:9"])
		api._set_policy(email, "Limited", None, limits=[])
		self.assertNotIn(grove_user, limit_rows())


class IntegrationTestOneGeographyPerUser(IntegrationTestCase):
	"""A user's spend must land on one store, so every user is pinned to exactly one geography."""

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

	def test_a_user_saved_without_one_is_pinned_to_the_default(self):
		doc = frappe.get_doc({"doctype": "Grove User", "user": register_user("grove-geo-default@example.com")}).insert()
		self.assertEqual(doc.geography, self.default)

	def test_a_picked_geography_is_kept(self):
		other = self.other_geography("geo-picked")
		doc = frappe.get_doc({
			"doctype": "Grove User", "user": register_user("grove-geo-picked@example.com"), "geography": other,
		}).insert()
		self.assertEqual(doc.geography, other)

	def test_with_no_default_a_user_without_one_is_refused(self):
		frappe.db.set_value("Geography", self.default, "is_default", 0)
		self.addCleanup(frappe.db.set_value, "Geography", self.default, "is_default", 1)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Grove User", "user": register_user("grove-geo-none@example.com")}).insert()

	def test_ticking_a_second_default_unticks_the_first(self):
		other = frappe.get_doc("Geography", self.other_geography("geo-second"))
		self.addCleanup(frappe.db.set_value, "Geography", self.default, "is_default", 1)
		self.addCleanup(frappe.db.set_value, "Geography", other.name, "is_default", 0)
		other.is_default = 1
		other.save(ignore_permissions=True)
		self.assertEqual(frappe.get_all("Geography", filters={"is_default": 1}, pluck="name"), [other.name])
