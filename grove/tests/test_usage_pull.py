# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The usage pull inserts one Usage Record per key per drain per day — the UTC day the gateway
served it — and acknowledges each (drain, key, day) once it is committed. One re-sent after a
failure records nothing twice; a team whose share cannot be recorded stays unacknowledged, is
logged as Stuck Usage, and holds nobody else back."""

import json
import unittest.mock
from decimal import Decimal

import frappe
from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from grove.billing.doctype.stuck_usage.stuck_usage import StuckUsage
from grove.billing.pricing import committed
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.pathway import reconcile, run, usage
from grove.pathway.reconcile import FIELDS, Reconciler
from grove.pathway.run import Target
from grove.tests.test_api_permissions import billed_record
from grove.utils import utc_today


def a_store(name):
	"""A Gateway Store row and nothing else: its controller provisions a machine, which no test wants,
	and a Link only needs the row to exist."""
	if not frappe.db.exists("Gateway Store", name):
		frappe.get_doc({"doctype": "Gateway Store", "name": name}).db_insert()
	return name


class PullCase(IntegrationTestCase):
	"""One team, one model, one gateway box on one store — the fleet hooks reach for DNS, which no
	test wants."""

	email = "usage-pull@grove.test"
	box = "usage-pull-box"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		cls.day = utc_today()
		cls.today = str(cls.day)
		cls.store = a_store(f"{cls.box}-store")
		cls.team = cls.grove_team(cls.email)
		model_id = f"{cls.box}-7b"
		doc = (
			frappe.db.exists("Model", {"model_id": model_id})
			or frappe.get_doc({"doctype": "Model", "model_id": model_id, "hf_repo": f"org/{model_id}"})
			.insert(ignore_permissions=True)
			.name
		)
		cls.model = frappe.db.get_value("Model", doc, "model_key")
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"):
			machine = frappe.get_doc(
				{"doctype": "Machine", "name": cls.box, "machine_type": "Gateway"}
			).insert(ignore_permissions=True)
			cls.gateway = (
				frappe.get_doc(
					{
						"doctype": "Gateway Server",
						"name": machine.name,
						"machine": machine.name,
						"gateway_store": cls.store,
					}
				)
				.insert(ignore_permissions=True, ignore_mandatory=True)
				.name
			)

	@classmethod
	def grove_team(cls, email):
		return (
			frappe.get_doc(
				{
					"doctype": "Grove Team",
					"__newname": email,
					"override_max_keys": 1,
					"max_keys": 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def key(self, team=None):
		return (
			frappe.get_doc({"doctype": "Grove API Key", "team": team or self.team})
			.insert(ignore_permissions=True)
			.name
		)

	def pull(self, usages, drain_id="d1", store=None, dead=(), day=None):
		"""`usages`: {key: hash}, each served on `day` — today unless said, "" for a gateway that
		names none."""
		day = self.today if day is None else day
		drains = {drain_id: {key: {day: h} for key, h in usages.items()}} if usages else {}
		with unittest.mock.patch.object(frappe.db, "commit"):
			return usage.record_drains(self.gateway, drains, dead, gateway_store=store or self.store)

	def records(self, key):
		return [
			frappe.get_doc("Usage Record", name)
			for name in frappe.get_all("Usage Record", filters={"api_key": key}, pluck="name")
		]

	def detail(self, record):
		return frappe.parse_json(record.usage)


class TestADrainIsOneRecordPerKey(PullCase):
	def test_a_drain_inserts_one_record_per_key_with_its_detail_as_json(self):
		key = self.key()
		drained = {
			"request_count": "2",
			f"m:completion_tokens:{self.model}": "10",
			f"m:request_count:{self.model}": "2",
		}
		self.assertEqual(self.pull({key: drained}), (1, {"d1": {key: [self.today]}}, [], 0))
		[doc] = self.records(key)
		self.assertEqual(
			(
				doc.team,
				doc.day,
				doc.drain_id,
				doc.gateway_store,
				doc.request_count,
				doc.total_tokens,
				doc.cost,
				doc.gateway_cost,
			),
			(self.team, self.day, "d1", self.store, 2, 10, 0, 0),
		)
		[entry] = self.detail(doc)
		self.assertEqual(
			(entry["model"], entry["pricing"], entry["requests"], entry["completion_tokens"]),
			(self.model, None, 2, 10),
		)

	def test_a_blank_day_lands_on_the_drains_own_day_and_never_twice(self):
		key = self.key()
		for _ in range(2):
			self.pull({key: {"request_count": "1"}}, drain_id="20250101T000000Z-abcd", day="")
		[doc] = self.records(key)
		self.assertEqual((doc.day, doc.request_count), (getdate("2025-01-01"), 1))

	def test_total_tokens_counts_prompt_and_completion_once_and_no_part_again(self):
		"""A long prompt's tokens are counted under the derived counter instead of the root, so
		they count too; a cached part's derived counter is a part still."""
		key = self.key()
		counters = {
			"prompt_tokens": "100",
			"cached_tokens": "60",
			"completion_tokens": "7",
			"prompt_tokens_above_272k": "300000",
			"cached_tokens_above_272k": "1000",
		}
		self.pull({key: {"request_count": "1", **{f"m:{c}:{self.model}": v for c, v in counters.items()}}})
		[doc] = self.records(key)
		self.assertEqual(doc.total_tokens, 300107)

	def test_the_gateways_own_view_of_the_key_is_ignored(self):
		key = self.key()
		self.pull({key: {"request_count": "1", "key_spent": "300000000", "key_balance": "700000000"}})
		[doc] = self.records(key)
		self.assertEqual((doc.request_count, doc.cost), (1, 0))

	def test_the_next_drain_is_a_new_record_and_a_resent_one_records_nothing_twice(self):
		key = self.key()
		self.pull({key: {"request_count": "1"}}, drain_id="d1")
		self.pull({key: {"request_count": "1"}}, drain_id="d1")
		self.pull({key: {"request_count": "3"}}, drain_id="d2")
		self.assertEqual(
			sorted((r.drain_id, r.request_count) for r in self.records(key)), [("d1", 1), ("d2", 3)]
		)

	def test_a_name_grove_does_not_know_gets_no_entry(self):
		key = self.key()
		self.pull(
			{
				key: {
					"request_count": "1",
					f"m:completion_tokens:{self.model}": "4",
					"m:completion_tokens:MD-00007": "6",
				}
			}
		)
		self.assertEqual([e["model"] for e in self.detail(self.records(key)[0])], [self.model])

	def test_a_hash_the_box_stamped_with_a_team_lands_under_it_and_one_without_under_the_keys(self):
		"""A key that changed hands has its books closed at the push, stamped with the team charged."""
		key, trial = self.key(), self.grove_team("usage-pull-trial@grove.test")
		self.pull({key: {"request_count": "2", "team": trial}}, drain_id="d-closed")
		self.pull({key: {"request_count": "1"}}, drain_id="d-live")
		self.assertEqual(
			sorted((r.drain_id, r.team, r.request_count) for r in self.records(key)),
			[("d-closed", trial, 2), ("d-live", self.team, 1)],
		)

	def test_one_drain_that_splits_a_key_between_two_teams_acks_every_day(self):
		key, trial = self.key(), self.grove_team("usage-pull-split@grove.test")
		yesterday = str(frappe.utils.add_days(self.day, -1))
		days = {yesterday: {"request_count": "2", "team": trial}, self.today: {"request_count": "1"}}
		drains = {"d-split": {key: days}}
		with unittest.mock.patch.object(frappe.db, "commit"):
			pulled, acks, _dead, stuck = usage.record_drains(self.gateway, drains, gateway_store=self.store)
		self.assertEqual((pulled, stuck), (2, 0))
		self.assertEqual(sorted(acks["d-split"][key]), sorted([yesterday, self.today]))
		self.assertEqual(
			sorted((r.team, r.request_count) for r in self.records(key)), sorted([(trial, 2), (self.team, 1)])
		)

	def test_a_key_used_on_two_stores_gets_a_record_on_each(self):
		key, other_store = self.key(), a_store("usage-pull-other-store")
		self.pull({key: {"request_count": "1"}}, drain_id="d-a")
		self.pull({key: {"request_count": "1"}}, drain_id="d-b", store=other_store)
		self.assertEqual(
			sorted(r.gateway_store for r in self.records(key)), sorted([self.store, other_store])
		)

	def test_a_named_gateways_drain_lands_under_its_store_and_one_without_fails(self):
		key = self.key()
		with unittest.mock.patch.object(frappe.db, "commit"):
			usage.record_drains(self.gateway, {"d-named": {key: {self.today: {"request_count": "1"}}}})
		self.assertEqual(self.records(key)[0].gateway_store, self.store)
		with (
			unittest.mock.patch.object(usage.snapshot, "gateway_store", return_value=None),
			self.assertRaises(frappe.ValidationError),
		):
			usage.record_drains(self.gateway, {"d-none": {key: {self.today: {"request_count": "1"}}}})

	def test_each_touched_team_is_landed_once_per_drain_not_per_key(self):
		keys = [self.key() for _ in range(3)]
		with unittest.mock.patch.object(Reconciler, "team") as land:
			self.pull({k: {"request_count": "1"} for k in keys})
		land.assert_called_once()
		team, drains = land.call_args.args
		self.assertEqual((team, sorted(drains)), (self.team, sorted(keys)))

	def test_a_teams_share_lands_as_one_insert_however_many_keys(self):
		keys = [self.key() for _ in range(3)]
		with unittest.mock.patch.object(frappe.db, "bulk_insert", wraps=frappe.db.bulk_insert) as insert:
			self.pull({k: {"request_count": "1"} for k in keys})
		insert.assert_called_once()
		key_column = FIELDS.index("api_key")
		self.assertEqual(sorted(row[key_column] for row in insert.call_args.args[2]), sorted(keys))
		self.assertEqual(len(frappe.get_all("Usage Record", filters={"api_key": ("in", keys)})), 3)

	def test_a_key_grove_does_not_hold_is_acknowledged_so_it_stops_coming(self):
		self.assertEqual(
			self.pull({"no-such-key": {"request_count": "1"}}, drain_id="d9"),
			(1, {"d9": {"no-such-key": [self.today]}}, [], 0),
		)

	def test_a_hash_lands_on_the_day_the_gateway_served_it_and_is_acknowledged_by_day(self):
		key = self.key()
		self.assertEqual(
			self.pull({key: {"request_count": "1"}}, day="2026-10-31"),
			(1, {"d1": {key: ["2026-10-31"]}}, [], 0),
		)
		[doc] = self.records(key)
		self.assertEqual((doc.day, doc.drain_id), (getdate("2026-10-31"), "d1"))

	def test_a_drain_spanning_two_days_is_a_record_per_day_and_a_resent_one_lands_nothing_twice(self):
		key = self.key()
		drains = {"d1": {key: {"2026-10-31": {"request_count": "1"}, "2026-11-01": {"request_count": "2"}}}}
		with unittest.mock.patch.object(frappe.db, "commit"):
			usage.record_drains(self.gateway, drains, gateway_store=self.store)
			usage.record_drains(self.gateway, drains, gateway_store=self.store)
		self.assertEqual(
			sorted((str(r.day), r.request_count) for r in self.records(key)),
			[("2026-10-31", 1), ("2026-11-01", 2)],
		)

	def test_a_hash_a_gateway_names_no_day_for_lands_today(self):
		key = self.key()
		self.assertEqual(self.pull({key: {"request_count": "1"}}, day=""), (1, {"d1": {key: [""]}}, [], 0))
		self.assertEqual(self.records(key)[0].day, self.day)


