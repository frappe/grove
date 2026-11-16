# import frappe
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
