# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Fleet certificates: a staging lineage is replaced, and renewal runs per Geography. Pure — no site."""

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove import tls


class TestLineageStaleness(unittest.TestCase):
	"""Whether certbot is holding a staging certificate for the zone. It exits 0 and keeps what it has
	when a valid certificate is not due for renewal, so without this check Issue reports success and
	leaves the boxes on a certificate no client trusts."""

	def setUp(self):
		self.config = tempfile.mkdtemp()
		os.makedirs(os.path.join(self.config, "renewal"))
		patcher = patch.object(tls, "certbot_dir", lambda kind: self.config)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(shutil.rmtree, self.config, True)

	def write_renewal(self, server):
		path = os.path.join(self.config, "renewal", "grove.example.com.conf")
		with open(path, "w") as handle:
			handle.write(f"[renewalparams]\nserver = {server}\nauthenticator = dns-route53\n")

	def stale(self):
		return tls.is_lineage_stale("grove.example.com")

	def test_nothing_issued_yet_is_not_stale(self):
		self.assertFalse(self.stale())

	def test_a_staging_certificate_is_stale(self):
		self.write_renewal("https://acme-staging-v02.api.letsencrypt.org/directory")
		self.assertTrue(self.stale())

	def test_a_production_certificate_is_kept(self):
		self.write_renewal("https://acme-v02.api.letsencrypt.org/directory")
		self.assertFalse(self.stale())


class TestRenewalIsPerGeography(unittest.TestCase):
	def test_each_issued_zone_renews_and_one_failing_does_not_stop_the_rest(self):
		issued = [
			frappe._dict(name="in", fleet_zone="in.example.com"),
			frappe._dict(name="eu", fleet_zone="eu.example.com"),
		]
		renewed, pushed = [], []

		def run_certbot(settings, arguments, geography):
			if geography == "in":
				raise RuntimeError("rate limited")
			renewed.append((geography, arguments))

		with (
			patch.object(frappe, "get_single"),
			patch.object(frappe, "get_all", return_value=issued),
			patch.object(frappe, "db", SimpleNamespace(rollback=lambda: None)),
			patch.object(frappe, "log_error"),
			patch.object(tls, "run_certbot", side_effect=run_certbot),
			patch.object(tls, "store_certificate", return_value=True),
			patch.object(tls, "push_to_proxies", side_effect=pushed.append),
			patch.object(tls.failure, "report") as report,
		):
			tls.renew_fleet_certificate()
		self.assertEqual(renewed, [("eu", ["renew", "--cert-name", "eu.example.com"])])
		self.assertEqual(pushed, ["eu"])
		self.assertEqual(report.call_args.args[:2], ("Geography", "in"))

	def test_a_certificate_reaches_only_its_geographys_boxes(self):
		queried = []
		with patch.object(
			frappe, "get_all", side_effect=lambda doctype, filters, pluck: queried.append(filters) or []
		):
			tls.push_to_proxies("eu")
		self.assertEqual(len(queried), 3)
		self.assertEqual({filters["geography"] for filters in queried}, {"eu"})


class TestServiceDiscoveryToken(IntegrationTestCase):
	"""Site-backed: the check reads the Password field and whether it changed on this save."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.addClassCleanup(frappe.clear_document_cache, "Grove Settings", "Grove Settings")

	def save_with_token(self, token):
		settings = frappe.get_single("Grove Settings")
		settings.sd_token = token
		settings.save()

	def test_a_short_or_unsafe_token_is_refused(self):
		for token in ("short", "an&ampersand-long-enough", "a#hash-long-enough-too"):
			with self.subTest(token), self.assertRaises(frappe.ValidationError):
				self.save_with_token(token)

	def test_a_long_url_safe_token_is_taken(self):
		self.save_with_token("0f3a-b21c_9d.e~4a7b-0f3a")
		self.assertEqual(
			frappe.get_single("Grove Settings").get_password("sd_token"), "0f3a-b21c_9d.e~4a7b-0f3a"
		)

	def test_a_save_that_leaves_the_token_alone_is_not_checked(self):
		frappe.get_single("Grove Settings").save()


if __name__ == "__main__":
	unittest.main()
