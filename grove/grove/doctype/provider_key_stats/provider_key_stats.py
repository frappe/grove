# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ProviderKeyStats(Document):
	"""What one vendor key answered on one gateway store since the pull before, one JSON field,
	by the 6-hourly pull and never updated; a store that did not dial the key leaves no row."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		gateway_store: DF.Link | None
		key_title: DF.Data | None
		model_provider: DF.Link | None
		stats: DF.Code | None
	# end: auto-generated types

	pass


def on_doctype_update():
	"""Every read is one key's rows in time order."""
	frappe.db.add_index("Provider Key Stats", ["key_title", "creation"])
