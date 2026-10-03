# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.model_provider.test_model_provider import our_model, provider, vendor_model


class IntegrationTestModelGroup(IntegrationTestCase):
	"""A group is one geography's: the vendor models it lists, and the default it can be."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.here, cls.away = make_test_geography("group-here"), make_test_geography("group-away")

	@staticmethod
	def group(name, geography, **fields):
		return frappe.get_doc({"doctype": "Model Group", "__newname": name, "geography": geography, **fields})

	def test_a_vendor_model_of_another_geography_is_refused_and_ours_is_not(self):
		vendor = provider("group-vendor", base_url="https://group.test/v1", api_key="k", geography=self.away).insert()
		theirs = vendor_model("group-big", vendor.name).insert().name
		ours = our_model("group-ours-7b").insert().name
		self.group("group-ours", self.here, models=[{"model": ours}]).insert()
		with self.assertRaises(frappe.ValidationError):
			self.group("group-theirs", self.here, models=[{"model": theirs}]).insert()
		self.group("group-theirs", self.away, models=[{"model": theirs}]).insert()

	def test_a_group_saved_without_one_is_in_the_default_geography(self):
		group = frappe.get_doc({"doctype": "Model Group", "__newname": "group-blank"}).insert()
		self.assertEqual(group.geography, frappe.db.get_value("Geography", {"is_default": 1}))

	def test_a_default_unticks_only_its_own_geography(self):
		self.group("group-default-first", self.here, is_default=1).insert()
		away = self.group("group-default-away", self.away, is_default=1).insert().name
		second = self.group("group-default-second", self.here, is_default=1).insert().name
		ticked = frappe.get_all(
			"Model Group", filters={"is_default": 1, "geography": ("in", [self.here, self.away])}, pluck="name"
		)
		self.assertEqual(sorted(ticked), sorted([away, second]))
