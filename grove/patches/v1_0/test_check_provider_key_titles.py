from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.model_provider.test_model_provider import add_key, keyed_provider
from grove.patches.v1_0.check_provider_key_titles import execute

TABLE = "tabModel Provider Key"


def forget_columns():
	frappe.client_cache.delete_value(f"table_columns::{TABLE}")


def make_child_row(key, provider):
	"""Put a key back in the old shape: a child row naming its provider in `parent` alone."""
	table = frappe.qb.DocType("Model Provider Key")
	frappe.qb.update(table).set(table.model_provider, None).set(table.parent, provider).where(
		table.name == key.name
	).run()


class TestTheKeyTitleCheckBeforeSync(IntegrationTestCase):
	"""Runs as on a site whose keys are still child rows: a `parent` column is added if missing."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.added_parent = not frappe.db.has_column("Model Provider Key", "parent")
		if cls.added_parent:
			frappe.db.sql_ddl(f"alter table `{TABLE}` add column parent varchar(140)")
			forget_columns()

	@classmethod
	def tearDownClass(cls):
		if cls.added_parent:
			frappe.db.sql_ddl(f"alter table `{TABLE}` drop column parent")
			forget_columns()
		super().tearDownClass()

	def tearDown(self):
		frappe.db.rollback()

	def test_twins_among_child_rows_stop_and_are_named(self):
		vendor = keyed_provider("_T-probe-child-twins", base_url="https://api.probe.test")
		make_child_row(
			frappe.get_last_doc("Model Provider Key", {"model_provider": vendor.name}), vendor.name
		)
		make_child_row(add_key(vendor, "k2"), vendor.name)
		with self.assertRaises(frappe.ValidationError) as raised:
			execute()
		self.assertIn(f"_T-probe-child-twins ({vendor.name}), title key: 2 keys", str(raised.exception))

	def test_a_site_without_twins_passes(self):
		vendor = keyed_provider("_T-probe-child-single", base_url="https://api.probe.test")
		make_child_row(add_key(vendor, "k2", title="second"), vendor.name)
		execute()

	def test_a_site_without_the_columns_passes(self):
		with patch.object(frappe.db, "has_column", return_value=False):
			execute()
