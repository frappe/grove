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

from grove.tests.model_rows import model_row, placement

MODELS = [
	# Ours, hosted. The engine answers to frappe/qwen3-8b, so nothing may be rewritten.
	model_row("frappe/qwen3-8b", model_id="qwen3-8b", modality="text"),
	# A vendor, taking the id it knows itself by.
	model_row("anthropic/claude-4-5", "anthropic", model_id="claude-4-5",
	          upstream_model_id="claude-sonnet-4-5-20250929", modality="text"),
	# A vendor with no override: the bare id is the best guess at its namespace.
	model_row("anthropic/claude-haiku", "anthropic", model_id="claude-haiku", modality="text"),
	# Ours, but the container advertises its own name — the one local case that rewrites.
	model_row("frappe/nemo-asr", model_id="nemo-asr", upstream_model_id="test-nemo-asr", modality="audio"),
	# A vendor model nothing has published yet: no route at all.
	model_row("anthropic/claude-draft", "anthropic", model_id="claude-draft", published=0, modality="text"),
	# A vendor that speaks the other dialect.
	model_row("deepseek/deepseek-chat", "deepseek", model_id="deepseek-chat", modality="text"),
	# A vendor with no dialect set: claims both shapes at its base URL.
	model_row("dual/mix-1", "dual", model_id="mix-1", modality="text"),
	# A vendor running two fronts, one per dialect, under one provider record.
	model_row("kimi/k2", "kimi", model_id="k2", modality="text"),
	# The same vendor model under another geography's record: a doc of its own, not routed here.
	model_row("kimi-eu/k2", "kimi", "eu", model_key="kimi/k2", model_id="k2",
	          upstream_model_id="k2-eu", modality="text"),
]
PROVIDERS = {
	# The URL fields are the dialect declaration; there is no dialect field to keep in step.
	# `keys` becomes the Keys table: (row id, secret) pairs, a blank secret standing for a row whose
	# password was never set.
	"frappe": {"base_url": None, "api_version": None, "keys": []},
	"anthropic": {"anthropic_base_url": "https://api.anthropic.com",
	              "api_version": "2023-06-01", "keys": [("k-anthropic-1", "vendor-key")]},
	"deepseek": {"base_url": "https://api.deepseek.com", "api_version": "",
	             "keys": [("k-deepseek-1", "ds-key"), ("k-deepseek-2", "ds-key-2")]},
	# One URL genuinely answering both shapes: the same address in both fields.
	"dual": {"base_url": "https://api.dual.test",
	         "anthropic_base_url": "https://api.dual.test", "api_version": "",
	         "keys": [("k-dual-1", "dual-key")]},
	"kimi": {"base_url": "https://api.kimi.test", "api_version": "", "keys": [("k-kimi-1", "kimi-key")],
	         "anthropic_base_url": "https://api.kimi.test/anthropic"},
	# An address with no key is not a route — half a provider dials nothing. A row with no
	# password set is no key either.
	"halfway": {"base_url": "https://api.halfway.test", "api_version": "", "keys": [("k-halfway-1", "")]},
}
DEPLOYMENTS = [
	placement("frappe/qwen3-8b", name="MD-1", engine_url="https://203.0.113.1/e/md-1",
	          status="Active", inference_server="INF-direct", max_num_seqs=4),
]
PODS = [placement("frappe/nemo-asr", name="POD-1", engine_url="http://1.2.3.4:8081", max_num_seqs=2)]


