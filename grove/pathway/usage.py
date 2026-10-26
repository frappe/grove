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

Each touched team is landed in one step: its records, priced by Grove at the pricing the gateway
tagged each request with, a Credit Discrepancy per pricing the gateway charged differently, and
the verdict. A team whose step fails is rolled back alone, left unacknowledged,
and logged as Stuck Usage until a later pull lands it. One bad team never holds anyone else's
usage back.

A pull for one team sends only its keys: the box sets aside and returns just those.

The box also hands over its dead lines: usage it spooled while its store was down and then could
not replay. Grove lands each like any other usage, under the drain id `dead:<request id>`; one it
cannot read, or whose key it does not hold, is kept as a Stuck Usage row. Either way the box is told
to drop it.

Gateway Redises are drained one at a time and each drain is recorded the moment it arrives."""

import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal

import frappe
from frappe.utils import getdate

from grove.billing.doctype.stuck_usage.stuck_usage import record_stuck, resolve_stuck
from grove.billing.pricing import PriceBook
from grove.pathway import snapshot
from grove.pathway.reconcile import DEAD, Reconciler
from grove.pathway.run import SyncRun, Target, error_text, gateway_units, guarded, in_turn
from grove.utils import utc_today

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
		self.hashes = {}

	def units(self):
		if self.team:
			keys = frappe.get_all(
				"Grove API Key", filters={"team": self.team}, fields=["name", "key_hash", "geography"]
			)
			self.keys = [k.name for k in keys]
			self.hashes = {k.key_hash: k.name for k in keys if k.key_hash}
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
	"""What the boxes count as this month's spend of one team's keys, off each key's own counter —
	the one its cap is gated on — for a cap check. Every store of the team's geographies is asked
	for every key in one call; a key a store does not hold is left out of its answer. No lock and
	no run doc: nothing moves, nothing is logged, and two checks may read beside a pull."""

	def run(self):
		return self.record([self.settle(unit, guarded(self.work, unit)) for unit in self.units()])

	def work(self, unit):
		return in_turn(unit, lambda target: fetch_spent(target, list(self.hashes)))

	def settle(self, unit, result):
		return super(Usage, self).settle(unit, result)

	def record(self, answers):
		"""→ {"ok": every store answered through one of its writers, "spent": nano-USD by key name,
		summed over the stores that hold it}. A writer that failed before the next one answered
		leaves its row, which says nothing about the store."""
		ok = all(outcome is True for outcome, _rows in answers)
		spent = {}
		for _outcome, rows in answers:
			for row in rows:
				for key_hash, nano in (row.get("spent") or {}).items():
					if name := self.hashes.get(key_hash):
						spent[name] = spent.get(name, 0) + int(nano)
		return {"ok": ok, "spent": spent if ok else {}}


def pull_all(gateways=None, trigger="Scheduled", wait=30, team=None):
	"""Scheduled: pull + drain every gateway Redis. Named `gateways` are pulled themselves; a `team`
	is pulled alone. Waits up to `wait` seconds for a run in flight — one team's manual pull —
	then skips."""
	return Usage(gateways, trigger, wait, team).run()


def peek_team(team):
	"""This month's spend the boxes count for each of `team`'s keys, USD by key name: the figure a
	cap check puts beside the records, the boxes' own and not up to an hour old. Refused rather
	than stale: while pushes are paused (a lowered cap would not reach the box) and when a store
	answered through none of its writers. A key no store holds is left out."""
	from grove.billing.pricing import NANO

	if frappe.db.get_single_value("Grove Settings", "pause_projection"):
		frappe.throw(
			"Pushes to the gateways are paused, so a cap change would not reach them. Try again once they resume."
		)
	answer = Peek(trigger="Manual", team=team).run()
	if not answer["ok"]:
		frappe.throw("A gateway store could not be reached at the moment. Please try again later.")
	return {name: Decimal(nano) / NANO for name, nano in answer["spent"].items()}


def fetch_usage(target, keys=None):
	"""GET /usage off one gateway: {drain id: {key: {day: hash}}} of everything it holds
	unacknowledged, `keys` alone when given, and its dead lines."""

	def drain(row):
		answer = target.get("usage" + (f"?keys={','.join(keys)}" if keys else ""), timeout=DRAIN_TIMEOUT)
		row["drains"], row["dead"] = answer.get("drains") or {}, answer.get("dead") or []
		row["spool"] = answer.get("spool") or {}

	return target.dial(drain, had_data=0, drains={}, dead=[], spool={})


def fetch_spent(target, hashes):
	"""GET /spent off one gateway: {key hash: nano-USD this month} of those of `hashes` it holds,
	in one round trip. Nothing moves."""

	def read(row):
		# ponytail: ids ride the query string; a body if a team ever holds thousands of keys.
		row["spent"] = target.get("spent?ids=" + ",".join(hashes)) or {}

	return target.dial(read, spent={})


def landed_pairs(drain_ids):
	"""{(drain id, key, day)} of every record these drains have landed: one read however many."""
	if not drain_ids:
		return set()
	rows = frappe.get_all(
		"Usage Record", filters={"drain_id": ("in", sorted(drain_ids))}, fields=["drain_id", "api_key", "day"]
	)
	return {(row.drain_id, row.api_key, str(row.day)) for row in rows}


def record_drain(outcome, rows, gateway_store=None):
	"""Land what a group drained, as soon as it arrives, and acknowledge what landed.
	A drain that cannot be recorded at all is rolled back and acknowledges nothing, so the box sends
	it again. `gateway_store` is the store drained, or None for a gateway dialled by name."""
	for row in rows:
		drains, dead = row.pop("drains", None) or {}, row.pop("dead", None) or []
		spool = row.pop("spool", None) or {}
		if not row.get("success"):
			continue
		start = time.monotonic()
		try:
			pulled, acks, dead_acks, stuck = record_drains(
				row["server"], drains, dead, gateway_store=gateway_store
			)
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
	counters; which team each hash is, `sort_drains` says. → (hashes pulled, {drain id: {key: days}}
	to acknowledge, dead line ids to acknowledge, teams stuck)."""
	gateway_store = gateway_store or store_of(proxy_name)
	by_team, acks, pulled = sort_drains(drains)
	dead_acks = []
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
					Reconciler(book, served_on(day, drain_id), gateway_store, drain_id, landed).team(
						team, {k: parse_drain(h, book.counters) for k, h in hashes.items()}
					)
			resolve_stuck(team, gateway_store, shares)
		except Exception:
			frappe.db.rollback(save_point="usage_team")
			record_stuck(team, gateway_store, shares)
			stuck += 1
			continue
		for drain_id, keys in shares.items():
			if drain_id.startswith(DEAD):
				dead_acks.append(drain_id.removeprefix(DEAD))
			else:
				for prefix, days in keys.items():
					acks.setdefault(drain_id, {}).setdefault(prefix, []).extend(days)

	frappe.db.commit()
	return pulled, acks, dead_acks, stuck


