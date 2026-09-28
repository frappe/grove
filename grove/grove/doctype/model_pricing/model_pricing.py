import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from grove.pricing import validate_price_rows
from grove.utils import utc_today


class ModelPricing(Document):
	"""The SELL price of one model: a status, when it takes over, and one rate per counter.
	One way only — a pricing is Scheduled, the gateways switch to it at `activates_at` on their own
	clocks, and `enable_due` then retires its predecessor. A Scheduled one stays editable until the
	lead time before it fires, which is what lets every gateway hold its rates first."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.model_pricing_rate.model_pricing_rate import ModelPricingRate

		activates_at: DF.Datetime | None
		enabled_on: DF.Date | None
		model: DF.Link
		rates: DF.Table[ModelPricingRate]
		status: DF.Literal["Disabled", "Scheduled", "Enabled"]
	# end: auto-generated types

	def validate(self):
		validate_price_rows(self.rates, key=lambda row: row.counter)
		before = self.get_doc_before_save()
		was_scheduled = bool(before and before.status == "Scheduled")
		if before and before.enabled_on and not was_scheduled:
			self.validate_frozen(before)
			return
		if self.status == "Scheduled":
			self.validate_scheduled()
		elif self.status == "Enabled":
			self.enable(was_scheduled)
		else:
			# Disabled with no window yet: a draft, or a cancelled schedule.
			self.activates_at = self.enabled_on = None

	def validate_frozen(self, before):
		"""Once enabled, a pricing is history: the requests it charged are billed. A wrong price is
		a new pricing plus a credit row, never an edit here."""
		if before.status == "Enabled" and self.status == "Disabled":
			frappe.throw("Schedule a successor instead — disabled by hand, the model goes unpriced.")
		if before.status == "Disabled" and self.status != "Disabled":
			frappe.throw("This pricing already had its window. Duplicate it to price again.")
		rates = [(row.counter, row.rate) for row in self.rates]
		if self.model != before.model or rates != [(row.counter, row.rate) for row in before.rates]:
			frappe.throw("An enabled pricing cannot change. Schedule a new one and credit the difference.")

	def validate_scheduled(self):
		lead = lead_minutes()
		if not self.activates_at or get_datetime(self.activates_at) < add_to_date(now_datetime(), minutes=lead):
			frappe.throw(
				f"Activates At must be at least {lead} minutes ahead, so every gateway holds these rates "
				"before it switches to them."
			)
		other = frappe.db.exists(
			"Model Pricing", {"model": self.model, "status": "Scheduled", "name": ("!=", self.name)}
		)
		if other:
			frappe.throw(f"{other} is already scheduled for {self.model} — one at a time.")

	def enable(self, was_scheduled):
		"""Only a due schedule enables: the gateways already switched at `activates_at`."""
		if not was_scheduled:
			frappe.throw("Schedule it instead: the gateways switch to a pricing at its Activates At.")
		if get_datetime(self.activates_at) > now_datetime():
			frappe.throw("Not due yet: the gateways switch at Activates At.")
		self.enabled_on = utc_today()
		self.flags.enabling = True

	def on_update(self):
		"""The predecessor steps down in the same save that enables its successor."""
		if not self.flags.enabling:
			return
		frappe.db.set_value(
			"Model Pricing", {"model": self.model, "status": "Enabled", "name": ("!=", self.name)}, "status", "Disabled"
		)


def lead_minutes():
	"""Unset reads as 0, which would let a pricing fire before the push carrying it lands."""
	lead = cint(frappe.db.get_single_value("Grove Settings", "pricing_lead_minutes"))
	if lead < 1:
		frappe.throw("Set Pricing Lead (minutes) in Grove Settings before scheduling a pricing.")
	return lead


def enable_due():
	"""Every minute: a Scheduled pricing whose `activates_at` has passed goes Enabled and retires its
	predecessor. The gateways switched on their own; this makes the status say so. One failure
	skips nobody."""
	due = frappe.get_all(
		"Model Pricing", filters={"status": "Scheduled", "activates_at": ("<=", now_datetime())}, pluck="name"
	)
	for name in due:
		try:
			doc = frappe.get_doc("Model Pricing", name)
			doc.status = "Enabled"
			doc.save()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Scheduled pricing {name} failed to enable"[:140])
