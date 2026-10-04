# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class Modality(Document):
	"""One kind of thing a model takes or gives (`Text`, `Image`, `Embeddings`). The gateway reads
	the name lowercased on a route row. Seeded from the catalog."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		is_input: DF.Check
		is_output: DF.Check
	# end: auto-generated types

	def before_naming(self):
		"""A capital first letter, however the name was typed."""
		if self.name:
			self.name = self.name[0].upper() + self.name[1:]
