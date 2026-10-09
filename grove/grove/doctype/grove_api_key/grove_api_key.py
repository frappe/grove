# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import hashlib
import secrets
from decimal import Decimal

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from grove.access import validate_model_geography

KEY_PREFIX = "gr_"
# Guards a race where a key is created and revoked at the same time.
REVOKE_AFTER_HOURS = 6
DEFAULT_LIMITS = (
	{"metric": "requests", "window": "1m", "value": 20},
	{"metric": "total_tokens", "window": "1m", "value": 100_000},
)


def hash_secret(secret: str) -> str:
	"""The Redis key id the gateway uses = sha256 of the full presented key."""
	return hashlib.sha256(secret.encode()).hexdigest()


class GroveAPIKey(Document):
	"""A credential and the whole policy behind it: which models (its groups, plus its own
	Allow, less its Deny), the one geography it works in, its rate limits, and `cap` — the slice
	of its team's balance it may spend. The gateway holds it as one record, so every gate reads
	the key and nothing else; a key lives on one geography's store, which gates its cap exactly
	however many keys the team holds elsewhere.

	No sync hook: only ACTIVE keys are projected, so revoking one moves its bucket's snapshot hash
	and the next tick prunes it off every box."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow
		from grove.grove.doctype.model_group_row.model_group_row import ModelGroupRow
		from grove.grove.doctype.model_limit.model_limit import ModelLimit

		allow: DF.TableMultiSelect[GroveModelRow]
		api_secret: DF.Password | None
		cap: DF.Currency
		deny: DF.TableMultiSelect[GroveModelRow]
		geography: DF.Link
		key_hash: DF.Data | None
		limits: DF.Table[ModelLimit]
		model_groups: DF.TableMultiSelect[ModelGroupRow]
		spent: DF.Currency
		status: DF.Literal["active", "revoked"]
		team: DF.Link
		title: DF.Data | None
	# end: auto-generated types

	def before_insert(self):
		"""A new key starts in its geography's default Model Group and under DEFAULT_LIMITS, unless
		the insert names its own groups or limits."""
		self.check_key_count()
		# key_hash is what the gateway keys on and what revoke looks up; api_secret holds the full
		# key encrypted at rest, for reveal-later.
		full_key = KEY_PREFIX + secrets.token_hex(24)
		self.key_hash = hash_secret(full_key)
		self.api_secret = full_key
		self.set_geography()
		default = frappe.db.get_value("Model Group", {"is_default": 1, "geography": self.geography})
		if default and not self.model_groups:
			self.append("model_groups", {"model_group": default})
		if not self.limits:
			for limit in DEFAULT_LIMITS:
				self.append("limits", dict(limit))

	def check_key_count(self):
		"""Rate limits are per key, so the team's key count is what bounds its throughput."""
		max_keys, active = frappe.db.get_value("Central Team", self.team, "max_keys"), frappe.db.count(
			"Grove API Key", {"team": self.team, "status": "active"}
		)
		if active >= (max_keys or 0):
			frappe.throw(f"This team already holds {active} live keys, its limit. Revoke one first.")

	def set_geography(self):
		"""One geography per key: blank is the default one."""
		if not self.geography:
			self.geography = frappe.db.get_value("Geography", {"is_default": 1})
		if not self.geography:
			frappe.throw("Mark one Geography as default, or pick a geography for this key.")

	def validate(self):
		validate_model_geography([*self.allow, *self.deny], self.geography)
		# Deny wins anyway, so a model on both lists is a mistake worth surfacing.
		both = {row.model_key for row in self.allow} & {row.model_key for row in self.deny}
		if both:
			frappe.throw(f"{', '.join(sorted(both))} is in both Allow and Deny")
		self.validate_limits()
		before = self.get_doc_before_save()
		if before:
			# Its counters and spend live on the geography's store: a key does not move. Mint another.
			if before.geography != self.geography:
				frappe.throw("A key's geography is set once. Mint a new key for another geography.")
			# save() writes every column and the pull writes this one behind the form's back.
			self.spent = frappe.db.get_value("Grove API Key", self.name, "spent", for_update=True)
		if self.is_new() or flt(before.cap, 9) != flt(self.cap, 9):
			self.validate_cap()

	def validate_limits(self):
		"""One limit per metric and window; the gateway refuses a push carrying one it cannot read."""
		seen = set()
		for row in self.limits:
			if (row.value or 0) <= 0:
				frappe.throw(f"Limit row {row.idx}: the value must be above zero.")
			if (row.metric, row.window) in seen:
				frappe.throw(f"Two limits on {row.metric} per {row.window}.")
			seen.add((row.metric, row.window))

	def validate_cap(self):
		"""A prepaid team's key spends only through its cap, so it needs one above zero, and
		Σ (cap - spent) over the team's live keys never exceeds the team's balance: every store
		then gates its key exactly, and the keys together can never outspend the team."""
		if flt(self.cap) < 0:
			frappe.throw("A cap cannot be negative.")
		team = frappe.db.get_value("Central Team", self.team, ["free", "balance"], as_dict=True, for_update=True)
		if team.free or self.status != "active":
			return
		if flt(self.cap) <= 0:
			frappe.throw("A key of a prepaid team needs a cap above zero: that is all it may spend.")
		free_of_this = allotted(self.team, except_key=self.name) + max(Decimal(0), Decimal(str(self.cap or 0)) - Decimal(str(self.spent or 0)))
		if free_of_this > Decimal(str(team.balance or 0)):
			frappe.throw(
				f"A cap of {flt(self.cap, 2)} would hand out {flt(free_of_this, 2)} of a balance of "
				f"{flt(team.balance, 2)}. Top the team up, or lower another key's cap."
			)

	@property
	def revocable_at(self):
		"""When `revoke` stops refusing this key, in site time. Parsed first: a doc the desk sends
		back carries `creation` as a string, and `add_to_date` hands a string back for one."""
		return frappe.utils.add_to_date(frappe.utils.get_datetime(self.creation), hours=REVOKE_AFTER_HOURS)

	@frappe.whitelist()
	def revoke(self):
		"""Retire the credential. The row stays as the record that it existed and when it stopped,
		its unspent cap returns to the team's unallocated balance, and the gateways' copy goes when
		the next sync prunes the unprojected key."""
		if frappe.utils.now_datetime() < self.revocable_at:
			frappe.throw(f"API key cannot be revoked less than {REVOKE_AFTER_HOURS} hours of it's creation")

		self.status = "revoked"
		self.save(ignore_permissions=True)


def allotted(team, except_key=None):
	"""Σ max(cap - spent, 0) over the team's live keys: what its balance is already spoken for."""
	rows = frappe.db.sql(
		"""select coalesce(sum(greatest(cap - spent, 0)), 0) from `tabGrove API Key`
		where team = %s and status = 'active' and name != %s""",
		[team, except_key or ""],
	)
	return Decimal(str(rows[0][0]))


def on_doctype_update():
	"""The hash is what the gateways key on and what every lookup here uses: unique, so a
	collision, however unlikely at 192 random bits, refuses the insert rather than merging two
	keys into one, and indexed, so a lookup by hash does not scan the table."""
	frappe.db.add_unique("Grove API Key", ["key_hash"], constraint_name="unique_key_hash")
