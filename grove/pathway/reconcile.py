"""One user's share of a drain, landed: the records, the bill, and the audit of the box's charge.

Grove bills its own price: each pricing the gateway tagged a request with is priced here at that
pricing's rates, and `spent` moves by the sum. The gateway's own cost sits beside it; where the two
differ beyond the gateway's truncation, a Credit Discrepancy says so.

What the gateway served free (tagged `f:`, not `p:`) is recorded and priced the same, in a record
of its own, but not billed: `spent` does not move and nothing is audited, since no money changed
hands on either side. The gateway decides per request, so a drain that spans a flip lands as both."""

from decimal import Decimal

import frappe

from grove.billing.doctype.credit_discrepancy.credit_discrepancy import record
from grove.billing.pricing import NANO, settle

NANO_USD = Decimal(1) / NANO


def usd(nano_usd):
	return Decimal(nano_usd) / NANO


class Reconciler:
	"""One per drain: the price book, the day, which store, under which drain id."""

	def __init__(self, book, day, gateway_store, drain_id):
		self.book = book
		self.day = day
		self.gateway_store = gateway_store
		self.drain_id = drain_id

	def user(self, user, drains):
		"""`drains`: {API key: parsed hash} for `user`'s keys in this drain. A key this drain already
		landed is skipped, so a re-sent drain bills nothing twice."""
		records = [
			doc for prefix, drain in drains.items() if not self.landed(prefix)
			for doc in self.records(user, prefix, drain)
		]
		if billed := [doc for doc in records if doc.billed]:
			self.bill(user, billed)
		settle(user)

	def records(self, user, prefix, drain):
		"""One key's share as up to two records: what the gateway charged, and what it served free
		or unpriced. Their request counts sum to the key's."""
		charged, free = self.priced(drain.pricings), self.priced(drain.free) + self.unpriced(drain)
		if not charged:
			return [self.insert(user, prefix, 0, free, drain.requests)]
		requests = sum(entry["requests"] for entry in charged) if free else drain.requests
		records = [self.insert(user, prefix, 1, charged, requests)]
		if free:
			records.append(self.insert(user, prefix, 0, free, drain.requests - requests))
		return records

	def bill(self, user, records):
		charged = sum((entry["grove_cost"] for doc in records for entry in doc.entries), Decimal(0))
		frappe.db.sql("update `tabGrove User` set spent = spent + %s where name = %s", [charged, user])
		for doc in records:
			self.audit(doc)

	def landed(self, prefix):
		return frappe.db.exists("Usage Record", {"drain_id": self.drain_id, "api_key": prefix})

	def insert(self, user, prefix, billed, entries, requests):
		doc = frappe.get_doc({
			"doctype": "Usage Record", "api_key": prefix, "user": user, "day": self.day,
			"gateway_store": self.gateway_store, "drain_id": self.drain_id,
			"billed": billed, "request_count": requests,
			"cost": sum((e["grove_cost"] for e in entries), Decimal(0)),
			"gateway_cost": sum((e["gateway_cost"] for e in entries), Decimal(0)),
			# The gateway's cost is kept as the header total only; per pricing it is compared, not stored.
			"usage": frappe.as_json([{k: v for k, v in e.items() if k != "gateway_cost"} for e in entries], indent=None),
		})
		doc.insert(ignore_permissions=True)
		doc.entries = entries
		return doc

	def priced(self, pricings):
		"""One entry per pricing the gateway priced at. Costs are rounded to the nano, so the header
		totals are exactly the sum of what is stored."""
		return [
			self.entry(
				self.book.pricing(pricing).model_key, pricing, counts,
				usd(counts.get("cost", 0)), self.book.pricing_cost(pricing, counts),
			)
			for pricing, counts in pricings.items()
		]

	def unpriced(self, drain):
		"""One entry per model key Grove knows that the gateway served without a pricing."""
		priced = {self.book.pricing(pricing).model_key for pricing in (*drain.pricings, *drain.free)}
		return [
			self.entry(model, None, counts, Decimal(0), Decimal(0))
			for model, counts in drain.counters.items()
			if model in self.book.models and model not in priced
		]

	def entry(self, model, pricing, counts, gateway_cost, grove_cost):
		"""`model` is the key — what the bucket, the report and the customer name."""
		return {
			"model": model, "pricing": pricing, "requests": counts.get("request_count", 0),
			**{counter: counts.get(counter, 0) for counter in self.book.counters.names if counter != "request_count"},
			"gateway_cost": gateway_cost.quantize(NANO_USD), "grove_cost": grove_cost.quantize(NANO_USD),
		}

	def audit(self, doc):
		"""The gateway undercharges by at most its truncation; anything else is a discrepancy."""
		for entry in doc.entries:
			gateway, grove = entry["gateway_cost"], entry["grove_cost"]
			if entry["pricing"] and abs(gateway - grove) > self.book.counters.tolerance(entry["requests"]):
				record(
					usage_record=doc.name, grove_user=doc.user, api_key=doc.api_key, pricing=entry["pricing"],
					gateway_store=self.gateway_store, gateway_value=gateway, grove_value=grove, delta=gateway - grove,
				)
