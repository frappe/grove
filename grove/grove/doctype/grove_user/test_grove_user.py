# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt
"""One policy per login, and a login for every policy.

An email per test: IntegrationTestCase rolls back once when the class is done, not between
tests, so anything registered here is still there for the next one."""

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_user.grove_user import GROVE_USER_ROLE, register_user
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
