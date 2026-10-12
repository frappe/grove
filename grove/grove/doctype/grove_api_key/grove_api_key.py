# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import hashlib
import secrets
import frappe
from frappe.model.document import Document
from frappe.utils import flt

from grove.access import team_limits, validate_limit_rows, validate_model_geography

KEY_PREFIX = "gr_"
# Guards a race where a key is created and revoked at the same time.
REVOKE_AFTER_HOURS = 6


def hash_secret(secret: str) -> str:
	"""The Redis key id the gateway uses = sha256 of the full presented key."""
	return hashlib.sha256(secret.encode()).hexdigest()


class GroveAPIKey(Document):
	"""A credential and the whole policy behind it: which models (its groups, plus its own
	Allow, less its Deny), the one geography it works in, its rate limits (its own rows, else its
	team's Tier's), and `cap` — what it may spend a month, within its team's spend
	limit. The gateway holds it as one record, so every gate reads the key and nothing else; a key
	lives on one geography's store, which counts its spend per month and gates its cap exactly
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
		"""A new key starts in its geography's default Model Group unless the insert names its own
		groups. With no limit rows of its own it runs under its team's Tier."""
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

	def check_key_count(self):
		"""Rate limits are per key, so the team's key count is what bounds its throughput. A team with
		`max_keys` 0 holds as many as it likes."""
		max_keys = team_limits(self.team).max_keys
		active = frappe.db.count("Grove API Key", {"team": self.team, "status": "active"})
		if max_keys and active >= max_keys:
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
		validate_limit_rows(self.limits)
		before = self.get_doc_before_save()
		if before:
			# Its counters and spend live on the geography's store: a key does not move. Mint another.
			if before.geography != self.geography:
				frappe.throw("A key's geography is set once. Mint a new key for another geography.")
			# save() writes every column and the pull writes this one behind the form's back.
			self.spent = frappe.db.get_value("Grove API Key", self.name, "spent", for_update=True)
		if flt(self.cap) < 0:
			frappe.throw("A cap cannot be negative.")
		# A raise, or a key coming back live: either puts its cap back under the team's limit.
		revived = before and before.status != "active" and self.status == "active"
		if flt(self.cap, 9) > flt(before.cap if before else 0, 9) or revived:
			self.validate_cap()

	def validate_cap(self):
		"""A raise keeps the team's live caps within its monthly spend limit: every store gates its
		key exactly, so the keys together never outspend the limit. Only a raise is checked, so a
		lowered limit stops no key and a key can always be minted at 0, refused until capped."""
		team = team_limits(self.team, for_update=True)
		if team.free or self.status != "active":
			return
		total = flt(capped(self.team, except_key=self.name) + flt(self.cap), 9)
		if total > flt(team.spend_limit, 9):
			frappe.throw(
				f"Caps would add up to {total:.2f} a month of a spend limit of {flt(team.spend_limit):.2f}. "
				"Lower another key's cap first."
			)

	@property
	def revocable_at(self):
		"""When `revoke` stops refusing this key, in site time. Parsed first: a doc the desk sends
		back carries `creation` as a string, and `add_to_date` hands a string back for one."""
		return frappe.utils.add_to_date(frappe.utils.get_datetime(self.creation), hours=REVOKE_AFTER_HOURS)

	@frappe.whitelist()
	def revoke(self):
		"""Retire the credential. The row stays as the record that it existed and when it stopped,
		its cap leaves the team's spend limit free for another key, and the gateways' copy goes
		when the next sync prunes the unprojected key."""
		if frappe.utils.now_datetime() < self.revocable_at:
			frappe.throw(f"API key cannot be revoked less than {REVOKE_AFTER_HOURS} hours of it's creation")

		self.status = "revoked"
		self.save(ignore_permissions=True)


def capped(team, except_key=None):
	"""Σ the monthly caps of the team's live keys: how much of its spend limit is spoken for."""
	rows = frappe.db.sql(
		"""select coalesce(sum(cap), 0) from `tabGrove API Key`
		where team = %s and status = 'active' and name != %s""",
		[team, except_key or ""],
	)
	return flt(rows[0][0], 9)


def on_doctype_update():
	"""The hash is what the gateways key on and what every lookup here uses: unique, so a
	collision, however unlikely at 192 random bits, refuses the insert rather than merging two
	keys into one, and indexed, so a lookup by hash does not scan the table."""
	frappe.db.add_unique("Grove API Key", ["key_hash"], constraint_name="unique_key_hash")
