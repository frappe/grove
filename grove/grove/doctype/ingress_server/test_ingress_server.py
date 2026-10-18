# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""What an Ingress Server is allowed to hold. Pure — reads the doctype JSON, no site.

Risk 2 of the two-plane split: one binary runs both planes, and the tenant/infra boundary is
enforced by what each side is GIVEN rather than by the code. The doctype carrying no tenant field
is what keeps the control plane from ever pushing keys to an ingress — the moment one grows an
`api_key` or a `grove_user` Link, somebody's keys push will find it.
"""

import json
import unittest
from pathlib import Path

DOCTYPE = Path(__file__).parent / "ingress_server.json"

TENANT_DOCTYPES = {
	"Grove API Key",
	"Central Team",
	"Model",
	"Model Group",
	"Usage Record",
}


class TestAnIngressHoldsNoTenantState(unittest.TestCase):
	def setUp(self):
		self.fields = json.loads(DOCTYPE.read_text())["fields"]

	def test_it_links_to_nothing_tenant_shaped(self):
		for field in self.fields:
			with self.subTest(field["fieldname"]):
				self.assertNotIn(field.get("options"), TENANT_DOCTYPES)

	def test_its_only_secrets_are_the_two_infra_tokens(self):
		"""Separate on purpose: one token would put the control plane's credential on every gateway."""
		passwords = [f["fieldname"] for f in self.fields if f["fieldtype"] == "Password"]
		self.assertEqual(sorted(passwords), ["admin_token", "data_token"])

	def test_it_knows_exactly_one_network_and_it_is_its_boxs(self):
		from grove.grove.doctype.ingress_server.ingress_server import IngressServer

		[network] = [field for field in self.fields if field["fieldname"] == "network"]
		self.assertEqual(network["options"], "Network")
		self.assertTrue(network["reqd"])
		self.assertEqual(network["fetch_from"], "machine.network")
		self.assertTrue(network["read_only"])
		self.assertFalse(hasattr(IngressServer, "validate_machine_network"))


if __name__ == "__main__":
	unittest.main()