class TestDeadLines(PullCase):
	"""Usage the box spooled and could not replay comes back on the pull."""

	email = "usage-dead@grove.test"
	box = "usage-dead-box"

	def line(self, rid, **accrual):
		return {"id": rid, "line": json.dumps(accrual) if accrual else "{not json", "error": "script failed"}

	def test_a_readable_line_lands_as_usage_under_its_own_drain(self):
		key = self.key()
		dead = [
			self.line("r1", prefix=key, fields={"request_count": 1, f"m:completion_tokens:{self.model}": 7})
		]
		self.assertEqual(self.pull({}, dead=dead), (1, {}, ["r1"], 0))
		[doc] = self.records(key)
		self.assertEqual((doc.drain_id, self.detail(doc)[0]["completion_tokens"]), ("dead:r1", 7))

	def test_a_dead_line_lands_on_the_day_it_was_spooled(self):
		key = self.key()
		self.pull(
			{}, dead=[self.line("r5", prefix=key, fields={"request_count": 1}, at="2026-10-31T23:59:58.5Z")]
		)
		self.assertEqual(str(self.records(key)[0].day), "2026-10-31")

	def test_an_unreadable_line_or_an_unknown_key_is_kept_as_stuck_usage_and_dropped_on_the_box(self):
		dead = [self.line("r2"), self.line("r3", prefix="no-such-key", fields={"request_count": 1})]
		self.assertEqual(self.pull({}, dead=dead), (2, {}, ["r2", "r3"], 0))
		rows = frappe.get_all(
			"Stuck Usage",
			filters={"dead_line": ("in", ["r2", "r3"])},
			fields=["team", "gateway_store", "resolved"],
		)
		self.assertEqual(
			sorted((r.team, r.gateway_store, r.resolved) for r in rows), [(None, self.store, 0)] * 2
		)

	def test_a_dead_line_row_is_resolved_by_hand(self):
		self.pull({}, dead=[self.line("r4")])
		row = frappe.get_doc("Stuck Usage", {"dead_line": "r4"})
		row.mark_resolved()
		self.assertEqual(frappe.db.get_value("Stuck Usage", row.name, "resolved"), 1)


