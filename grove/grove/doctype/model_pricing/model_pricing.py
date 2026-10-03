import frappe
from frappe.model.document import Document

from grove.pricing import CounterTable, validate_price_rows
from grove.utils import utc_today


class ModelPricing(Document):
	"""The SELL price of one model: a status and one rate per counter. One way only — enabling
	retires the predecessor, and the next push carries it to the gateways. Each request is tagged
	with the pricing that charged it, so the pull bills the same rates whenever a gateway switched."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.model_pricing_rate.model_pricing_rate import ModelPricingRate

		enabled_on: DF.Date | None
		model: DF.Link
		model_key: DF.Data | None
		rates: DF.Table[ModelPricingRate]
		status: DF.Literal["Disabled", "Enabled"]
	# end: auto-generated types

	def validate(self):
		validate_price_rows(self.rates, key=lambda row: row.counter)
		self.validate_long_context_rates()
		before = self.get_doc_before_save()
		if before and before.enabled_on:
			self.validate_frozen(before)
		elif self.status == "Enabled":
			self.enabled_on = utc_today()
			self.flags.enabling = True

	def validate_long_context_rates(self):
		"""A derived counter's rate needs its base's: a request under the threshold bills at it."""
		table, counters = CounterTable.load(), {row.counter for row in self.rates}
		for counter in counters:
			base = table.base(counter)
			if base and base not in counters:
				frappe.throw(f"{counter} needs a rate for {base}: a shorter request bills at it.")

	def validate_frozen(self, before):
		"""Once enabled, a pricing is history: the requests it charged are billed. A wrong price is
		a new pricing plus a credit row, never an edit here."""
		if before.status == "Enabled" and self.status == "Disabled":
			frappe.throw("Enable a successor instead — disabled by hand, the model goes unpriced.")
		if before.status == "Disabled" and self.status != "Disabled":
			frappe.throw("This pricing already had its window. Duplicate it to price again.")
		rates = [(row.counter, row.rate) for row in self.rates]
		if self.model != before.model or rates != [(row.counter, row.rate) for row in before.rates]:
			frappe.throw("An enabled pricing cannot change. Enable a new one and credit the difference.")

	def on_update(self):
		"""The predecessor steps down in the same save that enables its successor."""
		if not self.flags.enabling:
			return
		frappe.db.set_value(
			"Model Pricing", {"model": self.model, "status": "Enabled", "name": ("!=", self.name)}, "status", "Disabled"
		)
