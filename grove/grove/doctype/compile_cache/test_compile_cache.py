# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.compile_cache.compile_cache import _upsert

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []

KEY = ("ab12cd34ef56ab78", "NVIDIA_L40S", 2, "probe--upsert-7b")


class IntegrationTestCompileCache(IntegrationTestCase):
	def test_a_key_seen_again_updates_its_one_row(self):
		frappe.clear_messages()
		first = _upsert(*KEY, {"objects": 1, "bytes": 1_000_000}, "probe-bucket")
		again = _upsert(*KEY, {"objects": 3, "bytes": 2_500_000}, "probe-bucket")
		self.assertEqual(first.name, again.name)
		self.assertEqual(
			(3, 2.5), frappe.db.get_value("Compile Cache", first.name, ["objects", "size_mb"])
		)
		self.assertEqual([], frappe.get_message_log())
