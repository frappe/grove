"""Prices per counter, and the prepaid balance they debit.

One rate table, joined at evaluation and never snapshotted onto usage: SELL on `Model Pricing`.
A drain is priced by the pricing id the gateway tagged each request with, so both sides price the
same counters at the same rates. The provider's cost card is reference data, not read here.

`Grove User.spent` is the running USD total the pull increments; `settle` is the one writer of
`balance` and the `credit_exhausted` verdict."""

from decimal import Decimal

import frappe

from grove.grove.doctype.grove_user.grove_user import set_credit_exhausted

NANO = 10**9

# Unit divisor per priced counter: a rate is USD per Mtok, per request. The gateway
# holds the same table (pathway internal/domain/price.go); the README lists both.
COUNTERS = {
	"input_tokens": 1_000_000,
	"cached_tokens": 1_000_000,
	"cache_write_tokens": 1_000_000,
	"cache_write_1h_tokens": 1_000_000,
	"completion_tokens": 1_000_000,
	"audio_tokens": 1_000_000,
	"input_tokens_above_272k": 1_000_000,
	"cached_tokens_above_272k": 1_000_000,
	"cache_write_tokens_above_272k": 1_000_000,
	"completion_tokens_above_272k": 1_000_000,
	"request_count": 1,
}

# What a request whose prompt exceeds 272k tokens is counted under, and the base counter each
# falls back to: with no rate of its own it bills at the base rate. The gateway holds the same.
LONG_CONTEXT_COUNTERS = {
	"input_tokens_above_272k": "input_tokens",
	"cached_tokens_above_272k": "cached_tokens",
	"cache_write_tokens_above_272k": "cache_write_tokens",
	"completion_tokens_above_272k": "completion_tokens",
}


def nano(usd):
	"""USD → whole nano-USD, the gateway's unit. A float is read as its decimal text, so 0.3 is
	300 000 000 and not one short."""
	return int(Decimal(str(usd)) * NANO)


def cost(counts, rates):
	"""USD for `{counter: amount}` at `{counter: USD per unit}`. An above-272k counter with no rate
	bills at its base counter's; any other counter with usage and no rate bills 0 — the Model
	form flags a model with no enabled pricing. Never raises."""
	total = Decimal(0)
	for counter, amount in counts.items():
		rate = rates.get(counter, rates.get(LONG_CONTEXT_COUNTERS.get(counter)))
		if amount and rate is not None and counter in COUNTERS:
			total += Decimal(amount) * rate / COUNTERS[counter]
	return total


class PriceBook:
	"""Every sell rate the site holds, by pricing id, read once per run. A Disabled pricing is in
	it: a gateway charges at it until the push carrying its successor lands."""

	def __init__(self):
		self.models = set()
		self.pricings = {}

	@classmethod
	def load(cls):
		book = cls()
		book.models = {model.name for model in frappe.get_all("Model", fields=["name"])}
		pricings = {p.name: p.model for p in frappe.get_all("Model Pricing", fields=["name", "model"])}
		book.pricings = {name: frappe._dict(model=model, rates={}) for name, model in pricings.items()}
		rates = frappe.get_all(
			"Model Pricing Rate",
			filters={"parent": ("in", list(pricings))},
			fields=["parent", "counter", "rate"],
			parent_doctype="Model Pricing",
		) if pricings else []
		for row in rates:
			book.pricings[row.parent].rates[row.counter] = Decimal(str(row.rate))
		return book

	def pricing(self, pricing_id):
		"""The pricing a gateway charged at. Unknown is a bug on one side, so it raises."""
		if pricing_id not in self.pricings:
			frappe.throw(f"The gateway charged at pricing {pricing_id!r}, which Grove does not hold.")
		return self.pricings[pricing_id]

	def nano_rates(self, pricing_id):
		"""{counter: nano-USD per unit} — what a route row carries to the gateway."""
		return {counter: nano(rate) for counter, rate in self.pricing(pricing_id).rates.items()}

	def pricing_cost(self, pricing_id, counts):
		"""USD for `{counter: amount}` charged at `pricing_id`."""
		return cost(counts, self.pricing(pricing_id).rates)


def allocated(user, lock=False):
	"""Σ the user's Grove Credit ledger. Locked while a verdict is being decided."""
	suffix = " for update" if lock else ""
	total = frappe.db.sql(
		f"select coalesce(sum(amount), 0) from `tabGrove Credit` where grove_user = %s{suffix}", [user]
	)[0][0]
	return Decimal(str(total))


def allocations():
	"""{user: Σ Grove Credit} for every user with a ledger entry — the push's budget, one query."""
	rows = frappe.db.sql("select grove_user, sum(amount) from `tabGrove Credit` group by grove_user")
	return {user: Decimal(str(total)) for user, total in rows}


def settle(user):
	"""The one writer of `balance` and the verdict: allocated − spent, written to the user, and
	`credit_exhausted` = not free and nothing left, both directions. A negative balance stays on
	the user until a top-up covers it. → actual balance."""
	doc = frappe.db.get_value("Grove User", user, ["free", "spent"], as_dict=True, for_update=True)
	actual = allocated(user, lock=True) - Decimal(str(doc.spent or 0))
	frappe.db.set_value("Grove User", user, "balance", float(actual), update_modified=False)
	set_credit_exhausted(user, int(not doc.free and actual <= 0))
	return actual


def credit_summary(user):
	"""{allocated, spent, remaining}, summed live rather than read off `balance`."""
	spent = Decimal(str(frappe.db.get_value("Grove User", user, "spent") or 0))
	total = allocated(user)
	return {"allocated": total, "spent": spent, "remaining": total - spent}


def tolerance(request_count):
	"""The gateway truncates each counter's nano-USD per request, so it can undercharge by under
	one nano per counter per request. A request is counted under a base counter or its above-272k
	one, never both. Beyond that is a discrepancy, not rounding."""
	return Decimal((request_count or 0) * (len(COUNTERS) - len(LONG_CONTEXT_COUNTERS))) / NANO


def validate_price_rows(rows, key):
	"""No blank or negative rate; no two rows `key` reads the same. 0 is a price: free."""
	seen = set()
	for row in rows:
		if row.rate is None:
			frappe.throw(f"Row {row.idx}: a blank rate is not a price — 0 means free.")
		if row.rate < 0:
			frappe.throw(f"Row {row.idx}: a rate cannot be negative.")
		k = key(row)
		if k in seen:
			frappe.throw(f"Row {row.idx} repeats {k} — one rate per counter per date.")
		seen.add(k)
