"""Prices per counter, and what each key spent at them this month.

One rate table, joined at evaluation and never snapshotted onto usage: SELL on `Model Pricing`.
A drain is priced by the pricing id the gateway tagged each request with, so both sides price the
same counters at the same rates.

No spend is stored on a key or a team: `month_spent_by` sums this month off the records and the
corrections, and whatever lands money forgets the team's figures (`forget_figures`)."""

from decimal import Decimal

import frappe

from grove.utils import utc_today

NANO = 10**9
MONTH_SPENT_BY = ("team", "api_key")

UNITS = {"Mtok": 1_000_000, "request": 1}
FIGURES_SECONDS = 30 * 60


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
		self.variants = {
			(row.base_counter, row.min_prompt_tokens): row.name for row in self.rows if row.base_counter
		}

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

	@property
	def whole(self):
		"""The root token counters that are not a part of another — prompt and completion once each,
		never their cached or audio parts again — and the derived rows counted instead of them for a
		long prompt. Their sum is a record's whole tokens."""
		roots = [r.name for r in self.rows if r.unit == "Mtok" and not r.base_counter and not r.part_of]
		return roots + [r.name for r in self.rows if r.base_counter in roots]

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
		keys = dict(frappe.get_all("Model", fields=["name", "model_key"], as_list=True))
		book.models = set(keys.values())
		pricings = frappe.get_all("Model Pricing", fields=["name", "model"])
		book.pricings = {p.name: frappe._dict(model_key=keys[p.model], rates={}) for p in pricings}
		rates = (
			frappe.get_all(
				"Model Pricing Rate",
				filters={"parent": ("in", list(book.pricings))},
				fields=["parent", "counter", "rate"],
				parent_doctype="Model Pricing",
			)
			if pricings
			else []
		)
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


def lock_team(team):
	"""The team row, held until this transaction ends. The pull and a correction take it before
	landing money, and a cap check reads the team FOR UPDATE, so the check sees a drain landed
	whole or not at all."""
	frappe.db.get_value("Central Team", team, "name", for_update=True)


def usd(amount):
	"""An amount for a message: as many decimals as it has, up to nano, so a cap of a few cents or a
	few millionths reads as itself rather than as 0.00."""
	return f"{float(amount):.9f}".rstrip("0").rstrip(".") or "0"


def committed(team, caps=None, lock=True, box_spent=None):
	"""USD of the team's spend limit the month has spoken for: each live key's cap or what it spent
	this month, whichever is more, plus what its revoked keys spent — a revoke frees a cap, never
	the spend. `caps` are proposed caps by key name, over the live keys' own; one for a key with no
	row yet counts as given. `box_spent` is what the boxes count for each key this month
	(`usage.peek_team`): a key's month is that or the records, whichever is more — the records lag
	the boxes by up to an hour, the boxes lose what a restore or a dead spool line left behind, and
	everything recorded was counted on a box first, so a sum would double. `lock` reads the keys
	FOR UPDATE, so two raises serialized on the team row see each other's caps rather than the
	snapshot taken before the lock."""
	keys = frappe.db.sql(
		"select name, cap, status from `tabGrove API Key` where team = %s" + (" for update" if lock else ""),
		[team],
		as_dict=True,
	)
	spent = month_spent_by("api_key", [k.name for k in keys])
	caps = dict(caps or {})
	total = Decimal(0)
	for k in keys:
		month = max(spent.get(k.name, Decimal(0)), Decimal(str((box_spent or {}).get(k.name, 0))))
		cap = Decimal(str(caps.pop(k.name, k.cap) or 0)) if k.status == "active" else Decimal(0)
		total += max(cap, month)
	total += sum((Decimal(str(cap or 0)) for cap in caps.values()), Decimal(0))
	return float(round(total, 9))


def month_spent_by(field, names):
	"""{name: USD spent this month} by `field` (team or api_key): Σ the Usage Records served
	on a day of this UTC month — the gateway's own day, so the month its `spent:<month>` counts —
	plus the Usage Corrections Grove made on one. A revoked key counts: what it spent is still
	the team's. Two grouped queries however many names."""
	if field not in MONTH_SPENT_BY:
		raise ValueError(f"month_spent_by sums by one of {MONTH_SPENT_BY}, not {field!r}")
	if not names:
		return {}
	first = utc_today().replace(day=1)
	spent = dict(
		frappe.db.sql(
			f"""select `{field}`, sum(cost) from `tabUsage Record`
		where `{field}` in %(names)s and day >= %(first)s group by `{field}`""",
			{"names": names, "first": first},
		)
	)
	corrected = frappe.db.sql(
		f"""select `{field}`, sum(cost) from `tabUsage Correction`
		where `{field}` in %(names)s and day >= %(first)s group by `{field}`""",
		{"names": names, "first": first},
	)
	for name, correction in corrected:
		spent[name] = (spent.get(name) or 0) + correction
	return {name: Decimal(str(total)) for name, total in spent.items()}


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


def remembered_figures(team, read):
	"""`read()` for `team`, kept until something moves its figures (`forget_figures`) and
	FIGURES_SECONDS at most: spend, keys and this month's usage read together, so every page
	shows one moment. The UTC day travels with them: a new day, so a new month, reads afresh.
	Read past the per-request memo, so a bust earlier in this request shows."""
	key = f"figures:{team}"
	figures = frappe.cache.get_value(key, use_local_cache=False)
	if not figures or figures["day"] != str(utc_today()):
		figures = {"day": str(utc_today()), **read()}
		frappe.cache.set_value(key, figures, expires_in_sec=FIGURES_SECONDS)
	return figures


def forget_figures(team):
	"""Every writer that moves a team's figures calls this: a record landed or removed, a Grove
	correction, a key or the team saved, its tier edited. Now, and again once this transaction
	commits, since a read in between would keep the old rows."""
	key = f"figures:{team}"
	frappe.cache.delete_value(key)
	frappe.db.after_commit.add(lambda: frappe.cache.delete_value(key))
