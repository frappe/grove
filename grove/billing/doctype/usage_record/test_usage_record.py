# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Log Settings purges records past the retention a chunk at a time and keeps the ones a Credit
Discrepancy names."""

import unittest.mock

import frappe
from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days

from grove.billing.doctype.usage_record import usage_record
from grove.billing.doctype.usage_record.usage_record import UsageRecord
from grove.utils import utc_today


def a_record(day, drain_id):
	"""A bare row: the purge reads `day` and nothing else, so no key or team has to exist."""
	doc = frappe.get_doc({
		"doctype": "Usage Record", "api_key": "k", "team": "t", "day": day, "drain_id": drain_id,
		"request_count": 1, "cost": 0, "gateway_cost": 0, "usage": "[]",
	})
	doc.flags.ignore_links = True
	return doc.insert(ignore_permissions=True).name


class TestOldRecordsArePurged(IntegrationTestCase):
	def test_log_settings_clears_past_the_retention_and_keeps_evidence(self):
		self.assertTrue(_supports_log_clearing("Usage Record"))
		# A retention no real record on the site predates, so only these rows are in range.
		days = 9000
		cutoff = add_days(utc_today(), -days)
		old = add_days(cutoff, -1)
		gone = [a_record(old, "d-gone-1"), a_record(old, "d-gone-2")]
		evidence = a_record(old, "d-evidence")
		kept = a_record(cutoff, "d-kept")
		frappe.get_doc({"doctype": "Credit Discrepancy", "usage_record": evidence}).insert(ignore_permissions=True)

		# The commit between chunks is patched out so the test's rows roll back with it.
		with (
			unittest.mock.patch.object(usage_record, "PURGE_CHUNK", 1),
			unittest.mock.patch.object(frappe.db, "commit"),
			unittest.mock.patch.object(frappe.db, "delete", wraps=frappe.db.delete) as delete,
		):
			UsageRecord.clear_old_logs(days=days)

		self.assertFalse(any(frappe.db.exists("Usage Record", name) for name in gone))
		self.assertTrue(frappe.db.exists("Usage Record", evidence))
		self.assertTrue(frappe.db.exists("Usage Record", kept))
		self.assertEqual(delete.call_count, 2)
