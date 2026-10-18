"""One team's share of a drain, landed: the records, the bill, and the audit of the box's charge.

Grove bills its own price: each pricing the gateway tagged a request with is priced here at that
pricing's rates, and the key's `spent` moves by the sum, then the team settles. The gateway's own
cost sits beside it; where the two differ beyond the gateway's truncation, a Credit Discrepancy
says so. A model the gateway served without a pricing lands in the same record at no cost."""

from decimal import Decimal

import frappe
from frappe.model.naming import make_autoname

from grove.billing.doctype.credit_discrepancy.credit_discrepancy import record
from grove.billing.pricing import NANO, settle

NANO_USD = Decimal(1) / NANO
FIELDS = (
	"name", "creation", "modified", "owner", "modified_by", "api_key", "team", "day", "gateway_store",
	"drain_id", "request_count", "total_tokens", "cost", "gateway_cost", "usage",
)


def usd(nano_usd):
	return Decimal(nano_usd) / NANO


class Reconciler:
	"""One per drain and day: the price book, the day the gateway served the buckets, which store,
	under which drain id, and `landed`, the (drain id, key, day) triples a record already holds."""

	def __init__(self, book, day, gateway_store, drain_id, landed=frozenset()):
		self.book = book
		self.day = day
		self.gateway_store = gateway_store
		self.drain_id = drain_id
		self.landed = landed

	def team(self, team, drains):
		"""`drains`: {API key: parsed hash} for `team`'s keys in this drain on this day. A key this
		drain already landed for the day is skipped, so a re-sent drain bills nothing twice. The rows
		go in as one statement however many keys: a store's hourly drain is a lakh of them."""
		rows = [
			self.row(team, prefix, self.priced(drain.pricings) + self.unpriced(drain), drain.requests)
			for prefix, drain in drains.items()
			if not self.has_landed(prefix)
		]
		frappe.db.bulk_insert("Usage Record", FIELDS, [[row[field] for field in FIELDS] for row in rows])
		for row in rows:
			self.bill(row)
		settle(team)

	def bill(self, row):
		"""The key's spend moves by Grove's price of what the gateway charged it."""
		charged = sum((entry["grove_cost"] for entry in row.entries), Decimal(0))
		# ponytail: one update per key, ~40 s an hour at a lakh; a CASE update per team if the pull nears the hour.
		frappe.db.sql("update `tabGrove API Key` set spent = spent + %s where name = %s", [charged, row.api_key])
		self.audit(row)

	def has_landed(self, prefix):
		return (self.drain_id, prefix, str(self.day)) in self.landed

	def row(self, team, prefix, entries, requests):
		"""A record as the table takes it, named and stamped as the ORM would have; `entries` ride
		along for `bill` and `audit`."""
		now = frappe.utils.now()
		return frappe._dict(
			name=make_autoname("hash", "Usage Record"), creation=now, modified=now,
			owner=frappe.session.user, modified_by=frappe.session.user,
			api_key=prefix, team=team, day=self.day, gateway_store=self.gateway_store, drain_id=self.drain_id,
			request_count=requests,
			total_tokens=sum(e[counter] for e in entries for counter in self.book.counters.whole),
			cost=sum((e["grove_cost"] for e in entries), Decimal(0)),
			gateway_cost=sum((e["gateway_cost"] for e in entries), Decimal(0)),
			usage=frappe.as_json([{k: v for k, v in e.items() if k != "gateway_cost"} for e in entries], indent=None),
			entries=entries,
		)

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
		priced = {self.book.pricing(pricing).model_key for pricing in drain.pricings}
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

	def audit(self, row):
		"""The gateway undercharges by at most its truncation; anything else is a discrepancy."""
		for entry in row.entries:
			gateway, grove = entry["gateway_cost"], entry["grove_cost"]
			if entry["pricing"] and abs(gateway - grove) > self.book.counters.tolerance(entry["requests"]):
				record(
					usage_record=row.name, team=row.team, api_key=row.api_key, pricing=entry["pricing"],
					gateway_store=self.gateway_store, gateway_value=gateway, grove_value=grove, delta=gateway - grove,
				)
