# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The memory cap on pathway's unit: what the drop-in renders, which plays write it, and what the
Update Memory Limit button runs and records. Pure — no site and no SSH."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from grove.fleet import PathwayHost

PLAYBOOKS = Path(__file__).parent.parent / "playbooks"
ROLE = PLAYBOOKS / "roles" / "pathway_memory"


def render(memtotal_mb, limit_mb=0):
	"""The drop-in a box with this much RAM gets, the cap worked out by the role's own default."""
	environment = Environment(
		loader=FileSystemLoader(ROLE / "templates"), trim_blocks=True, undefined=StrictUndefined
	)
	defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
	facts = {**defaults, "ansible_memtotal_mb": memtotal_mb, "pathway_memory_limit_mb": limit_mb}
	cap = environment.from_string(defaults["pathway_memory_cap_mb"]).render(**facts)
	return environment.get_template("memory.conf.j2").render(pathway_memory_cap_mb=cap)


def play(path):
	return yaml.safe_load((PLAYBOOKS / path).read_text())[0]


class FakeHost(SimpleNamespace):
	memory_variables = PathwayHost.memory_variables


def fake_host(rc=0, **fields):
	defaults = dict(doctype="Gateway Server", name="gw-1", memory_limit_mb=0)
	return FakeHost(
		**{**defaults, **fields}, run_playbook=Mock(return_value=("play-1", rc)), db_set=Mock()
	)


class TestTheDropIn(unittest.TestCase):
	def test_a_blank_limit_is_the_boxs_ram_less_the_reserve(self):
		self.assertEqual("[Service]\nMemoryMax=442M\nEnvironment=GOMEMLIMIT=397MiB", render(954).strip())

	def test_a_limit_on_the_doc_wins(self):
		self.assertIn("MemoryMax=600M\nEnvironment=GOMEMLIMIT=540MiB", render(954, limit_mb=600))


class TestWhichPlaysWriteIt(unittest.TestCase):
	def test_setup_caps_a_new_box(self):
		for path in ("gateway_server/gateway.yml", "ingress_server/ingress.yml"):
			with self.subTest(path):
				self.assertIn("pathway_memory", play(path)["roles"])

	def test_the_button_runs_the_role_and_nothing_else(self):
		memory = play("gateway_server/memory.yml")
		self.assertEqual(["pathway_memory"], memory["roles"])
		self.assertNotIn("tasks", memory)

	def test_changing_the_cap_is_not_a_traffic_event(self):
		# GOMEMLIMIT waits for the next restart; the role must not cause one to deliver it.
		tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
		modules = [key for task in tasks for key in task if key.startswith("ansible.builtin.")]
		self.assertEqual(
			["ansible.builtin.assert", "ansible.builtin.file", "ansible.builtin.template", "ansible.builtin.systemd"],
			modules,
		)
		self.assertEqual({"daemon_reload": True}, tasks[-1]["ansible.builtin.systemd"])


class TestTheButton(unittest.TestCase):
	def applied(self, limit_mb, **fields):
		doc = fake_host(**fields)
		with patch("frappe.throw", side_effect=frappe.ValidationError), patch("grove.failure.report"):
			try:
				PathwayHost.apply_memory_limit(doc, limit_mb)
			finally:
				self.doc = doc
		return doc.run_playbook.call_args

	def test_it_runs_the_shared_play_with_the_limit_it_was_given(self):
		call = self.applied(600, doctype="Ingress Server")
		self.assertEqual(("memory.yml",), call.args)
		self.assertEqual("Gateway Server", call.kwargs["project"])
		self.assertEqual({"pathway_memory_limit_mb": 600}, call.kwargs["extravars"])

	def test_the_doc_records_the_limit_once_the_box_holds_it(self):
		self.applied(600)
		self.doc.db_set.assert_called_once_with("memory_limit_mb", 600)

	def test_a_failed_play_is_an_error_and_records_nothing(self):
		with self.assertRaises(frappe.ValidationError):
			self.applied(600, rc=2)
		self.doc.db_set.assert_not_called()

	def test_it_is_queued_with_the_limit_not_run_in_the_request(self):
		doc = fake_host()
		with patch("frappe.enqueue_doc") as enqueue, patch("frappe.msgprint"):
			PathwayHost.set_memory_limit(doc, 600)
		self.assertEqual(("Gateway Server", "gw-1", "apply_memory_limit"), enqueue.call_args.args)
		self.assertEqual(600, enqueue.call_args.kwargs["limit_mb"])
		doc.run_playbook.assert_not_called()

	def test_a_negative_limit_is_refused_before_anything_is_queued(self):
		doc = fake_host()
		with (
			patch("frappe.enqueue_doc") as enqueue,
			patch("frappe.throw", side_effect=frappe.ValidationError),
			self.assertRaises(frappe.ValidationError),
		):
			PathwayHost.set_memory_limit(doc, -1)
		enqueue.assert_not_called()