class TestAStuckTeamHoldsOnlyItselfBack(PullCase):
	email = "usage-stuck@grove.test"
	box = "usage-stuck-box"

	def failing(self, bad_team):
		real = reconcile.forget_figures

		def forget_figures(team):
			# Runs once the team's rows are written, so the failure lands after a write the savepoint must undo.
			if team == bad_team:
				raise RuntimeError("disk on fire")
			return real(team)

		return unittest.mock.patch.object(reconcile, "forget_figures", forget_figures)

	def stuck(self, store=None):
		return frappe.get_doc("Stuck Usage", {"team": self.team, "gateway_store": store or self.store})

	def test_the_failed_team_is_rolled_back_logged_and_left_unacknowledged(self):
		other = self.grove_team("usage-stuck-2@grove.test")
		bad, good = self.key(), self.key(other)
		drained = {"request_count": "1", f"m:completion_tokens:{self.model}": "9"}
		with self.failing(self.team):
			self.assertEqual(
				self.pull({bad: drained, good: drained}, drain_id="d7"),
				(2, {"d7": {good: [self.today]}}, [], 1),
			)
		self.assertEqual(self.records(bad), [])
		self.assertEqual(len(self.records(good)), 1)
		row = self.stuck()
		self.assertEqual((row.api_keys, row.attempts, row.resolved), (bad, 1, 0))
		self.assertEqual(frappe.parse_json(row.last_payload), {"d7": {bad: {self.today: drained}}})
		self.assertIn("disk on fire", row.last_error)

	def test_a_counter_grove_does_not_price_leaves_the_team_stuck_not_underbilled(self):
		key, store = self.key(), a_store("usage-stuck-drift-store")
		drained = {"request_count": "1", "p:abc:prompt_tokens_v2": "9", "p:abc:cost": "1"}
		self.assertEqual(self.pull({key: drained}, drain_id="d9", store=store), (1, {}, [], 1))
		self.assertEqual(self.records(key), [])
		self.assertIn("p:abc:prompt_tokens_v2", self.stuck(store).last_error)

	def test_it_stays_one_row_while_failing_and_resolves_when_it_lands(self):
		key, store = self.key(), a_store("usage-stuck-heal-store")
		with self.failing(self.team):
			self.pull({key: {"request_count": "1"}}, drain_id="d8", store=store)
			self.pull({key: {"request_count": "1"}}, drain_id="d8", store=store)
		self.assertEqual((self.stuck(store).attempts, self.stuck(store).resolved), (2, 0))
		self.assertEqual(
			self.pull({key: {"request_count": "1"}}, drain_id="d8", store=store),
			(1, {"d8": {key: [self.today]}}, [], 0),
		)
		self.assertEqual((self.stuck(store).resolved, bool(self.stuck(store).resolved_on)), (1, True))
		[record] = self.records(key)
		self.assertEqual(record.drain_id, "d8")
		landed = [(r.usage_record, r.api_key, str(r.day)) for r in self.stuck(store).usage_records]
		self.assertEqual(landed, [(record.name, key, self.today)])

	def test_pull_now_pulls_just_that_team(self):
		key, store = self.key(), a_store("usage-stuck-pull-now-store")
		with self.failing(self.team):
			self.pull({key: {"request_count": "1"}}, drain_id="d10", store=store)
		with unittest.mock.patch.object(frappe, "enqueue") as enqueue:
			self.stuck(store).pull_now()
		self.assertEqual(enqueue.call_args.kwargs["team"], self.team)

	def test_log_settings_clears_old_resolved_rows_and_keeps_open_ones(self):
		self.assertTrue(_supports_log_clearing("Stuck Usage"))
		long_ago = frappe.utils.add_days(frappe.utils.now_datetime(), -100)
		key = self.key()
		self.pull({key: {"request_count": "1"}}, drain_id="d11")
		[record] = self.records(key)
		old = frappe.get_doc(
			{
				"doctype": "Stuck Usage",
				"team": self.team,
				"gateway_store": self.store,
				"resolved": 1,
				"resolved_on": long_ago,
				"usage_records": [{"usage_record": record.name}],
			}
		).insert(ignore_permissions=True)
		open_row = frappe.get_doc(
			{"doctype": "Stuck Usage", "team": self.team, "gateway_store": a_store("usage-stuck-open-store")}
		).insert(ignore_permissions=True)
		open_row.db_set("creation", long_ago)
		StuckUsage.clear_old_logs(days=90)
		self.assertFalse(frappe.db.exists("Stuck Usage", old.name))
		self.assertFalse(frappe.db.exists("Stuck Usage Record", {"parent": old.name}))
		self.assertTrue(frappe.db.exists("Stuck Usage", open_row.name))
		self.assertTrue(frappe.db.exists("Usage Record", record.name))


