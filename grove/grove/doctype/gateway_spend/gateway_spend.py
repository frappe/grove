from decimal import Decimal

import frappe
from frappe.model.document import Document


class GatewaySpend(Document):
	"""What one store's gateway last reported about one user's spend: its own lifetime counter and
	the balance it gated on. Overwritten each drain; never billed from. A counter below the stored
	one would mean the store was flushed."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		balance: DF.Currency
		drain_id: DF.Data | None
		drained_at: DF.Datetime | None
		gateway_store: DF.Link
		grove_user: DF.Link
		spent: DF.Currency
	# end: auto-generated types

	pass


def record_spend(grove_user, gateway_store, spent, balance, drain_id):
	"""One row per (user, store), written by the pull in the user's landing step."""
	values = {
		"spent": float(Decimal(spent)), "balance": float(Decimal(balance)), "drain_id": drain_id,
		"drained_at": frappe.utils.now_datetime(),
	}
	name = frappe.db.get_value("Gateway Spend", {"grove_user": grove_user, "gateway_store": gateway_store})
	if name:
		frappe.db.set_value("Gateway Spend", name, values, update_modified=False)
		return name
	return frappe.get_doc({
		"doctype": "Gateway Spend", "grove_user": grove_user, "gateway_store": gateway_store, **values,
	}).insert(ignore_permissions=True).name
