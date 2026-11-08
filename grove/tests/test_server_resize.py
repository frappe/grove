# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Server Resize: what it refuses, the rows it builds for a gateway and for a store, the steps that
wait, and what a failure undoes. Pure: frappe is patched and servers, machines and rows are fakes."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import requests

from grove import runner
from grove.fleet import NamedHost
from grove.grove.doctype.server_resize import server_resize as module
from grove.grove.doctype.server_resize.server_resize import ServerResize
from grove.runner import Status


class FakeRow(SimpleNamespace):
	def save(self):
		pass


def row(method_name, server=None, status=Status.Pending, **fields):
	return FakeRow(method_name=method_name, server=server, status=status, **{"attempt": None, **fields})


def resize(**fields):
	doc = ServerResize.__new__(ServerResize)
	doc.__dict__.update(
		doctype="Server Resize",
		name="r1",
		server_type="Gateway Server",
		server="gw1",
		machine="gw1",
		instance_type="m7g.xlarge",
		from_instance_type=None,
		status=Status.Pending,
		steps=[],
		error=None,
	)
	doc.__dict__.update(fields)
	doc.set = lambda field, rows: setattr(doc, field, [FakeRow(**{"server": None, **r}) for r in rows])
	doc.save = Mock()
	return doc


def machine(**fields):
	info = {"cpu_architecture": "arm64", "is_bare_metal": False, "memory_mb": 16384}
	doc = SimpleNamespace(
		name="gw1",
		instance_id="i-1",
		status="Active",
		is_bare_metal=0,
		instance_type="m7g.large",
		cpu_architecture="arm64",
		cloud_client=SimpleNamespace(get_instance_type_info=lambda t: info),
		require_instance=lambda: None,
		stop=Mock(),
		resume=Mock(),
		set_instance_type=Mock(),
		sync=Mock(),
	)
	doc.__dict__.update(fields)
	return doc


def gateway(**fields):
	doc = SimpleNamespace(
		doctype="Gateway Server",
		name="gw1",
		status="Active",
		machine="gw1",
		memory_limit_mb=0,
		is_in_maintenance=0,
		public_ip="203.0.113.7",
		written={},
		apply_config=Mock(return_value=("PLAY-1", 0)),
		get_in_flight=Mock(return_value={"maintenance": True, "in_flight": 0}),
		sync_dns_records=Mock(),
		save=Mock(),
	)
	doc.db_set = lambda field, value, **kw: doc.written.update({field: value})
	doc.__dict__.update(fields)
	return doc


class FakeCache(dict):
	def get_value(self, key, **kwargs):
		return self.get(key)

	def set_value(self, key, value, **kwargs):
		self[key] = value
		self.ttl = kwargs.get("expires_in_sec")


def refused():
	return patch.object(frappe, "throw", side_effect=frappe.ValidationError)


class TestResizeOptions(unittest.TestCase):
	def box(self, calls):
		types = [
			{"instance_type": "m7g.large", "gpus": [], "vcpus": 2, "memory_mb": 8192},
			{"instance_type": "g5g.xlarge", "gpus": [{"gpu_model": "T4g"}], "vcpus": 4, "memory_mb": 8192},
			{"instance_type": "m7g.xlarge", "gpus": [], "vcpus": 4, "memory_mb": 16384},
		]
		return machine(
			cloud_provider="AWS",
			region_code="ap-south-1",
			network_doc=SimpleNamespace(availability_zone="ap-south-1a"),
			cloud_client=SimpleNamespace(
				list_instance_types=lambda arch, zone: calls.append((arch, zone)) or types,
				get_instance_type_info=lambda t: {"instance_type": t, "vcpus": 2, "memory_mb": 8192},
			),
		)

	def options(self, cache, box):
		with patch.object(frappe, "get_doc", return_value=box), patch.object(frappe, "cache", cache):
			return NamedHost.resize_options(SimpleNamespace(machine="gw1"))

	def test_the_list_is_read_once_per_six_hours_the_current_type_live(self):
		calls, cache = [], FakeCache()
		box = self.box(calls)
		self.options(cache, box)
		result = self.options(cache, box)
		self.assertEqual(calls, [("arm64", "ap-south-1a")])
		self.assertEqual(cache.ttl, 6 * 3600)
		self.assertEqual(result["current"]["instance_type"], "m7g.large")
		self.assertEqual(result["current"]["size"], "2 vCPU · 8 GiB")

	def test_gpu_types_and_the_current_type_are_not_offered(self):
		result = self.options(FakeCache(), self.box([]))
		self.assertEqual(
			[(t["instance_type"], t["size"]) for t in result["options"]], [("m7g.xlarge", "4 vCPU · 16 GiB")]
		)


