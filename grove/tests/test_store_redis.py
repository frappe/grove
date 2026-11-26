"""Which Redis a Gateway Store gets, and how a live one is moved onto it."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

from grove.infrastructure.doctype.gateway_store import gateway_store
from grove.infrastructure.doctype.gateway_store.gateway_store import VERSION_TASK

PLAYBOOKS = pathlib.Path(__file__).resolve().parent.parent / "playbooks"
ROLE = PLAYBOOKS / "roles/redis"
MADISON = "redis-server | 6:{}~noble1 | https://packages.redis.io/deb noble/main amd64 Packages"


def role_tasks(name):
	return yaml.safe_load((ROLE / "tasks" / f"{name}.yml").read_text())


def named(tasks, name):
	return next(task for task in tasks if task.get("name") == name)


@unittest.skipUnless(shutil.which("dpkg"), "the role compares versions with dpkg")
class TestWhichReleaseAStoreTakes(unittest.TestCase):
	"""`redis_version` is pip's ~=: the newest release on its line, at or above the floor."""

	def choose(self, floor, releases):
		"""The role's own script, against an apt-cache that lists `releases`."""
		script = named(role_tasks("repository"), "choose the redis package")["ansible.builtin.shell"]
		with tempfile.TemporaryDirectory() as folder:
			stub = pathlib.Path(folder, "apt-cache")
			listing = "\n".join(MADISON.format(release) for release in releases)
			stub.write_text(f"#!/bin/sh\ncat <<'EOF'\n{listing}\nEOF\n")
			stub.chmod(0o755)
			env = {"PATH": f"{folder}:{os.environ['PATH']}", "FLOOR": floor}
			return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=False)

	def test_the_newest_release_on_the_line_is_taken(self):
		releases = ["8.10.3-1rl1", "8.12.0-1rl1", "8.12.1-1rl1", "8.12.9-1rl1", "8.12.10-1rl1", "8.14.0-1rl1"]
		self.assertEqual(self.choose("8.12.1", releases).stdout.strip(), "6:8.12.10-1rl1~noble1")

	def test_a_later_revision_of_one_release_wins(self):
		chosen = self.choose("8.12.1", ["8.12.1-1rl1", "8.12.1-1rl2"])
		self.assertEqual(chosen.stdout.strip(), "6:8.12.1-1rl2~noble1")

	def test_a_line_with_nothing_at_the_floor_is_refused(self):
		for releases in (["8.10.3-1rl1", "8.12.0-1rl1", "8.14.0-1rl1"], ["8.14.0-1rl1"]):
			refused = self.choose("8.12.1", releases)
			self.assertNotEqual(refused.returncode, 0)
			self.assertIn("no redis-server ~= 8.12.1", refused.stderr)
			self.assertEqual(refused.stdout, "")

	def test_the_floor_is_redis_version_without_its_operator(self):
		defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
		self.assertTrue(defaults["redis_version"].startswith("~="))
		choose = named(role_tasks("repository"), "choose the redis package")
		self.assertEqual(choose["environment"]["FLOOR"], "{{ redis_version | replace('~=', '') }}")


class TestAStoreRunsItsOwnRedis(unittest.TestCase):
	"""store-redis: Grove's own unit, config and directories, as the service account. The packages
	give the binary; their `redis-server` unit is masked and the directories their installs chown
	are not used."""

	def setUp(self):
		self.tasks = role_tasks("main")
		self.names = [task.get("name") for task in self.tasks]
		self.unit = (ROLE / "templates/store-redis.service.j2").read_text()
		self.conf = (ROLE / "templates/redis.conf.j2").read_text()

	def test_the_unit_is_ours_and_runs_as_the_service_account(self):
		unit = named(self.tasks, "redis unit")["ansible.builtin.template"]
		self.assertEqual(unit["dest"], "/etc/systemd/system/store-redis.service")
		self.assertIn("User={{ redis_user }}", self.unit)
		self.assertIn("ExecStart=/usr/bin/redis-server /etc/store-redis/redis.conf", self.unit)

	def test_systemd_is_told_when_redis_is_ready(self):
		"""The unit waits for the notify; a conf that daemonized would never send it."""
		self.assertIn("Type=notify", self.unit)
		self.assertIn("daemonize no", self.conf)
		self.assertIn("supervised systemd", self.conf)

	def test_nothing_of_ours_is_in_a_directory_the_packages_chown(self):
		directories = named(self.tasks, "store-redis's own directories")
		self.assertEqual(
			directories["loop"], ["/etc/store-redis", "/var/lib/store-redis", "/var/log/store-redis"]
		)
		self.assertEqual(directories["ansible.builtin.file"]["owner"], "{{ redis_user }}")
		self.assertIn("dir /var/lib/store-redis\n", self.conf)
		self.assertIn("logfile /var/log/store-redis/", self.conf)

	def test_the_packaged_unit_is_masked_before_the_install_would_start_it(self):
		"""Unmasked it would come up on 6379 at every boot and install, on the packages' files."""
		mask = named(self.tasks, "mask the packaged redis")["ansible.builtin.systemd"]
		self.assertEqual((mask["name"], mask["masked"]), ("redis-server", True))
		self.assertLess(self.names.index("mask the packaged redis"), self.names.index("install redis"))
		packages = named(self.tasks, "install redis")["ansible.builtin.apt"]["name"]
		self.assertEqual(packages, ["redis-server={{ redis_package }}", "redis-tools={{ redis_package }}"])

	def test_a_changed_config_or_unit_restarts_redis_by_a_task(self):
		restart = named(self.tasks, "restart redis onto a changed config or unit")
		self.assertEqual(restart["when"], "redis_conf is changed or redis_unit is changed")
		self.assertEqual(restart["ansible.builtin.systemd"]["name"], "store-redis")
		for name in ("redis config", "redis unit"):
			self.assertNotIn("notify", named(self.tasks, name))

	def test_the_doc_reads_the_release_the_role_checked(self):
		last = self.tasks[-1]
		self.assertEqual(VERSION_TASK, f"redis : {last['name']}")
		self.assertIn("redis_release", last["failed_when"])

	def test_everything_else_names_the_same_unit_and_paths(self):
		[stop] = yaml.safe_load((PLAYBOOKS / "gateway_store/stop.yml").read_text())
		self.assertEqual(stop["tasks"][0]["ansible.builtin.systemd"]["name"], "store-redis")
		exporter = (PLAYBOOKS / "roles/redis_exporter/templates/redis_exporter.service.j2").read_text()
		self.assertIn("store-redis.service", exporter)
		conf = named(self.tasks, "redis config")["ansible.builtin.template"]["dest"]
		self.assertEqual(gateway_store.CONFS.split()[0], conf)
		self.assertIn(f"logfile {gateway_store.LOGS[0].removesuffix('*.log')}", self.conf)


if __name__ == "__main__":
	unittest.main()
