# Copyright (c) 2026, Frappe and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.model_provider.test_model_provider import add_key, keyed_provider


class TestAKeyIsKnownByItsTitle(IntegrationTestCase):
	def test_two_keys_of_one_provider_cannot_share_a_title(self):
		vendor = keyed_provider("_T-probe-twice-titled", base_url="https://api.probe.test")
		with self.assertRaises(frappe.UniqueValidationError):
			add_key(vendor, "k2")

	def test_two_providers_can_each_title_a_key_the_same(self):
		keyed_provider("_T-probe-titled-here")
		keyed_provider("_T-probe-titled-there")


class TestTheKeysAGatewayMayDial(IntegrationTestCase):
	def test_they_are_read_oldest_first_and_a_disabled_one_stays_home(self):
		vendor = keyed_provider("_T-probe-ring", base_url="https://api.probe.test")
		add_key(vendor, "k-off", "off", disabled=1)
		add_key(vendor, "k2", "second")
		self.assertEqual([key.get_password("api_key") for key in vendor.enabled_keys], ["k", "k2"])

	def test_a_key_is_used_for_what_it_says_and_a_disabled_one_for_nothing(self):
		vendor = keyed_provider("_T-probe-uses", base_url="https://api.probe.test")
		add_key(vendor, "k-admin", "admin", used_for="Billing")
		add_key(vendor, "k-both", "both", used_for="API Calls and Billing")
		add_key(vendor, "k-off", "off", used_for="Billing", disabled=1)
		self.assertEqual([key.title for key in vendor.gateway_keys], ["key", "both"])
		self.assertEqual([key.title for key in vendor.billing_keys], ["admin", "both"])

	def test_a_key_saved_on_its_own_reaches_the_cached_provider(self):
		vendor = keyed_provider("_T-probe-cached", base_url="https://api.probe.test")
		cached = frappe.get_cached_doc("Model Provider", vendor.name)
		late = add_key(vendor, "k-late", "late")
		self.assertEqual(cached.enabled_keys[-1].name, late.name)
