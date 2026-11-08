# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.utils import slugify


class ModelID(Document):
	"""One model id (`deepseek-v4.1-flash`, `claude-sonnet-5-5`): what follows the provider's prefix
	in a Model Key. One record per id, whoever serves it; a Model under each provider links it.
	`display_name` is what a customer's dashboard shows for it; `available_models` falls back to
	the id."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		display_name: DF.Data | None
		model_id: DF.Data
	# end: auto-generated types

	def before_naming(self):
		"""'Claude Sonnet 4.5' → 'claude-sonnet-4.5', once: the name. A slash would split a key."""
		self.model_id = slugify(self.model_id)
		if not self.model_id:
			frappe.throw("No Model ID set")
		if "/" in self.model_id:
			frappe.throw("Model ID cannot contain '/'")


def ensure_model_id(model_id, display_name=None):
	"""The record's name for an id, inserted when the site lacks it. A display name given fills a
	blank one and never replaces what an operator set."""
	name = slugify(model_id)
	if not frappe.db.exists("Model ID", name):
		record = {"doctype": "Model ID", "model_id": model_id, "display_name": display_name}
		return frappe.get_doc(record).insert().name
	if display_name and not frappe.db.get_value("Model ID", name, "display_name"):
		frappe.db.set_value("Model ID", name, "display_name", display_name)
	return name
