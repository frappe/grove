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

	def test_a_unit_file_under_the_packaged_name_goes_before_the_mask_and_a_mask_is_kept(self):
		"""An earlier role wrote one there. The mask is a link at the same path: it must stay."""
		remove = named(self.tasks, "remove a unit file an earlier role put under the packaged name")
		self.assertEqual(
			remove["when"], "redis_packaged_name.stat.exists and not redis_packaged_name.stat.islnk"
		)
		self.assertLess(self.names.index(remove["name"]), self.names.index("mask the packaged redis"))

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


class TestTheMoveOntoStoreRedis(unittest.TestCase):
	"""Once per store: what ran as redis-server stopped, its data copied, that directory left as it
	was."""

	def setUp(self):
		self.tasks = role_tasks("main")
		self.names = [task.get("name") for task in self.tasks]
		self.adopt = named(self.tasks, "adopt the data the stopped redis held")

	def test_it_happens_only_while_our_unit_is_not_there_yet(self):
		"""Later, an emptied data dir must not be refilled from what an older redis once held."""
		stop = named(self.tasks, "stop the redis-server unit for the move")
		for task in (stop, self.adopt):
			self.assertEqual(task["when"], "not redis_own_unit.stat.exists")
		self.assertLess(
			self.names.index("look for a store-redis set up earlier"), self.names.index(stop["name"])
		)

	def test_the_old_redis_stops_before_its_files_are_copied_and_ours_starts_after(self):
		order = ["stop the redis-server unit for the move", self.adopt["name"], "redis unit", "redis started"]
		self.assertEqual(sorted(order, key=self.names.index), order)

	def adopt_into(self, ours, *theirs):
		env = {
			"PATH": os.environ["PATH"],
			"OURS": str(ours),
			"THEIRS": " ".join(str(path) for path in theirs),
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
			self.assertEqual(self.adopt_into(ours, theirs).stdout.strip(), f"adopted {theirs}")
			copied = sorted(str(path.relative_to(ours)) for path in ours.rglob("*") if path.is_file())
			self.assertEqual(copied, ["appendonlydir/appendonly.aof.manifest", "dump.rdb"])
			self.assertEqual((theirs / "dump.rdb").read_text(), "snapshot")
			(theirs / "later").write_text("written after the move")
			self.assertEqual(self.adopt_into(ours, theirs).stdout.strip(), "")
			self.assertFalse((ours / "later").exists())

	def test_the_directory_an_earlier_role_used_comes_before_the_packaged_one(self):
		"""On a store that role moved, the packaged directory holds what was there before it."""
		self.assertEqual(self.adopt["environment"]["THEIRS"], "/var/lib/redis-server /var/lib/redis")
		with tempfile.TemporaryDirectory() as folder:
			ours, newer, older = (pathlib.Path(folder, name) for name in ("ours", "newer", "older"))
			for directory, text in ((ours, None), (newer, "live"), (older, "stale")):
				directory.mkdir()
				if text:
					(directory / "dump.rdb").write_text(text)
			self.assertEqual(self.adopt_into(ours, newer, older).stdout.strip(), f"adopted {newer}")
			self.assertEqual((ours / "dump.rdb").read_text(), "live")

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

	def test_redis_stops_for_the_copy_only_when_the_package_changes_under_store_redis(self):
		"""A store not on store-redis yet is stopped by the role, for the move."""
		self.assertEqual(
			self.stop["when"], "redis_installed.stdout != redis_package and redis_own_data.stat.exists"
		)
		stopped, copied = self.stop["block"][-2:]
		self.assertEqual(stopped["ansible.builtin.systemd"]["name"], "store-redis")
		self.assertEqual(
			copied["ansible.builtin.command"], "cp -a /var/lib/store-redis {{ redis_data_copy }}"
		)

	def test_an_earlier_copy_is_refused_before_redis_stops(self):
		names = [task["name"] for task in self.stop["block"]]
		self.assertLess(names.index("refuse to write over an earlier copy"), names.index("redis stopped"))

	def test_the_role_runs_even_with_nothing_newer(self):
		"""It is what reads the version back, and it stops nothing."""
		self.assertNotIn("when", self.install)
		self.assertEqual(self.install["ansible.builtin.include_role"]["name"], "redis")

	def test_a_failed_step_starts_store_redis_again_once_it_exists(self):
		"""Before the unit exists there is nothing of ours to start: the play is run again."""
		looked, bring_back, report = self.swap["rescue"]
		self.assertEqual(looked["ansible.builtin.stat"]["path"], "/etc/systemd/system/store-redis.service")
		self.assertEqual(bring_back["ansible.builtin.systemd"], {"name": "store-redis", "state": "started"})
		self.assertEqual(bring_back["when"], "redis_unit_file.stat.exists")
		self.assertIn("ansible.builtin.fail", report)


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
