# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""What a gateway in one Geography is given. Routes are built only from what runs inside it, and every
user goes to every gateway carrying the pin the gateway enforces. Pure — the rows are faked."""

import unittest
from unittest.mock import patch

import frappe

from grove.pathway import projection, run, snapshot
from grove.pathway.routes import gateway_routes
from grove.pathway.run import Target

ZONE = "grove.example.com"
MODELS = [
	{"name": "frappe/qwen3-8b", "model_id": "qwen3-8b", "provider_name": "frappe", "published": 1},
	{"name": "openai/gpt-5", "model_id": "gpt-5", "provider_name": "openai", "published": 1},
]
REPLICAS = [
	{"name": "MD-in", "model": "frappe/qwen3-8b", "engine_url": "https://203.0.113.1/e/md-in",
	 "inference_server": "INF-in", "geography": "in"},
	{"name": "MD-eu", "model": "frappe/qwen3-8b", "engine_url": "https://203.0.113.2/e/md-eu",
	 "inference_server": "INF-eu-behind", "geography": "eu"},
]
INGRESSES = [{"name": "eu-i1", "region": "eu-central-1", "geography": "eu"}]
SERVERS = [{"name": "INF-eu-behind", "ingress": "eu-i1"}]
PODS = [{"name": "POD-1", "model": "frappe/qwen3-8b", "engine_url": "http://1.2.3.4:8081"}]
# Every pod serves in this one, set on Grove Settings.
POD_GEOGRAPHY = "in"
# One vendor, a record per geography: the ids are hashes, the name is what they share.
PROVIDERS = {
	"frappe": {"provider_name": "frappe", "geography": None},
	"a1b2": {"provider_name": "openai", "base_url": "https://api.openai.com/v1", "api_key": "in-key",
	         "geography": "in"},
	"c3d4": {"provider_name": "openai", "base_url": "https://eu.api.openai.com/v1",
	         "api_key": "eu-key", "geography": "eu"},
}


def in_geography(rows, filters):
	if "geography" not in (filters or {}):
		return rows
	return [row for row in rows if row.get("geography") == filters["geography"]]


def get_all(doctype, filters=None, pluck=None, **kwargs):
	rows = {
		"Model": MODELS,
		"Model Replica": REPLICAS,
		"Ingress Server": INGRESSES,
		"Inference Server": SERVERS,
		"Pod": PODS,
		"Model Provider": [{"name": name, **fields} for name, fields in PROVIDERS.items()],
	}.get(doctype, [])
	rows = in_geography(rows, filters)
	if pluck:
		return [row[pluck] for row in rows]
	return [frappe._dict(row) for row in rows]


def cached_provider(doctype, name):
	provider = frappe._dict(PROVIDERS[name])
	provider.get_password = lambda *args, **kwargs: provider.api_key
	return provider


def routes(geography):
	with (
		patch.object(frappe, "get_all", side_effect=get_all),
		patch.object(frappe, "get_cached_doc", side_effect=cached_provider),
		patch("grove.pathway.routes.published_routes", side_effect=lambda table, models: table),
		patch.object(frappe, "db", frappe._dict(get_value=lambda *args: ZONE, get_single_value=lambda *args: POD_GEOGRAPHY)),
		patch.object(frappe, "get_doc", side_effect=lambda *a, **k: frappe._dict(get_password=lambda *a, **k: "secret")),
	):
		return gateway_routes(geography)


class TestRoutesStayInTheirGeography(unittest.TestCase):
	def test_a_replica_elsewhere_is_not_routed_to(self):
		rows = routes("in")["frappe/qwen3-8b"]
		self.assertEqual({row["deployment"] for row in rows}, {"MD-in", "POD-1"})

	def test_pods_are_routed_only_in_the_pod_geography(self):
		self.assertNotIn("POD-1", str(routes("eu")))

	def test_an_ingress_row_comes_only_from_its_own_geography(self):
		self.assertEqual({row["deployment"] for row in routes("eu")["frappe/qwen3-8b"]}, {"eu-i1"})
		self.assertNotIn("eu-i1", str(routes("in")))

	def test_one_id_is_dialled_through_each_geographys_own_record(self):
		[row] = routes("in")["openai/gpt-5"]
		self.assertEqual((row["engine_url"], row["internal_key"]), ("https://api.openai.com/v1", "in-key"))
		[row] = routes("eu")["openai/gpt-5"]
		self.assertEqual((row["engine_url"], row["internal_key"]), ("https://eu.api.openai.com/v1", "eu-key"))
		# Usage is attributed to the vendor, not to a record id.
		self.assertEqual(row["deployment"], "openai")

	def test_a_vendor_is_dialled_only_where_it_has_a_record(self):
		self.assertNotIn("openai/gpt-5", routes("us"))

	def test_a_gateway_with_no_geography_gets_no_routes(self):
		# Fail closed: blank must not match rows that were never given a geography.
		self.assertEqual(routes(""), {})


class TestEveryUserCarriesTheirPin(unittest.TestCase):
	def users(self, rows):
		def get_all(doctype, **kwargs):
			return [frappe._dict(row) for row in rows] if doctype == "Grove User" else []

		with (
			patch.object(snapshot, "model_rows", return_value={}),
			patch.object(snapshot, "group_rows", return_value={}),
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(snapshot, "allocations", return_value={}),
		):
			return {user["name"]: user for user in snapshot.effective_users()}

	def test_pinned_and_unpinned_users_both_reach_every_gateway(self):
		users = self.users([
			{"name": "u1", "user": "a@x.test", "credit_exhausted": 0, "log_payloads": 0, "geography": "eu"},
			{"name": "u2", "user": "b@x.test", "credit_exhausted": 0, "log_payloads": 0, "geography": None},
		])
		self.assertEqual(users["u1"]["geography"], "eu")
		# Blank, never null: the gateway reads absent and "" as unpinned.
		self.assertEqual(users["u2"]["geography"], "")


class TestOneSnapshotPerGeography(unittest.TestCase):
	"""One snapshot per geography: only the routes differ between them, and every store is pushed
	the same budget, so gateways on different Redises in one geography share it."""

	def test_each_geography_is_built_once_and_shared_by_its_gateways(self):
		built, pushed = [], []
		geographies = {"gw-in-1": "in", "gw-in-2": "in", "gw-in-3": "in", "gw-eu-1": "eu"}

		def build(geography, shared=None):
			built.append(geography)
			return {"geography": geography}

		def push_target(target, desired, force):
			pushed.append((target.name, desired["geography"]))
			return None

		doc = unittest.mock.Mock(results=[])
		doc.acquire_lock.return_value = True
		with (
			patch.object(run, "new_run", return_value=doc),
			patch.object(run, "sync_targets", return_value=[(None, [name]) for name in geographies]),
			patch.object(projection, "active_ingresses", return_value=[]),
			patch.object(snapshot, "gateway_geography", side_effect=geographies.get),
			patch.object(snapshot, "gateway_snapshot", side_effect=build),
			patch.object(Target, "resolve", side_effect=lambda kind, name: Target(kind, name, "u", "t")),
			patch.object(projection, "push_target", side_effect=push_target),
			patch.object(frappe, "db", frappe._dict(commit=lambda: None)),
		):
			projection.sync_projection()
		self.assertEqual(sorted(built), ["eu", "in"])
		self.assertEqual(sorted(pushed), [("gw-eu-1", "eu"), ("gw-in-1", "in"), ("gw-in-2", "in"), ("gw-in-3", "in")])


if __name__ == "__main__":
	unittest.main()
