# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Vector on a gateway box: what the role renders for datum, the play that carries it, what the Install
Vector button, the Setup chain and the hourly token refresh run. Pure — no site, no SSH, no Central."""

import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from grove import datum
from grove.fleet import NamedHost, PathwayHost
from grove.grove.doctype.gateway_store.gateway_store import GatewayStore
from grove.grove.doctype.region.region import Region

PLAYBOOKS = Path(__file__).parent.parent / "playbooks"
NOW = datetime(2026, 11, 2, 9, 30)
EXPIRY = NOW + timedelta(days=7)
ROLE = PLAYBOOKS / "roles" / "vector"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
EXPORTER = PLAYBOOKS / "roles" / "redis_exporter"
EXPORTER_DEFAULTS = yaml.safe_load((EXPORTER / "defaults" / "main.yml").read_text())
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


def role_task(name, role=ROLE):
	tasks = yaml.safe_load((role / "tasks" / "main.yml").read_text())
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

	def test_host_metrics_skip_the_per_process_and_cgroup_collectors(self):
		collectors = config()["sources"]["host"]["collectors"]
		self.assertEqual(DEFAULTS["vector_host_collectors"], collectors)
		self.assertIn("cpu", collectors)
		self.assertNotIn("process", collectors)
		self.assertNotIn("cgroups", collectors)

	def test_every_pathway_log_and_the_redis_log_are_read(self):
		include = config()["sources"]["files"]["include"]
		self.assertEqual(DEFAULTS["vector_log_files"], include)
		self.assertIn("/var/log/pathway/*.log", include)
		# A payload line runs past Vector's 100 KiB default, which drops the line whole.
		self.assertGreater(config()["sources"]["files"]["max_line_bytes"], 102400)

	def test_host_metrics_are_a_switch_and_vectors_own_health_is_not(self):
		on = config(vector_ship_host_metrics=True)
		self.assertEqual("host_metrics", on["sources"]["host"]["type"])
		self.assertEqual(["host", "vector_self"], on["transforms"]["labelled_metrics"]["inputs"])
		off = config(vector_ship_host_metrics=False)
		self.assertNotIn("host", off["sources"])
		self.assertEqual(["vector_self"], off["transforms"]["labelled_metrics"]["inputs"])
		self.assertIn("metrics", off["sinks"])

	def test_a_stores_password_adds_the_redis_scrape_and_nothing_else(self):
		store = config(vector_redis_password="pw")
		self.assertEqual("prometheus_scrape", store["sources"]["redis"]["type"])
		self.assertEqual([DEFAULTS["vector_redis_exporter_url"]], store["sources"]["redis"]["endpoints"])
		self.assertEqual(["host", "redis", "vector_self"], store["transforms"]["labelled_metrics"]["inputs"])
		self.assertNotIn("pw", render(vector_redis_password="pw"))
		self.assertNotIn("redis", config()["sources"])
		self.assertIn(f":{EXPORTER_DEFAULTS['redis_exporter_port']}/", DEFAULTS["vector_redis_exporter_url"])

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
	def test_the_play_lays_the_grove_user_then_a_stores_exporter_before_the_role(self):
		roles = play()["roles"]
		self.assertEqual(["grove_user", "redis_exporter", "vector"], [r if isinstance(r, str) else r["role"] for r in roles])
		self.assertIn("vector_redis_password", roles[1]["when"])
		self.assertEqual({"redis_exporter_password": "{{ vector_redis_password }}"}, roles[1]["vars"])

	def test_the_exporters_password_is_a_root_only_secret_and_its_health_is_reaching_redis(self):
		password = role_task("Write the exporter's password", EXPORTER)
		self.assertTrue(password["no_log"])
		self.assertEqual("0600", password["ansible.builtin.copy"]["mode"])
		health = role_task("Check redis_exporter reaches redis", EXPORTER)
		self.assertIn("redis_up 1", health["until"])
		unit = (EXPORTER / "templates" / "redis_exporter.service.j2").read_text()
		self.assertIn("--web.listen-address=127.0.0.1:{{ redis_exporter_port }}", unit)

	def test_the_play_refuses_to_run_without_the_routes_and_the_token(self):
		checks = play()["pre_tasks"][0]["ansible.builtin.assert"]["that"]
		for variable in ("vector_metrics_url", "vector_logs_url", "vector_token"):
			self.assertTrue(any(variable in check for check in checks), variable)

	def test_the_config_is_a_secret_vector_can_read(self):
		task = role_task("Write the Vector config")
		self.assertTrue(task["no_log"])
		self.assertEqual({"owner": "root", "group": "vector", "mode": "0640"}, {k: task["ansible.builtin.template"][k] for k in ("owner", "group", "mode")})

	def test_vector_is_let_into_the_log_groups(self):
		groups = role_task("Let vector read the logs")["ansible.builtin.user"]["groups"]
		self.assertEqual(["{{ grove_user }}", "adm"], groups)


class FakeHost(SimpleNamespace):
	install_vector = NamedHost.install_vector
	check_vector_ready = NamedHost.check_vector_ready
	queue_vector = NamedHost.queue_vector
	provision_vector = NamedHost.provision_vector
	vector_exporter_variables = NamedHost.vector_exporter_variables


class FakeStore(FakeHost):
	vector_exporter_variables = GatewayStore.vector_exporter_variables


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
			patch("grove.datum.mint_token", return_value=("jwt-1", EXPIRY)),
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

	def test_the_job_mints_for_the_machine_in_centrals_region_runs_the_shared_play_and_keeps_the_expiry(self):
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
		doc.db_set.assert_called_once_with({"is_vector_setup": 1, "datum_token_expires_at": EXPIRY})

	def test_a_store_passes_its_redis_password_so_the_play_lays_the_exporter(self):
		doc = FakeStore(**vars(fake_host()), get_password=lambda field: "pw")
		doc.provision_vector()
		self.assertEqual("pw", doc.run_playbook.call_args.kwargs["extravars"]["vector_redis_password"])

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
	def test_every_active_box_with_vector_whose_token_has_a_day_left_or_no_known_expiry_gets_a_fresh_one(self):
		boxes = {"Gateway Server": ["gw-1", "gw-2"], "Gateway Store": ["store-1"], "Ingress Server": ["ing-1"]}

		def get_all(doctype, filters, or_filters, pluck):
			self.assertEqual({"status": "Active", "is_vector_setup": 1}, filters)
			self.assertEqual(
				[["datum_token_expires_at", "is", "not set"], ["datum_token_expires_at", "<=", NOW + timedelta(days=1)]],
				or_filters,
			)
			return boxes[doctype]

		with (
			patch("grove.datum.now_datetime", return_value=NOW),
			patch("frappe.get_all", side_effect=get_all),
			patch("frappe.enqueue_doc") as enqueue,
		):
			datum.refresh_tokens()
		self.assertEqual(
			[("Gateway Server", "gw-1"), ("Gateway Server", "gw-2"), ("Gateway Store", "store-1"), ("Ingress Server", "ing-1")],
			[call.args[:2] for call in enqueue.call_args_list],
		)
		self.assertTrue(all(call.args[2] == "provision_vector" for call in enqueue.call_args_list))


def fake_settings(url="https://central.test/", token="token-1"):
	labels = {"central_url": "Central URL", "central_token": "Central Token"}
	return SimpleNamespace(
		central_url=url,
		get_password=lambda field, raise_exception=True: token,
		get=lambda field: {"central_url": url}[field],
		meta=SimpleNamespace(get_label=labels.__getitem__),
	)


class TestMintToken(unittest.TestCase):
	def test_it_asks_central_with_its_own_token_for_this_machine_and_keeps_when_the_answer_runs_out(self):
		response = Mock(status_code=200, json=lambda: {"message": {"token": "jwt-1", "expires_in": 604800}})
		with (
			patch("frappe.get_single", return_value=fake_settings()),
			patch("grove.datum.now_datetime", return_value=NOW),
			patch("grove.datum.requests.get", return_value=response) as get,
		):
			self.assertEqual(("jwt-1", EXPIRY), datum.mint_token("ap-south-1", "grove:mc-1"))
		call = get.call_args
		self.assertEqual("https://central.test/api/method/central.api.grove.datum_token", call.args[0])
		self.assertEqual({"region": "ap-south-1", "resource_id": "grove:mc-1"}, call.kwargs["params"])
		self.assertEqual({"X-Grove-Token": "token-1"}, call.kwargs["headers"])

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
		with patch("frappe.get_single", return_value=fake_settings(url="", token="")):
			problems = datum.credential_problems()
		self.assertEqual(["Set Central URL in Grove Settings.", "Set Central Token in Grove Settings."], problems)

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


def fake_rows(regions, machines, vector_hosts=()):
	"""frappe.get_all by doctype: Regions with a datum, the Machines of one, the Vector hosts' machines."""
	rows = {"Region": regions, "Machine": machines, **{doctype: [] for doctype in datum.VECTOR_HOSTS}}
	for doctype, machine in vector_hosts:
		rows[doctype].append(machine)
	return lambda doctype, **kwargs: rows[doctype]


