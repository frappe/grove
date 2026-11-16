import frappe
from frappe.model.document import Document


class ModelProviderKey(Document):
	"""One credential of a vendor, held for one Model Provider record. The name is the id the
	gateway picks, rotates and counts under, so the secret itself is never named anywhere."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account: DF.Data | None
		api_key: DF.Password
		disabled: DF.Check
		model_provider: DF.Link
		title: DF.Data
	# end: auto-generated types

	pass


def on_doctype_update():
	"""A key is known by its title — Provider Key Stats names it so — one per provider. Title
	leads: Frappe names a twin by the index's first column ("Title must be unique")."""
	frappe.db.add_unique(
		"Model Provider Key", ["title", "model_provider"], constraint_name="unique_title_provider"
	)
