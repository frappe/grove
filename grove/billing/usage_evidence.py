# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json
from decimal import Decimal

import frappe

from grove.billing.doctype.usage_record.usage_record import LANDING_WINDOW, team_clause, usage_table
from grove.pathway.routes import utc_timestamp

VERSION = 1
MAXIMUM_BYTES = 8192
MAXIMUM_PRICINGS = 8
MAXIMUM_DISCREPANCIES = 20
PROMPT_ROOT = "prompt_tokens"
COMPLETION_ROOT = "completion_tokens"
LANDED = f"{LANDING_WINDOW} and {team_clause('r')}"
IS_CORRECTION = "coalesce(r.cost_discrepancy, '') != ''"


class DayEvidence:
	"""How each report line of one UTC landing day was derived: per team and model, the requests,
	tokens and cost under each pricing at its rates, a digest of the records summed, and the
	corrections among them. `window` is the line value's landing window ({since, until})."""

	def __init__(self, window, book):
		self.book = book
		self.counters = book.counters.usage_columns
		self.params = window

	def get_lines(self):
		"""{(team, model): evidence} for every line of the day."""
		priced = {}
		for row in self.get_pricing_rows():
			priced.setdefault((row.team, row.model), []).append(row)
		return {
			(row.team, row.model): fit(self.get_evidence(row, priced.get((row.team, row.model), [])))
			for row in self.get_record_rows()
		}

	def get_pricing_rows(self):
		"""Usage alone, per team, model and pricing: requests, each counter and the cost as text,
		since the database driver turns a decimal into a float."""
		sums = "".join(f", sum(u.`{counter}`) as `{counter}`" for counter in self.counters)
		# Fixed SQL and Usage Counter column names only; values are in `params`.
		return frappe.db.sql(  # nosemgrep
			f"""select r.team, coalesce(u.model, '') as model, coalesce(u.pricing, '') as pricing,
			sum(u.requests) as requests{sums}, cast(sum(u.grove_cost) as char) as cost
			from `tabUsage Record` r, {usage_table()}
			where {LANDED} and not ({IS_CORRECTION})
			group by r.team, coalesce(u.model, ''), coalesce(u.pricing, '')""",
			self.params,
			as_dict=True,
		)

	def get_record_rows(self):
		"""Per team and model: the records summed, the digest of their names in byte order, the
		landing span, and the corrections among them. Joined names that reach the session limit
		were cut, so their digest is not of every record."""
		# Fixed SQL only; values are in `params`.
		return frappe.db.sql(  # nosemgrep
			f"""select team, model, records, sha2(joined, 256) as digest,
			length(joined) < @@session.group_concat_max_len as is_whole,
			first_at, last_at, corrections, cast(correction_cost as char) as correction_cost,
			discrepancies from (
				select r.team, coalesce(u.model, '') as model, count(distinct r.name) as records,
				group_concat(distinct r.name order by r.name collate utf8mb4_bin separator ',') as joined,
				min(r.creation) as first_at, max(r.creation) as last_at,
				count(distinct case when {IS_CORRECTION} then r.name end) as corrections,
				sum(case when {IS_CORRECTION} then u.grove_cost else 0 end) as correction_cost,
				group_concat(distinct nullif(r.cost_discrepancy, '') order by r.cost_discrepancy
					separator ',') as discrepancies
				from `tabUsage Record` r, {usage_table()}
				where {LANDED}
				group by r.team, coalesce(u.model, '')
			) grouped""",
			self.params,
			as_dict=True,
		)

	def get_evidence(self, record_row, pricing_rows):
		# Joined names that reach the server's own limit were cut, so their digest is not of every record.
		is_digest_whole = bool(record_row.is_whole)
		records = {
			"count": record_row.records,
			"first_at": utc_timestamp(record_row.first_at),
			"last_at": utc_timestamp(record_row.last_at),
		}
		if is_digest_whole:
			records["sha256"] = record_row.digest
		return {
			"v": VERSION,
			"requests": sum(int(row.requests or 0) for row in pricing_rows),
			"tokens_in": self.get_tokens(pricing_rows, PROMPT_ROOT),
			"tokens_out": self.get_tokens(pricing_rows, COMPLETION_ROOT),
			"pricings": [self.get_pricing(row) for row in sorted(pricing_rows, key=lambda row: row.pricing)],
			"records": records,
			"corrections": get_corrections(record_row),
			"truncated": not is_digest_whole,
		}

	def get_tokens(self, pricing_rows, root):
		counters = self.book.counters.get_whole_of(root)
		return sum(int(row[counter] or 0) for row in pricing_rows for counter in counters)

	def get_pricing(self, row):
		pricing = self.book.pricings.get(row.pricing)
		rates = pricing.rates if pricing else {}
		return {
			"id": row.pricing,
			"requests": int(row.requests or 0),
			"counters": {counter: int(row[counter]) for counter in self.counters if row[counter]},
			"rates": {counter: f"{rate:f}" for counter, rate in sorted(rates.items())},
			"cost": row.cost or "0",
		}


def get_corrections(record_row):
	names = (record_row.discrepancies or "").split(",")
	return {
		"count": record_row.corrections,
		"cost": record_row.correction_cost or "0",
		"discrepancies": [name for name in names if name][:MAXIMUM_DISCREPANCIES],
	}


def fit(evidence):
	"""Within `MAXIMUM_PRICINGS` and `MAXIMUM_BYTES` of canonical JSON, or cut to the costliest
	pricings without their counters and rates, marked truncated."""
	if len(evidence["pricings"]) <= MAXIMUM_PRICINGS and size(evidence) <= MAXIMUM_BYTES:
		return evidence
	costliest = sorted(evidence["pricings"], key=lambda pricing: abs(Decimal(pricing["cost"])), reverse=True)
	pricings = [
		{key: pricing[key] for key in ("id", "requests", "cost")} for pricing in costliest[:MAXIMUM_PRICINGS]
	]
	return {**evidence, "pricings": pricings, "truncated": True}


def size(evidence):
	"""Bytes of the canonical JSON: sorted keys, no spaces."""
	return len(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode())
