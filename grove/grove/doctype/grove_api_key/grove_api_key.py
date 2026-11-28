# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import hashlib
import secrets

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, flt, get_datetime, now_datetime

from grove.access import team_limits
from grove.billing.pricing import committed, forget_figures, lock_team, usd
from grove.infrastructure.doctype.geography.geography import default_geography

KEY_PREFIX = "gr_"
# The expiry rules, days after the mint; Never is not in it.
EXPIRY_DAYS = {"1 Day": 1, "7 Days": 7, "14 Days": 14, "30 Days": 30}


def hash_secret(secret: str) -> str:
	"""The Redis key id the gateway uses = sha256 of the full presented key."""
	return hashlib.sha256(secret.encode()).hexdigest()


class GroveAPIKey(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.grove.doctype.model_group_row.model_group_row import ModelGroupRow

		api_secret: DF.Password | None
		cap: DF.Currency
		expires_at: DF.Datetime | None
		expiry: DF.Literal["Never", "1 Day", "7 Days", "14 Days", "30 Days"]
		expiry_from_first_use: DF.Check
		geography: DF.Link
		key_hash: DF.Data | None
		masked: DF.Data | None
		model_groups: DF.TableMultiSelect[ModelGroupRow]
		status: DF.Literal["active", "revoked"]
		team: DF.Link
		title: DF.Data | None
	# end: auto-generated types

	def before_insert(self):
		# Blank is no title: NULL, which the (title, team) unique index never counts as a twin.
		self.title = self.title or None
		full_key = KEY_PREFIX + secrets.token_hex(24)
		self.key_hash = hash_secret(full_key)
		self.api_secret = full_key
		self.masked = f"{full_key[: len(KEY_PREFIX) + 4]}…"
		days = EXPIRY_DAYS.get(self.expiry)
		self.expires_at = add_days(self.creation, days) if days and not self.expiry_from_first_use else None
		self.set_geography()

	def set_geography(self):
		"""One geography per key: blank is the default one."""
		if not self.geography:
			self.geography = default_geography()
		if not self.geography:
			frappe.throw("Mark one Geography as default, or pick a geography for this key.")

	def on_update(self):
		forget_figures(self.team)

	def validate(self):
		self.validate_groups()
		if flt(self.cap) < 0:
			frappe.throw("A cap cannot be negative.")

		before = self.get_doc_before_save()
		if before and before.status == "revoked" and self.status != "revoked":
			frappe.throw("A revoked key stays revoked. Mint a new one.")
		if self.needs_cap_check:
			self.validate_cap()

	def validate_groups(self):
		"""A group is one geography's, as the key is: every group named here is of the key's."""
		if not self.model_groups:
			return
		groups = frappe.get_all(
			"Model Group",
			filters={"name": ("in", [row.model_group for row in self.model_groups])},
			fields=["name", "geography"],
		)
		for group in groups:
			if group.geography != self.geography:
				frappe.throw(
					f"Model Group {group.name!r} is of {group.geography}, not this key's {self.geography}."
				)

	def validate_set_only_once(self):
		"""`team`, `title` and the expiry rule are set once — except by `transfer`, the one move
		that changes hands."""
		if not self.flags.transfer:
			super().validate_set_only_once()

	def check_if_latest(self):
		"""Frappe locks this row from here. A raise takes the team row ahead of it, so two raises
		queue on the team rather than deadlock on each other's row."""
		if self.needs_cap_check:
			lock_team(self.team)
		super().check_if_latest()

	@property
	def needs_cap_check(self):
		"""Only a raise of a live key: a lowered limit stops no key and a key can always be minted
		at 0, refused until capped. `flags.cap_checked` says the caller checked the whole batch
		(`api.update_caps`, lowers first, so no step passes what the end does not). A team whose
		Key Cap Window is Total has no spend limit: its caps are for life, so the month binds
		nothing. The cap before is read without a lock: this decides whether to lock."""
		if self.status != "active" or self.flags.cap_checked:
			return False
		if team_limits(self.team).key_cap_window == "Total":
			return False
		before = 0 if self.is_new() else frappe.db.get_value("Grove API Key", self.name, "cap")
		return flt(self.cap, 9) > flt(before, 9)

	def validate_cap(self):
		"""A raise or a mint keeps what the month binds of the team's spend limit within it: each
		live key's cap or what it spent this month, whichever is more, plus what revoked keys spent
		(`pricing.committed`). So moving a spent cap to another key, or revoking and minting afresh,
		frees nothing. The boxes are read here, under the team row."""
		from grove.pathway.usage import peek_team

		team = team_limits(self.team, for_update=True)
		total = committed(self.team, {self.name: self.cap}, box_spent=peek_team(self.team))
		if total > flt(team.spend_limit, 9):
			frappe.throw(
				f"This month's spend and caps would add up to {usd(total)} of a spend limit of "
				f"{usd(team.spend_limit)}."
			)

	@frappe.whitelist()
	def revoke(self):
		"""The row stays as the record that it existed and when it stopped, its cap leaves the team's
		spend limit free for another key, and the gateways' copy goes when the next sync prunes the
		unprojected key."""
		self.status = "revoked"
		self.save()

	def transfer(self, to_team: str, title: str | None = None):
		"""The trial's rules go with the trial: its own groups are cleared and its expiry lifted, so
		what it may call is the new team's; cap and geography stay. Counted against the new team like
		a mint. The next tick re-pushes team and window — the push at which the box closes the old
		team's books. Refused between two teams of one cap window: the box keeps a key's
		spend under one field per window, so it would follow the key."""
		if to_team == self.team:
			return
		if self.status != "active":
			frappe.throw("A revoked key stays revoked. Mint a new one.")
		window = team_limits(to_team).key_cap_window
		if window == team_limits(self.team).key_cap_window:
			frappe.throw(
				f"{to_team} caps keys over the same window as {self.team} ({window}): what this key spent "
				"would follow it. Mint a new one."
			)
		title = title or self.title
		old_team, old_title = self.team, self.title
		self.team = to_team
		if window != "Total":
			self.validate_cap()
		self.title = title
		self.expiry, self.expiry_from_first_use, self.expires_at = "Never", 0, None
		self.set("model_groups", [])
		self.flags.transfer = True
		self.save()
		frappe.get_doc(
			{
				"doctype": "Key Transfer",
				"api_key": self.name,
				"from_team": old_team,
				"to_team": to_team,
				"from_title": old_title,
			}
		).insert()
		forget_figures(old_team)

	def extend_expiry(self, days: int):
		"""`days` more from now, or from its moment when that is later — the arithmetic of the trial
		it follows. A key without a moment (Never, or from first use and unused) keeps none. No
		`modified` bump, as `start_expiry`: the boxes have nothing to learn."""
		if self.status != "active":
			frappe.throw("A revoked key stays revoked. Mint a new one.")
		if days <= 0:
			frappe.throw("Enter a positive number of days.")
		if not self.expires_at:
			return
		later = max(get_datetime(self.expires_at), now_datetime())
		self.db_set("expires_at", add_days(later, days), update_modified=False)
		forget_figures(self.team)


def capped(team):
	"""Σ the monthly caps of the team's live keys: how much of its spend limit is spoken for."""
	rows = frappe.db.sql(
		"select coalesce(sum(cap), 0) from `tabGrove API Key` where team = %s and status = 'active'",
		[team],
	)
	return flt(rows[0][0], 9)


def start_expiry(keys):
	"""A usage record landed for each of `keys`: the ones minted to expire from first use get their
	`expires_at` now, from their own rule, once; a key that has its moment keeps it."""
	due = frappe.get_all(
		"Grove API Key",
		filters=[
			["name", "in", keys],
			["status", "=", "active"],
			["expiry_from_first_use", "=", 1],
			["expiry", "!=", "Never"],
			["expires_at", "is", "not set"],
		],
		fields=["name", "expiry"],
	)
	for key in due:
		# No `modified` bump: the wire carries no expiry, so the boxes have nothing to learn.
		frappe.db.set_value(
			"Grove API Key",
			key.name,
			"expires_at",
			add_days(now_datetime(), EXPIRY_DAYS[key.expiry]),
			update_modified=False,
		)


def revoke_expired():
	"""Hourly. One that fails is logged and left for the next hour, the rest still go."""
	# `is set` first: a bare `<=` reads a NULL as year 1 and would take every Never key.
	due = frappe.get_all(
		"Grove API Key",
		filters=[
			["status", "=", "active"],
			["expires_at", "is", "set"],
			["expires_at", "<=", now_datetime()],
		],
		pluck="name",
	)
	for name in due:
		try:
			frappe.get_doc("Grove API Key", name).revoke()
		except Exception:
			frappe.log_error(title=f"Expired key {name} could not be revoked")


def on_doctype_update():
	"""The hash is what the gateways key on and what every lookup here uses: unique, so a
	collision, however unlikely at 192 random bits, refuses the insert rather than merging two
	keys into one, and indexed, so a lookup by hash does not scan the table."""
	frappe.db.add_unique("Grove API Key", ["key_hash"], constraint_name="unique_key_hash")
	# A title is set once and used once per team, ever, revoked keys included: Press titles a trial
	# site's key by the site, and a site must never get a second. NULL titles do not collide.
	# Title leads: Frappe names a twin by the index's first column ("Title must be unique").
	frappe.db.add_unique("Grove API Key", ["title", "team"], constraint_name="unique_title_team")
