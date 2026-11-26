# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""What a gateway is told about its store, and the store's backups. Pure: frappe's data calls are
stubbed, so no site."""

import base64
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove import failure
from grove.infrastructure.doctype.gateway_server.gateway_server import GatewayServer
from grove.infrastructure.doctype.gateway_store import gateway_store
from grove.infrastructure.doctype.gateway_store.gateway_store import (
	GatewayStore,
	backup_all,
	backup_key,
)


class Refused(Exception):
	pass


def stub_db(machines):
	"""frappe.db off {machine: {field: value}}."""
	return SimpleNamespace(get_value=lambda doctype, name, field: machines.get(name, {}).get(field))


def stub_get_all(stores):
	"""frappe.get_all off {network: [(store, status)]}, the Machine named after its store."""

	def get_all(doctype, filters=None, pluck=None, **kwargs):
		if doctype == "Machine":
			return [store for store, _ in stores.get(filters["network"], [])]
		wanted = filters["status"]
		matches = lambda status: status != wanted[1] if isinstance(wanted, tuple) else status == wanted  # noqa: E731
		return [
			store
			for pairs in stores.values()
			for store, status in pairs
			if store in filters["machine"][1] and matches(status)
		]

	return get_all


class TestWhatAGatewayIsGiven(unittest.TestCase):
	def test_a_store_is_dialled_at_its_private_address_with_its_password(self):
		store = SimpleNamespace(listen_ip="10.0.61.9", get_password=lambda field: "pw")
		self.assertEqual(
			GatewayStore.redis_variables.fget(store),
			{"redis_addr": "10.0.61.9:6379", "redis_password": "pw"},
		)

	def test_the_address_is_the_machines_read_live(self):
		with patch.object(frappe, "db", stub_db({"store1": {"private_ip": "10.0.61.9"}})):
			self.assertEqual(GatewayStore.listen_ip.fget(SimpleNamespace(machine="store1")), "10.0.61.9")

	def test_a_machine_with_no_private_address_is_refused(self):
		store = SimpleNamespace(machine="store1")
		with (
			patch.object(frappe, "db", stub_db({"store1": {}})),
			patch.object(frappe, "throw", side_effect=Refused),
			self.assertRaises(Refused),
		):
			GatewayStore.listen_ip.fget(store)

	def network_store(self, stores, network="Mumbai"):
		with (
			patch.object(frappe, "db", stub_db({"gw1": {"network": network}})),
			patch.object(frappe, "get_all", side_effect=stub_get_all(stores)),
			patch.object(frappe, "throw", side_effect=Refused),
		):
			return GatewayServer.network_store.fget(SimpleNamespace(name="gw1", machine="gw1"))

	def test_a_gateway_takes_its_networks_active_store(self):
		self.assertEqual(self.network_store({"Mumbai": [("store1", "Active")]}), "store1")

	def test_a_store_not_yet_active_refuses_the_gateway(self):
		with self.assertRaises(Refused):
			self.network_store({"Mumbai": [("store1", "Installing")]})

	def test_a_gateway_in_no_network_is_refused(self):
		with self.assertRaises(Refused):
			self.network_store({"Mumbai": [("store1", "Active")]}, network=None)


RDB = b"REDIS0010\xfa\tredis-ver\x067.0.15\xff"


class FakeS3:
	"""Records puts; a download writes the bytes it was given."""

	def __init__(self, body=RDB):
		self.puts, self.body = [], body

	def put_object(self, Bucket, Key, Body):
		self.puts.append((Bucket, Key, Body))

	def download_file(self, bucket, key, path):
		with open(path, "wb") as f:
			f.write(self.body)


def bucket_of(s3):
	"""A Geography stub whose backup bucket is `b` on `s3`."""
	return SimpleNamespace(get_backup_bucket_and_client=lambda: ("b", s3))


class TestBackup(unittest.TestCase):
	"""One RDB per store every 5 minutes into the weights bucket, the doc pointing at the newest."""

	def test_a_backup_key_is_per_store_and_stamped_like_a_drain_id(self):
		at = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
		self.assertEqual(backup_key("store1", at), "gateway-store/store1/20260928T030000Z.rdb")

	def backup(self, answer):
		s3, written, argv = FakeS3(), {}, []
		machine = SimpleNamespace(run_command=lambda command, timeout: argv.append(command) or answer)
		store = SimpleNamespace(name="store1", doctype="Gateway Store", machine="m1", geography="in")
		docs = {("Machine", "m1"): machine, ("Geography", "in"): bucket_of(s3)}
		with (
			patch.object(frappe, "get_doc", side_effect=lambda dt, name: docs[(dt, name)]),
			patch.object(
				frappe, "db", SimpleNamespace(set_value=lambda dt, dn, values: written.update(values))
			),
			patch.object(frappe, "throw", side_effect=Refused),
			patch.object(frappe.utils, "now_datetime", lambda: datetime(2026, 9, 28, 3, 0)),
		):
			key = GatewayStore.backup(store)
		return key, s3, written, argv

	def test_a_backup_uploads_what_the_box_returned_and_points_the_doc_at_it(self):
		# The doc carries no get_password: the box reads its own conf, grove never puts it in argv.
		key, s3, written, argv = self.backup(
			"Warning: Permanently added 'x' to known hosts.\n" + base64.b64encode(RDB).decode()
		)
		self.assertEqual(s3.puts, [("b", key, RDB)])
		self.assertTrue(key.startswith("gateway-store/store1/"))
		self.assertEqual(written["last_backup_key"], key)
		self.assertIn("last_backup_at", written)
		self.assertEqual(argv[0][:2], ["sh", "-c"])

	def test_a_backup_refuses_bytes_without_the_rdb_magic(self):
		for answer in ("NOAUTH Authentication required.", "", "not base64!"):
			with self.assertRaises(Refused):
				self.backup(answer)

	def backup_all(self, geographies, stores, failing=()):
		backed, reported = [], []
		docs = {
			name: SimpleNamespace(
				backup=(lambda n=name: (_ for _ in ()).throw(RuntimeError(n)))
				if name in failing
				else (lambda n=name: backed.append(n))
			)
			for name in stores
		}

		def get_all(doctype, filters, pluck):
			if doctype == "Geography":
				self.assertEqual(filters, {"backup_bucket": ("is", "set")})
				return geographies
			self.assertEqual(filters, {"status": "Active", "geography": ("in", geographies)})
			return stores

		with (
			patch.object(frappe, "get_all", get_all),
			patch.object(frappe, "get_doc", side_effect=lambda dt, name: docs[name]),
			patch.object(frappe, "db", SimpleNamespace(commit=lambda: None, rollback=lambda: None)),
			patch.object(frappe, "log_error", lambda *a, **k: None),
			patch.object(failure, "report", lambda *a, **k: reported.append(a[1])),
		):
			backup_all()
		return backed, reported

	def test_backup_all_is_off_until_a_geography_has_a_backup_bucket(self):
		self.assertEqual(self.backup_all([], ["store1"]), ([], []))

	def test_backup_all_survives_one_failing_store(self):
		backed, reported = self.backup_all(["in"], ["store1", "store2"], failing={"store1"})
		self.assertEqual((backed, reported), (["store2"], ["store1"]))


class TestUpgradeRedis(unittest.TestCase):
	"""An Active store is moved onto the newest Redis the role allows; the doc keeps what the box
	then reports."""

	def test_a_stores_plays_get_its_bind_address_and_password(self):
		store = SimpleNamespace(listen_ip="10.0.61.9", get_password=lambda field: "pw")
		self.assertEqual(
			GatewayStore.play_variables.fget(store), {"redis_bind_ip": "10.0.61.9", "redis_password": "pw"}
		)

	def test_the_button_takes_a_post_only(self):
		"""It stops the store's Redis: a link, which is a GET and skips the CSRF check, must not."""
		methods = frappe.allowed_http_methods_for_whitelisted_func[GatewayStore.upgrade_redis]
		self.assertEqual(list(methods), ["POST"])

	def test_only_an_active_store_is_upgraded(self):
		store = SimpleNamespace(name="store1", doctype="Gateway Store", status="Broken")
		with patch.object(frappe, "throw", side_effect=Refused), self.assertRaises(Refused):
			GatewayStore.upgrade_redis(store)

	def upgrade(self, rc=0, bucket="b"):
		calls, written = [], {}
		store = SimpleNamespace(
			name="store1",
			doctype="Gateway Store",
			geography="in",
			play_variables={"redis_bind_ip": "10.0.61.9", "redis_password": "pw"},
			backup=lambda: calls.append("backup"),
			run_playbook=lambda playbook, extravars: calls.append((playbook, extravars)) or ("play-1", rc),
		)
		store.record_redis_version = lambda play: GatewayStore.record_redis_version(store, play)
		db = SimpleNamespace(
			get_value=lambda doctype, name, field: bucket,
			set_value=lambda doctype, name, field, value: written.update({field: value}),
		)
		results = {("play-1", gateway_store.VERSION_TASK): {"stdout": "8.12.2\n"}}
		with (
			patch.object(frappe, "db", db),
			patch.object(gateway_store, "task_result", lambda play, task: results[(play, task)]),
			patch.object(frappe, "local", SimpleNamespace(grove_failure_reported=True)),
			patch.object(frappe, "throw", side_effect=Refused),
		):
			try:
				GatewayStore.run_redis_upgrade(store)
			except Refused:
				calls.append("refused")
		return calls, written

	def test_an_upgrade_snapshots_first_and_records_what_the_box_runs(self):
		calls, written = self.upgrade()
		play = ("upgrade_redis.yml", {"redis_bind_ip": "10.0.61.9", "redis_password": "pw"})
		self.assertEqual(calls, ["backup", play])
		self.assertEqual(written, {"redis_version": "8.12.2"})

	def test_a_geography_with_no_bucket_upgrades_without_a_snapshot(self):
		calls, _ = self.upgrade(bucket=None)
		self.assertEqual([call[0] for call in calls], ["upgrade_redis.yml"])

	def test_a_failed_play_throws_and_records_nothing(self):
		calls, written = self.upgrade(rc=2)
		self.assertEqual(calls[-1], "refused")
		self.assertEqual(written, {})


