# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe.model.document import Document

from grove.cloud_provider.base import build_cloud_client
from grove.grove.doctype.gateway_store.gateway_store import REDIS_PORT
from grove.monitoring import BOX_HTTP_PORT, BOX_HTTPS_PORT

PROXY_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 80, "to_port": 80, "cidr": "0.0.0.0/0"},
	{"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "0.0.0.0/0"},
]
# 443 is NOT here: it carries the engine proxy, which only the gateway and the metrics agent
# dial, so its sources are computed per box — see sync_inference_ingress.
INFERENCE_BASE_INGRESS_RULES = [
	{"protocol": "tcp", "from_port": 22, "to_port": 22, "cidr": "0.0.0.0/0"},
]

# /16s never collide, so any two Networks can be peered later without an overlap.
CIDR_POOL = ipaddress.ip_network("10.0.0.0/8")
CIDR_PREFIX = 16
# One public subnet per Network, carved off the front of its /16.
SUBNET_PREFIX = 24

# Both reconciled to the same sources. 80 is where the front is going — plain HTTP, reachable
# only from inside the fleet, so the box needs no certificate — and 443 is where it still is.
# 443 comes out once every box has moved.
FRONT_PORTS = (BOX_HTTP_PORT, BOX_HTTPS_PORT)

# A box in this state no longer exists, so its address is not one to keep a hole open for.
GONE_STATUS = "Terminated"


