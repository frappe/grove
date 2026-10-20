from frappe.model.document import Document


class StuckUsageRecord(Document):
	"""One Usage Record the pull that resolved a Stuck Usage row wrote (child)."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link | None
		day: DF.Date | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		usage_record: DF.Link
	# end: auto-generated types

	pass
