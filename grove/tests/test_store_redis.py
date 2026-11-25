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
	"""As the service account, from Grove's own unit, config and directories. The packages' unit of
	the same name is never read, and the three directories their installs chown are not used."""

	def setUp(self):
		self.tasks = role_tasks("main")
		self.names = [task.get("name") for task in self.tasks]
		self.unit = (ROLE / "templates/redis-server.service.j2").read_text()
		self.conf = (ROLE / "templates/redis.conf.j2").read_text()

	def test_the_unit_is_ours_and_runs_as_the_service_account(self):
		"""In /etc/systemd/system it outranks the packages' unit of the same name."""
		unit = named(self.tasks, "redis unit")["ansible.builtin.template"]
		self.assertEqual(unit["dest"], "/etc/systemd/system/redis-server.service")
		self.assertIn("User={{ redis_user }}", self.unit)
		self.assertIn("ExecStart=/usr/bin/redis-server /etc/redis-server/redis.conf", self.unit)

	def test_systemd_is_told_when_redis_is_ready(self):
		"""The unit waits for the notify; a conf that daemonized would never send it."""
		self.assertIn("Type=notify", self.unit)
		self.assertIn("daemonize no", self.conf)
		self.assertIn("supervised systemd", self.conf)

	def test_nothing_of_ours_is_in_a_directory_the_packages_chown(self):
		directories = named(self.tasks, "redis's own directories")
		self.assertEqual(
			directories["loop"], ["/etc/redis-server", "/var/lib/redis-server", "/var/log/redis-server"]
		)
		self.assertEqual(directories["ansible.builtin.file"]["owner"], "{{ redis_user }}")
		self.assertIn("dir /var/lib/redis-server\n", self.conf)
		self.assertIn("logfile /var/log/redis-server/", self.conf)

	def test_the_unit_and_config_are_there_before_the_package_starts_redis(self):
		"""The install starts `redis-server`; that has to be ours already, loaded."""
		install = self.names.index("install redis")
		for name in ("redis's own directories", "redis config", "redis unit", "load the unit"):
			self.assertLess(self.names.index(name), install, name)
		packages = named(self.tasks, "install redis")["ansible.builtin.apt"]["name"]
		self.assertEqual(packages, ["redis-server={{ redis_package }}", "redis-tools={{ redis_package }}"])

	def test_a_changed_config_or_unit_restarts_redis_by_a_task(self):
		restart = named(self.tasks, "restart redis onto a changed config or unit")
		self.assertEqual(restart["when"], "redis_conf is changed or redis_unit is changed")
		for name in ("redis config", "redis unit"):
			self.assertNotIn("notify", named(self.tasks, name))

	def test_the_doc_reads_the_release_the_role_checked(self):
		last = self.tasks[-1]
		self.assertEqual(VERSION_TASK, f"redis : {last['name']}")
		self.assertIn("redis_release", last["failed_when"])

	def test_the_backup_and_the_log_shipper_look_where_redis_now_is(self):
		conf = named(self.tasks, "redis config")["ansible.builtin.template"]["dest"]
		self.assertEqual(gateway_store.CONFS.split()[0], conf)
		log_dir = gateway_store.LOGS[0].removesuffix("*.log")
		self.assertIn(f"logfile {log_dir}", self.conf)


class TestTheMoveOffThePackagedRedis(unittest.TestCase):
	"""Once per store: the packaged redis stopped, its data copied, its directory left as it was."""

	def setUp(self):
		self.tasks = role_tasks("main")
		self.names = [task.get("name") for task in self.tasks]
		self.adopt = named(self.tasks, "adopt the data the packaged redis held")

	def test_it_happens_only_while_our_unit_is_not_there_yet(self):
		"""Later, an emptied data dir must not be refilled from what the packaged redis once held."""
		stop = named(self.tasks, "stop the packaged redis for the move")
		for task in (stop, self.adopt):
			self.assertEqual(task["when"], "not redis_own_unit.stat.exists")
		self.assertLess(self.names.index("look for our own redis unit"), self.names.index(stop["name"]))

	def test_the_packaged_redis_stops_before_its_files_are_copied_and_ours_starts_after(self):
		order = ["stop the packaged redis for the move", self.adopt["name"], "redis unit", "redis started"]
		self.assertEqual(sorted(order, key=self.names.index), order)

	def adopt_into(self, ours, theirs):
		env = {
			"PATH": os.environ["PATH"],
			"OURS": str(ours),
			"THEIRS": str(theirs),
			"OWNER": str(os.getuid()),
		}
		script = self.adopt["ansible.builtin.shell"]
		return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=True)

	def test_data_is_copied_into_an_empty_dir_and_the_old_one_is_left_whole(self):
		with tempfile.TemporaryDirectory() as folder:
			ours, theirs = pathlib.Path(folder, "ours"), pathlib.Path(folder, "theirs")
			ours.mkdir()
			(theirs / "appendonlydir").mkdir(parents=True)
			(theirs / "dump.rdb").write_text("snapshot")
			(theirs / "appendonlydir/appendonly.aof.manifest").write_text("manifest")
			self.assertEqual(self.adopt_into(ours, theirs).stdout.strip(), "adopted")
			copied = sorted(str(path.relative_to(ours)) for path in ours.rglob("*") if path.is_file())
			self.assertEqual(copied, ["appendonlydir/appendonly.aof.manifest", "dump.rdb"])
			self.assertEqual((theirs / "dump.rdb").read_text(), "snapshot")
			(theirs / "later").write_text("written after the move")
			self.assertEqual(self.adopt_into(ours, theirs).stdout.strip(), "")
			self.assertFalse((ours / "later").exists())

	def test_a_new_store_has_nothing_to_adopt(self):
		with tempfile.TemporaryDirectory() as folder:
			ours, theirs = pathlib.Path(folder, "ours"), pathlib.Path(folder, "theirs")
			ours.mkdir()
			self.assertEqual(self.adopt_into(ours, theirs).stdout.strip(), "")
			theirs.mkdir()
			self.assertEqual(self.adopt_into(ours, theirs).stdout.strip(), "")
			self.assertEqual(list(ours.iterdir()), [])


