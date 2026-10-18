# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Pull usage from each gateway Redis into one Usage Record per key per drain per day, and bill it.

The gateway accumulates each key's deltas in `usage:<prefix>:<YYYY-MM-DD>`, one bucket per UTC day
it served the key, named off the clock that names the key's `spent:<month>`. A pull GETs /usage: the
box sets each live bucket aside under a new drain id and returns it together with every bucket it
set aside earlier that Grove has not acknowledged, as {drain id: {key: {day: hash}}}. Grove records
each hash on its day — so a day and a month here are the gateway's own, however late the pull —
commits, then acknowledges the (drain id, key, day) counters it recorded; the box re-sends every
other one next pull. A pull that fails before the commit gets the same counters again, and one
landed twice records nothing twice (unique drain id + key + day).

Each touched team is landed in one step: its records, the move in each key's `spent` by Grove's
price of what the gateway charged, a Credit Discrepancy per pricing the gateway charged
differently, and the verdict. A team whose step fails is rolled back alone, left unacknowledged,
and logged as Stuck Usage until a later pull lands it. One bad team never holds anyone else's
usage back.

A pull for one team sends only its keys: the box sets aside and returns just those.

The box also hands over its dead lines: usage it spooled while its store was down and then could
not replay. Grove lands each like any other usage, under the drain id `dead:<request id>`; one it
cannot read, or whose key it does not hold, is kept as a Stuck Usage row. Either way the box is told
to drop it.

Gateway Redises are drained in parallel and each drain is recorded the moment it arrives, on the
main thread."""

import hashlib
import json
import time
from decimal import Decimal

import frappe
from frappe.utils import getdate

from grove.billing.doctype.stuck_usage.stuck_usage import record_stuck, resolve_stuck
from grove.billing.pricing import PriceBook
from grove.pathway import snapshot
from grove.pathway.reconcile import Reconciler
from grove.pathway.run import SyncRun, Target, error_text, gateway_units, in_turn
from grove.utils import utc_today

DEAD = "dead:"
# A store of a lakh keys answers tens of MB after as many Redis reads; the run's default is for a push.
DRAIN_TIMEOUT = 120


class Usage(SyncRun):
	"""Every gateway Redis drained once — a store through its first writer that answers — into
	Usage Records. With `team`, only that team's keys, from the stores of their geographies."""

	sync_type = "Usage"
	pause_field = "pause_usage_pull"

	def __init__(self, gateways=None, trigger="Scheduled", wait=0, team=None):
		super().__init__(trigger, wait)
		self.gateways = gateways
		self.team = team
		self.keys = None

	def units(self):
		if self.team:
			keys = frappe.get_all("Grove API Key", filters={"team": self.team}, fields=["name", "geography"])
			self.keys = [k.name for k in keys]
			if not self.keys:
				return []
			geographies = {k.geography for k in keys}
			if self.gateways is None and all(geographies):
				return [unit for g in sorted(geographies) for unit in gateway_units(None, geography=g)]
		return gateway_units(self.gateways)

	def work(self, unit):
		return in_turn(unit, lambda target: fetch_usage(target, self.keys))

	def settle(self, unit, result):
		"""Landed the moment it arrives, then acknowledged so the box stops re-sending it."""
		return record_drain(*super().settle(unit, result), gateway_store=unit.store)


class Peek(Usage):
	"""What the boxes hold of one team right now, read without moving it: a cap check's figure.
	Live buckets come under the drain id "" and set-aside ones under theirs; nothing is landed
	or acknowledged, so the next pull finds it all. Holds the pull lock meanwhile, so no drain
	renames a bucket between the two reads a box makes. No run doc: it logs nothing."""

	def work(self, unit):
		return in_turn(unit, lambda target: fetch_usage(target, self.keys, peek=True))

	def settle(self, unit, result):
		return super(Usage, self).settle(unit, result)

	def record(self, units, answers):
		"""→ {"ok": every store answered, "pending": nano-USD per prefix of what the boxes hold
		that no Usage Record has yet}."""
		rows = [row for _outcome, unit_rows in answers.values() for row in unit_rows]
		reached = all(outcome is not False for outcome, _rows in answers.values())
		ok = reached and all(row.get("success") for row in rows)
		return {"ok": ok, "pending": pending_spend(rows) if ok else {}}


def pull_all(gateways=None, trigger="Scheduled", wait=30, team=None):
	"""Scheduled: pull + drain every gateway Redis. Named `gateways` are pulled themselves; a `team`
	is pulled alone. Waits up to `wait` seconds for a run in flight — a cap check's peek is a
	second — then skips."""
	return Usage(gateways, trigger, wait, team).run()