class TestAPullForOneTeam(PullCase):
	email = "usage-one@grove.test"
	box = "usage-one-box"

	def test_every_key_it_holds_is_asked_for_from_the_stores_of_its_geographies(self):
		keys = sorted([self.key(), self.key()])
		frappe.db.set_value("Grove API Key", keys[0], "status", "revoked")
		pull = usage.Usage(team=self.team)
		with unittest.mock.patch.object(usage, "gateway_units", return_value=["unit"]) as units:
			self.assertEqual(pull.units(), ["unit"])
		units.assert_called_once_with(
			None, geography=frappe.db.get_value("Grove API Key", keys[0], "geography")
		)
		self.assertEqual(sorted(pull.keys), keys)
		self.assertEqual(sorted(pull.hashes.values()), keys)

	def test_a_team_with_no_keys_pulls_nothing(self):
		self.assertEqual(usage.Usage(team=self.grove_team("usage-one-nokeys@grove.test")).units(), [])


class TestTheCheckReadsTheBoxesForRealKeys(PullCase):
	"""`peek_team` end to end over real keys, only HTTP faked: the ids it sends are the keys' own
	hashes, the answer comes back by key name in USD, a key the box does not name is left out, and
	`committed` takes the box or the records, whichever is more."""

	email = "usage-check@grove.test"
	box = "usage-check-box"

	def test_the_box_figure_lands_on_its_key_and_the_check_takes_the_larger(self):
		first, second = self.key(), self.key()
		hashes = dict(
			frappe.get_all(
				"Grove API Key", filters={"team": self.team}, fields=["name", "key_hash"], as_list=True
			)
		)
		billed_record(first, self.team, self.day, 4)
		asked = []
		response = unittest.mock.Mock()
		response.json.return_value = {hashes[first]: 7_000_000_000, "stranger": 1}
		target = Target("Gateway Server", self.gateway, "http://box", "t")
		with (
			unittest.mock.patch.object(
				usage, "gateway_units", return_value=[run.Unit(self.store, (target,))]
			),
			unittest.mock.patch(
				"grove.pathway.run.requests.get", side_effect=lambda url, **_: asked.append(url) or response
			),
		):
			spent = usage.peek_team(self.team)
		[url] = asked
		path, ids = url.split("?ids=")
		self.assertEqual((path, set(ids.split(","))), ("http://box/spent", {hashes[first], hashes[second]}))
		self.assertEqual(spent, {first: Decimal("7")})
		self.assertEqual(committed(self.team, lock=False, box_spent=spent), 7.0)
		self.assertEqual(committed(self.team, lock=False, box_spent={first: Decimal("2")}), 4.0)