class TestALiveStoreIsUpgraded(unittest.TestCase):
	def setUp(self):
		[self.play] = yaml.safe_load((PLAYBOOKS / "gateway_store/upgrade_redis.yml").read_text())
		self.swap = named(self.play["tasks"], "swap redis, bringing it back if a step fails")
		self.stop, self.install = self.swap["block"]

	def test_the_play_refuses_a_blank_password(self):
		[check] = self.play["pre_tasks"]
		self.assertIn("redis_password", " ".join(check["ansible.builtin.assert"]["that"]))

	def test_redis_stops_for_the_copy_only_when_the_package_changes_under_our_own_unit(self):
		"""A store still on the packaged redis is stopped by the role, for the move."""
		self.assertEqual(
			self.stop["when"], "redis_installed.stdout != redis_package and redis_own_data.stat.exists"
		)
		names = [task["name"] for task in self.stop["block"]]
		self.assertEqual(names[-2:], ["redis stopped", "keep the data dir as the old redis left it"])
		copy = self.stop["block"][-1]["ansible.builtin.command"]
		self.assertEqual(copy, "cp -a /var/lib/redis-server {{ redis_data_copy }}")

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


class TestWhatAnEarlierRoleLeft(unittest.TestCase):
	"""It ran the packaged unit as the service account: a drop-in on the unit and a dpkg stat
	override on each packaged path. The upgrade play clears both and touches no data directory."""

	def setUp(self):
		self.tasks = role_tasks("leftovers")

	def test_the_upgrade_play_clears_them_after_the_swap(self):
		[play] = yaml.safe_load((PLAYBOOKS / "gateway_store/upgrade_redis.yml").read_text())
		last = play["tasks"][-1]["ansible.builtin.include_role"]
		self.assertEqual((last["name"], last["tasks_from"]), ("redis", "leftovers"))

	def test_the_drop_in_goes_and_systemd_is_told(self):
		drop_in = named(self.tasks, "remove the account drop-in from the unit")["ansible.builtin.file"]
		self.assertEqual(drop_in["path"], "/etc/systemd/system/redis-server.service.d/account.conf")
		self.assertEqual(drop_in["state"], "absent")
		reload = named(self.tasks, "reload systemd without the drop-in")
		self.assertEqual(reload["when"], "redis_drop_in is changed")

	def test_no_data_directory_is_removed(self):
		text = (ROLE / "tasks/leftovers.yml").read_text()
		for word in ("rm -", "/var/lib/redis-server", ".before-"):
			self.assertNotIn(word, text)

	@unittest.skipUnless(shutil.which("dpkg-statoverride"), "the role releases paths with dpkg-statoverride")
	def test_only_the_service_accounts_overrides_are_released(self):
		"""The packages hold one of their own on the conf, for `redis`: that one stays."""
		release = named(self.tasks, "release the packaged paths dpkg held for the service account")
		user, group = (
			subprocess.run(["id", flag], capture_output=True, text=True, check=True).stdout.strip()
			for flag in ("-un", "-gn")
		)
		real = shutil.which("dpkg-statoverride")
		with tempfile.TemporaryDirectory() as folder:
			held = pathlib.Path(folder, "statoverride")
			held.write_text(
				f"{user} {group} 2770 /etc/redis\n"
				f"{user} {group} 750 /var/lib/redis\n"
				f"{user} adm 2750 /var/log/redis\n"
				"root root 640 /etc/redis/redis.conf\n"
				f"{user} {group} 750 /somewhere/else\n"
			)
			stub = pathlib.Path(folder, "dpkg-statoverride")
			stub.write_text(f'#!/bin/sh\nexec {real} --admindir={folder} "$@"\n')
			stub.chmod(0o755)
			env = {"PATH": f"{folder}:{os.environ['PATH']}", "OWNER": user}
			script = release["ansible.builtin.shell"]
			runs = [
				subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=True)
				for _ in range(2)
			]
			left = sorted(held.read_text().splitlines())
		self.assertEqual(len(runs[0].stdout.splitlines()), 3)
		self.assertEqual(runs[1].stdout, "")
		self.assertEqual(
			left, sorted(["root root 640 /etc/redis/redis.conf", f"{user} {group} 750 /somewhere/else"])
		)


if __name__ == "__main__":
	unittest.main()
