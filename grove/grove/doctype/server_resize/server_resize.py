# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import time

import frappe
import requests
from frappe.model.document import Document
from frappe.utils import now_datetime

from grove import failure
from grove.cloud_provider import reconcile
from grove.cloud_provider.base import CloudClientError
from grove.grove.doctype.gateway_store.gateway_store import gateways_on
from grove.runner import Status, StepHandler

# Polls of 5 s: a gateway in maintenance gets this long to finish what it had, and a started box to answer.
DRAIN_ATTEMPTS = 60
ANSWER_ATTEMPTS = 60


class ServerResize(StepHandler, Document):
	"""Moves a Gateway Server or Gateway Store onto another instance type: drain, stop, change the
	type at the provider, start, put the server back — one step per background job. A failed step
	starts the box again on whatever type it has and lifts the maintenance this resize set."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.grove.doctype.server_resize_step.server_resize_step import ServerResizeStep

		ended_at: DF.Datetime | None
		error: DF.Code | None
		from_instance_type: DF.Data | None
		instance_type: DF.Data
		machine: DF.Link | None
		server: DF.DynamicLink
		server_type: DF.Literal["Gateway Server", "Gateway Store"]
		started_at: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Success", "Failure"]
		steps: DF.Table[ServerResizeStep]
	# end: auto-generated types

	@classmethod
	def create(cls, server, instance_type):
		"""A resize of `server`, Pending — its Resize Server button. Start runs it. → the doc name."""
		return (
			frappe.get_doc(
				{
					"doctype": "Server Resize",
					"server_type": server.doctype,
					"server": server.name,
					"instance_type": instance_type,
				}
			)
			.insert()
			.name
		)

	def before_insert(self):
		"""Everything checkable before the box is touched, then the rows."""
		server = self.server_doc
		if server.status == "Terminated":
			frappe.throw(f"{self.server} is Terminated — its box is gone.")
		running = frappe.db.get_value(
			self.doctype, {"server": self.server, "status": ("in", (Status.Pending, Status.Running))}
		)
		if running:
			frappe.throw(f"Server Resize {running} is still running on {self.server}.")
		machine = frappe.get_doc("Machine", server.machine)
		self.check_machine(machine)
		self.check_instance_type(machine, server)
		self.machine, self.from_instance_type = machine.name, machine.instance_type
		self.set("steps", self.get_rows(server))

	def check_machine(self, machine):
		machine.require_instance()
		if machine.status != "Active":
			frappe.throw(f"Machine {machine.name} is {machine.status} — only a running box is resized.")
		if machine.is_bare_metal:
			frappe.throw(f"Machine {machine.name} is bare metal — it cannot change type in place.")
		if self.instance_type == machine.instance_type:
			frappe.throw(f"{machine.name} already is a {machine.instance_type}.")

	def check_instance_type(self, machine, server):
		"""The image is fixed, so the type must keep the architecture; a gateway's memory cap must
		fit the new RAM or memory.yml refuses it after the box has already been resized."""
		info = machine.cloud_client.get_instance_type_info(self.instance_type)
		if info["cpu_architecture"] != machine.cpu_architecture:
			frappe.throw(
				f"{self.instance_type} is {info['cpu_architecture']}, but {machine.name} runs "
				f"{machine.cpu_architecture}: its image is fixed, so the type must keep the architecture."
			)
		if info["is_bare_metal"]:
			frappe.throw(f"{self.instance_type} is bare metal — pick a virtualised type.")
		limit = getattr(server, "memory_limit_mb", 0) or 0
		if limit and limit >= info["memory_mb"]:
			frappe.throw(
				f"{server.name} caps pathway at {limit} MB, but a {self.instance_type} has "
				f"{info['memory_mb']} MB — lower its Memory Limit first."
			)

	def get_rows(self, server):
		"""A gateway drains itself — its rows name no gateway, the doc's `server` is the one. A
		store drains every gateway on it, one row each naming its gateway, since each is its own
		play."""
		machine = [
			self.stop_machine,
			self.wait_for_machine_stopped,
			self.set_instance_type,
			self.start_machine,
			self.sync_server,
		]
		if self.server_type == "Gateway Server":
			return self.get_steps(
				[
					self.enter_maintenance,
					self.wait_for_drain,
					self.stop_pathway,
					*machine,
					self.wait_for_pathway,
					self.apply_memory_cap,
					self.leave_maintenance,
				]
			)
		gateways = gateways_on(server.name)
		return [
			*self.gateway_rows(gateways, self.enter_maintenance),
			*self.gateway_rows(gateways, self.wait_for_drain),
			*self.get_steps([self.stop_redis, *machine, self.check_redis]),
			*self.gateway_rows(gateways, self.leave_maintenance),
		]

	def gateway_rows(self, gateways, method):
		"""A store's rows for `method`, one per gateway, each naming its own."""
		return [
			{**row, "server": gateway, "step_name": f"{row['step_name']} on {gateway}"}
			for gateway in gateways
			for row in self.get_steps([method])
		]

	@frappe.whitelist()
	def start(self):
		"""Button: run the steps, one job each. Pending only — a resize runs once."""
		if self.status != Status.Pending:
			frappe.throw(f"{self.name} is {self.status} — only a Pending resize starts.")
		self.db_set({"status": Status.Running, "started_at": now_datetime()})
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_execute_steps",
			steps=self.steps,
			commit=True,
			queue="long",
			timeout=18000,
			enqueue_after_commit=True,
		)

	@property
	def server_doc(self):
		return frappe.get_doc(self.server_type, self.server)

	@property
	def machine_doc(self):
		return frappe.get_doc("Machine", self.machine)

	def gateway_name(self, step):
		"""The gateway a row acts on: named on a store's per-gateway rows, the resized server itself
		otherwise."""
		return step.server or self.server

	def gateway(self, step):
		return frappe.get_doc("Gateway Server", self.gateway_name(step))

	def play_reference(self):
		return {"reference_doctype": self.doctype, "reference_docname": self.name}

	def enter_maintenance(self, step):
		"""Enter maintenance"""
		gateway = self.gateway(step)
		if gateway.is_in_maintenance:
			# Someone else's: not this resize's to lift.
			step.status = Status.Skipped
		else:
			gateway.db_set("is_in_maintenance", 1)
			step.job_type, step.job = "Ansible Play", gateway.apply_config(**self.play_reference())[0]
			step.status = Status.Success
		step.save()

	def wait_for_drain(self, step):
		"""Wait for drain"""
		try:
			answer = self.gateway(step).get_in_flight()
		except requests.RequestException:
			answer = {}  # a dropped connect: asked again at the next poll
		drained = bool(answer.get("maintenance")) and answer.get("in_flight") == 0
		state = (
			f"maintenance {'on' if answer.get('maintenance') else 'off'}, {answer.get('in_flight')} in flight"
			if answer
			else "no answer"
		)
		self.poll(step, drained, f"{self.gateway_name(step)} to drain ({state})", max_attempts=DRAIN_ATTEMPTS)

	def stop_pathway(self, step):
		"""Stop pathway"""
		self.handle_ansible_play(step, self.server_doc, "stop.yml")

	def stop_redis(self, step):
		"""Stop redis"""
		self.handle_ansible_play(step, self.server_doc, "stop.yml")

	def stop_machine(self, step):
		"""Stop machine"""
		self.machine_doc.stop()
		step.status = Status.Success
		step.save()

	def wait_for_machine_stopped(self, step):
		"""Wait for machine to stop"""
		self.handle_machine_status(step, self.machine, "Stopped")

	def set_instance_type(self, step):
		"""Set instance type"""
		self.machine_doc.set_instance_type(self.instance_type)
		step.status = Status.Success
		step.save()

	def start_machine(self, step):
		"""Start machine"""
		self.machine_doc.resume()
		step.status = Status.Success
		step.save()

	def sync_server(self, step):
		"""Sync server"""
		self.refresh_server()
		step.status = Status.Success
		step.save()

	def refresh_server(self):
		"""The server back to Active on the Machine's addresses: the save refetches them, DNS and
		the health check follow, and a moved public IP goes into the security groups now rather
		than at the next tick."""
		server = self.server_doc
		before = server.public_ip
		server.status = "Active"
		server.save()
		server.sync_dns_records()
		if server.public_ip != before:
			reconcile.sync_networks()

	def wait_for_pathway(self, step):
		"""Wait for pathway"""
		try:
			self.gateway(step).get_in_flight()
			answers = True
		except requests.RequestException:
			answers = False
		self.poll(step, answers, f"{self.gateway_name(step)} to answer", max_attempts=ANSWER_ATTEMPTS)

	def apply_memory_cap(self, step):
		"""Apply memory cap"""
		gateway = self.server_doc
		self.handle_ansible_play(
			step,
			gateway,
			"memory.yml",
			extravars={**gateway.memory_variables, "pathway_memory_restart": True},
		)

	def check_redis(self, step):
		"""Check redis"""
		store = self.server_doc
		self.handle_ansible_play(
			step, store, "ping.yml", extravars={"redis_password": store.get_password("redis_password")}
		)

	def leave_maintenance(self, step):
		"""Leave maintenance"""
		if self.entered(step.server).status == Status.Success:
			step.job_type, step.job = "Ansible Play", self.lift_maintenance(self.gateway_name(step))
			step.status = Status.Success
		else:
			step.status = Status.Skipped
		step.save()

	def entered(self, gateway):
		"""This resize's Enter maintenance row for `gateway` (None on a gateway's own rows)."""
		return next(
			row for row in self.steps if row.method_name == "enter_maintenance" and row.server == gateway
		)

	def lift_maintenance(self, gateway):
		doc = frappe.get_doc("Gateway Server", gateway)
		doc.db_set("is_in_maintenance", 0)
		return doc.apply_config(**self.play_reference())[0]

	def succeed(self, success_status=Status.Success):
		self.ended_at = now_datetime()
		super().succeed(success_status)

	def fail(self, failure_status=Status.Failure):
		self.ended_at = now_datetime()
		super().fail(failure_status)

	def handle_step_failure(self):
		"""The traceback on the doc and in the Error Log, announced unless a failed play already was;
		then the box is brought back and what this resize changed is undone."""
		super().handle_step_failure()
		frappe.log_error(
			title=f"Server Resize {self.name} failed",
			message=self.error,
			reference_doctype=self.doctype,
			reference_name=self.name,
		)
		failed = [row for row in self.steps if row.status == Status.Failure][-1]
		if not getattr(frappe.local, "grove_failure_reported", False):
			failure.report(self.doctype, self.name, f"Resize of {self.server} failed", failed.output)
		# Committed first: recovery can raise, and the rollback must not take the failure record.
		frappe.db.commit()
		self.recover()

	def recover(self):
		"""The box first, then its doc, then the gateways this resize put in maintenance. Each on
		its own: one that cannot be undone is written to `error`, not allowed to stop the next."""
		self.undo("starting the machine", self.restart_machine)
		if frappe.db.get_value("Machine", self.machine, "status") == "Active":
			self.undo("syncing the server", self.refresh_server)
		for row in self.steps:
			# Failure set the flag too, before the play that applies it refused.
			if row.method_name == "enter_maintenance" and row.status in (Status.Success, Status.Failure):
				gateway = self.gateway_name(row)
				self.undo(f"lifting maintenance on {gateway}", self.lift_maintenance_when_up, gateway)
		for row in self.steps:
			if row.status == Status.Pending:
				row.status = Status.Skipped
		self.save()
		frappe.db.commit()

	def lift_maintenance_when_up(self, gateway):
		"""A box recovery resumed answers a minute or so after it reads Active: wait for pathway
		(capped) so the lift's play and its in-flight check land on a running process. One that never
		answers gets the play anyway — the file is what it boots from."""
		doc = frappe.get_doc("Gateway Server", gateway)
		for _ in range(ANSWER_ATTEMPTS):
			try:
				doc.get_in_flight()
				break
			except requests.RequestException:
				time.sleep(5)
		return self.lift_maintenance(gateway)

	def undo(self, what, action, *args):
		try:
			action(*args)
		except Exception as error:
			self.error = f"{self.error or ''}\n\nRecovery, {what}: {error}"

	def restart_machine(self):
		"""Start the box if it was left stopped; resume polls until it answers."""
		machine = self.machine_doc
		try:
			machine.sync()
		except CloudClientError:
			pass
		if machine.status == "Stopped":
			machine.resume()
