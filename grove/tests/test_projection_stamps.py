# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""What bounds a projection tick: the stamp each store carries, the key buckets a save after it
moved, the keys those buckets hold, and when the stamp is written. Pure — the docs are mocked,
no site."""

import unittest
import unittest.mock
from datetime import datetime, timedelta

import frappe

from grove.pathway import projection, snapshot
from grove.pathway.run import Target, Unit

AT = datetime(2026, 8, 16, 0, 1)
STAMP = datetime(2026, 8, 16, 0, 0)
AA, BB = snapshot.bucket_of("aa"), snapshot.bucket_of("bb")  # "96" and "3b": two buckets


def gateway(name):
	return Target("Gateway Server", name, "http://x", "t")


class TestDirtyBuckets(unittest.TestCase):
	"""Which key buckets a tick rebuilds: a saved key's own, a saved team's every live key's."""

	def dirty(self, keys=(), teams=(), team_keys=()):
		self.asked = []

		def get_all(doctype, filters=None, pluck=None, **kwargs):
			self.asked.append((doctype, filters))
			if doctype == "Central Team":
				return list(teams)
			return list(team_keys) if "team" in filters else list(keys)

		with unittest.mock.patch.object(frappe, "get_all", side_effect=get_all):
			return snapshot.dirty_buckets(STAMP)

	def test_a_saved_key_dirties_its_bucket_revoked_or_not(self):
		self.assertEqual(self.dirty(keys=["aa"]), {AA})
		self.assertEqual(self.asked[0], ("Grove API Key", {"modified": (">", STAMP)}))

	def test_a_saved_team_dirties_its_live_keys_buckets(self):
		self.assertEqual(self.dirty(teams=["T-1"], team_keys=["aa", "bb"]), {AA, BB})
		self.assertEqual(self.asked[-1], ("Grove API Key", {"team": ("in", ["T-1"]), "status": "active"}))

	def test_a_quiet_minute_dirties_nothing_and_asks_no_team_for_its_keys(self):
		self.assertEqual(self.dirty(), set())
		self.assertEqual(len(self.asked), 2)

	def test_a_key_with_no_hash_has_no_bucket(self):
		self.assertEqual(self.dirty(keys=[None]), set())


class TestNeedsFull(unittest.TestCase):
	"""A Tier or Grove Settings save reaches keys it does not name: every bucket is rebuilt."""

	def needs(self, tier_saved=False, settings=None):
		db = frappe._dict(
			exists=lambda doctype, filters: "Basic" if tier_saved else None,
			get_single_value=lambda doctype, field: settings,
		)
		with unittest.mock.patch.object(frappe, "db", db):
			return snapshot.needs_full(STAMP)

	def test_a_tier_save(self):
		self.assertTrue(self.needs(tier_saved=True))

	def test_a_settings_save_after_the_stamp(self):
		self.assertTrue(self.needs(settings="2026-08-16 00:00:30"))

	def test_not_an_older_settings_save_nor_none(self):
		self.assertFalse(self.needs(settings="2026-08-15 00:00:00"))
		self.assertFalse(self.needs())


class TestBucketNames(unittest.TestCase):
	"""The keys of the dirty buckets, picked by hashing every live key's hash."""

	def test_only_the_keys_of_the_labels_are_named(self):
		rows = [
			frappe._dict(name="K1", key_hash="aa"),
			frappe._dict(name="K2", key_hash="bb"),
			frappe._dict(name="K3", key_hash=None),
		]
		with unittest.mock.patch.object(frappe, "get_all", return_value=rows) as get_all:
			names = snapshot.bucket_names({AA})
		self.assertEqual(names, ["K1"])
		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": "active"})

	def test_no_labels_is_no_scan(self):
		with unittest.mock.patch.object(frappe, "get_all", side_effect=AssertionError("scan")):
			self.assertEqual(snapshot.bucket_names(set()), [])

	def test_the_section_is_every_key_without_labels_and_the_named_keys_with(self):
		with (
			unittest.mock.patch.object(snapshot, "bucket_names", return_value=["K1"]) as names,
			unittest.mock.patch.object(snapshot, "effective_keys", return_value=[]) as keys,
		):
			snapshot.keys_section()
			snapshot.keys_section({AA})
		self.assertEqual([call.args for call in keys.call_args_list], [(None,), (["K1"],)])
		names.assert_called_once_with({AA})


