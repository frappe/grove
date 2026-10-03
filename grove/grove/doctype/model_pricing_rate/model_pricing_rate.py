from frappe.model.document import Document


class ModelPricingRate(Document):
	"""One counter's sell rate (child)."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		counter: DF.Link
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		rate: DF.Currency
	# end: auto-generated types

	pass
