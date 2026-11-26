# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import base64
import binascii
from datetime import UTC, datetime

import frappe
from frappe.model.document import Document

from grove import failure
from grove.infrastructure.ansible import task_result
from grove.infrastructure.fleet import NamedHost

REDIS_PORT = 6379
RDB_MAGIC = b"REDIS"
# The password is read on the box from requirepass, so it never lands in argv or a log. A store not
# yet moved onto store-redis (Upgrade Redis) still has only the packaged conf and log directory.
CONFS = "/etc/store-redis/redis.conf /etc/redis/redis.conf"
LOGS = ["/var/log/store-redis/*.log", "/var/log/redis/*.log"]
DUMP = (
	f"REDISCLI_AUTH=\"$(cat {CONFS} 2>/dev/null | awk '/^requirepass/{{print $2; exit}}')\" "
	"redis-cli --rdb - 2>/dev/null | base64 -w0"
)
# The redis role's last task, as an Ansible Task records it.
VERSION_TASK = "redis : read the running redis version"


class GatewayStore(NamedHost, Document):
	"""The one Redis a Network's gateways share. One in-flight counter per replica is what caps a
	standalone box across those gateways; everything else they hold lives here with it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		datum_token_expires_at: DF.Datetime | None
		frappe_public_key: DF.Code | None
		geography: DF.Link | None
		is_vector_setup: DF.Check
		last_backup_at: DF.Datetime | None
		last_backup_key: DF.Data | None
		last_synced_at: DF.Datetime | None
		machine: DF.Link
		network: DF.Link | None
		private_ip: DF.Data | None
		public_ip: DF.Data | None
		redis_password: DF.Password | None
		redis_version: DF.Data | None
		region: DF.Link | None
		ship_host_metrics: DF.Check
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	def validate(self):
		self.set_redis_password()

	def on_update(self):
		if self.has_value_changed("status") and self.status == "Terminated":
			self.remove_dns_records()

	def on_trash(self):
		self.remove_dns_records()

	def set_redis_password(self):
		"""Tested through get_password: a saved Password field reads back as asterisks even when
		the value behind it is gone."""
		if not self.get_password("redis_password", raise_exception=False):
			self.redis_password = frappe.generate_hash(length=48)

	@property
	def archive_blockers(self):
		gateways = gateways_on(self.name, status=("!=", "Terminated"))
		if not gateways:
			return []
		return [f"Gateways still run on this store: {', '.join(gateways)}."]

	@property
	def listen_ip(self):
		"""The Machine's private address, live: where Redis binds and the gateways dial."""
		private_ip = frappe.db.get_value("Machine", self.machine, "private_ip")
		if not private_ip:
			frappe.throw(
				f"Machine {self.machine} has no private IP — its gateways would have nothing to dial."
			)
		return private_ip

	@property
	def redis_variables(self):
		"""What a gateway on this store is given. Carries the password, so resolve it inside a job."""
		return {
			"redis_addr": f"{self.listen_ip}:{REDIS_PORT}",
			"redis_password": self.get_password("redis_password"),
		}

	@property
	def play_variables(self):
		"""What this store's own plays are passed. Carries the password, so resolve it inside a job."""
		return {"redis_bind_ip": self.listen_ip, "redis_password": self.get_password("redis_password")}

	@property
	def vector_variables(self):
		"""redis's log, and the exporter that rides along on the store's own password; vector.yml
		lays it on seeing one."""
		return {
			"vector_log_files": LOGS,
			"vector_unshipped_lines": [],
			"vector_redis_password": self.get_password("redis_password"),
		}

	@frappe.whitelist()
	def setup(self):
		"""Button: install Redis on this store's box."""
		frappe.enqueue_doc(self.doctype, self.name, "provision", queue="long", timeout=1800)
		frappe.msgprint(f"Provisioning {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure(mark_broken=True)
	def provision(self):
		frappe.db.set_value(self.doctype, self.name, "status", "Installing")
		frappe.db.commit()
		play_name, rc = self.run_playbook("store.yml", extravars=self.play_variables)
		frappe.db.set_value(self.doctype, self.name, "status", "Active" if rc == 0 else "Broken")
		if rc == 0:
			self.record_redis_version(play_name)
			# Redis is up whatever Route53 says next: a DNS failure must not roll that back.
			frappe.db.commit()
			self.queue_vector()
			self.sync_dns_records()
		return play_name, rc

	@frappe.whitelist()
	def upgrade_redis(self):
		"""Button: move this store onto the newest Redis the role's `redis_version` allows, as
		store-redis. The store is down while the play swaps it; nothing is stopped when it is
		already both."""
		if self.status != "Active":
			frappe.throw(f"{self.name} is {self.status}: only an Active store's Redis is upgraded.")
		frappe.enqueue_doc(self.doctype, self.name, "run_redis_upgrade", queue="long", timeout=1800)
		frappe.msgprint(f"Upgrading Redis on {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure()
	def run_redis_upgrade(self):
		"""Worker: a snapshot off the box first, where the geography has a bucket, then the play."""
		if frappe.db.get_value("Geography", self.geography, "backup_bucket"):
			self.backup()
		play_name, rc = self.run_playbook("upgrade_redis.yml", extravars=self.play_variables)
		if rc != 0:
			frappe.throw(f"Upgrade play {play_name} exited {rc}.")
		self.record_redis_version(play_name)
		return play_name, rc

	def record_redis_version(self, play_name):
		"""What the box reports after a play, not the role's `~=` setting: stores take patch
		releases on different days."""
		version = task_result(play_name, VERSION_TASK).get("stdout", "").strip()
		frappe.db.set_value(self.doctype, self.name, "redis_version", version)

	def backup(self):
		"""A point-in-time RDB off the live Redis into its geography's backup bucket, and the doc points at it.
		run_command merges stderr and never raises, so the RDB magic is the check: NOAUTH, a dead
		box or a warning-only answer all fail here, loudly. → the object key."""
		output = frappe.get_doc("Machine", self.machine).run_command(["sh", "-c", DUMP], timeout=120)
		try:
			data = base64.b64decode(output.split()[-1]) if output else b""
		except binascii.Error:
			data = b""
		if not data.startswith(RDB_MAGIC):
			frappe.throw(f"{self.name}: no RDB came back: {output[-200:]!r}")
		# ponytail: the whole RDB rides base64 through one SSH read; stream to a file past ~100 MB.
		bucket, client = frappe.get_doc("Geography", self.geography).get_backup_bucket_and_client()
		key = backup_key(self.name)
		client.put_object(Bucket=bucket, Key=key, Body=data)
		frappe.db.set_value(
			self.doctype, self.name, {"last_backup_key": key, "last_backup_at": frappe.utils.now_datetime()}
		)
		return key


def stores_in(network, **filters):
	"""The stores whose Machine is in `network`, membership read off the Machine live."""
	boxes = frappe.get_all("Machine", filters={"network": network}, pluck="name")
	if not boxes:
		return []
	return frappe.get_all("Gateway Store", filters={"machine": ("in", boxes), **filters}, pluck="name")


def gateways_on(store, **filters):
	"""The gateways running on `store` — Active ones unless `filters` says otherwise."""
	return frappe.get_all(
		"Gateway Server",
		filters={"gateway_store": store, "status": "Active", **filters},
		order_by="name asc",
		pluck="name",
	)


def store_writers(store):
	"""The store's Active writers, in the order a sync tries them."""
	return frappe.get_all(
		"Gateway Server",
		filters={"gateway_store": store, "is_store_writer": 1, "status": "Active"},
		order_by="name asc",
		pluck="name",
	)


def backup_key(store, at=None):
	"""Per store, UTC-stamped in the drain-id format, so a listing sorts by time and a drain id
	compares against it as a string."""
	stamp = (at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
	return f"gateway-store/{store}/{stamp}.rdb"


def backup_all():
	"""Every 5 minutes: each Active store's RDB to its geography's bucket, one after another. A
	geography without a backup bucket is skipped. One store failing does not stop the next."""
	geographies = frappe.get_all("Geography", filters={"backup_bucket": ("is", "set")}, pluck="name")
	if not geographies:
		return
	stores = frappe.get_all(
		"Gateway Store", filters={"status": "Active", "geography": ("in", geographies)}, pluck="name"
	)
	for name in stores:
		try:
			frappe.get_doc("Gateway Store", name).backup()
			frappe.db.commit()
		except Exception as error:
			frappe.db.rollback()
			frappe.log_error(f"Gateway Store backup failed: {name}")
			failure.report("Gateway Store", name, "Backup failed", str(error))
