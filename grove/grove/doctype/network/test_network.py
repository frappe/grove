# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Ingress rules and security_group_ids parsing (pure), cidr_block auto-assignment (hits the DB)."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.cloud_provider import reconcile
from grove.cloud_provider.aws import EC2Client
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.network.network import (
	INFERENCE_BASE_INGRESS_RULES,
	PROXY_INGRESS_RULES,
	Network,
	inference_ingress_cidrs,
	parse_security_group_ids,
)


class TestIngressRules(unittest.TestCase):
	"""What Grove opens to 0.0.0.0/0 on a box it builds. Nothing at runtime notices a rule that
	is wider than it needs to be — a widened group is invisible until somebody scans the box."""

	def ports(self, rules):
		return {(rule["from_port"], rule["to_port"]) for rule in rules}

	def test_an_inference_box_opens_nothing_but_ssh_at_creation(self):
		# 443 is not a fixed rule: only the gateway and the metrics agents dial the engine proxy
		# behind it, so its sources are reconciled per Network.
		self.assertEqual(self.ports(INFERENCE_BASE_INGRESS_RULES), {(22, 22)})

	def test_no_engine_port_is_reachable_from_outside(self):
		# The old rule was a fixed 8080-8085 range, which _assign_engine_port could outgrow — the
		# seventh deployment provisioned green and was unreachable.
		for rule in INFERENCE_BASE_INGRESS_RULES:
			with self.subTest(rule["from_port"]):
				self.assertIsInstance(rule["from_port"], int)
				self.assertFalse(rule["from_port"] <= 8080 <= rule["to_port"])

	def test_a_proxy_box_still_serves_the_client_api(self):
		# 443 stays open here: the customer API and the admin API, neither with an address worth
		# pinning.
		self.assertEqual(self.ports(PROXY_INGRESS_RULES), {(22, 22), (80, 80), (443, 443)})


def proxy(public_ip, status="Active"):
	return {"public_ip": public_ip, "status": status}


def agent(public_ip, network="", private_ip="", status="Active"):
	return {"public_ip": public_ip, "network": network, "private_ip": private_ip, "status": status}


class TestInferenceIngressCidrs(unittest.TestCase):
	"""Who may reach an inference box on 443 from outside its VPC. Everything wrong here fails
	silently in one of two directions: too narrow and the gateway 504s or the metrics go dark, too wide and the engine
	front — which carries no auth of its own — is back on the open internet."""

	def test_a_proxy_is_allowed_as_a_single_address(self):
		self.assertEqual(
			inference_ingress_cidrs([proxy("54.251.169.42")], [], "Mumbai"),
			["54.251.169.42/32"],
		)

	def test_a_terminated_proxy_is_dropped(self):
		# Its address goes back to AWS, and the next tenant would inherit the hole.
		cidrs = inference_ingress_cidrs(
			[proxy("1.1.1.1"), proxy("2.2.2.2", status="Terminated")], [], "Mumbai"
		)
		self.assertEqual(cidrs, ["1.1.1.1/32"])

	def test_a_proxy_with_no_address_yet_contributes_nothing(self):
		self.assertEqual(inference_ingress_cidrs([proxy("")], [], "Mumbai"), [])

	def test_an_agent_in_this_network_needs_no_rule_of_its_own(self):
		# It dials privately and the VPC range covers it; a public /32 would open a hole nothing uses.
		cidrs = inference_ingress_cidrs(
			[], [agent("3.3.3.3", network="Mumbai", private_ip="10.0.0.7")], "Mumbai"
		)
		self.assertEqual(cidrs, [])

	def test_an_agent_elsewhere_arrives_publicly(self):
		cidrs = inference_ingress_cidrs(
			[], [agent("3.3.3.3", network="Frankfurt", private_ip="10.9.0.7")], "Mumbai"
		)
		self.assertEqual(cidrs, ["3.3.3.3/32"])

	def test_a_terminated_agent_is_dropped(self):
		self.assertEqual(inference_ingress_cidrs([], [agent("3.3.3.3", status="Terminated")], "M"), [])

	def test_the_same_address_twice_is_one_rule(self):
		# AWS rejects a duplicate rule outright, so a proxy and an agent on one box must collapse.
		cidrs = inference_ingress_cidrs([proxy("1.1.1.1")], [agent("1.1.1.1")], "Mumbai")
		self.assertEqual(cidrs, ["1.1.1.1/32"])

	def test_a_network_that_owns_nothing_yet_allows_nobody_outside(self):
		self.assertEqual(inference_ingress_cidrs([], [], "Mumbai"), [])

	def test_the_vpc_itself_is_always_let_in(self):
		# Boxes in one VPC reach each other: an ingress and a same-VPC agent dial privately.
		network = SimpleNamespace(
			name="Mumbai", geography="India", cidr_block="10.3.0.0/16",
			scraping_agents=[agent("3.3.3.3", network="Frankfurt")],
		)
		with patch("frappe.get_all", return_value=[]):
			cidrs = Network.inference_ingress_cidrs.fget(network)
		self.assertEqual(cidrs, ["10.3.0.0/16", "3.3.3.3/32"])


