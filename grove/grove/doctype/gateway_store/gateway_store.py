# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import base64
import binascii
import os
from datetime import datetime, timezone

import frappe
from frappe.model.document import Document

from grove import failure
from grove.server import Server

REDIS_PORT = 6379
RDB_MAGIC = b"REDIS"
# The password is read on the box from the role's own requirepass line: never in argv, never in a
# log. Only base64 reaches stdout; redis-cli's progress goes to /dev/null.
DUMP = (
	"REDISCLI_AUTH=\"$(awk '/^requirepass/{print $2}' /etc/redis/redis.conf)\" "
	"redis-cli --rdb - 2>/dev/null | base64 -w0"
)


class GatewayStore(Server, Document):
	"""The one Redis a Network's gateways share. One in-flight counter per replica is what caps a
	standalone box across those gateways; everything else they hold lives here with it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		frappe_public_key: DF.Code | None
		geography: DF.Link | None
		last_backup_at: DF.Datetime | None
		last_backup_key: DF.Data | None
		machine: DF.Link
		network: DF.Link | None
		private_ip: DF.Data | None
		public_ip: DF.Data | None
		redis_password: DF.Password | None
		region: DF.Link | None
		status: DF.Literal["Pending", "Installing", "Active", "Broken", "Terminated"]
	# end: auto-generated types

	def validate(self):
		self.validate_one_per_network()
		self.set_redis_password()

	def validate_one_per_network(self):
		"""Two stores would split a Network's gateways onto two counters — the over-admission a
		store exists to remove."""
		network = frappe.db.get_value("Machine", self.machine, "network")
		if not network:
			frappe.throw(f"Machine {self.machine} is in no Network — a store serves one Network's gateways.")
		if self.status == "Terminated":
			return
		others = [name for name in stores_in(network, status=("!=", "Terminated")) if name != self.name]
		if others:
			frappe.throw(f"Network {network} already has Gateway Store {others[0]}.")

	def set_redis_password(self):
		"""Tested through get_password: a saved Password field reads back as asterisks even when
		the value behind it is gone."""
		if not self.get_password("redis_password", raise_exception=False):
			self.redis_password = frappe.generate_hash(length=48)

	@property
	def archive_blockers(self):
		gateways = frappe.get_all(
			"Gateway Server",
			filters={"gateway_store": self.name, "status": ("!=", "Terminated")},
			pluck="name",
		)
		if not gateways:
			return []
		return [f"Gateways still run on this store: {', '.join(gateways)}."]

	@property
	def listen_ip(self):
		"""The Machine's private address, live: where Redis binds and the gateways dial."""
		private_ip = frappe.db.get_value("Machine", self.machine, "private_ip")
		if not private_ip:
			frappe.throw(f"Machine {self.machine} has no private IP — its gateways would have nothing to dial.")
		return private_ip

	@property
	def redis_variables(self):
		"""What a gateway on this store is given. Carries the password, so resolve it inside a job."""
		return {
			"redis_addr": f"{self.listen_ip}:{REDIS_PORT}",
			"redis_password": self.get_password("redis_password"),
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
		play_name, rc = self.run_playbook(
			"store.yml",
			extravars={
				"redis_bind_ip": self.listen_ip,
				"redis_password": self.get_password("redis_password"),
			},
		)
		frappe.db.set_value(self.doctype, self.name, "status", "Active" if rc == 0 else "Broken")
		return play_name, rc

	def backup(self):
		"""A point-in-time RDB off the live Redis into the weights bucket, and the doc points at it.
		run_command merges stderr and never raises, so the RDB magic is the check: NOAUTH, a dead
		box or a warning-only answer all fail here, loudly. → the object key."""
		from grove.grove.doctype.compile_cache.compile_cache import bucket_and_client

		output = frappe.get_doc("Machine", self.machine).run_command(["sh", "-c", DUMP], timeout=120)
		try:
			data = base64.b64decode(output.split()[-1]) if output else b""
		except binascii.Error:
			data = b""
		if not data.startswith(RDB_MAGIC):
			frappe.throw(f"{self.name}: no RDB came back: {output[-200:]!r}")
		# ponytail: the whole RDB rides base64 through one SSH read; stream to a file past ~100 MB.
		bucket, client = bucket_and_client()
		key = backup_key(self.name)
		client.put_object(Bucket=bucket, Key=key, Body=data)
		frappe.db.set_value(
			self.doctype, self.name, {"last_backup_key": key, "last_backup_at": frappe.utils.now_datetime()}
		)
		return key

	@frappe.whitelist()
	def restore(self, source=None):
		"""Button: replace this store's Redis with `source`'s latest backup — itself, or the
		Terminated store this box replaces. Resolved here, before the insurance backup in the
		worker moves this store's own pointer."""
		source = source or self.name
		key = frappe.db.get_value("Gateway Store", source, "last_backup_key")
		if not key:
			frappe.throw(f"{source} has no backup yet.")
		frappe.enqueue_doc(self.doctype, self.name, "restore_from", source=source, key=key, queue="long", timeout=900)
		frappe.msgprint(f"Restoring {key} onto {self.name} — watch its Ansible Plays.", alert=True)

	@failure.reports_failure()
	def restore_from(self, source, key):
		"""Worker: this store's own snapshot first, then `key` loaded through a side Redis (see
		restore.yml). Live counters a later drain already billed are dropped on the box."""
		from grove.grove.doctype.compile_cache.compile_cache import bucket_and_client

		self.backup()
		bucket, client = bucket_and_client()
		path = os.path.abspath(frappe.get_site_path("private", "gateway-store", f"{self.name}.rdb"))
		os.makedirs(os.path.dirname(path), exist_ok=True)
		client.download_file(bucket, key, path)
		try:
			play_name, rc = self.run_playbook(
				"restore.yml",
				extravars={
					"restore_file": path,
					"redis_password": self.get_password("redis_password"),
					"drop_prefixes": landed_since(source, key),
				},
			)
		finally:
			os.remove(path)
		if rc != 0:
			frappe.throw(f"Restore play {play_name} exited {rc}.")
		return play_name, rc


def stores_in(network, **filters):
	"""The stores whose Machine is in `network`, membership read off the Machine live."""
	boxes = frappe.get_all("Machine", filters={"network": network}, pluck="name")
	if not boxes:
		return []
	return frappe.get_all(
		"Gateway Store", filters={"machine": ("in", boxes), **filters}, pluck="name"
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
	stamp = (at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
	return f"gateway-store/{store}/{stamp}.rdb"


def landed_since(store, key):
	"""The API-key prefixes a drain AFTER the snapshot in `key` landed: their live counters in it
	are billed already, so a restore drops them. Drain ids are minted on the box at the rename in
	the stamp's format, so strings order them; a blank id (a pre-ack gateway) never matches."""
	stamp = key.rsplit("/", 1)[-1].removesuffix(".rdb")
	# A dead line (`dead:<request id>`) was never a live counter, so it names nothing to drop.
	return frappe.get_all(
		"Usage Record",
		filters=[["gateway_store", "=", store], ["drain_id", ">", stamp], ["drain_id", "not like", "dead:%"]],
		pluck="api_key", distinct=True,
	)


def backup_all():
	"""Every 5 minutes: each Active store's RDB to the bucket, one after another. Off until the
	bucket and Mirror keys are set. One store failing does not stop the next."""
	if not frappe.get_single("Grove Settings").weights_s3_write_environment:
		return
	for name in frappe.get_all("Gateway Store", filters={"status": "Active"}, pluck="name"):
		try:
			frappe.get_doc("Gateway Store", name).backup()
			frappe.db.commit()
		except Exception as error:
			frappe.db.rollback()
			frappe.log_error(f"Gateway Store backup failed: {name}")
			failure.report("Gateway Store", name, "Backup failed", str(error))