if __name__ == "__main__":
	unittest.main()


class TestDnsRecord(unittest.TestCase):
	"""A store's own name is what operators SSH to: written on a good Setup, removed on Terminated."""

	def provision(self, rc):
		synced, self.vector_queued, self.version_read_off = [], [], []
		store = SimpleNamespace(
			name="store1",
			doctype="Gateway Store",
			play_variables={"redis_bind_ip": "10.0.0.5", "redis_password": "pw"},
			run_playbook=lambda playbook, **kwargs: ("play-1", rc),
			record_redis_version=self.version_read_off.append,
			sync_dns_records=lambda: synced.append(True),
			queue_vector=lambda: self.vector_queued.append(True),
		)
		with patch.object(frappe, "db", SimpleNamespace(set_value=lambda *a: None, commit=lambda: None)):
			GatewayStore.provision.__wrapped__(store)
		return bool(synced)

	def test_a_good_setup_writes_the_record_and_a_failed_one_does_not(self):
		self.assertTrue(self.provision(0))
		self.assertFalse(self.provision(2))

	def test_a_good_setup_chains_vector_and_a_failed_one_does_not(self):
		self.provision(0)
		self.assertEqual([True], self.vector_queued)
		self.provision(2)
		self.assertEqual([], self.vector_queued)

	def test_a_good_setup_records_the_redis_its_play_installed(self):
		self.provision(2)
		self.assertEqual(self.version_read_off, [])
		self.provision(0)
		self.assertEqual(self.version_read_off, ["play-1"])

	def test_terminating_removes_the_record(self):
		removed = []
		for status, changed in (("Terminated", True), ("Active", True), ("Terminated", False)):
			store = SimpleNamespace(
				status=status,
				has_value_changed=lambda field, changed=changed: changed,
				remove_dns_records=lambda: removed.append(status),
			)
			GatewayStore.on_update(store)
		self.assertEqual(removed, ["Terminated"])
