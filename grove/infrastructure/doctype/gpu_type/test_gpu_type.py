# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from grove.infrastructure.doctype.gpu_type.gpu_type import ensure_gpu_type

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestGPUType(IntegrationTestCase):
	def test_a_card_is_made_once_and_learns_what_later_sources_say(self):
		frappe.clear_messages()
		self.assertEqual("Probe X9", ensure_gpu_type("NVIDIA Probe X9"))
		self.assertEqual("Probe X9", ensure_gpu_type("Tesla Probe X9", vram_gb=24, compute_capability=8.9))
		self.assertEqual("Probe X9", ensure_gpu_type("Tesla Probe X9", vram_gb=48))
		doc = frappe.get_doc("GPU Type", "Probe X9")
		self.assertEqual((24, 8.9), (doc.vram_gb, doc.compute_capability))
		self.assertEqual(["NVIDIA Probe X9", "Tesla Probe X9"], [row.alias for row in doc.aliases])
		self.assertEqual([], frappe.get_message_log())
