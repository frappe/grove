from frappe.model.document import Document


class ModelProviderKey(Document):
	"""One credential of a vendor (child of Model Provider). The row name is the id the gateway
	picks, rotates and counts under, so the secret itself is never named anywhere."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		title: DF.Data | None
	# end: auto-generated types

	pass