class Network(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		availability_zone: DF.Data | None
		cidr_block: DF.Data | None
		cloud_provider: DF.Link | None
		geography: DF.Link | None
		inference_security_group_ids: DF.Data | None
		internet_gateway_id: DF.Data | None
		machine_image: DF.Data | None
		provider_type: DF.Data | None
		proxy_security_group_ids: DF.Data | None
		region: DF.Link
		route_table_id: DF.Data | None
		store_security_group_ids: DF.Data | None
		subnet_cidr_block: DF.Data | None
		subnet_id: DF.Data | None
		vpc_id: DF.Data | None
	# end: auto-generated types

	def validate(self):
		"""The whole address plan is derived: an operator picks a provider and a region, Grove
		carves the ranges out, and every field below is read-only on the form."""
		if not self.cloud_provider:
			return
		if not self.cidr_block:
			self.cidr_block = self.next_available_cidr_block
		if not self.subnet_cidr_block:
			self.subnet_cidr_block = self.first_subnet_cidr_block

	@property
	def next_available_cidr_block(self):
		"""First /16 in 10.0.0.0/8 not already used by another Network."""
		filters = {"name": ["!=", self.name]} if not self.is_new() else {}
		used = {row for row in frappe.get_all("Network", pluck="cidr_block", filters=filters) if row}
		for block in CIDR_POOL.subnets(new_prefix=CIDR_PREFIX):
			if str(block) not in used:
				return str(block)
		frappe.throw(f"No /16 CIDR block available within {CIDR_POOL}.")

	@property
	def first_subnet_cidr_block(self):
		"""First /24 of the VPC CIDR; the rest of the /16 is left for subnets Grove does not
		create today."""
		try:
			block = ipaddress.ip_network(self.cidr_block)
		except ValueError as e:
			frappe.throw(f"CIDR Block '{self.cidr_block}' on Network {self.name} is not valid: {e}")
		return str(next(block.subnets(new_prefix=SUBNET_PREFIX)))

	@property
	def proxy_security_group_id_list(self):
		"""proxy_security_group_ids as a list, for a Gateway Server box."""
		return parse_security_group_ids(self.proxy_security_group_ids)

	@property
	def inference_security_group_id_list(self):
		"""inference_security_group_ids as a list, for an Inference Server box."""
		return parse_security_group_ids(self.inference_security_group_ids)

	@property
	def store_security_group_id_list(self):
		"""store_security_group_ids as a list, for a Gateway Store box."""
		return parse_security_group_ids(self.store_security_group_ids)

	@property
	def cloud_client(self):
		if not self.cloud_provider:
			frappe.throw(f"Network {self.name} has no Cloud Provider set.")

		provider = frappe.get_doc("Cloud Provider", self.cloud_provider)
		secret = provider.get_password("api_key", raise_exception=False)
		if not (provider.access_key_id and secret):
			frappe.throw(f"Cloud Provider {provider.name} has no credentials set.")
		if not self.region:
			frappe.throw(f"Network {self.name} has no Region set.")

		return build_cloud_client(provider.provider_type, provider.access_key_id, secret, self.region)

	@frappe.whitelist()
	def create_network(self):
		"""Button: create the VPC and public subnet on AWS — an Internet Gateway route and
		auto-assigned public IPs, so a launched Machine is reachable over SSH — then its security
		groups, so one click gets a Network fully ready."""
		if self.vpc_id:
			frappe.throw(f"Network {self.name} already has a VPC ID set.")

		network = self.cloud_client.create_network(
			self.name, self.cidr_block, self.subnet_cidr_block, self.availability_zone
		)
		self.db_set({
			"vpc_id": network["vpc_id"],
			"subnet_id": network["subnet_id"],
			"internet_gateway_id": network["internet_gateway_id"],
			"route_table_id": network["route_table_id"],
			"availability_zone": network["availability_zone"],
		})
		frappe.msgprint(f"VPC and subnet created for {self.name}.", alert=True)
		self.create_security_groups()

	@frappe.whitelist()
	def create_security_groups(self):
		"""Button: create the Proxy and Inference security groups with their fixed ingress rules.
		Skips a role whose field is already set, so re-clicking never creates a duplicate.

		Only ever creates — what those groups allow on 443 is sync_inference_ingress's, not
		fixed at creation."""
		if not self.vpc_id:
			frappe.throw(f"Set a VPC ID on Network {self.name} before creating security groups.")

		if not self.proxy_security_group_ids:
			proxy_sg_id = self.cloud_client.create_security_group(
				f"{self.name}-proxy", "Grove-managed: SSH + gateway (80/443)", self.vpc_id
			)
			self.cloud_client.authorize_ingress(proxy_sg_id, PROXY_INGRESS_RULES)
			self.db_set("proxy_security_group_ids", proxy_sg_id)

		if not self.inference_security_group_ids:
			inference_sg_id = self.cloud_client.create_security_group(
				f"{self.name}-inference", "Grove-managed: SSH + engine proxy (80, 443)", self.vpc_id
			)
			self.cloud_client.authorize_ingress(inference_sg_id, INFERENCE_BASE_INGRESS_RULES)
			self.db_set("inference_security_group_ids", inference_sg_id)

		if not self.store_security_group_ids:
			store_sg_id = self.cloud_client.create_security_group(
				f"{self.name}-store", "Grove-managed: SSH + redis (6379)", self.vpc_id
			)
			# SSH only, like an inference box: 6379 is opened to the VPC below.
			self.cloud_client.authorize_ingress(store_sg_id, INFERENCE_BASE_INGRESS_RULES)
			self.db_set("store_security_group_ids", store_sg_id)

		frappe.msgprint(f"Security groups created for {self.name}.", alert=True)
		# Straight after creation, so a new group is never briefly open to the world.
		self.sync_inference_ingress()

	@frappe.whitelist()
	def sync_inference_ingress(self):
		"""Button + scheduled tick: make the box's front ports reachable from this VPC, this
		geography's gateways and the agents that scrape it, and from nowhere else.

		Both 80 and 443, to the same sources — the box's nginx fronts every engine and both
		exporters either way, so those ports stay on loopback and need no hole of their own.

		RECONCILES rather than adds: a proxy back on a new address leaves its old /32 behind, and
		a pre-existing 0.0.0.0/0 is closed here. Port 22 is deliberately untouched — Ansible
		reaches these boxes from wherever bench runs.

		A store's 6379 is reconciled the same way, to this Network's VPC."""
		if not (self.inference_security_group_ids or self.store_security_group_ids):
			frappe.msgprint(f"Network {self.name} has no inference or gateway store security group.")
			return None
		client = self.cloud_client
		results = {}
		if self.inference_security_group_ids:
			results["inference"] = reconcile_ingress(
				client, self.inference_security_group_id_list, FRONT_PORTS, self.inference_ingress_cidrs
			)
		if self.store_security_group_ids:
			results["gateway_store"] = reconcile_ingress(
				client, self.store_security_group_id_list, (REDIS_PORT,), self.store_ingress_cidrs
			)
		return results

	@property
	def inference_ingress_cidrs(self):
		"""Who may reach an inference box on this Network's front ports, read live: anything in
		this VPC, and from outside it only this geography's gateways and the agents scraping a box here."""
		gateways = _with_machine(frappe.get_all(
			"Gateway Server", filters={"geography": self.geography}, fields=["machine", "public_ip", "status"]
		))
		return [self.cidr_block, *inference_ingress_cidrs(gateways, self.scraping_agents, self.name)]

	@property
	def scraping_agents(self):
		"""The Monitoring Agents named by an inference box in this Network, membership read off the
		Machine live."""
		boxes = frappe.get_all("Machine", filters={"network": self.name}, pluck="name")
		names = boxes and frappe.get_all(
			"Inference Server",
			filters={"machine": ("in", boxes), "monitoring_agent": ("is", "set"), "status": ("!=", GONE_STATUS)},
			pluck="monitoring_agent",
		)
		if not names:
			return []
		return _with_machine(frappe.get_all(
			"Monitoring Agent", filters={"name": ("in", names)}, fields=["machine", "public_ip", "status"]
		))

	@property
	def store_ingress_cidrs(self):
		"""The whole VPC may reach this Network's store on 6379, and nothing outside it: every box
		in it is Grove's, and redis still wants its password."""
		return [self.cidr_block]


def reconcile_ingress(client, group_ids, ports, cidrs):
	"""Make each port on these groups allow exactly `cidrs`, and say what moved."""
	changes = [client.sync_ingress(group_id, port, cidrs) for group_id in group_ids for port in ports]
	opened = sorted({cidr for change in changes for cidr in change["opened"]})
	closed = sorted({cidr for change in changes for cidr in change["closed"]})
	frappe.msgprint(
		f"Ports {', '.join(str(port) for port in ports)} now allow {', '.join(cidrs) or 'nothing'}."
		+ (f"<br>Opened: {', '.join(opened)}." if opened else "")
		+ (f"<br>Closed: {', '.join(closed)}." if closed else "")
	)
	return {"allowed": cidrs, "opened": opened, "closed": closed}


def _with_machine(rows):
	"""Each row's Machine joined on, read live rather than mirrored onto the server doc, where the
	copy is only as fresh as that doc's last save."""
	machines = {
		machine["name"]: machine
		for machine in frappe.get_all(
			"Machine",
			filters={"name": ("in", [row["machine"] for row in rows if row.get("machine")])},
			fields=["name", "network", "public_ip", "private_ip"],
		)
	} if rows else {}
	return [{**row, **machines.get(row.get("machine"), {})} for row in rows]


def inference_ingress_cidrs(gateways, agents, network):
	"""Every address outside `network`'s VPC allowed to reach an inference box in it on its front
	ports, as /32s. The control plane is not a caller — it reaches a box over SSH and a server at
	its own admin URL.

	Each contributes the address the box will actually SEE it arrive from. A gateway dials the
	box's public IP, so even same-VPC traffic leaves through the internet gateway and arrives from
	the public side: every gateway is listed, by its public address. An agent in this Network dials
	privately and the VPC range already covers it, so only one outside it is listed.

	A Terminated box is gone, and its address belongs to whoever AWS hands it to next."""
	live = lambda rows: [row for row in rows if row.get("status") != GONE_STATUS]  # noqa: E731
	addresses = [gateway.get("public_ip") for gateway in live(gateways)]
	addresses += [agent.get("public_ip") for agent in live(agents) if agent.get("network") != network]
	return sorted({f"{address}/32" for address in addresses if address})


def parse_security_group_ids(raw):
	"""A comma-separated security_group_ids field into a list, blanks and whitespace stripped."""
	return [group.strip() for group in (raw or "").split(",") if group.strip()]
