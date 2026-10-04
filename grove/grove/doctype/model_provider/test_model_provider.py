# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""One provider is ours — the one flagged Self Hosted — and every other one is a vendor.

Site-backed — the flag decides whether a model under a provider can be published or deployed at
all, and that is a real insert and a real db write.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.model_provider import model_provider
from grove.grove.doctype.model_provider.model_provider import self_hosted_provider


def provider(provider_name, api_key=None, **fields):
	"""`api_key` is one row of the Keys table — the single-key spelling most tests want."""
	if not fields.get("is_self_hosted"):
		fields.setdefault("geography", make_test_geography())
	if api_key:
		fields["api_keys"] = [{"api_key": api_key}]
	return frappe.get_doc({"doctype": "Model Provider", "provider_name": provider_name, **fields})


def vendor_model(model_id, provider):
	"""A model nobody hosts: no HF Repo anywhere, which is the point."""
	return frappe.get_doc(
		{"doctype": "Model", "model_id": model_id, "provider": provider}
	)


def our_model(model_id):
	"""A model under the Self Hosted provider, named there by leaving the provider blank."""
	return frappe.get_doc(
		{"doctype": "Model", "model_id": model_id, "hf_repo": "probe/Repo"}
	)


class TestWhatAVendorModelNeeds(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		"""One provider for the class: the rollback is once at the end, not between tests."""
		super().setUpClass()
		cls.vendor = provider("probe-vendor", base_url="https://api.probe.test", api_key="k").insert()

	def test_it_needs_no_hf_repo(self):
		# There is no engine to start, so there are no weights to name.
		doc = vendor_model("probe-sonnet", self.vendor.name).insert()
		self.assertFalse(doc.hf_repo)

	def test_it_is_not_published_until_someone_ticks_it(self):
		# Publishing is by hand, and refused while no pricing is enabled: a new vendor model is on
		# the catalog, not on sale.
		doc = vendor_model("probe-published", self.vendor.name).insert()
		self.assertFalse(doc.published)
		doc.published = 1
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_the_host_only_operations_refuse_it_by_name(self):
		# The form hides the buttons; these are reachable without one, and the errors underneath
		# are about a missing repo or a missing deployment — true, and no help at all.
		doc = vendor_model("probe-buttons", self.vendor.name).insert()
		for operation in (doc.fetch_architecture, doc.mirror_weights):
			with self.subTest(operation.__name__), self.assertRaises(frappe.ValidationError) as caught:
				operation()
			self.assertIn(self.vendor.provider_name, str(caught.exception))

	def test_nothing_of_ours_will_deploy_it(self):
		# The form filters it out of the picker; an API insert lands here, on validate's first
		# line, before a missing image or repo gets a turn.
		model = vendor_model("probe-undeployable", self.vendor.name).insert()
		for doc in (
			{"doctype": "Model Deployment", "model": model.name},
			{"doctype": "Pod", "name": "probe-vendor-pod", "model": model.name},
		):
			with self.subTest(doc["doctype"]), self.assertRaises(frappe.ValidationError) as caught:
				frappe.get_doc(doc).insert()
			self.assertIn(self.vendor.provider_name, str(caught.exception))

	def test_one_of_ours_still_needs_a_repo(self):
		with self.assertRaises(frappe.MandatoryError):
			vendor_model("probe-ours", self_hosted_provider()).insert()


class TestTheFlagSaysWhoIsOurs(IntegrationTestCase):
	def test_a_model_under_an_unflagged_provider_is_a_vendors(self):
		# No endpoint yet, but no engine of ours either: it owes no repo and stays dark until the
		# provider can be dialled.
		theirs = provider("probe-unflagged").insert()
		doc = vendor_model("probe-unflagged-model", theirs.name).insert()
		self.assertFalse(doc.published)
		self.assertFalse(doc.provider_is_self_hosted)

	def test_a_model_under_the_flagged_provider_is_ours(self):
		doc = our_model("probe-ours-mirror").insert()
		self.assertEqual(self_hosted_provider(), doc.provider)
		self.assertTrue(doc.provider_is_self_hosted)


class TestOneProviderIsSelfHosted(IntegrationTestCase):
	def test_a_second_one_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			provider("probe-second-ours", is_self_hosted=1).insert()
		self.assertIn(self_hosted_provider(), str(caught.exception))

	def test_it_dials_nothing(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			provider("probe-dialer", is_self_hosted=1, base_url="https://api.probe.test", api_key="k").insert()
		self.assertIn("Self Hosted", str(caught.exception))


class TestOneNameAcrossGeographies(IntegrationTestCase):
	"""A vendor is one record per geography under one name, so the model id is the same everywhere."""

	def test_a_second_geography_shares_the_name(self):
		first = provider("probe-shared").insert()
		second = provider("probe-shared", geography=make_test_geography("test-2")).insert()
		self.assertEqual(first.provider_name, second.provider_name)
		# The name is the prefix; the doc id says nothing.
		self.assertNotIn("probe-shared", (first.name, second.name))

	def test_a_geography_holds_one_record_per_name(self):
		provider("probe-twice").insert()
		with self.assertRaises(frappe.ValidationError) as caught:
			provider("probe-twice").insert()
		self.assertIn("probe-twice", str(caught.exception))

	def test_the_database_refuses_the_pair_too(self):
		# validate reads before it writes, so two inserts at once both pass it.
		provider("probe-raced").insert()
		twin = provider("probe-raced")
		twin.flags.ignore_validate = True
		with self.assertRaises(frappe.UniqueValidationError):
			twin.insert()

	def test_a_vendor_cannot_take_our_name(self):
		# The flag is read off whichever record a Model links, so one name cannot be both.
		ours = frappe.db.get_value("Model Provider", self_hosted_provider(), "provider_name")
		with self.assertRaises(frappe.ValidationError):
			provider(ours).insert()

	def test_a_model_moves_only_between_records_of_its_own_name(self):
		first = provider("probe-moves").insert()
		sibling = provider("probe-moves", geography=make_test_geography("test-2")).insert()
		stranger = provider("probe-stranger").insert()
		doc = vendor_model("probe-moved", first.name).insert()
		self.assertEqual("probe-moves/probe-moved", doc.model_key)
		doc.provider = sibling.name
		doc.save()
		# The name is inside the id every caller already sends.
		doc.provider = stranger.name
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()


class TestWithNoSelfHostedProvider(IntegrationTestCase):
	"""The flag cleared for the class; the rollback puts it back."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.set_value("Model Provider", self_hosted_provider(), "is_self_hosted", 0)

	def test_a_blank_provider_has_nothing_to_default_to(self):
		with self.assertRaises(frappe.ValidationError):
			our_model("probe-orphan").insert()

	def test_flagging_a_provider_reaches_the_models_under_it(self):
		ours = provider("probe-newly-ours").insert()
		doc = vendor_model("probe-follows-flag", ours.name).insert()
		self.assertFalse(doc.provider_is_self_hosted)
		ours.is_self_hosted = 1
		ours.save()
		self.assertTrue(frappe.db.get_value("Model", doc.name, "provider_is_self_hosted"))


class TestKeyStats(IntegrationTestCase):
	"""Counts come off the boxes; Grove only adds them up per key row and names a store that
	did not answer."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.vendor = provider(
			"probe-counted", base_url="https://api.counted.test",
			api_keys=[{"title": "first", "api_key": "k1"}, {"api_key": "k2"}],
		).insert()

	def stats(self, answers):
		"""`answers`: {unit label: {key id: counts} or None for a store nobody answered for}."""
		from grove.pathway.run import Target, Unit

		units = [Unit(label, (Target("Gateway Server", f"gw-{label}"),)) for label in answers]

		def fetch(target, ids):
			answer = answers[target.name.removeprefix("gw-")]
			if answer is None:
				return {"success": 0, "error": "down", "stats": {}}
			return {"success": 1, "stats": {key: counts for key, counts in answer.items() if key in ids}}

		with (
			patch.object(model_provider, "gateway_units", return_value=units) as units_of,
			patch.object(model_provider, "fetch_key_stats", side_effect=fetch),
		):
			result = self.vendor.key_stats()
		units_of.assert_called_once_with(geography=self.vendor.geography)
		return result

	def test_counts_add_up_across_stores_and_timestamps_take_the_latest(self):
		first, second = (row.name for row in self.vendor.api_keys)
		result = self.stats({
			"store-a": {first: {"requests": "3", "ok": "2", "rate_limited": "1", "last_used": "100", "last_rate_limited": "90"},
			            second: {"requests": "1", "rejected": "1", "last_used": "50"}},
			"store-b": {first: {"requests": "2", "ok": "2", "last_used": "80"}},
		})
		self.assertEqual(result["unreached"], [])
		self.assertEqual(
			result["keys"],
			[{"title": "first", "requests": 5, "ok": 4, "rate_limited": 1, "rejected": 0, "failed": 0,
			  "last_used": 100, "last_rate_limited": 90},
			 {"title": second, "requests": 1, "ok": 0, "rate_limited": 0, "rejected": 1, "failed": 0,
			  "last_used": 50, "last_rate_limited": 0}],
		)

	def test_a_store_nobody_answered_for_is_named_not_summed(self):
		first, _ = (row.name for row in self.vendor.api_keys)
		result = self.stats({"store-a": {first: {"requests": "4"}}, "store-b": None})
		self.assertEqual(result["unreached"], ["store-b"])
		self.assertEqual(result["keys"][0]["requests"], 4)


class TestFetchModels(IntegrationTestCase):
	"""The list comes off the vendor's own `/v1/models`; Grove splits it against what the record
	holds and turns the picked ids into Models."""

	def fetch(self, vendor, answers):
		"""`answers`: {url: one body, or the bodies of successive pages}. Returns (result, calls)."""
		calls = []

		def get(url, headers, params, timeout):
			calls.append((url, headers, dict(params)))
			pages = answers[url]
			body = pages.pop(0) if isinstance(pages, list) else pages
			return Mock(ok=True, status_code=200, json=lambda: body)

		with patch.object(model_provider.requests, "get", side_effect=get):
			return vendor.fetch_models(), calls

	def test_an_openai_front_is_asked_with_a_bearer(self):
		vendor = provider("probe-lister", base_url="https://api.lister.test/", api_key="k").insert()
		url = "https://api.lister.test/v1/models"
		result, calls = self.fetch(vendor, {url: {"object": "list", "data": [{"id": "b"}, {"id": "a"}]}})
		self.assertEqual(calls, [(url, {"Authorization": "Bearer k"}, {})])
		self.assertEqual(result, {"new": ["a", "b"], "held": []})

	def test_an_anthropic_front_is_asked_with_its_headers_and_paged(self):
		vendor = provider("probe-pager", anthropic_base_url="https://api.pager.test", api_key="k").insert()
		url = "https://api.pager.test/v1/models"
		result, calls = self.fetch(vendor, {url: [
			{"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"},
			{"data": [{"id": "claude-b"}], "has_more": False, "last_id": "claude-b"},
		]})
		headers = {"x-api-key": "k", "anthropic-version": "2023-06-01"}
		self.assertEqual(
			calls,
			[(url, headers, {"limit": 1000}), (url, headers, {"limit": 1000, "after_id": "claude-a"})],
		)
		self.assertEqual(result["new"], ["claude-a", "claude-b"])

	def test_a_dual_front_is_asked_on_its_openai_side_and_held_ids_are_set_apart(self):
		# DeepSeek's Anthropic shim has no /v1/models; the OpenAI front lists the same vendor.
		vendor = provider(
			"probe-dual-list", base_url="https://api.dual.test",
			anthropic_base_url="https://api.dual.test/anthropic", api_key="k",
		).insert()
		vendor_model("held-plain", vendor.name).insert()
		frappe.get_doc({
			"doctype": "Model", "provider": vendor.name, "model_id": "held-dated",
			"upstream_model_id": "Held-Dated-20250101",
		}).insert()
		listing = {"data": [{"id": "held-plain"}, {"id": "Held-Dated-20250101"}, {"id": "fresh"}]}
		result, calls = self.fetch(vendor, {"https://api.dual.test/v1/models": listing})
		self.assertEqual([url for url, _, _ in calls], ["https://api.dual.test/v1/models"])
		self.assertEqual(result, {"new": ["fresh"], "held": ["Held-Dated-20250101", "held-plain"]})

	def test_a_refusal_names_the_provider(self):
		vendor = provider("probe-refused", base_url="https://api.refused.test", api_key="k").insert()
		answer = Mock(ok=False, status_code=401, text="invalid key")
		with patch.object(model_provider.requests, "get", return_value=answer):
			with self.assertRaises(frappe.ValidationError) as caught:
				vendor.fetch_models()
		self.assertIn("probe-refused answered 401", str(caught.exception))

	def test_a_record_with_nothing_to_dial_is_refused(self):
		vendor = provider("probe-mute").insert()
		with self.assertRaises(frappe.ValidationError):
			vendor.fetch_models()

	def test_picked_ids_become_unpublished_models_under_the_record(self):
		vendor = provider("probe-adder", base_url="https://api.adder.test", api_key="k").insert()
		keys = vendor.add_models(["gpt-4o", "GPT 4o Mini", "org/Model"])
		self.assertEqual(keys, ["probe-adder/gpt-4o", "probe-adder/gpt-4o-mini", "probe-adder/org-model"])
		rows = frappe.get_all(
			"Model",
			filters={"provider": vendor.name},
			fields=["model_key", "upstream_model_id", "published"],
			order_by="model_key",
		)
		# The vendor's own spelling is kept only where ours differs from it.
		self.assertEqual(
			[(row.model_key, row.upstream_model_id or None, row.published) for row in rows],
			[
				("probe-adder/gpt-4o", None, 0),
				("probe-adder/gpt-4o-mini", "GPT 4o Mini", 0),
				("probe-adder/org-model", "org/Model", 0),
			],
		)