class TestSyncIngress(unittest.TestCase):
	"""Reconciling one port. The rule that matters most is the one about the OTHER ports: a diff
	that revoked everything it did not recognise would take 22 with it, and Ansible reaches every
	box in the fleet over 22."""

	def client(self, permissions):
		"""An EC2Client whose group reports `permissions`, recording what it is asked to change."""
		client = EC2Client.__new__(EC2Client)  # no boto3, no credentials
		client.region = "ap-south-1"
		client.authorized, client.revoked = [], []
		client.ec2 = SimpleNamespace(
			describe_security_groups=lambda **kw: {"SecurityGroups": [{"IpPermissions": permissions}]},
			authorize_security_group_ingress=lambda **kw: client.authorized.append(kw) or {},
			revoke_security_group_ingress=lambda **kw: client.revoked.append(kw) or {},
		)
		return client

	def cidrs_in(self, calls):
		return sorted(
			ip_range["CidrIp"]
			for call in calls
			for permission in call["IpPermissions"]
			for ip_range in permission.get("IpRanges", [])
		)

	def permission(self, port, *cidrs, source_groups=()):
		return {
			"FromPort": port, "ToPort": port, "IpProtocol": "tcp",
			"IpRanges": [{"CidrIp": cidr} for cidr in cidrs],
			"UserIdGroupPairs": [{"GroupId": group} for group in source_groups],
		}

	def test_the_world_is_closed_and_the_fleet_opened(self):
		client = self.client([self.permission(443, "0.0.0.0/0")])
		result = client.sync_ingress("sg-1", 443, ["1.1.1.1/32"])
		self.assertEqual(result, {"opened": ["1.1.1.1/32"], "closed": ["0.0.0.0/0"]})
		self.assertEqual(self.cidrs_in(client.authorized), ["1.1.1.1/32"])
		self.assertEqual(self.cidrs_in(client.revoked), ["0.0.0.0/0"])

	def test_ssh_is_never_touched(self):
		client = self.client([self.permission(22, "0.0.0.0/0"), self.permission(443, "0.0.0.0/0")])
		client.sync_ingress("sg-1", 443, ["1.1.1.1/32"])
		for call in client.authorized + client.revoked:
			for permission in call["IpPermissions"]:
				self.assertEqual(permission["FromPort"], 443)

	def test_a_group_already_correct_is_left_alone(self):
		# Called on every proxy save — an AWS write per save would be noise and a rate limit.
		client = self.client([self.permission(443, "1.1.1.1/32", "2.2.2.2/32")])
		result = client.sync_ingress("sg-1", 443, ["2.2.2.2/32", "1.1.1.1/32"])
		self.assertEqual(result, {"opened": [], "closed": []})
		self.assertEqual(client.authorized, [])
		self.assertEqual(client.revoked, [])

	def test_a_readdressed_proxy_loses_its_old_rule(self):
		client = self.client([self.permission(443, "1.1.1.1/32")])
		result = client.sync_ingress("sg-1", 443, ["9.9.9.9/32"])
		self.assertEqual(result, {"opened": ["9.9.9.9/32"], "closed": ["1.1.1.1/32"]})

	def test_a_group_source_on_this_port_is_revoked_too(self):
		# The desired state is a list of addresses, so a group-to-group rule is never in it — and
		# left behind it is a hole no /32 diff can see.
		client = self.client([self.permission(443, source_groups=["sg-proxy"])])
		result = client.sync_ingress("sg-1", 443, ["1.1.1.1/32"])
		self.assertEqual(result["closed"], ["sg-proxy"])
		self.assertEqual(
			[pair["GroupId"] for call in client.revoked
			 for permission in call["IpPermissions"]
			 for pair in permission.get("UserIdGroupPairs", [])],
			["sg-proxy"],
		)

	def test_an_empty_group_opens_the_whole_desired_set(self):
		client = self.client([])
		result = client.sync_ingress("sg-1", 443, ["1.1.1.1/32", "2.2.2.2/32"])
		self.assertEqual(result["opened"], ["1.1.1.1/32", "2.2.2.2/32"])
		self.assertEqual(client.revoked, [])

	def test_one_port_is_reconciled_without_disturbing_the_other_front_port(self):
		# Separate calls against the same group, so each must see only its own permission or every
		# run revokes what the last one wrote.
		client = self.client([self.permission(443, "1.1.1.1/32")])
		result = client.sync_ingress("sg-1", 80, ["1.1.1.1/32"])
		self.assertEqual(result, {"opened": ["1.1.1.1/32"], "closed": []})
		self.assertEqual(client.revoked, [])