class TestADrainIsAcknowledgedOnceRecorded(unittest.TestCase):
	"""Pure: the pull and the box are fakes."""

	def land(self, drains, record=(1, {"d1": ["k"]}, [], 0), ack=None):
		row = {"server": "gw1", "success": 1, "duration_ms": 0, "drains": drains, "dead": []}
		target = unittest.mock.Mock(error=None)
		target.post.side_effect = ack
		with (
			unittest.mock.patch.object(
				usage, "record_drains", side_effect=record if isinstance(record, Exception) else [record]
			) as recorded,
			unittest.mock.patch.object(Target, "resolve", return_value=target),
			unittest.mock.patch.object(
				frappe, "db", frappe._dict(commit=lambda: None, rollback=lambda: None)
			),
		):
			outcome, [row] = usage.record_drain(True, [row])
		self.recorded = recorded
		return outcome, row, target.post

	def test_what_landed_is_acknowledged_by_drain_and_key(self):
		outcome, row, post = self.land({"d1": {"k": {"request_count": "1"}}})
		post.assert_called_once_with(
			"usage/ack", {"acks": {"d1": ["k"]}, "dead": []}, timeout=usage.DRAIN_TIMEOUT
		)
		self.assertEqual((outcome, row["detail"]), (True, "pulled:1"))

	def test_a_drain_that_cannot_be_recorded_acknowledges_nothing(self):
		outcome, row, post = self.land({"d1": {"k": {}}}, record=RuntimeError("db gone"))
		post.assert_not_called()
		self.assertEqual((outcome, row["success"]), (False, 0))
		self.assertIn("db gone", row["error"])

	def test_nothing_landed_sends_no_ack(self):
		_outcome, _row, post = self.land({"d1": {"k": {}}}, record=(1, {}, [], 1))
		post.assert_not_called()

	def test_dead_lines_alone_are_acknowledged_too(self):
		_outcome, _row, post = self.land({}, record=(1, {}, ["r1"], 0))
		post.assert_called_once_with("usage/ack", {"acks": {}, "dead": ["r1"]}, timeout=usage.DRAIN_TIMEOUT)

	def test_a_failed_ack_loses_nothing_and_says_so(self):
		outcome, row, _post = self.land({"d1": {"k": {}}}, ack=ConnectionError("box gone"))
		self.assertEqual((outcome, row["success"]), (True, 1))
		self.assertIn("ack failed", row["detail"])

	def test_the_sync_row_says_how_many_teams_are_stuck(self):
		_outcome, row, _post = self.land({"d1": {"k": {}}}, record=(3, {"d1": ["a"]}, [], 1))
		self.assertEqual(row["detail"], "pulled:3 stuck:1")


