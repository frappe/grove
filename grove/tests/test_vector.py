# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Vector on a gateway box: what the role renders for datum, the play that carries it, what the Install
Vector button, the Setup chain and the daily token refresh run. Pure — no site, no SSH, no Central."""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from grove import datum
from grove.fleet import NamedHost, PathwayHost
from grove.grove.doctype.region.region import Region

PLAYBOOKS = Path(__file__).parent.parent / "playbooks"
ROLE = PLAYBOOKS / "roles" / "vector"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
DATUM_LINE = ("ts", "product", "service", "level", "source", "message", "attributes")
DATUM_BATCH_CAP = 10_000

VARIABLES = {
	"vector_metrics_url": "http://datum.test/v1/ingest/remote",
	"vector_logs_url": "http://datum.test/v1/logs/ingest",
	"vector_token": "jwt-1",
	"vector_ship_host_metrics": True,
	"vector_server": "gw-1",
	"vector_machine": "mc-1",
	"vector_region": "ap-south-1",
}


def render(**overrides):
	environment = Environment(
		loader=FileSystemLoader(ROLE / "templates"), trim_blocks=True, undefined=StrictUndefined
	)
	return environment.get_template("vector.yaml.j2").render(**{**DEFAULTS, **VARIABLES, **overrides})


def config(**overrides):
	return yaml.safe_load(render(**overrides))


def play():
	return yaml.safe_load((PLAYBOOKS / "monitoring_agent" / "vector.yml").read_text())[0]


def role_task(name):
	tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
	return next(task for task in tasks if task["name"] == name)


class TestTheConfig(unittest.TestCase):
	def test_both_sinks_are_the_regions_routes_under_the_boxs_token(self):
		sinks = config()["sinks"]
		self.assertEqual("http://datum.test/v1/ingest/remote", sinks["metrics"]["endpoint"])
		self.assertEqual("http://datum.test/v1/logs/ingest", sinks["logs"]["uri"])
		for sink in sinks.values():
			self.assertEqual({"strategy": "bearer", "token": "jwt-1"}, sink["auth"])

	def test_logs_leave_as_datum_lines_and_nothing_else(self):
		source = config()["transforms"]["datum_lines"]["source"]
		built = re.search(r"\. = \{(.*)", source, re.S).group(1)
		self.assertEqual(set(DATUM_LINE), set(re.findall(r'^ {2}"(\w+)":', built, re.M)))
		self.assertIn('"product": "grove"', built)
		self.assertIn("truncate(message, 8000)", source)

	def test_every_pathway_log_and_the_redis_log_are_read(self):
		include = config()["sources"]["files"]["include"]
		self.assertEqual(DEFAULTS["vector_log_files"], include)
		self.assertIn("/var/log/pathway/*.log", include)

	def test_host_metrics_are_a_switch_and_vectors_own_health_is_not(self):
		on = config(vector_ship_host_metrics=True)
		self.assertEqual("host_metrics", on["sources"]["host"]["type"])
		self.assertEqual(["host", "vector_self"], on["transforms"]["labelled_metrics"]["inputs"])
		off = config(vector_ship_host_metrics=False)
		self.assertNotIn("host", off["sources"])
		self.assertEqual(["vector_self"], off["transforms"]["labelled_metrics"]["inputs"])
		self.assertIn("metrics", off["sinks"])

	def test_the_three_box_labels_ride_on_metrics_and_logs(self):
		transforms = config()["transforms"]
		for label, value in (("server", "gw-1"), ("machine", "mc-1"), ("region", "ap-south-1")):
			self.assertIn(f'.tags.{label} = "{value}"', transforms["labelled_metrics"]["source"])
			self.assertIn(f'"{label}": "{value}"', transforms["datum_lines"]["source"])

	def test_healthchecks_are_off_because_datum_refuses_a_get(self):
		for sink in config()["sinks"].values():
			self.assertEqual({"enabled": False}, sink["healthcheck"])

	def test_batches_stay_under_datums_caps(self):
		self.assertLessEqual(DEFAULTS["vector_batch_max_events"], DATUM_BATCH_CAP)
		for sink in config()["sinks"].values():
			self.assertLessEqual(sink["batch"]["max_events"], DATUM_BATCH_CAP)
			self.assertEqual("disk", sink["buffer"]["type"])

	def test_every_variable_the_template_reads_is_a_declared_default(self):
		text = (ROLE / "templates" / "vector.yaml.j2").read_text()
		used = set(re.findall(r"{[{%]\s*(?:if\s+)?(\w+)", text)) - {"endif"}
		self.assertEqual(set(), used - set(DEFAULTS), "a template variable with no default")
		self.assertEqual(set(), set(VARIABLES) - set(DEFAULTS), "an extra-var the role never declares")


class TestThePlayAndRole(unittest.TestCase):
	def test_the_play_lays_the_grove_user_before_the_role(self):
		self.assertEqual(["grove_user", "vector"], play()["roles"])

	def test_the_play_refuses_to_run_without_the_routes_and_the_token(self):
		checks = play()["pre_tasks"][0]["ansible.builtin.assert"]["that"]
		for variable in ("vector_metrics_url", "vector_logs_url", "vector_token"):
			self.assertTrue(any(variable in check for check in checks), variable)

	def test_the_config_is_a_secret_vector_can_read(self):
		task = role_task("Write the Vector config")
		self.assertTrue(task["no_log"])
		self.assertEqual({"owner": "root", "group": "vector", "mode": "0640"}, {k: task["ansible.builtin.template"][k] for k in ("owner", "group", "mode")})

	def test_vector_is_let_into_the_log_and_journal_groups(self):
		groups = role_task("Let vector read the logs and the journal")["ansible.builtin.user"]["groups"]
		self.assertEqual(["{{ grove_user }}", "systemd-journal"], groups)


class FakeHost(SimpleNamespace):
	install_vector = NamedHost.install_vector
	check_vector_ready = NamedHost.check_vector_ready
	queue_vector = NamedHost.queue_vector
	vector_variables = NamedHost.vector_variables
	provision_vector = NamedHost.provision_vector


def fake_host(rc=0, **fields):
	defaults = dict(
		doctype="Gateway Store", name="store-1", machine="mc-1", region="ap-south-1", status="Active", ship_host_metrics=1
	)
	return FakeHost(**{**defaults, **fields}, run_playbook=Mock(return_value=("play-1", rc)), db_set=Mock())


def fake_region(problems=()):
	return SimpleNamespace(
		central_region="blr-1",
		metrics_url="http://datum.test/v1/ingest/remote",
		logs_url="http://datum.test/v1/logs/ingest",
		vector_problems=list(problems),
	)


class TestTheButton(unittest.TestCase):
	def setUp(self):
		patches = [
			patch("frappe.get_doc", return_value=fake_region()),
			patch("grove.datum.credential_problems", return_value=[]),
			patch("grove.datum.mint_token", return_value="jwt-1"),
			patch("frappe.throw", side_effect=lambda msg, *a, **k: (_ for _ in ()).throw(frappe.ValidationError(msg))),
			patch("frappe.msgprint"),
			patch("grove.failure.report"),
		]
		self.mocks = [p.start() for p in patches]
		for p in patches:
			self.addCleanup(p.stop)
		self.get_doc, self.credential_problems, self.mint_token = self.mocks[:3]

	def test_it_is_queued_not_run(self):
		doc = fake_host()
		with patch("frappe.enqueue_doc") as enqueue:
			doc.install_vector()
		self.assertEqual(("Gateway Store", "store-1", "provision_vector"), enqueue.call_args.args)
		doc.run_playbook.assert_not_called()
		self.mint_token.assert_not_called()

	def test_a_box_not_yet_set_up_is_refused(self):
		with patch("frappe.enqueue_doc") as enqueue, self.assertRaises(frappe.ValidationError):
			fake_host(status="Pending").install_vector()
		enqueue.assert_not_called()

	def test_every_problem_is_named_at_once(self):
		self.get_doc.return_value = fake_region(["Set Logs Endpoint on Region ap-south-1's Monitoring tab."])
		self.credential_problems.return_value = ["Set Central URL in Grove Settings."]
		with patch("frappe.enqueue_doc") as enqueue, self.assertRaises(frappe.ValidationError) as caught:
			fake_host().install_vector()
		self.assertIn("Logs Endpoint", str(caught.exception))
		self.assertIn("Central URL", str(caught.exception))
		enqueue.assert_not_called()

	def test_the_job_mints_for_the_machine_in_centrals_region_and_runs_the_shared_play(self):
		doc = fake_host(ship_host_metrics=0)
		doc.provision_vector()
		self.mint_token.assert_called_once_with("blr-1", "grove:mc-1")
		call = doc.run_playbook.call_args
		self.assertEqual(("vector.yml",), call.args)
		self.assertEqual("Monitoring Agent", call.kwargs["project"])
		self.assertEqual(
			{
				"vector_metrics_url": "http://datum.test/v1/ingest/remote",
				"vector_logs_url": "http://datum.test/v1/logs/ingest",
				"vector_token": "jwt-1",
				"vector_ship_host_metrics": False,
				"vector_server": "store-1",
				"vector_machine": "mc-1",
				"vector_region": "ap-south-1",
			},
			call.kwargs["extravars"],
		)
		doc.db_set.assert_called_once_with("is_vector_setup", 1)

	def test_a_failed_play_is_an_error_and_records_nothing(self):
		doc = fake_host(rc=2)
		with self.assertRaises(frappe.ValidationError):
			doc.provision_vector()
		doc.db_set.assert_not_called()

	def test_the_job_looks_before_it_dials(self):
		self.get_doc.return_value = fake_region(["Set Metrics Endpoint on Region ap-south-1's Monitoring tab."])
		doc = fake_host()
		with self.assertRaises(frappe.ValidationError):
			doc.provision_vector()
		doc.run_playbook.assert_not_called()
		self.mint_token.assert_not_called()


class TestTheRefresh(unittest.TestCase):
	def test_every_active_box_with_vector_gets_a_fresh_token(self):
		boxes = {"Gateway Server": ["gw-1", "gw-2"], "Gateway Store": ["store-1"], "Ingress Server": ["ing-1"]}

		def get_all(doctype, filters, pluck):
			self.assertEqual({"status": "Active", "is_vector_setup": 1}, filters)
			return boxes[doctype]

		with patch("frappe.get_all", side_effect=get_all), patch("frappe.enqueue_doc") as enqueue:
			datum.refresh_tokens()
		self.assertEqual(
			[("Gateway Server", "gw-1"), ("Gateway Server", "gw-2"), ("Gateway Store", "store-1"), ("Ingress Server", "ing-1")],
			[call.args[:2] for call in enqueue.call_args_list],
		)
		self.assertTrue(all(call.args[2] == "provision_vector" for call in enqueue.call_args_list))


def fake_settings(url="https://central.test/", key="key-1", secret="secret-1"):
	labels = {"central_url": "Central URL", "central_api_key": "Central API Key", "central_api_secret": "Central API Secret"}
	return SimpleNamespace(
		central_url=url,
		central_api_key=key,
		get_password=lambda field, raise_exception=True: secret,
		get=lambda field: {"central_url": url, "central_api_key": key}[field],
		meta=SimpleNamespace(get_label=labels.__getitem__),
	)


class TestMintToken(unittest.TestCase):
	def test_it_asks_central_as_the_grove_user_for_this_machine(self):
		response = Mock(status_code=200, json=lambda: {"message": {"token": "jwt-1", "expires_in": 604800}})
		with patch("frappe.get_single", return_value=fake_settings()), patch("grove.datum.requests.get", return_value=response) as get:
			self.assertEqual("jwt-1", datum.mint_token("ap-south-1", "grove:mc-1"))
		call = get.call_args
		self.assertEqual("https://central.test/api/method/central.api.grove.datum_token", call.args[0])
		self.assertEqual({"region": "ap-south-1", "resource_id": "grove:mc-1"}, call.kwargs["params"])
		self.assertEqual({"Authorization": "token key-1:secret-1"}, call.kwargs["headers"])

	def test_a_refusal_is_an_error_naming_centrals_answer(self):
		response = Mock(status_code=401, text="not permitted")
		with (
			patch("frappe.get_single", return_value=fake_settings()),
			patch("grove.datum.requests.get", return_value=response),
			patch("frappe.throw", side_effect=frappe.ValidationError) as throw,
			self.assertRaises(frappe.ValidationError),
		):
			datum.mint_token("ap-south-1", "grove:mc-1")
		self.assertIn("401 not permitted", throw.call_args.args[0])


class TestCredentialProblems(unittest.TestCase):
	def test_each_blank_field_is_named(self):
		with patch("frappe.get_single", return_value=fake_settings(url="", secret="")):
			problems = datum.credential_problems()
		self.assertEqual(["Set Central URL in Grove Settings.", "Set Central API Secret in Grove Settings."], problems)

	def test_a_filled_credential_has_none(self):
		with patch("frappe.get_single", return_value=fake_settings()):
			self.assertEqual([], datum.credential_problems())


def fake_region_doc(**fields):
	values = {"central_region": "blr-1", "metrics_url": "", "logs_url": "", **fields}
	labels = {"central_region": "Central Region", "metrics_url": "Metrics Endpoint", "logs_url": "Logs Endpoint"}
	return SimpleNamespace(
		name="ap-south-1",
		get=values.get,
		meta=SimpleNamespace(get_label=labels.__getitem__),
		is_new=lambda: True,
	)


class TestRegion(unittest.TestCase):
	def test_a_blank_endpoint_is_a_named_problem(self):
		self.assertEqual(
			["Set Logs Endpoint on Region ap-south-1's Monitoring tab."],
			Region.vector_problems.fget(fake_region_doc(metrics_url="http://datum.test/v1/ingest/remote")),
		)
		self.assertEqual(2, len(Region.vector_problems.fget(fake_region_doc())))
		self.assertEqual(
			["Set Central Region on Region ap-south-1's Monitoring tab."],
			Region.vector_problems.fget(fake_region_doc(central_region="", metrics_url="http://a", logs_url="http://b")),
		)
		self.assertEqual([], Region.vector_problems.fget(fake_region_doc(metrics_url="http://a", logs_url="http://b")))

	def test_an_endpoint_without_a_scheme_is_refused(self):
		with patch("frappe.throw", side_effect=frappe.ValidationError), self.assertRaises(frappe.ValidationError):
			Region.validate(fake_region_doc(logs_url="datum.test/v1/logs/ingest"))
		with patch("frappe.throw", side_effect=frappe.ValidationError):
			Region.validate(fake_region_doc(logs_url="https://datum.test/v1/logs/ingest"))


class TestTheMigration(unittest.TestCase):
	"""Migrate to Vector on a box set up before it: vector.yml first, the exporter's retirement after."""

	def test_vector_lands_before_the_exporter_goes(self):
		doc = SimpleNamespace(name="gw-1", provision_vector=Mock(), run_playbook=Mock(return_value=("play-2", 0)))
		PathwayHost.migrate_vector(doc)
		doc.provision_vector.assert_called_once_with()
		doc.run_playbook.assert_called_once_with("retire_exporter.yml", project="Gateway Server")

	def test_a_failed_vector_install_leaves_the_exporter_in_place(self):
		doc = SimpleNamespace(
			name="gw-1",
			doctype="Gateway Server",
			provision_vector=Mock(side_effect=frappe.ValidationError("vector.yml failed")),
			run_playbook=Mock(),
		)
		with patch("grove.failure.report"), self.assertRaises(frappe.ValidationError):
			PathwayHost.migrate_vector(doc)
		doc.run_playbook.assert_not_called()

	def test_the_retirement_removes_exactly_what_the_old_plays_installed(self):
		tasks = yaml.safe_load((PLAYBOOKS / "gateway_server" / "retire_exporter.yml").read_text())[0]["tasks"]
		removed = next(task for task in tasks if "ansible.builtin.file" in task)["loop"]
		https_defaults = yaml.safe_load((PLAYBOOKS / "roles" / "grove_https" / "defaults" / "main.yml").read_text())
		self.assertEqual(
			{"/etc/systemd/system/node_exporter.service", "/usr/local/bin/node_exporter", https_defaults["grove_metrics_htpasswd"]},
			set(removed),
		)