def teams_of_keys(drains):
	"""{key: its team} for every key the drains name: one read however many. A key Grove does not
	hold is absent."""
	prefixes = {prefix for keys in drains.values() for prefix in keys}
	if not prefixes:
		return {}
	rows = frappe.get_all(
		"Grove API Key", filters={"name": ("in", sorted(prefixes))}, fields=["name", "team"]
	)
	return {row.name: row.team for row in rows}


def sort_drains(drains):
	"""Each hash to the team it lands under: the one the box stamped on it (`team`: a key that
	changed hands has its books closed at the push, under the team charged), else the key's team
	now; a hash of a key Grove does not hold is acknowledged unlanded. → ({team: {drain id: {key:
	{day: hash}}}}, {drain id: {key: [days]}} to acknowledge, hashes seen)."""
	team_of = teams_of_keys(drains)
	by_team, acks, pulled = {}, {}, 0
	for drain_id, keys in drains.items():
		for prefix, days in keys.items():
			pulled += len(days)
			for day, h in days.items():
				if team := h.get("team") or team_of.get(prefix):
					by_team.setdefault(team, {}).setdefault(drain_id, {}).setdefault(prefix, {})[day] = h
				else:
					acks.setdefault(drain_id, {}).setdefault(prefix, []).append(day)
	return by_team, acks, pulled


def by_day(keys):
	"""One drain's {key: {day: hash}} turned {day: {key: hash}}: what lands as one Reconciler each."""
	days = {}
	for prefix, hashes in keys.items():
		for day, h in hashes.items():
			days.setdefault(day, {})[prefix] = h
	return days


def served_on(day, drain_id=""):
	"""The UTC day a hash was served, as the gateway named it. Blank is a counter from before the
	gateway bucketed by day: it lands on the day its drain started, which the drain id carries
	(`20060102T150405Z-…`), so a re-send across midnight meets the record it already left."""
	if day:
		return getdate(day)
	stamp = drain_id[:8]
	# ponytail: a dead line's id is a request id with no stamp, so a blank-day dead line re-sent
	# across midnight could land twice; boxes that spool blank days are gone before that matters.
	return datetime.strptime(stamp, "%Y%m%d").date() if stamp.isdigit() else utc_today()


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
	"""One drained hash split three ways: the key's request count, the per-(model, counter)
	quantities the reports read, and the counters and cost per pricing (`p:`, what the gateway
	charged). A priced counter not in `table` is refused, not dropped, and so is an `f:` tag, which
	only a gateway from before every key was charged writes: this Grove and the gateway disagree,
	and the team waits as Stuck Usage until they do not."""
	requests = int(h.get("request_count", 0) or 0)
	counters, pricings = {}, {}
	for k, v in h.items():
		if k.startswith("m:"):
			metric, _, model = k[2:].partition(":")  # model may contain ':' — keep the rest
			if model and metric in table:
				counters.setdefault(model, {})[metric] = int(v or 0)
		elif k.startswith("f:"):
			raise ValueError(f"{k}: the gateway still tags unbilled usage — is it older than Grove?")
		elif k.startswith("p:"):
			pricing, _, counter = k[2:].partition(":")
			if counter not in table and counter != "cost":
				raise ValueError(f"{k}: not a counter Grove prices — is Grove older than the gateway?")
			if pricing:
				pricings.setdefault(pricing, {})[counter] = int(v or 0)
	return frappe._dict(requests=requests, counters=counters, pricings=pricings)
