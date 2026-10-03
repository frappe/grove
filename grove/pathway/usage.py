# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Pull usage from each gateway Redis into one Usage Record per key per drain, and bill it.

The gateway accumulates per-key deltas in `usage:<prefix>`. A pull GETs /usage: the box sets each
live counter aside under a new drain id and returns it together with every key it set aside earlier
that Grove has not acknowledged, grouped by drain id. Grove records them, commits, then acknowledges
the (drain id, key) pairs it recorded; the box re-sends every other pair next pull. A pull that
fails before the commit gets the same pairs again, and a pair landed twice records nothing twice
(unique drain id + key).

Each touched user is landed in one step: their records, the move in `spent` by Grove's price of
what the gateway charged, a Credit Discrepancy per pricing the gateway charged differently, and
the verdict. A user whose step fails is rolled back alone, left unacknowledged, and logged as Stuck
Usage until a later pull lands it. One bad user never holds anyone else's usage back.

A pull for one user sends only their keys: the box sets aside and returns just those.

The box also hands over its dead lines: usage it spooled while its store was down and then could
not replay. Grove lands each like any other usage, under the drain id `dead:<request id>`; one it
cannot read, or whose key it does not hold, is kept as a Stuck Usage row. Either way the box is told
to drop it.

Gateway Redises are drained in parallel and each drain is recorded the moment it arrives, on the
main thread."""

import hashlib
import json
import time

import frappe

from grove.grove.doctype.stuck_usage.stuck_usage import record_stuck, resolve_stuck
from grove.pathway import snapshot
from grove.pathway.reconcile import Reconciler
from grove.pathway.run import SyncRun, Target, error_text, gateway_units, in_turn
from grove.pricing import PriceBook
from grove.utils import utc_today

DEAD = "dead:"


class Usage(SyncRun):
	"""Every gateway Redis drained once — a store through its first writer that answers — into
	Usage Records. With `user`, only that user's keys, from every store."""

	sync_type = "Usage"

	def __init__(self, gateways=None, trigger="Scheduled", wait=0, user=None):
		super().__init__(trigger, wait)
		self.gateways = gateways
		self.user = user
		self.keys = None

	def units(self):
		if self.user:
			# Every key they ever held: a revoked key's last usage may still be on a box.
			self.keys = frappe.get_all("Grove API Key", filters={"user": self.user}, pluck="name")
			if not self.keys:
				return []
		return gateway_units(self.gateways)

	def work(self, unit):
		return in_turn(unit, lambda target: fetch_usage(target, self.keys))

	def settle(self, unit, result):
		"""Landed the moment it arrives, then acknowledged so the box stops re-sending it."""
		return record_drain(*super().settle(unit, result), gateway_store=unit.store)


def pull_all(gateways=None, trigger="Scheduled", wait=0, user=None):
	"""Scheduled: pull + drain every gateway Redis. Named `gateways` are pulled themselves; a `user`
	is pulled alone. Skips if another pull is in flight, unless told to wait for it."""
	return Usage(gateways, trigger, wait, user).run()


def fetch_usage(target, keys=None):
	"""GET /usage off one gateway: {drain id: {key: hash}} of everything it holds unacknowledged,
	`keys` alone when given, and its dead lines. Pool thread: no frappe."""

	def drain(row):
		answer = target.get("usage" + (f"?keys={','.join(keys)}" if keys else ""), timeout=15)
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
				# The box's usage spool: what it could not write to its store and is still holding.
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
		Target.resolve("Gateway Server", gateway).post("usage/ack", {"acks": acks, "dead": dead_acks})
		return ""
	except Exception as e:
		return f" ack failed: {error_text(e)}"


def store_of(gateway):
	"""The store a gateway drains. One with none has not been set up, and has nothing to drain."""
	store = snapshot.gateway_store(gateway)
	if not store:
		frappe.throw(f"{gateway} is on no Gateway Store — set it up before pulling its usage.")
	return store