class TestAStoreIsPulledThroughOneWriter(unittest.TestCase):
	"""Every gateway on a store shares its counters, so the pull drains a store once, through the
	first of its writers that answers. Pure: the run doc and the pull are fakes."""

	paused = ""

	def pull(self, groups, succeeds, record=None, fetch=None, **kwargs):
		doc, pulled = unittest.mock.MagicMock(), []
		doc.acquire_lock.return_value = True
		doc.results = []
		doc.append.side_effect = lambda _field, row: doc.results.append(row)
		self.doc = doc

		def fetch_one(target):
			pulled.append(target.name)
			return {
				"success": int(target.name in succeeds),
				"reachable": 1,
				"duration_ms": 0,
				"drains": {"d1": {"key": {"request_count": "1"}}},
			}

		with (
			unittest.mock.patch.object(run, "new_run", return_value=doc),
			unittest.mock.patch.object(run, "sync_targets", return_value=groups),
			unittest.mock.patch.object(
				Target, "resolve", side_effect=lambda kind, name: Target(kind, name, "http://x", "t")
			),
			unittest.mock.patch.object(
				usage, "fetch_usage", side_effect=lambda target, _keys: (fetch or fetch_one)(target)
			),
			unittest.mock.patch.object(
				usage, "record_drains", side_effect=record or (lambda *_, **__: (1, {}, [], 0))
			),
			unittest.mock.patch.object(run, "finalize") as finalize,
			unittest.mock.patch.object(
				frappe,
				"db",
				frappe._dict(
					commit=lambda: None,
					rollback=lambda: None,
					get_value=lambda *a, **k: "store9",
					get_single_value=lambda _doctype, field: field == self.paused,
				),
			),
		):
			usage.pull_all(**kwargs)
		return pulled, finalize.call_args.args[1:] if finalize.called else None

	def test_a_teams_pull_dials_only_the_geographies_its_keys_live_in(self):
		keys = [
			frappe._dict(name="k1", geography="eu"),
			frappe._dict(name="k2", geography="in"),
			frappe._dict(name="k3", geography="in"),
		]
		asked = []

		def targets(geography=None):
			asked.append(geography)
			return [(f"store-{geography}", [f"gw-{geography}"])]

		with (
			unittest.mock.patch.object(frappe, "get_all", return_value=keys),
			unittest.mock.patch.object(run, "sync_targets", side_effect=targets),
			unittest.mock.patch.object(
				usage,
				"fetch_usage",
				side_effect=lambda *_: {"success": 1, "reachable": 1, "duration_ms": 0, "drains": {}},
			),
			unittest.mock.patch.object(usage, "record_drains", return_value=(0, {}, [], 0)),
			unittest.mock.patch.object(run, "finalize"),
			unittest.mock.patch.object(run, "new_run", return_value=self.run_doc()),
			unittest.mock.patch.object(
				Target, "resolve", side_effect=lambda kind, name: Target(kind, name, "http://x", "t")
			),
			unittest.mock.patch.object(
				frappe,
				"db",
				frappe._dict(commit=lambda: None, rollback=lambda: None, get_single_value=lambda *_: False),
			),
		):
			usage.pull_all(trigger="Manual", team="T-1")
		self.assertEqual(asked, ["eu", "in"])

	def run_doc(self):
		doc = unittest.mock.MagicMock()
		doc.acquire_lock.return_value = True
		doc.results = []
		doc.append.side_effect = lambda _field, row: doc.results.append(row)
		return doc

	def test_a_paused_tick_pulls_nothing(self):
		self.paused = "pause_usage_pull"
		self.assertEqual(self.pull([("store1", ["gw1"])], succeeds={"gw1"}), ([], None))

	def test_pausing_the_projection_leaves_the_pull_running(self):
		self.paused = "pause_projection"
		self.assertEqual(self.pull([("store1", ["gw1"])], succeeds={"gw1"}), (["gw1"], (1, 1)))

	def test_a_button_still_pulls_while_paused(self):
		self.paused = "pause_usage_pull"
		pulled, _counts = self.pull([("store1", ["gw1"])], succeeds={"gw1"}, trigger="Manual")
		self.assertEqual(pulled, ["gw1"])

	def test_a_named_gateway_is_pulled_itself_under_its_own_store(self):
		landed = []

		def record(*_args, **kwargs):
			landed.append(kwargs.get("gateway_store"))
			return 1, {}, [], 0

		pulled, counts = self.pull([("store1", ["gw1"])], succeeds={"gw9"}, gateways=["gw9"], record=record)
		self.assertEqual((pulled, counts, landed), (["gw9"], (1, 1), ["store9"]))

	def test_the_next_writer_is_tried_when_one_fails_and_none_after_a_success(self):
		pulled, counts = self.pull(
			[(None, ["gw0"]), ("store1", ["gw1", "gw2", "gw3"])], succeeds={"gw0", "gw2"}
		)
		self.assertEqual(sorted(pulled), ["gw0", "gw1", "gw2"])
		self.assertLess(pulled.index("gw1"), pulled.index("gw2"))
		self.assertEqual(counts, (2, 2))

	def test_a_store_with_no_writer_is_a_failed_target(self):
		pulled, counts = self.pull([("store1", [])], succeeds=set())
		self.assertEqual((pulled, counts), ([], (1, 0)))

	def test_a_drain_that_cannot_be_recorded_fails_its_own_row(self):
		def record(gateway, _drains, _dead, **_):
			if gateway == "gw1":
				raise RuntimeError("disk on fire")
			return 4, {}, [], 0

		_pulled, counts = self.pull(
			[(None, ["gw1"]), (None, ["gw2"])], succeeds={"gw1", "gw2"}, record=record
		)
		bad, good = self.doc.results
		self.assertEqual(counts, (2, 1))
		self.assertEqual((bad["success"], good["success"]), (0, 1))
		self.assertIn("disk on fire", bad["error"])
		self.assertEqual(good["detail"], "pulled:4")

	def test_the_drained_hashes_never_reach_the_row(self):
		self.pull([(None, ["gw1"])], succeeds={"gw1"})
		self.assertNotIn("drains", self.doc.results[0])
		self.assertNotIn("dead", self.doc.results[0])


