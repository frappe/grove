"""Which Redis a Gateway Store gets, and how a live one is moved onto it."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

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


class TestAnInstallLeavesAStoreThatStarts(unittest.TestCase):
	def setUp(self):
		self.tasks = role_tasks("main")
		self.names = [task.get("name") for task in self.tasks]

	def test_a_kept_conf_stops_daemonizing_before_the_package_restarts_redis(self):
		"""Ubuntu's conf says `daemonize yes`; under redis.io's bare unit that never reports ready."""
		foreground = named(self.tasks, "redis stays in the foreground under systemd")
		self.assertEqual(foreground["loop"], ["daemonize no", "supervised auto"])
		self.assertEqual(foreground["when"], "redis_conf.stat.exists")
		self.assertLess(self.names.index(foreground["name"]), self.names.index("install redis"))

	def test_the_install_is_the_chosen_package_and_keeps_our_conf(self):
		install = named(self.tasks, "install redis")["ansible.builtin.apt"]
		self.assertEqual(
			install["name"], ["redis-server={{ redis_package }}", "redis-tools={{ redis_package }}"]
		)
		self.assertIn("force-confold", install["dpkg_options"])

	def test_a_fresh_install_drops_the_packaged_modules_and_restarts(self):
		modules = named(self.tasks, "redis loads no modules")
		self.assertEqual(modules["ansible.builtin.lineinfile"]["regexp"], "^loadmodule ")
		self.assertEqual(modules["ansible.builtin.lineinfile"]["state"], "absent")
		restart = named(self.tasks, "restart redis onto its listen address and password")
		self.assertIn("redis_modules is changed", restart["when"])

	def test_the_doc_reads_the_release_the_role_checked(self):
		last = self.tasks[-1]
		self.assertEqual(VERSION_TASK, f"redis : {last['name']}")
		self.assertIn("redis_release", last["failed_when"])


class TestALiveStoreIsUpgraded(unittest.TestCase):
	def setUp(self):
		[self.play] = yaml.safe_load((PLAYBOOKS / "gateway_store/upgrade_redis.yml").read_text())
		self.swap = named(self.play["tasks"], "swap redis, bringing it back if a step fails")
		self.stop, self.install = self.swap["block"]

	def test_the_play_refuses_a_blank_password(self):
		[check] = self.play["pre_tasks"]
		self.assertIn("redis_password", " ".join(check["ansible.builtin.assert"]["that"]))

	def test_redis_stops_only_when_the_package_changes(self):
		self.assertEqual(self.stop["when"], "redis_installed.stdout != redis_package")
		names = [task["name"] for task in self.stop["block"]]
		self.assertEqual(names[-2:], ["redis stopped", "keep the data dir as the old redis left it"])

	def test_an_earlier_copy_is_refused_before_redis_stops(self):
		names = [task["name"] for task in self.stop["block"]]
		self.assertLess(names.index("refuse to write over an earlier copy"), names.index("redis stopped"))

	def test_the_role_runs_even_with_nothing_newer(self):
		"""It is what reads the version back, and it stops nothing."""
		self.assertNotIn("when", self.install)
		self.assertEqual(self.install["ansible.builtin.include_role"]["name"], "redis")

	def test_a_failed_step_starts_redis_again(self):
		bring_back = self.swap["rescue"][0]["ansible.builtin.systemd"]
		self.assertEqual((bring_back["name"], bring_back["state"]), ("redis-server", "started"))
		self.assertIn("ansible.builtin.fail", self.swap["rescue"][1])


if __name__ == "__main__":
	unittest.main()
