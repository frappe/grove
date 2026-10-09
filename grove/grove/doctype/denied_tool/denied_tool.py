# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.grove.doctype.model_provider.model_provider import PROVIDER_NAME


class DeniedTool(Document):
	"""A tool a vendor would run on its own side, which the gateway refuses to send it.

	A vendor bills web search, web fetch, code execution or an MCP connector outside the token
	counts, and nothing meters that. Each row is one such name for one vendor: a `tools[].type`
	or a top-level request field. The rows ride every provider route row of the vendor as
	`denied_tools`, and a request that names one is a 400 at the gateway before the dial. Delete
	the row once the tool is priced, and it flows again. Keyed by Provider Name rather than a
	Model Provider link because a vendor has one record per Geography and the list is the
	vendor's, not the geography's."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		provider_name: DF.Data
		tool: DF.Data
	# end: auto-generated types

	def before_insert(self):
		# The name is built off both fields before validate runs, so the trim is here.
		self.provider_name = (self.provider_name or "").strip()
		self.tool = (self.tool or "").strip()

	def validate(self):
		if not PROVIDER_NAME.fullmatch(self.provider_name or ""):
			frappe.throw(
				f"Provider name {self.provider_name!r} must be lowercase letters, digits and single "
				"hyphens — the Provider Name of the vendor's records."
			)
		if not self.tool:
			frappe.throw("Tool names the tool type or request field the vendor would run.")