class TestFetchUsage(unittest.TestCase):
	"""GET /usage off one box: the URL asked and the row it leaves."""

	def fetch(self, answer, keys=None):
		response = unittest.mock.Mock()
		response.json.return_value = answer
		with unittest.mock.patch("grove.pathway.run.requests.get", return_value=response) as get:
			row = usage.fetch_usage(Target("Gateway Server", "gw", "http://x", "t"), keys)
		return row, get.call_args.args[0]

	def test_drains_come_back_grouped_by_id(self):
		row, url = self.fetch({"drains": {"d1": {"k": {"request_count": "2"}}}})
		self.assertEqual(
			(row["success"], row["drains"], url), (1, {"d1": {"k": {"request_count": "2"}}}, "http://x/usage")
		)

	def test_a_pull_for_one_team_names_its_keys(self):
		_row, url = self.fetch({"drains": {}}, keys=["k1", "k2"])
		self.assertEqual(url, "http://x/usage?keys=k1,k2")

	def test_dead_lines_ride_along(self):
		dead = [{"id": "r1", "line": "{}", "error": "x"}]
		row, _url = self.fetch({"drains": {}, "dead": dead})
		self.assertEqual(row["dead"], dead)

	def test_a_box_that_could_not_be_resolved_is_a_failed_row_not_a_call(self):
		with unittest.mock.patch("grove.pathway.run.requests.get") as get:
			row = usage.fetch_usage(Target("Gateway Server", "gw", error="no admin_url"))
		get.assert_not_called()
		self.assertEqual((row["success"], row["error"]), (0, "no admin_url"))


