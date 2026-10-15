# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Project Grove state into each Gateway Server's local Redis, and the replica table into each
Ingress Server. Grove is the source of truth.

The push is desired state, gated by hashes the AGENT stores (`grove:state_hash`): a tick reads
every box's hashes, then sends each box only the sections and key buckets it does not already
hold. A box that loses its Redis loses its hashes with it, so the next tick rebuilds everything for
it; no special path, it simply holds no key hash.

What a tick builds is bounded by a stamp. Every Gateway Store, every gateway not yet on one and
every Ingress Server carries `last_synced_at`: the read time of the last tick it took a push from
or already held. A tick rebuilds only the key buckets a save moved since the oldest stamp among
the gateway units that answered, less LOOKBACK; everything under force, every FULL_EVERY minutes,
for a unit with no stamp or no key hashes, after a Tier or Grove Settings save, and past
FULL_ABOVE dirty buckets.

What each plane is given is what keeps them apart: a GATEWAY takes the full snapshot with only its
own Geography's routes, an INGRESS takes only the replica table for the boxes it owns.

`sync_projection` is the cron tick and the ONLY automatic path: nothing pushes inline; state moves,
and the next tick carries it. `full_sync` is the operator buttons: force-push, skipping the gate.

The tick also carries each store the spend adjustments it is owed (a Credit Discrepancy decided
against the gateway): not state, so not hashed — sent after it, every tick until the box answers."""

from datetime import timedelta

import frappe

from grove.billing.doctype.credit_discrepancy.credit_discrepancy import mark_corrected, pending_adjustments
from grove.pathway import snapshot
from grove.pathway.run import InSync, SyncRun, Target, Unit, each_in_parallel, gateway_units, in_turn, redact

LOOKBACK = 120  # seconds behind a stamp a save is still read: one stamped before the read commits after it
FULL_EVERY = 10  # minutes: every bucket rebuilt and compared, whatever the stamps say
FULL_ABOVE = 32  # dirty buckets past which naming their keys costs more than reading every key


class Projection(SyncRun):
	"""Every target box brought to the current desired state. A fleet already in sync leaves no
	doc: a Pathway Sync is written only when something was pushed or failed."""

	sync_type = "Projection"
	pause_field = "pause_projection"

	def __init__(self, trigger="Scheduled", proxies=None, ingresses=None, force=False, wait=0):
		super().__init__(trigger, wait)
		self.proxies = proxies
		self.ingresses = ingresses
		self.force = force
		self.read_at = None
		self.remote = {}
		self.labels = None  # the key buckets this tick rebuilt; None is all of them
		self.deltas = {}
		self.adjustments = {}

	def units(self):
		"""Both kinds default to every Active box. `is None` and not truthiness: an empty list is a
		caller saying "no boxes of this kind", which is how an ingress-only run asks for no gateway
		work. Everything a pool thread will need is decided here: what each box is sent."""
		ingresses = self.ingresses
		if ingresses is None:
			ingresses = [] if self.proxies else active_ingresses()
		units = gateway_units(self.proxies) + [
			Unit(None, (Target.resolve("Ingress Server", ingress),)) for ingress in ingresses
		]
		targets = [target for unit in units for target in unit.targets]
		self.read_at = frappe.utils.now_datetime()
		self.remote = read_hashes(targets)
		self.labels = self.buckets_in_play(units)
		snapshots = snapshots_for(targets, self.labels)
		self.deltas = {target: self.delta_for(target, snapshots[target]) for target in targets}
		self.adjustments = pending_adjustments()
		return units

	def answered_units(self, units):
		"""(unit, its hashes) for each gateway unit some writer answered: a store's writers share
		one Redis, so any one's map is the store's."""
		for unit in units:
			for target in unit.targets:
				remote = self.remote.get(target)
				if target.server_type == "Gateway Server" and isinstance(remote, dict):
					yield unit, remote
					break

	def buckets_in_play(self, units):
		"""The key buckets this tick rebuilds: those a save moved since the oldest stamp among the
		gateway units that answered, less LOOKBACK. None — every bucket — under force, on a full
		minute, for a unit with no stamp or no key hash, after a Tier or Grove Settings save, and
		past FULL_ABOVE. Nothing when no gateway answered."""
		if self.force or is_full_tick(self.read_at):
			return None
		stamps = []
		for unit, remote in self.answered_units(units):
			stamp = frappe.db.get_value(*stamp_doc(unit), "last_synced_at")
			if not stamp or not any(label.startswith("keys:") for label in remote):
				return None
			stamps.append(frappe.utils.get_datetime(stamp))
		if not stamps:
			return set()
		since = min(stamps) - timedelta(seconds=LOOKBACK)
		if snapshot.needs_full(since):
			return None
		labels = snapshot.dirty_buckets(since)
		return None if len(labels) > FULL_ABOVE else labels

	def delta_for(self, target, desired):
		"""What `target` is sent: the whole snapshot under force, else what its hashes lack among
		the buckets in play. A read that failed is kept as is, for the dial to raise; a box never
		resolved gets nothing, its dial fails on its own error first."""
		remote = self.remote.get(target)
		if target.error or isinstance(remote, Exception):
			return remote
		if self.force:
			return dict(desired)
		return snapshot.snapshot_delta(desired, remote or {}, self.labels)

	def work(self, unit):
		owed = self.adjustments.get(unit.store, ())
		return in_turn(unit, lambda target: push_target(target, self.deltas.get(target), owed))

	def settle(self, unit, result):
		outcome, rows = super().settle(unit, result)
		stamp_synced(unit, outcome, self.read_at)
		for row in rows:
			mark_corrected(row.pop("adjusted", {}))
		return outcome, rows


def sync_projection(trigger="Scheduled", proxies=None, ingresses=None, force=False, wait=0):
	"""The cron tick: bring every target box to the current desired state. The scheduler swallows
	a job's exception and keeps no log of this one, so a failure lands in the Error Log here."""
	try:
		return Projection(trigger, proxies, ingresses, force, wait).run()
	except Exception:
		frappe.log_error(title="Pathway projection failed")
		raise


