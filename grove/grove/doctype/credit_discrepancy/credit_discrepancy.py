from decimal import Decimal

import frappe
from frappe.model.document import Document


class CreditDiscrepancy(Document):
	"""The gateway charged a drain differently from Grove's price of the same counters at the same
	pricing. Logged by the pull and never acted on by it: a person decides which side is wrong and
	corrects that side's spend, which brings the two balances back together."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link | None
		correction: DF.Currency
		delta: DF.Currency
		gateway_spent: DF.Currency
		gateway_store: DF.Link | None
		gateway_value: DF.Currency
		grove_user: DF.Link | None
		grove_value: DF.Currency
		model: DF.Link | None
		model_key: DF.Data | None
		note: DF.SmallText | None
		pricing: DF.Link | None
		resolution: DF.Literal['', 'Grove corrected', 'Gateway corrected']
		resolved: DF.Check
		usage_record: DF.Link | None
	# end: auto-generated types

	@frappe.whitelist()
	def correct_grove(self):
		"""Grove priced it wrong: `spent` moves by the delta and the user is settled, so Grove's
		balance meets the gateway's."""
		from grove.pricing import settle

		self.check_open()
		delta = Decimal(str(self.delta))
		frappe.db.sql("update `tabGrove User` set spent = spent + %s where name = %s", [delta, self.grove_user])
		settle(self.grove_user)
		self.db_set({"correction": delta, "resolution": "Grove corrected", "resolved": 1})

	@frappe.whitelist()
	def correct_gateway(self):
		"""The gateway charged it wrong: its spend counter for the user on that store moves by minus
		the delta, so its balance meets Grove's. Sent through the store's writers in turn; the box
		applies it once under this row's name, so pressing again after a failure is safe."""
		from grove.pricing import NANO, nano

		self.check_open()
		answer = post_to_store(
			self.gateway_store, "spend-adjust", {"user": self.grove_user, "delta": -nano(self.delta), "id": self.name}
		)
		self.db_set({
			"correction": -Decimal(str(self.delta)), "resolution": "Gateway corrected", "resolved": 1,
			"gateway_spent": Decimal(answer["spent"]) / NANO,
		})

	def check_open(self):
		frappe.only_for("System Manager")
		if self.resolved:
			frappe.throw("This discrepancy is already resolved.")


def post_to_store(gateway_store, path, body):
	"""POST through the store's Active writers in name order until one answers; the last error
	otherwise. Every box on a store shares its Redis, so any writer will do."""
	from grove.pathway.run import Target

	writers = frappe.get_all(
		"Gateway Server", filters={"gateway_store": gateway_store, "is_store_writer": 1, "status": "Active"},
		pluck="name", order_by="name asc",
	)
	if not writers:
		frappe.throw(f"{gateway_store} has no Active writer to send this through.")
	for index, writer in enumerate(writers):
		try:
			target = Target.resolve("Gateway Server", writer)
			if target.error:
				raise RuntimeError(target.error)
			return target.post(path, body)
		except Exception:
			if index == len(writers) - 1:
				raise


def record(**facts):
	"""The one writer: one row per (Usage Record, pricing) the gateway charged differently."""
	facts = {k: float(v) if isinstance(v, Decimal) else v for k, v in facts.items()}
	return frappe.get_doc({"doctype": "Credit Discrepancy", **facts}).insert(ignore_permissions=True).name