def record_drains(proxy_name, drains, dead=(), gateway_store=None, day=None):
	"""Record one gateway's answer under `day` (today unless told otherwise) and commit. Everything
	lands under the store drained: the boxes on a store share one set of counters. → (keys pulled,
	{drain id: keys to acknowledge}, dead line ids to acknowledge, users stuck)."""
	day = day or utc_today()
	gateway_store = gateway_store or store_of(proxy_name)
	acks, dead_acks, by_user, pulled = {}, [], {}, 0
	for drain_id, usages in drains.items():
		for prefix, h in usages.items():
			pulled += 1
			if user := frappe.db.get_value("Grove API Key", prefix, "user"):
				by_user.setdefault(user, {}).setdefault(drain_id, {})[prefix] = h
			else:
				# Nothing in Grove can be billed for it: acknowledged so the box stops re-sending it.
				acks.setdefault(drain_id, []).append(prefix)
	for line in dead:
		pulled += 1
		if landing := read_dead_line(line, gateway_store):
			user, prefix, fields = landing
			by_user.setdefault(user, {}).setdefault(DEAD + line["id"], {})[prefix] = fields
		elif line["id"] not in dead_acks:
			dead_acks.append(line["id"])

	book, stuck = PriceBook.load(), 0
	for user, shares in by_user.items():
		frappe.db.savepoint("usage_user")
		try:
			for drain_id, hashes in shares.items():
				Reconciler(book, day, gateway_store, drain_id).user(
					user, {k: parse_drain(h, book.counters) for k, h in hashes.items()}
				)
			resolve_stuck(user, gateway_store)
		except Exception:
			frappe.db.rollback(save_point="usage_user")
			record_stuck(user, gateway_store, shares)
			stuck += 1
			continue
		for drain_id, hashes in shares.items():
			if drain_id.startswith(DEAD):
				dead_acks.append(drain_id.removeprefix(DEAD))
			else:
				acks.setdefault(drain_id, []).extend(hashes)

	frappe.db.commit()
	return pulled, acks, dead_acks, stuck


def read_dead_line(line, gateway_store):
	"""(user, key, fields) for a dead line Grove can land, else None after keeping it as Stuck
	Usage: the box drops it once acknowledged, so this row is the only copy left."""
	try:
		accrual = json.loads(line["line"])
		prefix, fields = accrual["prefix"], accrual["fields"]
		user = frappe.db.get_value("Grove API Key", prefix, "user")
		if not user:
			raise ValueError(f"No Grove API Key {prefix!r}")
		return user, prefix, fields
	except Exception:
		# An unreadable line comes with a blank id; its text names it instead.
		name = line["id"] or "sha1:" + hashlib.sha1(line["line"].encode()).hexdigest()
		record_stuck(None, gateway_store, line, dead_line=name)
		return None


def parse_drain(h, table):
	"""One drained hash split three ways: the key's request count, the per-(model, counter)
	quantities the reports read, and the counters and cost per pricing the gateway charged at. A
	priced counter not in `table` is refused, not dropped: this Grove is behind the gateway, and the
	user waits as Stuck Usage until it is not."""
	requests = int(h.get("request_count", 0) or 0)
	counters, pricings = {}, {}
	for k, v in h.items():
		if k.startswith("m:"):
			metric, _, model = k[2:].partition(":")  # model may contain ':' — keep the rest
			if model and metric in table:
				counters.setdefault(model, {})[metric] = int(v or 0)
		elif k.startswith("p:"):
			pricing, _, counter = k[2:].partition(":")
			if counter not in table and counter != "cost":
				raise ValueError(f"{k}: not a counter Grove prices — is Grove older than the gateway?")
			if pricing:
				pricings.setdefault(pricing, {})[counter] = int(v or 0)
	return frappe._dict(requests=requests, counters=counters, pricings=pricings)
