# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""A model a third party serves, and what the gateway is told to ask it for.

Two things are being pinned. A vendor-backed model gets a route with no engine behind it — there is
nothing to deploy, so the provider record IS the placement. And `upstream_model` says what goes in
the body: blank for everything we run, because an engine is started under the full Grove id and
rewriting it would ask for a name no engine answers to.

Pure — the rows are faked, no site.
"""

import unittest
from unittest.mock import patch

import frappe

from grove.tests.model_rows import modality_rows, model_row, placement

MODELS = [
	model_row("frappe/qwen3-8b", model_id="qwen3-8b"),
	model_row(
		"anthropic/claude-4-5",
		"anthropic",
		model_id="claude-4-5",
		upstream_model_id="claude-sonnet-4-5-20250929",
	),
	model_row("anthropic/claude-haiku", "anthropic", model_id="claude-haiku"),
	model_row(
		"frappe/nemo-asr",
		model_id="nemo-asr",
		upstream_model_id="test-nemo-asr",
		input_modalities=["audio"],
		output_modalities=["transcription"],
	),
	model_row("anthropic/claude-draft", "anthropic", model_id="claude-draft", published=0),
	model_row("deepseek/deepseek-chat", "deepseek", model_id="deepseek-chat"),
	model_row("dual/mix-1", "dual", model_id="mix-1"),
	model_row("kimi/k2", "kimi", model_id="k2"),
	model_row("kimi-eu/k2", "kimi", "eu", model_key="kimi/k2", model_id="k2", upstream_model_id="k2-eu"),
]
PROVIDERS = {
	# `keys` becomes the Keys table: (row id, secret[, disabled]) rows, a blank secret standing for
	# a row whose password was never set.
	"frappe": {"base_url": None, "api_version": None, "keys": []},
	"anthropic": {
		"anthropic_base_url": "https://api.anthropic.com",
		"api_version": "2023-06-01",
		"keys": [("k-anthropic-1", "vendor-key")],
	},
	"deepseek": {
		"base_url": "https://api.deepseek.com",
		"api_version": "",
		"keys": [("k-deepseek-1", "ds-key"), ("k-deepseek-2", "ds-key-2"), ("k-deepseek-3", "ds-key-3", 1)],
	},
	"dual": {
		"base_url": "https://api.dual.test",
		"anthropic_base_url": "https://api.dual.test",
		"api_version": "",
		"keys": [("k-dual-1", "dual-key")],
	},
	"kimi": {
		"base_url": "https://api.kimi.test",
		"api_version": "",
		"keys": [("k-kimi-1", "kimi-key")],
		"anthropic_base_url": "https://api.kimi.test/anthropic",
	},
	"halfway": {"base_url": "https://api.halfway.test", "api_version": "", "keys": [("k-halfway-1", "")]},
}
DEPLOYMENTS = [
	placement(
		"frappe/qwen3-8b",
		name="MD-1",
		engine_url="https://203.0.113.1/e/md-1",
		status="Active",
		inference_server="INF-direct",
		max_num_seqs=4,
	),
]
PODS = [placement("frappe/nemo-asr", name="POD-1", engine_url="http://1.2.3.4:8081", max_num_seqs=2)]
# What a vendor would run on its own side, as the operator listed it; out of order on purpose.
DENIED_TOOLS = [
	{"provider_name": "anthropic", "tool": "web_search_20250305"},
	{"provider_name": "anthropic", "tool": "mcp_servers"},
	{"provider_name": "nobody", "tool": "web_search_options"},
]


class FakeQuery:
	def __call__(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		rows = {
			"Model": MODELS,
			"Model Modality Row": modality_rows(MODELS),
			"Model Replica": DEPLOYMENTS,
			"Pod": PODS,
			"Model Provider": [{"name": name} for name in PROVIDERS],
			"Inference Server": [{"name": "INF-direct", "ingress": None}],
			"Denied Tool": DENIED_TOOLS,
		}.get(doctype, [])
		if pluck:
			return [r[pluck] for r in rows]
		return [frappe._dict(r) for r in rows]


def fake_key_row(row_id, secret, disabled=0):
	row = frappe._dict(name=row_id, api_key="*****" if secret else None, disabled=disabled)
	row.get_password = lambda *a, **k: secret or None
	return row


def fake_cached_doc(doctype, name):
	provider = frappe._dict(PROVIDERS[name], provider_name=name)
	provider.api_keys = [fake_key_row(*pair) for pair in PROVIDERS[name]["keys"]]
	return provider


def routes():
	from grove.pathway import routes

	with (
		patch.object(frappe, "get_all", side_effect=FakeQuery()),
		patch("grove.pathway.routes.published_routes", side_effect=lambda table, models: table),
		patch.object(frappe, "get_cached_doc", side_effect=fake_cached_doc),
		patch.object(
			frappe, "db", frappe._dict(get_value=lambda *args: "", get_single_value=lambda *args: "in")
		),
		patch.object(
			frappe,
			"get_doc",
			side_effect=lambda *a, **k: frappe._dict(get_password=lambda *a, **k: "secret"),
		),
	):
		return routes.gateway_routes("in")


class TestAVendorModelIsRoutable(unittest.TestCase):
	def test_it_gets_one_row_naming_the_vendor(self):
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["kind"], "provider")
		self.assertEqual(row["engine_url"], "https://api.anthropic.com")
		self.assertEqual(row["internal_key"], "")
		self.assertEqual(row["credentials"], [{"id": "k-anthropic-1", "secret": "vendor-key"}])
		self.assertEqual(row["key_selection"], "round_robin")
		self.assertEqual(row["api_version"], "2023-06-01")
		self.assertEqual(row["dialect"], "anthropic")

	def test_it_carries_the_vendors_denied_tools_sorted_and_a_blank_list_when_none(self):
		table = routes()
		[row] = table["anthropic/claude-4-5"]
		self.assertEqual(row["denied_tools"], ["mcp_servers", "web_search_20250305"])
		[row] = table["deepseek/deepseek-chat"]
		self.assertEqual(row["denied_tools"], [])

	def test_each_url_field_names_its_own_dialect(self):
		[row] = routes()["deepseek/deepseek-chat"]
		self.assertEqual(row["dialect"], "openai")
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["dialect"], "anthropic")

	def test_one_url_answering_both_shapes_is_the_same_address_twice(self):
		rows = routes()["dual/mix-1"]
		self.assertEqual({r["engine_url"] for r in rows}, {"https://api.dual.test"})
		self.assertEqual({r["dialect"] for r in rows}, {"openai", "anthropic"})

	def test_a_dual_front_vendor_gets_one_row_per_front(self):
		rows = routes()["kimi/k2"]
		self.assertEqual(
			{(r["engine_url"], r["dialect"]) for r in rows},
			{("https://api.kimi.test", "openai"), ("https://api.kimi.test/anthropic", "anthropic")},
		)
		for row in rows:
			self.assertEqual(row["credentials"], [{"id": "k-kimi-1", "secret": "kimi-key"}])
			self.assertEqual(row["upstream_model"], "k2")
			self.assertEqual(row["vendor"], "kimi")

	def test_another_geographys_doc_of_the_same_key_is_not_routed_here(self):
		rows = routes()["kimi/k2"]
		self.assertEqual(len(rows), 2)
		self.assertNotIn("k2-eu", str(rows))

	def test_an_engine_row_pushes_no_dialect(self):
		for rows in routes().values():
			for row in rows:
				if row["kind"] != "provider":
					self.assertNotIn("dialect", row)

	def test_the_provider_is_the_vendor_not_a_placement(self):
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["vendor"], "anthropic")
		self.assertEqual((row["deployment"], row["server"]), ("", ""))

	def test_it_claims_no_capacity(self):
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["capacity"], 0)

	def test_an_unpublished_vendor_model_has_no_route(self):
		self.assertNotIn("anthropic/claude-draft", routes())

	def test_an_endpoint_with_no_key_is_not_a_route(self):
		for rows in routes().values():
			for row in rows:
				self.assertNotIn("halfway", str(row))

	def test_every_enabled_key_goes_under_its_rows_id(self):
		[row] = routes()["deepseek/deepseek-chat"]
		self.assertEqual(
			row["credentials"],
			[{"id": "k-deepseek-1", "secret": "ds-key"}, {"id": "k-deepseek-2", "secret": "ds-key-2"}],
		)


class TestWhichSurfacesReachAModel(unittest.TestCase):
	def dialects(self):
		from grove.pathway import routes

		models = [frappe._dict(m, provider=m["provider_name"]) for m in MODELS]
		query = FakeQuery()

		def get_all(doctype, filters=None, *args, **kwargs):
			if doctype == "Model Provider" and filters == {"is_self_hosted": 1}:
				return ["frappe"]
			return query(doctype, filters, *args, **kwargs)

		with (
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(frappe, "get_cached_doc", side_effect=fake_cached_doc),
		):
			return routes.get_dialects(models, "in")

	def test_our_chat_engines_answer_on_both(self):
		self.assertEqual(self.dialects()["frappe/qwen3-8b"], ["openai", "anthropic"])

	def test_our_other_engines_answer_on_openai_only(self):
		self.assertEqual(self.dialects()["frappe/nemo-asr"], ["openai"])

	def test_a_vendor_answers_on_the_fronts_it_runs(self):
		dialects = self.dialects()
		self.assertEqual(dialects["anthropic/claude-4-5"], ["anthropic"])
		self.assertEqual(dialects["deepseek/deepseek-chat"], ["openai"])
		self.assertEqual(dialects["kimi/k2"], ["openai", "anthropic"])


class TestWhatTheUpstreamIsAskedFor(unittest.TestCase):
	def test_our_own_engines_are_asked_for_the_id_they_serve(self):
		[row] = routes()["frappe/qwen3-8b"]
		self.assertEqual(row["upstream_model"], "")

	def test_a_vendor_gets_the_exact_string_it_was_given(self):
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["upstream_model"], "claude-sonnet-4-5-20250929")

	def test_a_vendor_without_an_override_gets_the_bare_id(self):
		[row] = routes()["anthropic/claude-haiku"]
		self.assertEqual(row["upstream_model"], "claude-haiku")

	def test_an_override_reaches_a_local_pod_too(self):
		[row] = routes()["frappe/nemo-asr"]
		self.assertEqual(row["kind"], "direct")
		self.assertEqual(row["upstream_model"], "test-nemo-asr")