REGION = frappe._dict(name="ap-south-1", central_region="blr-1", metrics_url="http://datum.test/v1/ingest/remote")
MACHINES = [frappe._dict(name="mc-1", status="Active"), frappe._dict(name="mc-2", status="Terminated")]


class TestRoster(unittest.TestCase):
	"""Every minute each Region's datum gets every Machine's status under the Region's own id, so a
	reader knows which quiet boxes are expected to be quiet — Atlas's shape, not a tombstone."""

	def test_it_posts_every_machine_of_a_region_under_the_regions_own_id_to_the_json_route(self):
		with (
			patch("frappe.get_all", side_effect=fake_rows([REGION], MACHINES, [("Gateway Store", "mc-1")])),
			patch("grove.datum.mint_token", return_value=("jwt-1", EXPIRY)) as mint,
			patch("grove.datum.requests.post", return_value=Mock(status_code=200)) as post,
		):
			datum.push_roster()
		mint.assert_called_once_with("blr-1", "grove:region-ap-south-1")
		call = post.call_args
		self.assertEqual("http://datum.test/v1/ingest", call.args[0])
		self.assertEqual({"Authorization": "Bearer jwt-1"}, call.kwargs["headers"])
		samples = call.kwargs["json"]["samples"]
		self.assertTrue(all(sample.pop("ts").endswith("+00:00") for sample in samples))
		self.assertEqual(
			[
				{"metric": "grove_machine_info", "value": 1, "labels": {"machine": "mc-1", "status": "Active", "vector": "1"}},
				{"metric": "grove_machine_info", "value": 1, "labels": {"machine": "mc-2", "status": "Terminated", "vector": "0"}},
			],
			samples,
		)

	def test_a_region_without_machines_posts_nothing(self):
		with (
			patch("frappe.get_all", side_effect=fake_rows([REGION], [])),
			patch("grove.datum.mint_token") as mint,
			patch("grove.datum.requests.post") as post,
		):
			datum.push_roster()
		mint.assert_not_called()
		post.assert_not_called()

	def test_a_refusal_is_logged_naming_datums_answer_and_the_next_region_still_gets_its_roster(self):
		other = frappe._dict(REGION, name="us-east-1")
		with (
			patch("frappe.get_all", side_effect=fake_rows([REGION, other], MACHINES)),
			patch("grove.datum.mint_token", return_value=("jwt-1", EXPIRY)),
			patch("grove.datum.requests.post", side_effect=[Mock(status_code=401, text="expired"), Mock(status_code=200)]) as post,
			patch("frappe.throw", side_effect=frappe.ValidationError) as throw,
			patch("frappe.log_error") as log_error,
		):
			datum.push_roster()
		self.assertIn("ap-south-1's roster: 401 expired", throw.call_args.args[0])
		log_error.assert_called_once_with("datum roster failed: ap-south-1")
		self.assertEqual(2, post.call_count)

	def test_the_json_route_sits_beside_the_remote_write_one(self):
		self.assertEqual("http://datum.test/v1/ingest", datum.ingest_url("http://datum.test/v1/ingest/remote"))