class TestStart(unittest.TestCase):
	def start(self, doc):
		doc.db_set = lambda values, **kw: doc.__dict__.update(values)
		with (
			patch.object(frappe, "enqueue_doc") as enqueue,
			patch.object(module, "now_datetime", return_value="2026-11-08 12:00:00"),
		):
			ServerResize.start(doc)
		return enqueue

	def test_start_runs_the_steps_one_job_each(self):
		doc = resize(steps=[row("stop_machine")])
		enqueue = self.start(doc)
		self.assertEqual(doc.status, Status.Running)
		self.assertTrue(doc.started_at)
		self.assertEqual(enqueue.call_args.args, ("Server Resize", "r1", "_execute_steps"))
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_only_a_pending_resize_starts(self):
		for status in (Status.Running, Status.Success, Status.Failure):
			with refused(), self.assertRaises(frappe.ValidationError):
				self.start(resize(status=status))


class TestInsert(unittest.TestCase):
	def insert(self, doc, server, box, running=None):
		docs = {
			("Gateway Server", "gw1"): server,
			("Gateway Store", "store1"): server,
			("Machine", "gw1"): box,
		}
		with (
			patch.object(frappe, "get_doc", side_effect=lambda doctype, name: docs[(doctype, name)]),
			patch.object(frappe, "db", SimpleNamespace(get_value=lambda *a, **k: running)),
			patch.object(module, "gateways_on", return_value=["gw1", "gw2"]),
		):
			ServerResize.before_insert(doc)

	def assert_refused(self, doc=None, server=None, box=None, **kwargs):
		with refused(), self.assertRaises(frappe.ValidationError):
			self.insert(doc or resize(), server or gateway(), box or machine(), **kwargs)

	def test_the_machine_and_the_type_it_leaves_are_recorded(self):
		doc = resize()
		self.insert(doc, gateway(), machine())
		self.assertEqual(("gw1", "m7g.large"), (doc.machine, doc.from_instance_type))

	def test_a_gateway_drains_itself_stops_pathway_and_comes_back_capped(self):
		doc = resize()
		self.insert(doc, gateway(), machine())
		self.assertEqual(
			[
				("enter_maintenance", None),
				("wait_for_drain", None),
				("stop_pathway", None),
				("stop_machine", None),
				("wait_for_machine_stopped", None),
				("set_instance_type", None),
				("start_machine", None),
				("sync_server", None),
				("wait_for_pathway", None),
				("apply_memory_cap", None),
				("leave_maintenance", None),
			],
			[(r.method_name, r.server) for r in doc.steps],
		)
		self.assertEqual("Enter maintenance", doc.steps[0].step_name)
		self.assertTrue(all(r.status == Status.Pending for r in doc.steps))

	def test_a_store_drains_every_gateway_on_it_and_checks_redis_before_lifting_them(self):
		doc = resize(server_type="Gateway Store", server="store1")
		self.insert(doc, gateway(doctype="Gateway Store", name="store1"), machine(name="store1"))
		self.assertEqual(
			[
				("enter_maintenance", "gw1"),
				("enter_maintenance", "gw2"),
				("wait_for_drain", "gw1"),
				("wait_for_drain", "gw2"),
				("stop_redis", None),
				("stop_machine", None),
				("wait_for_machine_stopped", None),
				("set_instance_type", None),
				("start_machine", None),
				("sync_server", None),
				("check_redis", None),
				("leave_maintenance", "gw1"),
				("leave_maintenance", "gw2"),
			],
			[(r.method_name, r.server) for r in doc.steps],
		)

	def test_any_server_but_a_terminated_one_is_resized(self):
		self.assert_refused(server=gateway(status="Terminated"))
		doc = resize()
		self.insert(doc, gateway(status="Broken"), machine())
		self.assertTrue(doc.steps)

	def test_one_resize_at_a_time_per_server(self):
		self.assert_refused(running="r0")

	def test_the_box_must_be_running_with_an_instance_and_not_bare_metal(self):
		self.assert_refused(box=machine(status="Stopped"))
		self.assert_refused(box=machine(is_bare_metal=1))

	def test_the_same_type_is_refused(self):
		self.assert_refused(doc=resize(instance_type="m7g.large"))

	def test_the_type_must_keep_the_boxs_architecture(self):
		self.assert_refused(box=machine(cpu_architecture="amd64"))

	def test_a_bare_metal_target_is_refused(self):
		info = {"cpu_architecture": "arm64", "is_bare_metal": True, "memory_mb": 262144}
		self.assert_refused(box=machine(cloud_client=SimpleNamespace(get_instance_type_info=lambda t: info)))

	def test_a_memory_limit_the_new_box_cannot_hold_is_refused_before_anything_stops(self):
		self.assert_refused(server=gateway(memory_limit_mb=16384))
		doc = resize()
		self.insert(doc, gateway(memory_limit_mb=8192), machine())
		self.assertTrue(doc.steps)


