# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.access import validate_model_geography


class ModelGroup(Document):
	"""A named set of models in one geography. Membership lives on Grove API Key, and a key reaches
	the union of every group it lists.

	The gateway holds this as its own Redis record and each member key names it, so an edit here
	is ONE push however many members the group has."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow

		description: DF.Data | None
		geography: DF.Link
		is_default: DF.Check
		models: DF.Table[GroveModelRow]
	# end: auto-generated types

	def before_validate(self):
		"""One geography per group: blank is the default one."""
		if not self.geography:
			self.geography = frappe.db.get_value("Geography", {"is_default": 1})

	def validate(self):
		# The name travels inside a comma-joined membership list, so a comma would split it into
		# two groups that resolve to nothing.
		if "," in self.name:
			frappe.throw("A group name cannot contain a comma")
		validate_model_geography(self.models, self.geography)

	def on_update(self):
		"""One default per geography: this one ticked unticks every other there."""
		if self.is_default:
			others = {"is_default": 1, "geography": self.geography, "name": ("!=", self.name)}
			frappe.db.set_value("Model Group", others, "is_default", 0)