def peek_team(team):
	"""This month's spend the boxes hold of `team` that Grove has not landed yet, USD per key: what
	a cap check adds to the records so its figure is the boxes' own and not up to an hour old.
	Read without moving anything; the next pull lands it. Refused rather than stale: while pushes
	are paused (a lowered cap would not reach the box), while another pull holds the lock, and
	when a store could not be read. → {prefix: Decimal USD}."""
	from grove.billing.pricing import NANO

	if frappe.db.get_single_value("Grove Settings", "pause_projection"):
		frappe.throw(
			"Pushes to the gateways are paused, so a cap change would not reach them. Try again once they resume."
		)
	peek = Peek(trigger="Manual", wait=30, team=team)
	answer = peek.run()
	if peek.skipped == "locked":
		frappe.throw("Usage is being pulled right now. Try again in a minute.")
	if answer is None:
		return {}
	if not answer["ok"]:
		frappe.throw("A gateway store could not be reached at the moment. Please try again later.")
	return {prefix: Decimal(nano) / NANO for prefix, nano in answer["pending"].items()}


def pending_spend(rows):
	"""Nano-USD per prefix of what peeked boxes hold that no Usage Record has yet: every live
	bucket, every set-aside one whose (drain id, key, day) has not landed — ack or not — and every
	dead line, which is only acknowledged once it lands."""
	pending, aside = {}, {}
	for row in rows:
		for drain_id, keys in (row.get("drains") or {}).items():
			for prefix, days in keys.items():
				for day, h in days.items():
					if drain_id:
						aside[(drain_id, prefix, day)] = int(h.get("cost") or 0)
					else:
						pending[prefix] = pending.get(prefix, 0) + int(h.get("cost") or 0)
		for line in row.get("dead") or []:
			try:
				accrual = json.loads(line["line"])
				pending[accrual["prefix"]] = pending.get(accrual["prefix"], 0) + int(accrual["fields"].get("cost") or 0)
			except Exception:
				continue  # unreadable: the pull keeps it as Stuck Usage, and it prices nothing
	landed = landed_pairs({drain_id for drain_id, _, _ in aside})
	for (drain_id, prefix, day), cost in aside.items():
		if (drain_id, prefix, day) not in landed:
			pending[prefix] = pending.get(prefix, 0) + cost
	return pending


def landed_pairs(drain_ids):
	"""{(drain id, key, day)} of every record these drains have landed: one read however many."""
	if not drain_ids:
		return set()
	rows = frappe.get_all(
		"Usage Record", filters={"drain_id": ("in", sorted(drain_ids))}, fields=["drain_id", "api_key", "day"]
	)
	return {(row.drain_id, row.api_key, str(row.day)) for row in rows}


def fetch_usage(target, keys=None, peek=False):
	"""GET /usage off one gateway: {drain id: {key: {day: hash}}} of everything it holds
	unacknowledged, `keys` alone when given, and its dead lines. `peek` reads the same without
	setting anything aside, the live buckets under the id "". Pool thread: no frappe."""

	def drain(row):
		parts = ([f"keys={','.join(keys)}"] if keys else []) + (["peek=1"] if peek else [])
		answer = target.get("usage" + ("?" + "&".join(parts) if parts else ""), timeout=DRAIN_TIMEOUT)
		row["drains"], row["dead"] = answer.get("drains") or {}, answer.get("dead") or []
		row["spool"] = answer.get("spool") or {}

	return target.dial(drain, had_data=0, drains={}, dead=[], spool={})


def record_drain(outcome, rows, gateway_store=None):
	"""Land what a group drained — main thread, as soon as it arrives — and acknowledge what landed.
	A drain that cannot be recorded at all is rolled back and acknowledges nothing, so the box sends
	it again. `gateway_store` is the store drained, or None for a gateway dialled by name."""
	for row in rows:
		drains, dead = row.pop("drains", None) or {}, row.pop("dead", None) or []
		spool = row.pop("spool", None) or {}
		if not row.get("success"):
			continue
		start = time.monotonic()
		try:
			pulled, acks, dead_acks, stuck = record_drains(row["server"], drains, dead, gateway_store=gateway_store)
			row["had_data"] = 1 if pulled else 0
			row["detail"] = f"pulled:{pulled} stuck:{stuck}" if stuck else f"pulled:{pulled}"
			if spool.get("depth") or spool.get("dead"):
				row["detail"] += f" spool:{spool.get('depth', 0)} dead:{spool.get('dead', 0)}"
			if acks or dead_acks:
				row["detail"] += acknowledge(row["server"], acks, dead_acks)
		except Exception as e:
			frappe.db.rollback()
			row["success"], row["error"], outcome = 0, error_text(e), False
		row["duration_ms"] += int((time.monotonic() - start) * 1000)
	return outcome, rows


def acknowledge(gateway, acks, dead_acks):
	"""Tell the box which pairs and dead lines are recorded. A failed ack loses nothing: the box
	re-sends them and every record is already there. → a note for the row."""
	try:
		Target.resolve("Gateway Server", gateway).post(
			"usage/ack", {"acks": acks, "dead": dead_acks}, timeout=DRAIN_TIMEOUT
		)
		return ""
	except Exception as e:
		return f" ack failed: {error_text(e)}"


def store_of(gateway):
	"""The store a gateway drains. One with none has not been set up, and has nothing to drain."""
	store = snapshot.gateway_store(gateway)
	if not store:
		frappe.throw(f"{gateway} is on no Gateway Store — set it up before pulling its usage.")
	return store