class FakeQuery:
	def __call__(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		rows = {
			"Model": MODELS,
			"Model Replica": DEPLOYMENTS,
			"Pod": PODS,
			"Model Provider": [{"name": name} for name in PROVIDERS],
			"Inference Server": [{"name": "INF-direct", "ingress": None}],
		}.get(doctype, [])
		if pluck:
			return [r[pluck] for r in rows]
		return [frappe._dict(r) for r in rows]


def fake_key_row(row_id, secret):
	row = frappe._dict(name=row_id, api_key="*****" if secret else None)
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
		patch.object(frappe, "db", frappe._dict(get_value=lambda *args: "", get_single_value=lambda *args: "in")),
		patch.object(
			frappe, "get_doc",
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

	def test_each_url_field_names_its_own_dialect(self):
		[row] = routes()["deepseek/deepseek-chat"]
		self.assertEqual(row["dialect"], "openai")
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["dialect"], "anthropic")

	def test_one_url_answering_both_shapes_is_the_same_address_twice(self):
		# No dialect field to say "both": a vendor like that fills both fields with one URL and
		# gets a row per surface, both dialling the same place.
		rows = routes()["dual/mix-1"]
		self.assertEqual({r["engine_url"] for r in rows}, {"https://api.dual.test"})
		self.assertEqual({r["dialect"] for r in rows}, {"openai", "anthropic"})

	def test_a_dual_front_vendor_gets_one_row_per_front(self):
		# One provider record, both surfaces reachable, each front dialled in its own shape.
		rows = routes()["kimi/k2"]
		self.assertEqual(
			{(r["engine_url"], r["dialect"]) for r in rows},
			{("https://api.kimi.test", "openai"),
			 ("https://api.kimi.test/anthropic", "anthropic")},
		)
		for row in rows:
			self.assertEqual(row["credentials"], [{"id": "k-kimi-1", "secret": "kimi-key"}])
			self.assertEqual(row["upstream_model"], "k2")
			self.assertEqual(row["deployment"], "kimi")

	def test_another_geographys_doc_of_the_same_key_is_not_routed_here(self):
		# kimi/k2 is two docs, one per provider record; this geography's table carries only its
		# own — two rows for its two fronts, the eu doc's upstream id nowhere.
		rows = routes()["kimi/k2"]
		self.assertEqual(len(rows), 2)
		self.assertNotIn("k2-eu", str(rows))

	def test_an_engine_row_pushes_no_dialect(self):
		# Blank means "both" on an engine; the gateway's blank rules depend on absence here.
		for rows in routes().values():
			for row in rows:
				if row["kind"] != "provider":
					self.assertNotIn("dialect", row)

	def test_the_provider_is_the_placement(self):
		# There is no deployment doc to name, and usage has to be attributable to something.
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["deployment"], "anthropic")
		self.assertEqual(row["server"], "anthropic")

	def test_it_claims_no_capacity(self):
		# We divide GPUs we own. A vendor's own 429 is the only cap there is.
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["capacity"], 0)

	def test_an_unpublished_vendor_model_has_no_route(self):
		self.assertNotIn("anthropic/claude-draft", routes())

	def test_an_endpoint_with_no_key_is_not_a_route(self):
		# Half a provider would push a row that 401s every request.
		for rows in routes().values():
			for row in rows:
				self.assertNotIn("halfway", str(row))

	def test_every_key_goes_under_its_rows_id(self):
		# The gateway picks among them per request; the id is what it rotates and counts by.
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
		# The engine's --served-model-name IS the prefixed id. Stripping it would be a 400.
		[row] = routes()["frappe/qwen3-8b"]
		self.assertEqual(row["upstream_model"], "")

	def test_a_vendor_gets_the_exact_string_it_was_given(self):
		[row] = routes()["anthropic/claude-4-5"]
		self.assertEqual(row["upstream_model"], "claude-sonnet-4-5-20250929")

	def test_a_vendor_without_an_override_gets_the_bare_id(self):
		# Our namespace is not theirs, so the prefix cannot go out.
		[row] = routes()["anthropic/claude-haiku"]
		self.assertEqual(row["upstream_model"], "claude-haiku")

	def test_an_override_reaches_a_local_pod_too(self):
		# A custom image advertises its own name; asking it for the Grove id is a 400.
		[row] = routes()["frappe/nemo-asr"]
		self.assertEqual(row["kind"], "direct")
		self.assertEqual(row["upstream_model"], "test-nemo-asr")
