import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.model_provider.test_model_provider import add_key, keyed_provider
from grove.grove.doctype.model_provider_key.model_provider_key import on_doctype_update
from grove.patches.v1_0.unique_provider_key_title import execute

INDEX = "unique_title_model_provider"


class TestTheKeyTitlePatch(IntegrationTestCase):
	"""Runs as on a site saved before the index: it is dropped for each test and put back after."""

	def setUp(self):
		if frappe.db.has_index("tabModel Provider Key", INDEX):
			frappe.db.sql_ddl(f"alter table `tabModel Provider Key` drop index {INDEX}")

	def tearDown(self):
		frappe.db.rollback()
		on_doctype_update()

	def test_a_site_without_twins_gets_the_index(self):
		execute()
		self.assertTrue(frappe.db.has_index("tabModel Provider Key", INDEX))

	def test_a_site_with_twins_stops_and_names_them(self):
		vendor = keyed_provider("_T-probe-twins", base_url="https://api.probe.test")
		add_key(vendor, "k2")
		with self.assertRaises(frappe.ValidationError) as raised:
			execute()
		twin = f"_T-probe-twins ({vendor.name}), title key: 2 keys"
		self.assertIn(twin, str(raised.exception))
		self.assertFalse(frappe.db.has_index("tabModel Provider Key", INDEX))
		self.assertEqual(frappe.db.count("Model Provider Key", {"model_provider": vendor.name}), 2)