class TestBucketsInPlay(unittest.TestCase):
	"""Which buckets a tick rebuilds: those saved since the oldest stamp among the gateway units
	that answered, less the lookback — or every bucket, in the cases that need it all."""

	def in_play(self, units, remote, stamps=None, force=False, at=AT, dirty=frozenset(), full=False):
		tick = projection.Projection(force=force)
		tick.read_at = at
		tick.remote = remote
		stamps = stamps or {}
		self.since = []

		def dirty_buckets(since):
			self.since.append(since)
			return set(dirty)

		with (
			unittest.mock.patch.object(
				frappe, "db", frappe._dict(get_value=lambda doctype, name, field: stamps.get(name))
			),
			unittest.mock.patch.object(snapshot, "dirty_buckets", side_effect=dirty_buckets),
			unittest.mock.patch.object(snapshot, "needs_full", return_value=full),
		):
			return tick.buckets_in_play(units)

	def test_what_moved_since_the_stamp_less_the_lookback(self):
		gw1 = gateway("gw1")
		labels = self.in_play(
			[Unit("store1", (gw1,))], {gw1: {"keys:96": "h"}}, {"store1": STAMP}, dirty={AA}
		)
		self.assertEqual(labels, {AA})
		self.assertEqual(self.since, [STAMP - timedelta(seconds=projection.LOOKBACK)])

	def test_the_oldest_stamp_among_the_units_that_answered_wins(self):
		gw1, gw2, gw3 = gateway("gw1"), gateway("gw2"), gateway("gw3")
		older = STAMP - timedelta(minutes=5)
		units = [Unit("store1", (gw1,)), Unit(None, (gw2,)), Unit(None, (gw3,))]
		remote = {gw1: {"keys:96": "h"}, gw2: {"keys:96": "h"}, gw3: ConnectionError("down")}
		self.in_play(units, remote, {"store1": STAMP, "gw2": older, "gw3": older - timedelta(days=1)})
		self.assertEqual(self.since, [older - timedelta(seconds=projection.LOOKBACK)])

	def test_a_store_is_judged_by_whichever_writer_answered(self):
		gw1, gw2 = gateway("gw1"), gateway("gw2")
		remote = {gw1: ConnectionError("down"), gw2: {"keys:96": "h"}}
		self.assertEqual(self.in_play([Unit("store1", (gw1, gw2))], remote, {"store1": STAMP}), set())
		self.assertEqual(len(self.since), 1)

	def test_no_gateway_answering_builds_nothing(self):
		gw1 = gateway("gw1")
		self.assertEqual(self.in_play([Unit(None, (gw1,))], {gw1: ConnectionError("down")}), set())
		self.assertEqual(self.since, [])

	def test_a_unit_with_no_stamp_rebuilds_everything(self):
		gw1 = gateway("gw1")
		self.assertIsNone(self.in_play([Unit(None, (gw1,))], {gw1: {"keys:96": "h"}}))

	def test_a_box_holding_no_key_hash_rebuilds_everything(self):
		gw1 = gateway("gw1")
		self.assertIsNone(self.in_play([Unit(None, (gw1,))], {gw1: {"groups": "g"}}, {"gw1": STAMP}))

	def test_force_and_the_full_minute_rebuild_everything_without_a_read(self):
		gw1 = gateway("gw1")
		remote, stamps = {gw1: {"keys:96": "h"}}, {"gw1": STAMP}
		self.assertIsNone(self.in_play([Unit(None, (gw1,))], remote, stamps, force=True))
		self.assertIsNone(self.in_play([Unit(None, (gw1,))], remote, stamps, at=datetime(2026, 8, 16, 0, 10)))
		self.assertEqual(self.since, [])

	def test_a_tier_or_settings_save_rebuilds_everything(self):
		gw1 = gateway("gw1")
		self.assertIsNone(
			self.in_play([Unit(None, (gw1,))], {gw1: {"keys:96": "h"}}, {"gw1": STAMP}, full=True)
		)

	def test_past_the_ceiling_every_bucket_is_cheaper(self):
		gw1 = gateway("gw1")
		many = {f"{i:02x}" for i in range(projection.FULL_ABOVE + 1)}
		self.assertIsNone(
			self.in_play([Unit(None, (gw1,))], {gw1: {"keys:96": "h"}}, {"gw1": STAMP}, dirty=many)
		)

	def test_an_ingress_has_no_say(self):
		ingress = Target("Ingress Server", "ing1", "http://x", "t")
		self.assertEqual(self.in_play([Unit(None, (ingress,))], {ingress: {"routes": "r"}}), set())


class TestStamp(unittest.TestCase):
	"""Where a unit's stamp lives and when it is written."""

	def stamp(self, unit, outcome):
		writes = []
		db = frappe._dict(set_value=lambda *args, **kwargs: writes.append((args, kwargs)))
		with unittest.mock.patch.object(frappe, "db", db):
			written = projection.stamp_synced(unit, outcome, AT)
		return written, writes

	def test_a_store_unit_stamps_the_store(self):
		written, writes = self.stamp(Unit("store1", (gateway("gw1"), gateway("gw2"))), True)
		self.assertTrue(written)
		self.assertEqual(
			writes, [(("Gateway Store", "store1", "last_synced_at", AT), {"update_modified": False})]
		)

	def test_a_lone_gateway_and_an_ingress_stamp_themselves(self):
		_written, [(args, _kwargs)] = self.stamp(Unit(None, (gateway("gw1"),)), None)
		self.assertEqual(args[:2], ("Gateway Server", "gw1"))
		_written, [(args, _kwargs)] = self.stamp(Unit(None, (Target("Ingress Server", "ing1"),)), True)
		self.assertEqual(args[:2], ("Ingress Server", "ing1"))

	def test_a_failed_unit_is_not_stamped(self):
		self.assertEqual(self.stamp(Unit("store1", ()), False), (False, []))


if __name__ == "__main__":
	unittest.main()
