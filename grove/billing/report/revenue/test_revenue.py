"""Revenue is what each billed drain was charged, summed by the database over the records'
per-model detail, cut by model, key, team or day."""

from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.billing.report.revenue.revenue import execute
from grove.utils import utc_today


class TestRevenue(IntegrationTestCase):
	"""1 USD billed today over two requests, 2 USD forty days ago over one."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_test_geography()
		cls.today = utc_today()
		cls.model = frappe.get_doc(
			{"doctype": "Model", "model_id": "revenue-7b", "hf_repo": "org/revenue-7b"}
		).insert(ignore_permissions=True).name
		cls.team = frappe.get_doc(
			{"doctype": "Central Team", "__newname": "revenue", "email": "revenue@grove.test", "free": 1}
		).insert(ignore_permissions=True).name
		cls.key = frappe.get_doc({"doctype": "Grove API Key", "team": cls.team}).insert(ignore_permissions=True).name
		cls.other_key = frappe.get_doc({"doctype": "Grove API Key", "team": cls.team}).insert(ignore_permissions=True).name
		cls.record(cls.key, cls.today, cost=1, requests=2)
		cls.record(cls.key, cls.today - timedelta(days=40), cost=2, requests=1)
		# Usage while the team was Free: recorded with its cost, never charged, so not revenue.
		cls.record(cls.key, cls.today, cost=5, requests=9, billed=0)

	@classmethod
	def record(cls, key, day, cost, requests, billed=1):
		entry = {"model": cls.model, "pricing": None, "requests": requests, "completion_tokens": 999, "grove_cost": cost}
		frappe.get_doc({
			"doctype": "Usage Record", "api_key": key, "team": cls.team, "day": day,
			"drain_id": f"revenue-{day}-{billed}", "billed": billed,
			"request_count": requests, "cost": cost, "usage": frappe.as_json([entry]),
		}).insert(ignore_permissions=True)

	def report(self, group_by="Model", days=30, **filters):
		_, rows = execute({
			"from_date": self.today - timedelta(days=days), "to_date": self.today, "group_by": group_by,
			"team": self.team, **filters,
		})
		return [(row["label"], row["requests"], row["revenue"]) for row in rows]

	def test_the_last_month_by_model(self):
		self.assertEqual(self.report(), [(self.model, 2, 1.0)])

	def test_by_key_team_and_day(self):
		self.assertEqual(self.report("API Key"), [(self.key, 2, 1.0)])
		self.assertEqual(self.report("Team"), [(self.team, 2, 1.0)])
		self.assertEqual(self.report("Day"), [(self.today, 2, 1.0)])

	def test_a_wider_range_adds_the_old_day(self):
		self.assertEqual(self.report(days=60), [(self.model, 3, 3.0)])
		self.assertEqual(
			self.report("Day", days=60), [(self.today - timedelta(days=40), 1, 2.0), (self.today, 2, 1.0)]
		)

	def test_a_key_filter_narrows(self):
		self.assertEqual(self.report(api_key=self.other_key), [])
		self.assertEqual(self.report("API Key", days=60, api_key=self.key), [(self.key, 3, 3.0)])