class TestMaintenanceSteps(unittest.TestCase):
	def enter(self, gw):
		step, doc = row("enter_maintenance", "gw1"), resize()
		with patch.object(frappe, "get_doc", return_value=gw):
			ServerResize.enter_maintenance(doc, step)
		return step

	def test_entering_sets_the_flag_applies_it_and_links_the_play(self):
		gw = gateway()
		step = self.enter(gw)
		self.assertEqual(gw.written, {"is_in_maintenance": 1})
		gw.apply_config.assert_called_once_with(reference_doctype="Server Resize", reference_docname="r1")
		self.assertEqual((Status.Success, "Ansible Play", "PLAY-1"), (step.status, step.job_type, step.job))

	def test_a_gateway_already_in_maintenance_is_left_alone(self):
		gw = gateway(is_in_maintenance=1)
		self.assertEqual(Status.Skipped, self.enter(gw).status)
		self.assertEqual(gw.written, {})
		gw.apply_config.assert_not_called()

	def leave(self, entered_status):
		gw = gateway(is_in_maintenance=1)
		step = row("leave_maintenance", "gw1")
		doc = resize(steps=[row("enter_maintenance", "gw1", entered_status), step])
		with patch.object(frappe, "get_doc", return_value=gw):
			ServerResize.leave_maintenance(doc, step)
		return gw, step

	def test_leaving_lifts_only_what_this_resize_set(self):
		gw, step = self.leave(Status.Success)
		self.assertEqual(gw.written, {"is_in_maintenance": 0})
		self.assertEqual((Status.Success, "PLAY-1"), (step.status, step.job))

	def test_a_maintenance_that_was_already_on_stays_on(self):
		gw, step = self.leave(Status.Skipped)
		self.assertEqual(gw.written, {})
		self.assertEqual(Status.Skipped, step.status)


class TestWaits(unittest.TestCase):
	def drain(self, in_flight, attempt=None):
		step = row("wait_for_drain", "gw1", attempt=attempt)
		gw = gateway(get_in_flight=Mock(return_value={"maintenance": True, "in_flight": in_flight}))
		with patch.object(frappe, "get_doc", return_value=gw), patch.object(runner.time, "sleep"):
			ServerResize.wait_for_drain(resize(), step)
		return step

	def test_an_idle_gateway_passes_at_once(self):
		self.assertEqual(Status.Success, self.drain(0).status)

	def test_a_busy_gateway_is_polled_again(self):
		self.assertEqual(Status.Running, self.drain(3).status)

	def test_the_drain_gives_up_after_its_cap(self):
		with refused(), self.assertRaises(frappe.ValidationError):
			self.drain(3, attempt=module.DRAIN_ATTEMPTS - 1)

	def test_a_dropped_connect_or_a_box_still_serving_is_polled_again(self):
		for answer in (requests.ConnectTimeout("dropped"), {"maintenance": False, "in_flight": 0}):
			step = row("wait_for_drain", "gw1")
			gw = gateway(get_in_flight=Mock(side_effect=[answer]))
			with patch.object(frappe, "get_doc", return_value=gw), patch.object(runner.time, "sleep"):
				ServerResize.wait_for_drain(resize(), step)
			self.assertEqual(Status.Running, step.status)

	def test_pathway_is_waited_for_until_it_answers(self):
		step = row("wait_for_pathway", "gw1")
		gw = gateway(get_in_flight=Mock(side_effect=requests.ConnectionError("refused")))
		with patch.object(frappe, "get_doc", return_value=gw), patch.object(runner.time, "sleep"):
			ServerResize.wait_for_pathway(resize(), step)
		self.assertEqual(Status.Running, step.status)
		gw.get_in_flight = Mock(return_value={"maintenance": True, "in_flight": 0})
		with patch.object(frappe, "get_doc", return_value=gw):
			ServerResize.wait_for_pathway(resize(), step)
		self.assertEqual(Status.Success, step.status)