class TestACapCheckReadsEachKeysOwnCounter(unittest.TestCase):
	"""`peek_team` reads what the boxes count as this month's spend of a team's keys, off each key's
	own counter, and refuses rather than answer stale: pushes paused, a store it could not read.
	No lock and no run doc: it reads beside a pull. Pure."""

	def peek(self, answer=None, paused=False):
		fake = unittest.mock.MagicMock()
		fake.run.return_value = answer
		db = frappe._dict(get_single_value=lambda _doctype, field: paused and field == "pause_projection")
		with (
			unittest.mock.patch.object(usage, "Peek", return_value=fake) as cls,
			unittest.mock.patch.object(frappe, "db", db),
		):
			result = usage.peek_team("T-1")
		cls.assert_called_once_with(trigger="Manual", team="T-1")
		return result

	def test_what_the_boxes_count_comes_back_in_dollars_per_key(self):
		self.assertEqual(self.peek({"ok": True, "spent": {"k1": 15_500}}), {"k1": Decimal("0.0000155")})
		self.assertEqual(self.peek({"ok": True, "spent": {}}), {})

	def test_it_refuses_rather_than_answer_stale(self):
		for case in ({"paused": True}, {"answer": {"ok": False, "spent": {}}}):
			with self.subTest(**case), self.assertRaises(frappe.ValidationError):
				self.peek(**case)

	def test_a_store_answers_through_whichever_writer_got_through(self):
		"""Answers come back by key name, summed over the stores that hold the key; a key no store
		holds is left out, and so is a hash that was never asked for."""
		failed = {"success": 0, "error": "down"}
		answered = {"success": 1, "spent": {"h1": 9, "h9": 1}}
		elsewhere = {"success": 1, "spent": {"h1": 2}}
		peek = usage.Peek.__new__(usage.Peek)
		peek.hashes = {"h1": "k1", "h2": "k2"}
		self.assertEqual(
			peek.record([(True, [failed, answered]), (True, [elsewhere])]), {"ok": True, "spent": {"k1": 11}}
		)
		self.assertEqual(peek.record([(False, [failed])]), {"ok": False, "spent": {}})

	def test_it_takes_no_lock_and_logs_no_run(self):
		peek = usage.Peek(trigger="Manual", team="T-1")
		with (
			unittest.mock.patch.object(peek, "units", return_value=[]),
			unittest.mock.patch.object(run, "new_run", side_effect=AssertionError("a run doc was built")),
		):
			self.assertEqual(peek.run(), {"ok": True, "spent": {}})

	def test_a_read_asks_for_every_hash_in_one_call(self):
		asked = []
		target = unittest.mock.MagicMock()
		target.get.side_effect = lambda path: asked.append(path) or {"h1": 5}
		target.dial.side_effect = lambda work, **row: work(row) or row
		self.assertEqual(usage.fetch_spent(target, ["h1", "h2"])["spent"], {"h1": 5})
		self.assertEqual(asked, ["spent?ids=h1,h2"])