def full_sync(proxies=None, trigger="Manual", ingresses=None, wait=60):
	"""Force-push the complete snapshot, skipping the hash gate. A button means "this box missed
	something", so it WAITS for an in-flight run rather than skipping."""
	return Projection(trigger, proxies, ingresses, force=True, wait=wait).run()


def is_full_tick(at):
	"""Every FULL_EVERY minutes every bucket is rebuilt and compared, whatever the stamps say: the
	one path that heals a store restored from a backup, whose stamp is fresh and map is full."""
	return at.minute % FULL_EVERY == 0


def snapshots_for(targets, labels=None):
	"""{target: its desired state}, read on the main thread — a gateway snapshot once per
	geography over one `keys` section (the buckets in `labels`, all when None), an ingress its
	own replica table."""
	per_geography, snapshots, keys = {}, {}, None
	for target in targets:
		if target.server_type == "Ingress Server":
			snapshots[target] = snapshot.ingress_snapshot(target.name)
			continue
		if keys is None:
			keys = snapshot.keys_section(labels)
		geography = snapshot.gateway_geography(target.name)
		if geography not in per_geography:
			per_geography[geography] = snapshot.gateway_snapshot(geography, keys)
		snapshots[target] = per_geography[geography]
	return snapshots


def read_hashes(targets):
	"""Every box's state hashes, read side by side on the pool: plain requests, no frappe. A box
	that did not answer keeps its exception, which its dial raises so the row reads as the
	failure it was. A box that could not be resolved is not asked."""
	asked = list(dict.fromkeys(target for target in targets if not target.error))
	answers = dict(each_in_parallel(asked, Target.remote_hashes))
	return {target: answers[index] for index, target in enumerate(asked)}


def push_target(target, delta, adjustments=()):
	"""Send one box `delta` — decided on the main thread, or the exception its hash read ended in —
	then the spend adjustments its store is owed. None when it holds the state and is owed nothing:
	nothing pushed, nothing to log. Runs on a pool thread: no frappe here, and the payload and the
	answers come back raw."""

	def push(row):
		if isinstance(delta, Exception):
			raise delta
		if not delta and not adjustments:
			raise InSync
		if delta:
			# Recorded before the push: what a rejected one tried to send is the whole question.
			row["payload"].append({"push": "state", "body": redact(delta)})
			row["detail"] = snapshot.describe(delta, target.post("state", delta))
		# After the state, so an adjustment the box refuses never holds it back.
		for body in adjustments:
			row["payload"].append({"push": "spend-adjust", "body": body})
			row["adjusted"][body["id"]] = target.post("spend-adjust", body)["spent"]
		if adjustments:
			row["detail"] = f"{row['detail']} spend-adjust:{len(adjustments)}".strip()

	return target.dial(push, payload=[], adjusted={})


def check_state(server_type, name):
	"""What a tick would push right now, without pushing it, every bucket rebuilt. A bracketed
	count is how many buckets of that section differ."""
	target = Target.of(frappe.get_doc(server_type, name))
	desired = snapshots_for([target])[target]
	delta = snapshot.snapshot_delta(desired, target.remote_hashes())
	drift = [snapshot.section_label(section, content) for section, content in delta.items()]
	return {"in_sync": not delta, "drift": sorted(drift)}


def active_ingresses():
	"""One with no Network is skipped rather than thrown on: a scheduled run must not die over one
	misconfigured box."""
	return frappe.get_all(
		"Ingress Server", filters={"status": "Active", "network": ("is", "set")}, pluck="name"
	)


def stamp_doc(unit):
	"""Where a unit's stamp lives: its store — one Redis, however many writers — else the box."""
	if unit.store:
		return "Gateway Store", unit.store
	target = unit.targets[0]
	return target.server_type, target.name


def stamp_synced(unit, outcome, at):
	"""Record that a unit holds the desired state as of `at`, the tick's read time — on an in-sync
	skip, where no row is written and this is the only trace, and on a successful push. Never on
	failure: the stamp going stale is what says a box has been failing, and what makes the next
	tick that reaches it rebuild everything saved since."""
	if outcome is False:
		return False
	frappe.db.set_value(*stamp_doc(unit), "last_synced_at", at, update_modified=False)
	return True