def record_drains(proxy_name, drains, dead=(), gateway_store=None):
	"""Record one gateway's answer — {drain id: {key: {day: hash}}}, each hash on its day — and
	commit. Everything lands under the store drained: the boxes on a store share one set of
	counters. → (hashes pulled, {drain id: {key: days}} to acknowledge, dead line ids to
	acknowledge, teams stuck)."""
	gateway_store = gateway_store or store_of(proxy_name)
	acks, dead_acks, by_team, pulled = {}, [], {}, 0
	team_of = teams_of_keys(drains)
	for drain_id, keys in drains.items():
		for prefix, days in keys.items():
			pulled += len(days)
			if team := team_of.get(prefix):
				by_team.setdefault(team, {}).setdefault(drain_id, {})[prefix] = days
			else:
				acks.setdefault(drain_id, {})[prefix] = list(days)
	for line in dead:
		pulled += 1
		if landing := read_dead_line(line, gateway_store):
			team, prefix, day, fields = landing
			by_team.setdefault(team, {}).setdefault(DEAD + line["id"], {})[prefix] = {day: fields}
		elif line["id"] not in dead_acks:
			dead_acks.append(line["id"])

	book, stuck = PriceBook.load(), 0
	landed = landed_pairs({drain_id for shares in by_team.values() for drain_id in shares})
	for team, shares in by_team.items():
		frappe.db.savepoint("usage_team")
		try:
			for drain_id, keys in shares.items():
				for day, hashes in by_day(keys).items():
					Reconciler(book, served_on(day), gateway_store, drain_id, landed).team(
						team, {k: parse_drain(h, book.counters) for k, h in hashes.items()}
					)
			resolve_stuck(team, gateway_store)
		except Exception:
			frappe.db.rollback(save_point="usage_team")
			record_stuck(team, gateway_store, shares)
			stuck += 1
			continue
		for drain_id, keys in shares.items():
			if drain_id.startswith(DEAD):
				dead_acks.append(drain_id.removeprefix(DEAD))
			else:
				acks.setdefault(drain_id, {}).update({prefix: list(days) for prefix, days in keys.items()})

	frappe.db.commit()
	return pulled, acks, dead_acks, stuck


def teams_of_keys(drains):
	"""{key: its team} for every key the drains name: one read however many. A key Grove does not
	hold is absent."""
	prefixes = {prefix for keys in drains.values() for prefix in keys}
	if not prefixes:
		return {}
	rows = frappe.get_all("Grove API Key", filters={"name": ("in", sorted(prefixes))}, fields=["name", "team"])
	return {row.name: row.team for row in rows}


def by_day(keys):
	"""One drain's {key: {day: hash}} turned {day: {key: hash}}: what lands as one Reconciler each."""
	days = {}
	for prefix, hashes in keys.items():
		for day, h in hashes.items():
			days.setdefault(day, {})[prefix] = h
	return days


def served_on(day):
	"""The UTC day a hash was served, as the gateway named it. Blank is a counter from before the
	gateway bucketed by day: it lands today, as every record did until then."""
	return getdate(day) if day else utc_today()


def read_dead_line(line, gateway_store):
	"""(team, key, day, fields) for a dead line Grove can land — the UTC day it was spooled, blank
	(today) when it carries none — else None after keeping it as Stuck Usage: the box drops it once
	acknowledged, so this row is the only copy left."""
	try:
		accrual = json.loads(line["line"])
		prefix, fields = accrual["prefix"], accrual["fields"]
		team = frappe.db.get_value("Grove API Key", prefix, "team")
		if not team:
			raise ValueError(f"No Grove API Key {prefix!r}")
		return team, prefix, (accrual.get("at") or "")[:10], fields
	except Exception:
		name = line["id"] or "sha1:" + hashlib.sha1(line["line"].encode()).hexdigest()
		record_stuck(None, gateway_store, line, dead_line=name)
		return None


def parse_drain(h, table):
	"""One drained hash split four ways: the key's request count, the per-(model, counter)
	quantities the reports read, and the counters and cost per pricing — `p:` what the gateway
	charged, `f:` what it served free. A priced counter not in `table` is refused, not dropped:
	this Grove is behind the gateway, and the team waits as Stuck Usage until it is not."""
	requests = int(h.get("request_count", 0) or 0)
	counters, pricings, free = {}, {}, {}
	for k, v in h.items():
		if k.startswith("m:"):
			metric, _, model = k[2:].partition(":")  # model may contain ':' — keep the rest
			if model and metric in table:
				counters.setdefault(model, {})[metric] = int(v or 0)
		elif k.startswith(("p:", "f:")):
			pricing, _, counter = k[2:].partition(":")
			if counter not in table and counter != "cost":
				raise ValueError(f"{k}: not a counter Grove prices — is Grove older than the gateway?")
			if pricing:
				(pricings if k[0] == "p" else free).setdefault(pricing, {})[counter] = int(v or 0)
	return frappe._dict(requests=requests, counters=counters, pricings=pricings, free=free)
