"""Prices per counter, and the prepaid balance they debit.

One rate table, joined at evaluation and never snapshotted onto usage: SELL on `Model Pricing`.
A drain is priced by the pricing id the gateway tagged each request with, so both sides price the
same counters at the same rates.

`Grove User.spent` is the running USD total the pull increments; `settle` is the one writer of
`balance` and the `credit_exhausted` verdict."""

from decimal import Decimal

import frappe

from grove.grove.doctype.grove_user.grove_user import set_credit_exhausted

NANO = 10**9

# Tokens per unit a rate is quoted per: USD per Mtok, or per request.
UNITS = {"Mtok": 1_000_000, "request": 1}


class CounterTable:
	"""Every Usage Counter the site holds: roots first, each group in catalog order. Built from rows (`name` or `counter_name`, `unit`, `part_of`, `base_counter`,
	`min_prompt_tokens`), so the catalog's rows serve the pure tests. The same table is pushed to
	the gateways inside each pricing; `parts` of a derived counter are the variants of its base's
	parts at the same threshold, which is what `validate` guarantees exist."""

	FIELDS = ("name", "label", "unit", "part_of", "base_counter", "min_prompt_tokens")

	def __init__(self, rows):
		rows = [frappe._dict(row, name=row.get("name") or row["counter_name"]) for row in rows]
		self.rows = sorted(rows, key=lambda r: bool(r.base_counter))
		self.by_name = {row.name: row for row in self.rows}
		self.variants = {(row.base_counter, row.min_prompt_tokens): row.name for row in self.rows if row.base_counter}

	@classmethod
	def load(cls):
		return cls(frappe.get_all("Usage Counter", fields=cls.FIELDS, order_by="creation"))

	def __contains__(self, counter):
		return counter in self.by_name

	@property
	def names(self):
		return [row.name for row in self.rows]

	@property
	def roots(self):
		return [row.name for row in self.rows if not row.base_counter]

	def base(self, counter):
		return self.by_name[counter].base_counter or None

	def divisor(self, counter):
		row = self.by_name[counter]
		return UNITS[row.unit or self.by_name[row.base_counter].unit]

	def parts(self, counter):
		"""The counters charged out of this one. A derived counter's are its base's parts at its
		own threshold — the ones that moved with it."""
		row = self.by_name[counter]
		if not row.base_counter:
			return [r.name for r in self.rows if r.part_of == counter]
		base_parts = self.parts(row.base_counter)
		return [v for v in (self.variants.get((p, row.min_prompt_tokens)) for p in base_parts) if v]

	def tolerance(self, request_count):
		"""The gateway truncates each counter's nano-USD per request, so it can undercharge by under
		one nano per root per request: a request is charged under a root or its one variant, never
		both. Beyond that is a discrepancy, not rounding."""
		return Decimal((request_count or 0) * len(self.roots)) / NANO

	def validate(self):
		"""A part is never bracketed, or bracketed at exactly its container's thresholds. Otherwise
		a request could land a part's variant inside a container variant whose parts do not
		subtract it — a double charge. → self, so a push can read the validated table."""
		thresholds = {}
		for row in self.rows:
			if row.base_counter:
				thresholds.setdefault(row.base_counter, set()).add(row.min_prompt_tokens)
		for row in self.rows:
			own, container = thresholds.get(row.name), thresholds.get(row.part_of)
			if row.part_of and own and own != (container or set()):
				frappe.throw(
					f"{row.name} is bracketed at {sorted(own)} but {row.part_of} at {sorted(container or ())}: "
					"a part is bracketed exactly like its container, or not at all."
				)
		return self

	@property
	def published(self):
		"""The rows as the gateway reads them, only the keys that are set, in table order."""
		rows = []
		for row in self.rows:
			entry = {"name": row.name, "divisor": self.divisor(row.name)}
			if row.part_of:
				entry["part_of"] = row.part_of
			if row.base_counter:
				entry["base"], entry["min_prompt_tokens"] = row.base_counter, row.min_prompt_tokens
			rows.append(entry)
		return rows


def nano(usd):
	"""USD → whole nano-USD, the gateway's unit. A float is read as its decimal text, so 0.3 is
	300 000 000 and not one short."""
	return int(Decimal(str(usd)) * NANO)


def cost(counts, rates, counters):
	"""USD for `{counter: amount}` at `{counter: USD per unit}` under `counters`. A counter's rate
	is charged on what its parts left of it. A derived counter with no rate bills at its base's;
	any other counter with usage and no rate bills 0 — the Model form flags a model with no enabled
	pricing. Never raises."""
	total = Decimal(0)
	for counter, amount in counts.items():
		if counter not in counters:
			continue
		rate = rates.get(counter, rates.get(counters.base(counter)))
		amount = (amount or 0) - sum(counts.get(part) or 0 for part in counters.parts(counter))
		if amount > 0 and rate is not None:
			total += Decimal(amount) * rate / counters.divisor(counter)
	return total


class PriceBook:
	"""Every sell rate the site holds, by pricing id, read once per run. A Disabled pricing is in
	it: a gateway charges at it until the push carrying its successor lands."""

	def __init__(self):
		self.models = set()
		self.pricings = {}
		self.counters = CounterTable([])

	@classmethod
	def load(cls):
		book = cls()
		book.counters = CounterTable.load()
		# Keys, not docs: a usage bucket names the key, and a key may be several docs.
		book.models = {model.model_key for model in frappe.get_all("Model", fields=["model_key"])}
		pricings = frappe.get_all("Model Pricing", fields=["name", "model", "model_key"])
		book.pricings = {
			p.name: frappe._dict(model=p.model, model_key=p.model_key, rates={}) for p in pricings
		}
		rates = frappe.get_all(
			"Model Pricing Rate",
			filters={"parent": ("in", list(book.pricings))},
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
		return cost(counts, self.pricing(pricing_id).rates, self.counters)


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
