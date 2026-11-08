# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

# The two datum routes a region's boxes push to, and Central's name for the region that datum serves.
ENDPOINTS = ("metrics_url", "logs_url")
DATUM_FIELDS = ("central_region", *ENDPOINTS)


class Region(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		central_region: DF.Data | None
		cloud_provider: DF.Literal["", "runpod", "aws"]
		geography: DF.Link
		label: DF.Data | None
		logs_url: DF.Data | None
		metrics_url: DF.Data | None
	# end: auto-generated types

	"""A provider's region. Its Monitoring tab names the datum this region's boxes push to — two routes
	of one service, held as two fields so either can move alone — and the Central Region that datum
	belongs to, since Grove's region codes and Central's are not the same list."""

	def validate(self):
		for field in ENDPOINTS:
			url = self.get(field) or ""
			if url and not url.startswith(("http://", "https://")):
				frappe.throw(f"{self.meta.get_label(field)} must start with http:// or https://.")
		if self.is_new() or not self.has_value_changed("geography"):
			return
		for doctype in ("Network", "Machine"):
			if frappe.db.exists(doctype, {"region": self.name}):
				frappe.throw(
					f"A {doctype} is in {self.name}, and every box under it carries this geography — "
					"a region cannot move between geographies once it holds boxes."
				)

	@property
	def vector_problems(self):
		"""What stops a box here from shipping to datum: a blank field on the Monitoring tab, named."""
		return [
			f"Set {self.meta.get_label(field)} on Region {self.name}'s Monitoring tab."
			for field in DATUM_FIELDS
			if not self.get(field)
		]

	def gateways(self, exclude=None):
		"""Every gateway still in this region, by name."""
		filters = {"region": self.name, "status": ("!=", "Terminated")}
		if exclude:
			filters["name"] = ("!=", exclude)
		return frappe.get_all("Gateway Server", filters=filters, pluck="name")
