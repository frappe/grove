# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""One store per Network, and what a gateway is told about it. Pure: frappe's data calls are
stubbed, so no site."""

import base64
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from grove import failure
from grove.grove.doctype.compile_cache import compile_cache
from grove.grove.doctype.gateway_server.gateway_server import GatewayServer
from grove.grove.doctype.gateway_store import gateway_store
from grove.grove.doctype.gateway_store.gateway_store import GatewayStore, backup_all, backup_key, landed_since


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
		wanted = filters["status"]  # "Active", or ("!=", "Terminated")
		matches = lambda status: status != wanted[1] if isinstance(wanted, tuple) else status == wanted  # noqa: E731
		return [
			store for pairs in stores.values() for store, status in pairs
			if store in filters["machine"][1] and matches(status)
		]

	return get_all


class TestOneStorePerNetwork(unittest.TestCase):
	def validate(self, name, stores, status="Pending", network="Mumbai"):
		doc = SimpleNamespace(name=name, machine=name, status=status)
		with (
			patch.object(frappe, "db", stub_db({name: {"network": network}})),
			patch.object(frappe, "get_all", side_effect=stub_get_all(stores)),
			patch.object(frappe, "throw", side_effect=Refused),
		):
			GatewayStore.validate_one_per_network(doc)

	def test_the_first_store_of_a_network_is_accepted(self):
		self.validate("store1", {"Mumbai": [("store1", "Pending")]})

	def test_a_second_live_store_is_refused(self):
		# Two stores split the gateways onto two counters, which is the over-admission a store removes.
		with self.assertRaises(Refused):
			self.validate("store2", {"Mumbai": [("store1", "Active"), ("store2", "Pending")]})

	def test_a_terminated_store_leaves_room_for_its_replacement(self):
		self.validate("store2", {"Mumbai": [("store1", "Terminated"), ("store2", "Pending")]})

	def test_a_box_in_no_network_serves_no_gateways(self):
		with self.assertRaises(Refused):
			self.validate("store1", {}, network=None)


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
		# Setup has not finished: there is no Redis there to run on.
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


class TestBackup(unittest.TestCase):
	"""One RDB per store every 5 minutes into the weights bucket, the doc pointing at the newest."""

	def test_a_backup_key_is_per_store_and_stamped_like_a_drain_id(self):
		at = datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc)
		self.assertEqual(backup_key("store1", at), "gateway-store/store1/20260928T030000Z.rdb")

	def backup(self, answer):
		s3, written, argv = FakeS3(), {}, []
		machine = SimpleNamespace(run_command=lambda command, timeout: argv.append(command) or answer)
		store = SimpleNamespace(name="store1", doctype="Gateway Store", machine="m1")
		with (
			patch.object(frappe, "get_doc", return_value=machine),
			patch.object(frappe, "db", SimpleNamespace(set_value=lambda dt, dn, values: written.update(values))),
			patch.object(frappe, "throw", side_effect=Refused),
			patch.object(frappe.utils, "now_datetime", lambda: datetime(2026, 9, 28, 3, 0)),
			patch.object(compile_cache, "bucket_and_client", return_value=("b", s3)),
		):
			key = GatewayStore.backup(store)
		return key, s3, written, argv

	def test_a_backup_uploads_what_the_box_returned_and_points_the_doc_at_it(self):
		# The doc carries no get_password: the box reads its own conf, grove never puts it in argv.
		key, s3, written, argv = self.backup("Warning: Permanently added 'x' to known hosts.\n" + base64.b64encode(RDB).decode())
		self.assertEqual(s3.puts, [("b", key, RDB)])
		self.assertTrue(key.startswith("gateway-store/store1/"))
		self.assertEqual(written["last_backup_key"], key)
		self.assertIn("last_backup_at", written)
		self.assertEqual(argv[0][:2], ["sh", "-c"])

	def test_a_backup_refuses_bytes_without_the_rdb_magic(self):
		# NOAUTH, a dead box or an empty answer: nothing is uploaded and nothing is recorded.
		for answer in ("NOAUTH Authentication required.", "", "not base64!"):
			with self.assertRaises(Refused):
				self.backup(answer)

	def backup_all(self, env, stores, failing=()):
		backed, reported = [], []
		docs = {name: SimpleNamespace(backup=(lambda n=name: (_ for _ in ()).throw(RuntimeError(n))) if name in failing else (lambda n=name: backed.append(n))) for name in stores}
		with (
			patch.object(frappe, "get_single", return_value=SimpleNamespace(weights_s3_write_environment=env)),
			patch.object(frappe, "get_all", return_value=stores),
			patch.object(frappe, "get_doc", side_effect=lambda dt, name: docs[name]),
			patch.object(frappe, "db", SimpleNamespace(commit=lambda: None, rollback=lambda: None)),
			patch.object(frappe, "log_error", lambda *a, **k: None),
			patch.object(failure, "report", lambda *a, **k: reported.append(a[1])),
		):
			backup_all()
		return backed, reported

	def test_backup_all_is_off_until_the_bucket_is_configured(self):
		self.assertEqual(self.backup_all({}, ["store1"]), ([], []))

	def test_backup_all_survives_one_failing_store(self):
		backed, reported = self.backup_all({"AWS_ACCESS_KEY_ID": "k"}, ["store1", "store2"], failing={"store1"})
		self.assertEqual((backed, reported), (["store2"], ["store1"]))


class TestRestore(unittest.TestCase):
	"""A store's latest backup loaded onto a box; counters a later drain billed are dropped."""

	def test_landed_since_names_the_prefixes_a_later_drain_billed(self):
		calls = []
		with patch.object(frappe, "get_all", side_effect=lambda *a, **k: calls.append((a, k)) or ["k1"]):
			self.assertEqual(landed_since("store1", "gateway-store/store1/20260928T100000Z.rdb"), ["k1"])
		self.assertEqual(
			calls[0][1]["filters"],
			[["gateway_store", "=", "store1"], ["drain_id", ">", "20260928T100000Z"], ["drain_id", "not like", "dead:%"]],
		)

	def test_a_drain_id_orders_against_the_stamp_as_a_string(self):
		# Same format on both sides, so the SQL comparison is plain; a blank id never matches.
		stamp = "20260928T100000Z"
		self.assertGreater("20260928T100001Z-1a2b3c4d", stamp)
		self.assertLess("20260928T095959Z-1a2b3c4d", stamp)
		self.assertLess("", stamp)

	def test_restore_refuses_a_source_with_no_backup(self):
		store = SimpleNamespace(name="store2", doctype="Gateway Store")
		with (
			patch.object(frappe, "db", SimpleNamespace(get_value=lambda *a: None)),
			patch.object(frappe, "throw", side_effect=Refused),
			self.assertRaises(Refused),
		):
			GatewayStore.restore(store, source="store1")

	def test_restore_from_ships_the_landed_prefixes_and_fails_on_a_bad_play(self):
		plays, site = [], tempfile.mkdtemp()
		self.addCleanup(lambda: __import__("shutil").rmtree(site, True))
		store = SimpleNamespace(
			name="store2", doctype="Gateway Store", get_password=lambda field: "pw", backup=lambda: "insurance",
			run_playbook=lambda playbook, **kwargs: plays.append((playbook, kwargs, os.path.exists(kwargs["extravars"]["restore_file"]))) or ("play-1", 2),
		)
		with (
			patch.object(compile_cache, "bucket_and_client", return_value=("b", FakeS3())),
			patch.object(frappe, "get_site_path", lambda *parts: os.path.join(site, *parts)),
			patch.object(gateway_store, "landed_since", lambda store, key: ["k1"]),
			patch.object(frappe, "local", SimpleNamespace(grove_failure_reported=True)),
			patch.object(frappe, "throw", side_effect=Refused),
			self.assertRaises(Refused),
		):
			GatewayStore.restore_from(store, "store1", "gateway-store/store1/20260928T100000Z.rdb")
		playbook, kwargs, file_was_there = plays[0]
		self.assertEqual(playbook, "restore.yml")
		self.assertEqual(kwargs["extravars"]["drop_prefixes"], ["k1"])
		self.assertEqual(kwargs["extravars"]["redis_password"], "pw")
		self.assertTrue(file_was_there)
		self.assertFalse(os.path.exists(kwargs["extravars"]["restore_file"]))


if __name__ == "__main__":
	unittest.main()


class TestDnsRecord(unittest.TestCase):
	"""A store's own name is what operators SSH to: written on a good Setup, removed on Terminated."""

	def provision(self, rc):
		synced = []
		store = SimpleNamespace(
			name="store1", doctype="Gateway Store", listen_ip="10.0.0.5", get_password=lambda field: "pw",
			run_playbook=lambda playbook, **kwargs: ("play-1", rc), sync_dns_records=lambda: synced.append(True),
		)
		with patch.object(frappe, "db", SimpleNamespace(set_value=lambda *a: None, commit=lambda: None)):
			GatewayStore.provision.__wrapped__(store)
		return bool(synced)

	def test_a_good_setup_writes_the_record_and_a_failed_one_does_not(self):
		self.assertTrue(self.provision(0))
		self.assertFalse(self.provision(2))

	def test_terminating_removes_the_record(self):
		removed = []
		for status, changed in (("Terminated", True), ("Active", True), ("Terminated", False)):
			store = SimpleNamespace(
				status=status, has_value_changed=lambda field, changed=changed: changed,
				remove_dns_records=lambda: removed.append(status),
			)
			GatewayStore.on_update(store)
		self.assertEqual(removed, ["Terminated"])
