# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Where an agent pushes: its Region's Metrics Endpoint, and nothing else — sending a region's
samples across the world to a single ingestion URL is what regional endpoints exist to avoid, so
there is no fleet-wide fallback to drift onto."""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography

REGIONAL_URL = "http://ap-south-1.example.com/v1/write"


class TestRemoteWriteUrl(IntegrationTestCase):
	def agent(self, name, remote_write_url):
		"""An agent on a box in its own Region, which may or may not name an endpoint."""
		region = frappe.get_doc(
			{
				"doctype": "Region",
				"name": f"test-agent-region-{name}",
				"label": name,
				"metrics_url": remote_write_url,
				"geography": make_test_geography(),
			}
		).insert(ignore_permissions=True)
		self.addCleanup(region.delete, ignore_permissions=True)

		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"name": f"test-agent-box-{name}",
				"region": region.name,
				"machine_type": "Monitoring Agent",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(machine.delete, ignore_permissions=True)

		agent = frappe.get_doc(
			{
				"doctype": "Monitoring Agent",
				"name": f"test-agent-{name}",
				"machine": machine.name,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(agent.delete, ignore_permissions=True)
		return agent

	def test_a_regions_own_endpoint_wins(self):
		self.assertEqual(self.agent("regional", REGIONAL_URL).remote_write_url, REGIONAL_URL)

	def test_a_region_without_one_leaves_the_agent_nowhere_to_push(self):
		agent = self.agent("blank", "")
		self.assertEqual(agent.remote_write_url, "")
		with self.assertRaises(frappe.ValidationError) as caught:
			agent.preflight()
		self.assertIn("Metrics Endpoint on Region test-agent-region-blank", str(caught.exception))

	def test_the_resolved_url_is_what_ansible_is_given(self):
		agent = self.agent("vars", REGIONAL_URL)
		self.assertEqual(agent.ansible_variables["monitoring_remote_write_url"], REGIONAL_URL)


if __name__ == "__main__":
	unittest.main()