class TestSyncServer(unittest.TestCase):
	def refresh(self, gw):
		with (
			patch.object(frappe, "get_doc", return_value=gw),
			patch.object(module.reconcile, "sync_networks") as networks,
		):
			ServerResize.refresh_server(resize())
		return networks

	def test_the_server_is_active_again_on_its_addresses_in_dns(self):
		gw = gateway(status="Broken")
		networks = self.refresh(gw)
		self.assertEqual("Active", gw.status)
		gw.save.assert_called_once()
		gw.sync_dns_records.assert_called_once()
		networks.assert_not_called()

	def test_a_moved_public_ip_reaches_the_security_groups_now(self):
		gw = gateway()

		def save():
			gw.public_ip = "203.0.113.99"

		gw.save = save
		self.refresh(gw).assert_called_once()


class TestRecover(unittest.TestCase):
	def recover(self, box, steps, machine_status="Active", lift=None, gw=None):
		doc = resize(steps=steps, error="Traceback")
		doc.refresh_server = Mock()
		doc.lift_maintenance = lift or Mock(return_value="PLAY-2")
		gw = gw or gateway()
		with (
			patch.object(
				frappe, "get_doc", side_effect=lambda doctype, name: box if doctype == "Machine" else gw
			),
			patch.object(
				frappe, "db", SimpleNamespace(get_value=lambda *a, **k: machine_status, commit=Mock())
			),
			patch.object(module.time, "sleep") as sleep,
		):
			ServerResize.recover(doc)
		doc.slept = sleep.call_count
		return doc

	def test_the_lift_waits_for_a_restarted_box_to_answer(self):
		gw = gateway(
			get_in_flight=Mock(
				side_effect=[requests.ConnectTimeout(), requests.ConnectionError(), {"in_flight": 0}]
			)
		)
		doc = self.recover(machine(), [row("enter_maintenance", status=Status.Success)], gw=gw)
		self.assertEqual(2, doc.slept)
		self.assertEqual([("gw1",)], [c.args for c in doc.lift_maintenance.call_args_list])
		self.assertEqual("", doc.error.replace("Traceback", ""))

	def test_a_stopped_box_is_started_and_its_server_put_back(self):
		box = machine(status="Stopped")
		doc = self.recover(box, [row("set_instance_type", status=Status.Failure)])
		box.resume.assert_called_once()
		doc.refresh_server.assert_called_once()

	def test_a_box_not_back_up_leaves_the_server_doc_alone(self):
		box = machine(status="Pending")
		doc = self.recover(box, [], machine_status="Pending")
		box.resume.assert_not_called()
		doc.refresh_server.assert_not_called()

	def test_only_the_maintenance_this_resize_set_is_lifted_and_the_rest_marked_skipped(self):
		steps = [
			row("enter_maintenance", "gw1", Status.Success),
			row("enter_maintenance", "gw2", Status.Skipped),
			row("enter_maintenance", "gw3", Status.Failure),
			row("stop_redis"),
			row("leave_maintenance", "gw1"),
		]
		doc = self.recover(machine(), steps)
		self.assertEqual([("gw1",), ("gw3",)], [c.args for c in doc.lift_maintenance.call_args_list])
		self.assertEqual([Status.Skipped, Status.Skipped], [r.status for r in steps[3:]])
		doc.save.assert_called_once()

	def test_an_undo_that_fails_is_written_down_and_the_next_still_runs(self):
		steps = [
			row("enter_maintenance", "gw1", Status.Success),
			row("enter_maintenance", "gw2", Status.Success),
		]
		lift = Mock(side_effect=[requests.ConnectionError("gw1 is down"), "PLAY-2"])
		doc = self.recover(machine(), steps, lift=lift)
		self.assertEqual(2, lift.call_count)
		self.assertIn("lifting maintenance on gw1: gw1 is down", doc.error)
		self.assertTrue(doc.error.startswith("Traceback"))
