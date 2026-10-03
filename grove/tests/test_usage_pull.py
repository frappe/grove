# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The usage pull inserts one Usage Record per key per drain and acknowledges each key once it is
committed. A pair re-sent after a failure records nothing twice; a user whose share cannot be
recorded stays unacknowledged, is logged as Stuck Usage, and holds nobody else back."""

import json
import threading
import unittest.mock

import frappe
from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_user.grove_user import register_user
from grove.grove.doctype.stuck_usage.stuck_usage import StuckUsage
from grove.pathway import run, usage
from grove.pathway.reconcile import Reconciler
from grove.pathway.run import Target
from grove.utils import utc_today


def a_store(name):
	"""A Gateway Store row and nothing else: its controller provisions a machine, which no test wants,
	and a Link only needs the row to exist."""
	if not frappe.db.exists("Gateway Store", name):
		frappe.get_doc({"doctype": "Gateway Store", "name": name}).db_insert()
	return name


class PullCase(IntegrationTestCase):
	"""One user, one model, one gateway box on one store — the fleet hooks reach for DNS, which no
	test wants."""

	email = "usage-pull@grove.test"
	box = "usage-pull-box"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		cls.day = utc_today()
		cls.store = a_store(f"{cls.box}-store")
		cls.user = cls.grove_user(cls.email)
		model_id = f"{cls.box}-7b"
		doc = frappe.db.exists("Model", {"model_id": model_id}) or frappe.get_doc(
			{"doctype": "Model", "model_id": model_id, "modality": "text", "hf_repo": f"org/{model_id}"}
		).insert(ignore_permissions=True).name
		# The key: what a bucket and an entry name. The doc is never looked at here.
		cls.model = frappe.db.get_value("Model", doc, "model_key")
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with (
			unittest.mock.patch(f"{gateway_module}.sync_fleet_ingress"),
			unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"),
		):
			machine = frappe.get_doc({"doctype": "Machine", "name": cls.box, "machine_type": "Gateway"}).insert(ignore_permissions=True)
			cls.gateway = frappe.get_doc({
				"doctype": "Gateway Server", "name": machine.name, "machine": machine.name, "gateway_store": cls.store,
			}).insert(ignore_permissions=True, ignore_mandatory=True).name

	@classmethod
	def grove_user(cls, email):
		return frappe.get_doc({"doctype": "Grove User", "user": register_user(email)}).insert(ignore_permissions=True).name

	def key(self, user=None):
		return frappe.get_doc({"doctype": "Grove API Key", "user": user or self.user}).insert(ignore_permissions=True).name

	def pull(self, usages, drain_id="d1", store=None, dead=()):
		with unittest.mock.patch.object(frappe.db, "commit"):
			return usage.record_drains(self.gateway, {drain_id: usages} if usages else {}, dead, gateway_store=store or self.store)

	def records(self, key):
		return [frappe.get_doc("Usage Record", name) for name in frappe.get_all("Usage Record", filters={"api_key": key}, pluck="name")]

	def detail(self, record):
		return frappe.parse_json(record.usage)


class TestADrainIsOneRecordPerKey(PullCase):
	def test_a_drain_inserts_one_record_per_key_with_its_detail_as_json(self):
		key = self.key()
		drained = {"request_count": "2", f"m:completion_tokens:{self.model}": "10", f"m:request_count:{self.model}": "2"}
		self.assertEqual(self.pull({key: drained}), (1, {"d1": [key]}, [], 0))
		[doc] = self.records(key)
		self.assertEqual(
			(doc.user, doc.day, doc.drain_id, doc.gateway_store, doc.request_count, doc.cost, doc.gateway_cost),
			(self.user, self.day, "d1", self.store, 2, 0, 0),
		)
		[entry] = self.detail(doc)
		self.assertEqual((entry["model"], entry["pricing"], entry["requests"], entry["completion_tokens"]), (self.model, None, 2, 10))

	def test_the_gateways_own_view_of_the_user_is_ignored(self):
		# The box still writes these two; Grove bills from the pricing-tagged counters alone.
		key = self.key()
		self.pull({key: {"request_count": "1", "user_spent": "300000000", "user_balance": "700000000"}})
		[doc] = self.records(key)
		self.assertEqual((doc.request_count, doc.cost), (1, 0))

	def test_the_next_drain_is_a_new_record_and_a_resent_one_records_nothing_twice(self):
		key = self.key()
		self.pull({key: {"request_count": "1"}}, drain_id="d1")
		self.pull({key: {"request_count": "1"}}, drain_id="d1")
		self.pull({key: {"request_count": "3"}}, drain_id="d2")
		self.assertEqual(sorted((r.drain_id, r.request_count) for r in self.records(key)), [("d1", 1), ("d2", 3)])

	def test_a_name_grove_does_not_know_gets_no_entry(self):
		key = self.key()
		self.pull({key: {"request_count": "1", f"m:completion_tokens:{self.model}": "4", "m:completion_tokens:MD-00007": "6"}})
		self.assertEqual([e["model"] for e in self.detail(self.records(key)[0])], [self.model])

	def test_a_key_used_on_two_stores_gets_a_record_on_each(self):
		key, other_store = self.key(), a_store("usage-pull-other-store")
		self.pull({key: {"request_count": "1"}}, drain_id="d-a")
		self.pull({key: {"request_count": "1"}}, drain_id="d-b", store=other_store)
		self.assertEqual(sorted(r.gateway_store for r in self.records(key)), sorted([self.store, other_store]))

	def test_a_named_gateways_drain_lands_under_its_store_and_one_without_fails(self):
		key = self.key()
		with unittest.mock.patch.object(frappe.db, "commit"):
			usage.record_drains(self.gateway, {"d-named": {key: {"request_count": "1"}}})
		self.assertEqual(self.records(key)[0].gateway_store, self.store)
		with unittest.mock.patch.object(usage.snapshot, "gateway_store", return_value=None), self.assertRaises(frappe.ValidationError):
			usage.record_drains(self.gateway, {"d-none": {key: {"request_count": "1"}}})

	def test_each_touched_user_is_landed_once_per_drain_not_per_key(self):
		keys = [self.key() for _ in range(3)]
		with unittest.mock.patch.object(Reconciler, "user") as land:
			self.pull({k: {"request_count": "1"} for k in keys})
		land.assert_called_once()
		user, drains = land.call_args.args
		self.assertEqual((user, sorted(drains)), (self.user, sorted(keys)))

	def test_a_key_grove_does_not_hold_is_acknowledged_so_it_stops_coming(self):
		self.assertEqual(self.pull({"no-such-key": {"request_count": "1"}}, drain_id="d9"), (1, {"d9": ["no-such-key"]}, [], 0))


class TestDeadLines(PullCase):
	"""Usage the box spooled and could not replay comes back on the pull."""

	email = "usage-dead@grove.test"
	box = "usage-dead-box"

	def line(self, rid, **accrual):
		return {"id": rid, "line": json.dumps(accrual) if accrual else "{not json", "error": "script failed"}

	def test_a_readable_line_lands_as_usage_under_its_own_drain(self):
		key = self.key()
		dead = [self.line("r1", prefix=key, fields={"request_count": 1, f"m:completion_tokens:{self.model}": 7})]
		self.assertEqual(self.pull({}, dead=dead), (1, {}, ["r1"], 0))
		[doc] = self.records(key)
		self.assertEqual((doc.drain_id, self.detail(doc)[0]["completion_tokens"]), ("dead:r1", 7))

	def test_an_unreadable_line_or_an_unknown_key_is_kept_as_stuck_usage_and_dropped_on_the_box(self):
		dead = [self.line("r2"), self.line("r3", prefix="no-such-key", fields={"request_count": 1})]
		self.assertEqual(self.pull({}, dead=dead), (2, {}, ["r2", "r3"], 0))
		rows = frappe.get_all("Stuck Usage", filters={"dead_line": ("in", ["r2", "r3"])}, fields=["grove_user", "gateway_store", "resolved"])
		self.assertEqual(sorted((r.grove_user, r.gateway_store, r.resolved) for r in rows), [(None, self.store, 0)] * 2)

	def test_a_dead_line_row_is_resolved_by_hand(self):
		self.pull({}, dead=[self.line("r4")])
		row = frappe.get_doc("Stuck Usage", {"dead_line": "r4"})
		row.mark_resolved()
		self.assertEqual(frappe.db.get_value("Stuck Usage", row.name, "resolved"), 1)


class TestAStuckUserHoldsOnlyThemselvesBack(PullCase):
	email = "usage-stuck@grove.test"
	box = "usage-stuck-box"

	def failing(self, bad_key):
		real = Reconciler.insert

		def insert(reconciler, user, prefix, drain, billed):
			# The record is written first, so the failure lands after a write the savepoint must undo.
			doc = real(reconciler, user, prefix, drain, billed)
			if prefix == bad_key:
				raise RuntimeError("disk on fire")
			return doc

		return unittest.mock.patch.object(Reconciler, "insert", insert)

	def stuck(self, store=None):
		return frappe.get_doc("Stuck Usage", {"grove_user": self.user, "gateway_store": store or self.store})

	def test_the_failed_user_is_rolled_back_logged_and_left_unacknowledged(self):
		other = self.grove_user("usage-stuck-2@grove.test")
		bad, good = self.key(), self.key(other)
		drained = {"request_count": "1", f"m:completion_tokens:{self.model}": "9"}
		with self.failing(bad):
			self.assertEqual(self.pull({bad: drained, good: drained}, drain_id="d7"), (2, {"d7": [good]}, [], 1))
		self.assertEqual(self.records(bad), [])
		self.assertEqual(len(self.records(good)), 1)
		row = self.stuck()
		self.assertEqual((row.api_keys, row.attempts, row.resolved), (bad, 1, 0))
		self.assertEqual(frappe.parse_json(row.last_payload), {"d7": {bad: drained}})
		self.assertIn("disk on fire", row.last_error)

	def test_it_stays_one_row_while_failing_and_resolves_when_it_lands(self):
		key, store = self.key(), a_store("usage-stuck-heal-store")
		with self.failing(key):
			self.pull({key: {"request_count": "1"}}, drain_id="d8", store=store)
			self.pull({key: {"request_count": "1"}}, drain_id="d8", store=store)
		self.assertEqual((self.stuck(store).attempts, self.stuck(store).resolved), (2, 0))
		self.assertEqual(self.pull({key: {"request_count": "1"}}, drain_id="d8", store=store), (1, {"d8": [key]}, [], 0))
		self.assertEqual((self.stuck(store).resolved, bool(self.stuck(store).resolved_on)), (1, True))
		self.assertEqual([r.drain_id for r in self.records(key)], ["d8"])

	def test_pull_now_pulls_just_that_user(self):
		key, store = self.key(), a_store("usage-stuck-pull-now-store")
		with self.failing(key):
			self.pull({key: {"request_count": "1"}}, drain_id="d10", store=store)
		with unittest.mock.patch.object(frappe, "enqueue") as enqueue:
			self.stuck(store).pull_now()
		self.assertEqual(enqueue.call_args.kwargs["user"], self.user)

	def test_log_settings_clears_old_resolved_rows_and_keeps_open_ones(self):
		self.assertTrue(_supports_log_clearing("Stuck Usage"))
		long_ago = frappe.utils.add_days(frappe.utils.now_datetime(), -100)
		old = frappe.get_doc({"doctype": "Stuck Usage", "grove_user": self.user, "gateway_store": self.store, "resolved": 1, "resolved_on": long_ago}).insert(ignore_permissions=True)
		open_row = frappe.get_doc({"doctype": "Stuck Usage", "grove_user": self.user, "gateway_store": a_store("usage-stuck-open-store")}).insert(ignore_permissions=True)
		open_row.db_set("creation", long_ago)
		StuckUsage.clear_old_logs(days=90)
		self.assertFalse(frappe.db.exists("Stuck Usage", old.name))
		self.assertTrue(frappe.db.exists("Stuck Usage", open_row.name))


class TestAPullForOneUser(PullCase):
	email = "usage-one@grove.test"
	box = "usage-one-box"

	def test_every_key_they_hold_is_asked_for_from_every_store(self):
		keys = sorted([self.key(), self.key()])
		frappe.db.set_value("Grove API Key", keys[0], "status", "revoked")
		pull = usage.Usage(user=self.user)
		with unittest.mock.patch.object(usage, "gateway_units", return_value=["unit"]) as units:
			self.assertEqual(pull.units(), ["unit"])
		units.assert_called_once_with(None)
		self.assertEqual(sorted(pull.keys), keys)

	def test_a_user_with_no_keys_pulls_nothing(self):
		self.assertEqual(usage.Usage(user=self.grove_user("usage-one-nokeys@grove.test")).units(), [])


class TestADrainIsAcknowledgedOnceRecorded(unittest.TestCase):
	"""Pure: the pull and the box are fakes."""

	def land(self, drains, record=(1, {"d1": ["k"]}, [], 0), ack=None):
		row = {"server": "gw1", "success": 1, "duration_ms": 0, "drains": drains, "dead": []}
		target = unittest.mock.Mock(error=None)
		target.post.side_effect = ack
		with (
			unittest.mock.patch.object(usage, "record_drains", side_effect=record if isinstance(record, Exception) else [record]) as recorded,
			unittest.mock.patch.object(Target, "resolve", return_value=target),
			unittest.mock.patch.object(frappe, "db", frappe._dict(commit=lambda: None, rollback=lambda: None)),
		):
			outcome, [row] = usage.record_drain(True, [row])
		self.recorded = recorded
		return outcome, row, target.post

	def test_what_landed_is_acknowledged_by_drain_and_key(self):
		outcome, row, post = self.land({"d1": {"k": {"request_count": "1"}}})
		post.assert_called_once_with("usage/ack", {"acks": {"d1": ["k"]}, "dead": []})
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
		post.assert_called_once_with("usage/ack", {"acks": {}, "dead": ["r1"]})

	def test_a_failed_ack_loses_nothing_and_says_so(self):
		outcome, row, _post = self.land({"d1": {"k": {}}}, ack=ConnectionError("box gone"))
		self.assertEqual((outcome, row["success"]), (True, 1))
		self.assertIn("ack failed", row["detail"])

	def test_the_sync_row_says_how_many_users_are_stuck(self):
		_outcome, row, _post = self.land({"d1": {"k": {}}}, record=(3, {"d1": ["a"]}, [], 1))
		self.assertEqual(row["detail"], "pulled:3 stuck:1")


class TestAStoreIsPulledThroughOneWriter(unittest.TestCase):
	"""Every gateway on a store shares its counters, so the pull drains a store once, through the
	first of its writers that answers. Pure: the run doc and the pull are fakes."""

	def pull(self, groups, succeeds, record=None, fetch=None, **kwargs):
		doc, pulled = unittest.mock.MagicMock(), []
		doc.acquire_lock.return_value = True
		doc.results = []
		doc.append.side_effect = lambda _field, row: doc.results.append(row)
		self.doc = doc

		def fetch_one(target):
			pulled.append(target.name)
			return {"success": int(target.name in succeeds), "reachable": 1, "duration_ms": 0,
			        "drains": {"d1": {"key": {"request_count": "1"}}}}

		with (
			unittest.mock.patch.object(run, "new_run", return_value=doc),
			unittest.mock.patch.object(run, "sync_targets", return_value=groups),
			unittest.mock.patch.object(
				Target, "resolve", side_effect=lambda kind, name: Target(kind, name, "http://x", "t")
			),
			unittest.mock.patch.object(usage, "fetch_usage", side_effect=lambda target, _keys: (fetch or fetch_one)(target)),
			unittest.mock.patch.object(usage, "record_drains", side_effect=record or (lambda *_, **__: (1, {}, [], 0))),
			unittest.mock.patch.object(run, "finalize") as finalize,
			unittest.mock.patch.object(frappe, "db", frappe._dict(commit=lambda: None, rollback=lambda: None)),
		):
			usage.pull_all(**kwargs)
		return pulled, finalize.call_args.args[1:]

	def test_a_named_gateway_is_pulled_itself_whatever_store_it_is_on(self):
		pulled, counts = self.pull([("store1", ["gw1"])], succeeds={"gw9"}, gateways=["gw9"])
		self.assertEqual((pulled, counts), (["gw9"], (1, 1)))

	def test_the_next_writer_is_tried_when_one_fails_and_none_after_a_success(self):
		pulled, counts = self.pull([(None, ["gw0"]), ("store1", ["gw1", "gw2", "gw3"])], succeeds={"gw0", "gw2"})
		# The two groups run side by side; only a store's own writers are ordered.
		self.assertEqual(sorted(pulled), ["gw0", "gw1", "gw2"])
		self.assertLess(pulled.index("gw1"), pulled.index("gw2"))
		self.assertEqual(counts, (2, 2))

	def test_a_store_with_no_writer_is_a_failed_target(self):
		pulled, counts = self.pull([("store1", [])], succeeds=set())
		self.assertEqual((pulled, counts), ([], (1, 0)))

	def test_rows_land_in_the_order_asked_whichever_store_answers_first(self):
		first_may_answer = threading.Event()

		def fetch(target):
			if target.name == "gw1":
				first_may_answer.wait(5)
			else:
				first_may_answer.set()
			return {"success": 1, "reachable": 1, "duration_ms": 0, "drains": {}}

		self.pull([(None, ["gw1"]), (None, ["gw2"])], succeeds=set(), fetch=fetch)
		self.assertEqual([row["server"] for row in self.doc.results], ["gw1", "gw2"])

	def test_a_drain_that_cannot_be_recorded_fails_its_own_row(self):
		def record(gateway, _drains, _dead, **_):
			if gateway == "gw1":
				raise RuntimeError("disk on fire")
			return 4, {}, [], 0

		_pulled, counts = self.pull([(None, ["gw1"]), (None, ["gw2"])], succeeds={"gw1", "gw2"}, record=record)
		bad, good = self.doc.results
		self.assertEqual(counts, (2, 1))
		self.assertEqual((bad["success"], good["success"]), (0, 1))
		self.assertIn("disk on fire", bad["error"])
		self.assertEqual(good["detail"], "pulled:4")

	def test_the_drained_hashes_never_reach_the_row(self):
		self.pull([(None, ["gw1"])], succeeds={"gw1"})
		self.assertNotIn("drains", self.doc.results[0])
		self.assertNotIn("dead", self.doc.results[0])


class TestFetchUsageNeedsNoFrappe(unittest.TestCase):
	"""It runs on a pool thread, where there is no frappe.local to reach for."""

	def on_a_bare_thread(self, work):
		box = {}
		thread = threading.Thread(target=lambda: box.update(result=work()))
		thread.start()
		thread.join(5)
		return box["result"]

	def fetch(self, answer, keys=None):
		response = unittest.mock.Mock()
		response.json.return_value = answer
		with unittest.mock.patch("grove.pathway.run.requests.get", return_value=response) as get:
			row = self.on_a_bare_thread(lambda: usage.fetch_usage(Target("Gateway Server", "gw", "http://x", "t"), keys))
		return row, get.call_args.args[0]

	def test_drains_come_back_grouped_by_id(self):
		row, url = self.fetch({"drains": {"d1": {"k": {"request_count": "2"}}}})
		self.assertEqual((row["success"], row["drains"], url), (1, {"d1": {"k": {"request_count": "2"}}}, "http://x/usage"))

	def test_a_pull_for_one_user_names_their_keys(self):
		_row, url = self.fetch({"drains": {}}, keys=["k1", "k2"])
		self.assertEqual(url, "http://x/usage?keys=k1,k2")

	def test_dead_lines_ride_along(self):
		dead = [{"id": "r1", "line": "{}", "error": "x"}]
		row, _url = self.fetch({"drains": {}, "dead": dead})
		self.assertEqual(row["dead"], dead)

	def test_a_box_that_could_not_be_resolved_is_a_failed_row_not_a_call(self):
		with unittest.mock.patch("grove.pathway.run.requests.get") as get:
			row = self.on_a_bare_thread(
				lambda: usage.fetch_usage(Target("Gateway Server", "gw", error="no admin_url"))
			)
		get.assert_not_called()
		self.assertEqual((row["success"], row["error"]), (0, "no admin_url"))
