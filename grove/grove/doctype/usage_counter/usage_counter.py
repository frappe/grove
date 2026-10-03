import re

import frappe
from frappe.model.document import Document

NAME = re.compile(r"^[a-z][a-z0-9_]*$")
FIXED = ("unit", "part_of", "base_counter", "min_prompt_tokens")


class UsageCounter(Document):
	"""One counter usage is counted and priced under. A root counter is a bucket the gateway fills
	from the response (`prompt_tokens`, `cached_tokens`, `web_search_requests`); a derived one is a
	root plus a condition — counted instead of its base when the request's prompt exceeds
	`min_prompt_tokens`, billed at its base's rate when a pricing holds none of its own.
	The table is pushed to the gateways inside each pricing, so the two sides count alike. Rows ship
	in the catalog and never change after insert: a wrong one is deleted and made again."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		base_counter: DF.Link | None
		counter_name: DF.Data
		label: DF.Data | None
		min_prompt_tokens: DF.Int
		part_of: DF.Link | None
		unit: DF.Literal["", "Mtok", "request"]
	# end: auto-generated types

	def validate(self):
		if not NAME.match(self.counter_name or ""):
			frappe.throw("A counter is lowercase letters, digits and underscores, starting with a letter.")
		if self.base_counter:
			self.validate_derived()
		else:
			self.validate_root()
		before = self.get_doc_before_save()
		if before and any((before.get(field) or None) != (self.get(field) or None) for field in FIXED):
			frappe.throw("A counter does not change once it exists. Delete it and make it again.")

	def validate_derived(self):
		base = frappe.get_doc("Usage Counter", self.base_counter)
		if base.base_counter:
			frappe.throw(f"{self.base_counter} is derived itself; a counter derives from a root.")
		if not self.min_prompt_tokens or self.min_prompt_tokens < 0:
			frappe.throw("A derived counter names the prompt size it is counted above.")
		if self.part_of:
			frappe.throw("A derived counter is a part of whatever its base is a part of.")
		self.unit = base.unit

	def validate_root(self):
		if not self.unit:
			frappe.throw("A root counter needs a unit.")
		if self.min_prompt_tokens:
			frappe.throw("Prompt Above only means something with a base counter.")
		if self.part_of == self.counter_name:
			frappe.throw("A counter cannot be a part of itself.")
		if self.part_of and frappe.db.get_value("Usage Counter", self.part_of, "base_counter"):
			frappe.throw(f"{self.part_of} is derived; a part belongs to a root.")

	def on_trash(self):
		"""Records are evidence and their columns come from this table."""
		if frappe.db.exists("Usage Record", {"usage": ("like", f'%"{self.name}"%')}):
			frappe.throw(f"Usage Records carry {self.name}; it stays.")
