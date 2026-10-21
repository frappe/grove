"""Vendor Reconciliation sets Grove's per-day, per-model figures beside the vendor's own, read off
its billing API: a day that matches, a day the vendor saw more (another instance on the same key),
a model only the vendor saw, and the same summed per model over the range. The filter names the
vendor; every record of it is read, and one with no key, or with a key the fake Baseten has never
seen, adds nothing."""

from datetime import timedelta
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.billing import vendor
from grove.billing.report.vendor_reconciliation.vendor_reconciliation import execute
from grove.grove.doctype.model_provider.test_model_provider import provider
from grove.utils import utc_today

UPSTREAM = "probe-ai/Probe-Flash"
VENDOR_ONLY = "probe-ai/Probe-Only"
HEADERS = {"Authorization": "Bearer k"}
NOTHING = {"items": [], "pagination": {"has_more": False, "cursor": None}}


class TestVendorReconciliation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(patch("grove.pathway.usage.peek_team", return_value={}))
		cls.yesterday = utc_today() - timedelta(days=1)
		cls.earlier = cls.yesterday - timedelta(days=1)
		cls.vendor = provider("baseten", base_url="https://inference.baseten.co", api_key="k").insert()
		cls.model = frappe.get_doc(
			{
				"doctype": "Model",
				"provider": cls.vendor.name,
				"model_id": "probe-flash",
				"upstream_model_id": UPSTREAM,
			}
		).insert()
		cls.team = (
			frappe.get_doc({"doctype": "Grove Team", "__newname": "reconcile"})
			.insert(ignore_permissions=True)
			.name
		)
		cls.key = (
			frappe.get_doc({"doctype": "Grove API Key", "team": cls.team})
			.insert(ignore_permissions=True)
			.name
		)
		cls.record(cls.yesterday, requests=2, prompt=1000, cached=400, completion=100, cost=0.5)
		cls.record(cls.earlier, requests=3, prompt=3000, cached=0, completion=300, cost=1.0)
		cls.record(cls.earlier, requests=1, prompt=10, cached=0, completion=1, cost=0.01, drain="-small")

	@classmethod
	def record(cls, day, requests, prompt, cached, completion, cost, drain=""):
		entry = {
			"model": cls.model.model_key,
			"pricing": None,
			"requests": requests,
			"prompt_tokens": prompt,
			"cached_tokens": cached,
			"completion_tokens": completion,
			"grove_cost": cost,
		}
		frappe.get_doc(
			{
				"doctype": "Usage Record",
				"api_key": cls.key,
				"team": cls.team,
				"day": day,
				"drain_id": f"reconcile-{day}{drain}",
				"request_count": requests,
				"cost": cost,
				"usage": frappe.as_json([entry]),
			}
		).insert(ignore_permissions=True)

	def baseten(self):
		"""Baseten's two answers to the test key: usage over two pages joined by a cursor, cost in
		one. Any other key (a site's own baseten record) saw nothing."""
		bucket = lambda day: f"{day}T00:00:00Z"  # noqa: E731
		usage = {
			"items": [
				{
					"start_time": bucket(self.earlier),
					"results": [
						{
							"model": UPSTREAM,
							"request_count": 5,
							"input_tokens": 3010,
							"cached_input_tokens": 0,
							"output_tokens": 301,
						},
					],
				}
			],
			"pagination": {"has_more": True, "cursor": "page-2"},
		}
		usage_page_2 = {
			"items": [
				{
					"start_time": bucket(self.yesterday),
					"results": [
						{
							"model": UPSTREAM,
							"request_count": 2,
							"input_tokens": 1000,
							"cached_input_tokens": 400,
							"output_tokens": 100,
						},
						{
							"model": VENDOR_ONLY,
							"request_count": 7,
							"input_tokens": 70,
							"cached_input_tokens": 0,
							"output_tokens": 7,
						},
					],
				}
			],
			"pagination": {"has_more": False, "cursor": None},
		}
		cost = {
			"items": [
				{"date": str(self.earlier), "results": [{"model": UPSTREAM, "subtotal": "0.900000"}]},
				{
					"date": str(self.yesterday),
					"results": [
						{"model": UPSTREAM, "subtotal": "0.400000"},
						{"model": VENDOR_ONLY, "subtotal": "0.000070"},
					],
				},
			],
			"pagination": {"has_more": False, "cursor": None},
		}
		calls = []

		def get(url, headers, params, timeout):
			if headers != HEADERS:
				return Mock(ok=True, status_code=200, json=lambda: NOTHING)
			calls.append((url.rsplit("/", 1)[-1], params))
			if url.endswith("/v1/model_apis/usage"):
				body = usage_page_2 if "cursor" in params else usage
			else:
				body = cost
			return Mock(ok=True, status_code=200, json=lambda: body)

		return get, calls

	def report(self, to_date=None, days=3, provider="baseten", group_by="Day and Model"):
		get, calls = self.baseten()
		with patch.object(vendor.requests, "get", side_effect=get):
			columns, rows = execute(
				{
					"provider": provider,
					"group_by": group_by,
					"from_date": self.yesterday - timedelta(days=days),
					"to_date": to_date or self.yesterday,
				}
			)
		self.assertEqual("day" in [column["fieldname"] for column in columns], group_by == "Day and Model")
		return {(row.get("day"), row["model"]): row for row in rows}, calls

	def test_a_day_that_matches_and_the_margin(self):
		rows, calls = self.report()
		row = rows[(self.yesterday, self.model.model_key)]
		self.assertEqual((row["requests_grove"], row["requests_vendor"]), (2, 2))
		self.assertEqual((row["input_grove"], row["input_vendor"]), (1000, 1000))
		self.assertEqual((row["cached_grove"], row["cached_vendor"]), (400, 400))
		self.assertEqual((row["output_grove"], row["output_vendor"]), (100, 100))
		self.assertEqual((row["charged"], row["vendor_billed"], row["margin"]), (0.5, 0.4, 0.1))
		self.assertEqual([call[0] for call in calls], ["usage", "usage", "model_apis"])
		self.assertEqual(calls[1][1], {"cursor": "page-2"})

	def test_a_day_the_vendor_saw_more(self):
		rows, _ = self.report()
		row = rows[(self.earlier, self.model.model_key)]
		self.assertEqual((row["requests_grove"], row["requests_vendor"]), (4, 5))
		self.assertEqual((row["input_grove"], row["input_vendor"]), (3010, 3010))
		self.assertEqual((row["charged"], row["vendor_billed"], row["margin"]), (1.01, 0.9, 0.11))

	def test_a_model_only_the_vendor_saw(self):
		rows, _ = self.report()
		row = rows[(self.yesterday, VENDOR_ONLY)]
		self.assertIsNone(row.get("requests_grove"))
		self.assertEqual(
			(row["requests_vendor"], row["vendor_billed"], row["margin"]), (7, 0.00007, -0.00007)
		)

	def test_per_model_sums_the_days(self):
		rows, _ = self.report(group_by="Model")
		row = rows[(None, self.model.model_key)]
		self.assertEqual((row["requests_grove"], row["requests_vendor"]), (6, 7))
		self.assertEqual((row["input_grove"], row["input_vendor"]), (4010, 4010))
		self.assertEqual((row["charged"], row["vendor_billed"], row["margin"]), (1.51, 1.3, 0.21))
		self.assertEqual(rows[(None, VENDOR_ONLY)]["requests_vendor"], 7)

	def test_today_an_unread_vendor_and_an_unknown_name_are_refused(self):
		with self.assertRaises(frappe.ValidationError) as today:
			self.report(to_date=utc_today())
		self.assertIn("yesterday", str(today.exception))
		provider("probe-unbilled", base_url="https://api.unbilled.test", api_key="k").insert()
		with self.assertRaises(frappe.ValidationError) as unread:
			self.report(provider="probe-unbilled")
		self.assertIn("probe-unbilled", str(unread.exception))
		with self.assertRaises(frappe.ValidationError) as unknown:
			self.report(provider="probe-nobody")
		self.assertIn("probe-nobody", str(unknown.exception))


class TestABillingReadIsOncePerAccount(IntegrationTestCase):
	"""Baseten bills per account, so `vendor_figures` reads each account its keys name once: two
	keys of one account (a rotation) read once, two accounts in one record both read, and a key
	that names no account is an account of its own."""

	def test_each_account_the_keys_name_is_read_once(self):
		rows = [
			{"title": "a", "api_key": "k", "account": "acct-a"},
			{"title": "a-rotated", "api_key": "k-rotated", "account": "acct-a"},
			{"title": "b", "api_key": "k2", "account": "acct-b"},
			{"title": "own", "api_key": "k3"},
		]
		doc = provider("baseten", base_url="https://inference.baseten.co", api_keys=rows).insert()
		bearers = []

		def get(url, headers, params, timeout):
			bearers.append(headers["Authorization"])
			return Mock(ok=True, status_code=200, json=lambda: NOTHING)

		with patch.object(vendor.requests, "get", side_effect=get):
			vendor.vendor_figures([doc], utc_today() - timedelta(days=2), utc_today() - timedelta(days=1))
		self.assertEqual(sorted(set(bearers)), ["Bearer k", "Bearer k2", "Bearer k3"])
		self.assertEqual(len(bearers), 6, "the usage and the cost endpoint, once per account")