class TestFrontPorts(unittest.TestCase):
	"""Which ports an inference box opens. Its nginx fronts every engine and both exporters, so
	those stay on loopback — the front port is the only hole the box needs beyond 22."""

	def network(self, groups=("sg-1",)):
		calls = []
		return SimpleNamespace(
			name="NET-1",
			inference_security_group_ids=",".join(groups),
			inference_security_group_id_list=list(groups),
			inference_ingress_cidrs=["1.1.1.1/32"],
			store_security_group_ids="",
			cloud_client=SimpleNamespace(
				sync_ingress=lambda gid, port, cidrs: calls.append((gid, port, tuple(cidrs)))
				or {"opened": [], "closed": []}
			),
			calls=calls,
		)

	def test_both_front_ports_are_reconciled_to_the_same_sources(self):
		# Opened before the box listens, so none is briefly unreachable when nginx moves off 443.
		network = self.network()
		with patch("frappe.msgprint"):
			Network.sync_inference_ingress(network)
		self.assertEqual(
			network.calls,
			[("sg-1", 80, ("1.1.1.1/32",)), ("sg-1", 443, ("1.1.1.1/32",))],
		)

	def test_no_engine_or_exporter_port_is_opened(self):
		# Reached through the box's nginx, never directly: a hole is an unauthenticated engine.
		network = self.network()
		with patch("frappe.msgprint"):
			Network.sync_inference_ingress(network)
		for _group, port, _cidrs in network.calls:
			with self.subTest(port):
				self.assertNotIn(port, (8080, 9100, 9400))


class TestStoreIngressCidrs(unittest.TestCase):
	def test_the_store_is_open_to_its_own_vpc_only(self):
		# Redis wants its password too; the VPC is all Grove's boxes, and no gateway change moves it.
		network = SimpleNamespace(cidr_block="10.3.0.0/16")
		self.assertEqual(Network.store_ingress_cidrs.fget(network), ["10.3.0.0/16"])


class TestTheStorePortIsReconciledToo(unittest.TestCase):
	def test_6379_goes_to_the_vpc_and_nothing_else(self):
		calls = []
		network = SimpleNamespace(
			name="NET-1",
			inference_security_group_ids="",
			store_security_group_ids="sg-store",
			store_security_group_id_list=["sg-store"],
			store_ingress_cidrs=["10.3.0.0/16"],
			cloud_client=SimpleNamespace(
				sync_ingress=lambda gid, port, cidrs: calls.append((gid, port, tuple(cidrs)))
				or {"opened": [], "closed": []}
			),
		)
		with patch("frappe.msgprint"):
			Network.sync_inference_ingress(network)
		self.assertEqual(calls, [("sg-store", 6379, ("10.3.0.0/16",))])


class TestParseSecurityGroupIds(unittest.TestCase):
	def test_splits_on_comma(self):
		self.assertEqual(parse_security_group_ids("sg-abc,sg-def"), ["sg-abc", "sg-def"])

	def test_strips_whitespace(self):
		self.assertEqual(parse_security_group_ids("sg-abc, sg-def , sg-ghi"),
						  ["sg-abc", "sg-def", "sg-ghi"])

	def test_blank_is_empty_list(self):
		self.assertEqual(parse_security_group_ids(""), [])
		self.assertEqual(parse_security_group_ids(None), [])

	def test_drops_empty_entries(self):
		self.assertEqual(parse_security_group_ids("sg-abc,,sg-def,"), ["sg-abc", "sg-def"])


class TestScheduledIngressSync(unittest.TestCase):
	def test_every_network_is_synced_and_one_failing_does_not_stop_the_next(self):
		synced = []

		def sync(name):
			synced.append(name)
			if name == "NET-1":
				raise RuntimeError("AWS said no")

		with (
			patch("frappe.get_all", return_value=["NET-1", "NET-2"]),
			patch("frappe.get_doc", lambda doctype, name: SimpleNamespace(
				sync_inference_ingress=lambda: sync(name)
			)),
			patch("frappe.db.rollback"),
			patch("frappe.log_error") as log_error,
		):
			reconcile.sync_networks()
		self.assertEqual(synced, ["NET-1", "NET-2"])
		log_error.assert_called_once()
