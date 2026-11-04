# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove import failure
from grove.fleet import PathwayHost, gateway_agent_release


class IngressServer(PathwayHost, Document):
	"""One VPC's front door: the gateways dial it by name over a verified certificate, and it dials
	the replicas in its own Network privately. It holds no tenant state — no keys, users, groups,
	usage or catalog — which is the security payoff of the split, and why this is a doctype of its
	own rather than a Gateway Server with a role flag."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_token: DF.Password | None
		admin_url: DF.Data | None
		agent_version: DF.Data | None
		data_token: DF.Password | None
		datum_token_expires_at: DF.Datetime | None
		frappe_public_key: DF.Code | None
		geography: DF.Link | None
		is_in_maintenance: DF.Check
		is_vector_setup: DF.Check
		last_synced_at: DF.Datetime | None
		machine: DF.Link
		memory_limit_mb: DF.Int
		network: DF.Link
		private_ip: DF.Data | None
		public_ip: DF.Data | None
		region: DF.Link | None
		ship_host_metrics: DF.Check
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	def validate(self):
		self.set_admin_url()
		self.set_secret("admin_token")
		self.set_secret("data_token")

	@property
	def archive_blockers(self):
		"""A box routed through this ingress would lose the private path its gateways dial."""
		boxes = frappe.get_all(
			"Inference Server",
			filters={"ingress": self.name, "status": ("!=", "Terminated")},
			pluck="name",
		)
		if not boxes:
			return []
		return [
			f"Inference Servers still routed through this ingress: {', '.join(boxes)}. "
			"Point them at another ingress, or clear the field, first."
		]

	@frappe.whitelist()
	def sync_replicas(self):
		"""Button: push this ingress's replica table now — every Active replica it owns, dialled
		privately.

		Through full_sync rather than straight at the agent, so the push lands on a Pathway Sync
		doc like every other. A button that reports "queued" and then leaves no record of whether
		it worked is the one you end up debugging by ssh."""
		frappe.enqueue(
			"grove.pathway.projection.full_sync",
			queue="short",
			proxies=[],
			ingresses=[self.name],
			trigger="Manual",
		)
		frappe.msgprint(f"Replica table queued for {self.name} — watch its Pathway Sync.", alert=True)

	@failure.reports_failure(mark_broken=True)
	def provision(self):
		"""Run ingress.yml against this box's Machine. On success, mark Active and put it in DNS —
		nothing resolves either of its names until that runs."""
		frappe.db.set_value("Ingress Server", self.name, "status", "Installing")
		frappe.db.commit()

		play_name, rc = self.run_playbook("ingress.yml", extravars=self.provision_variables)

		self.set_admin_url()
		frappe.db.set_value(
			"Ingress Server",
			self.name,
			{"status": "Active" if rc == 0 else "Broken", "admin_url": self.admin_url},
		)
		self.record_agent_version(rc)
		frappe.db.commit()

		if rc == 0:
			self.queue_vector()
			self.sync_dns_records()
		return play_name, rc

	@property
	def provision_variables(self):
		"""Everything ingress.yml needs. No tenant variables of any kind pass through here, and
		that absence is the point — see test_ingress_server."""
		return {
			"admin_token": self.get_password("admin_token"),
			"data_token": self.get_password("data_token"),
			**gateway_agent_release(),
			"ingress_id": self.short_name,
			"ingress_hostname": self.hostname,
			**self.tls_variables,
			**self.config_variables,
			**self.memory_variables,
		}

	@failure.reports_failure(mark_broken=False)
	def _deploy_agent(self, **play):
		"""Install the pinned release and rewrite both halves of its configuration: the provision
		variables less the fleet key (deploy_tls writes it; agent.env names only its path) and the
		memory cap (memory.yml's own). `play` names the doc the play is run for, e.g. a Pathway Update."""
		extravars = self.provision_variables
		for key in ("fleet_tls_key", *self.memory_variables):
			extravars.pop(key)
		play_name, rc = self.run_playbook("deploy_agent.yml", extravars=extravars, **play)
		self.record_agent_version(rc)
		return play_name, rc
