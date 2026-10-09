# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Project Grove state into each Gateway Server's local Redis, and the replica table into each
Ingress Server. Grove is the source of truth.

The push is desired state, whole, gated by hashes the AGENT stores (`grove:state_hash`): each tick
builds the snapshot, reads the box's hashes, and sends only the sections it does not already hold.
A box that loses its Redis loses its hashes with it, so the next tick re-pushes everything; that is
the only repair path there is.

What each plane is given is what keeps them apart: a GATEWAY takes the full snapshot with only its
own Geography's routes, an INGRESS takes only the replica table for the boxes it owns.

`sync_projection` is the cron tick and the ONLY automatic path: nothing pushes inline; state moves,
and the next tick carries it. `full_sync` is the operator buttons: force-push, skipping the gate.

The tick also carries each store the spend adjustments it is owed (a Credit Discrepancy decided
against the gateway): not state, so not hashed — sent after it, every tick until the box answers."""

import frappe

from grove.billing.doctype.credit_discrepancy.credit_discrepancy import mark_corrected, pending_adjustments
from grove.pathway import snapshot
from grove.pathway.run import InSync, SyncRun, Target, Unit, gateway_units, in_turn, redact


class Projection(SyncRun):
	"""Every target box brought to the current desired state. A fleet already in sync leaves no
	doc: a Pathway Sync is written only when something was pushed or failed."""

	sync_type = "Projection"

	def __init__(self, trigger="Scheduled", proxies=None, ingresses=None, force=False, wait=0):
		super().__init__(trigger, wait)
		self.proxies = proxies
		self.ingresses = ingresses
		self.force = force
		self.snapshots = {}
		self.adjustments = {}

	def units(self):
		"""Both kinds default to every Active box. `is None` and not truthiness: an empty list is a
		caller saying "no boxes of this kind", which is how an ingress-only run asks for no gateway
		work."""
		ingresses = self.ingresses
		if ingresses is None:
			ingresses = [] if self.proxies else active_ingresses()
		units = gateway_units(self.proxies) + [
			Unit(None, (Target.resolve("Ingress Server", ingress),)) for ingress in ingresses
		]
		self.snapshots = snapshots_for([target for unit in units for target in unit.targets])
		self.adjustments = pending_adjustments()
		return units

	def work(self, unit):
		owed = self.adjustments.get(unit.store, ())
		return in_turn(unit, lambda target: push_target(target, self.snapshots[target], self.force, owed))

	def settle(self, unit, result):
		outcome, rows = super().settle(unit, result)
		stamp_synced(unit, outcome)
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


def snapshots_for(targets):
	"""{target: what it is pushed}, read on the main thread — a gateway snapshot once per
	geography with the key records built once, an ingress its own replica table."""
	per_geography, snapshots, shared = {}, {}, {}
	for target in targets:
		if target.server_type == "Ingress Server":
			snapshots[target] = snapshot.ingress_snapshot(target.name)
			continue
		geography = snapshot.gateway_geography(target.name)
		if geography not in per_geography:
			per_geography[geography] = snapshot.gateway_snapshot(geography, shared=shared)
		snapshots[target] = per_geography[geography]
	return snapshots


def push_target(target, desired, force, adjustments=()):
	"""Bring one box to `desired`, then send the spend adjustments its store is owed. None when it
	holds the state and is owed nothing — nothing pushed, nothing to log. Runs on a pool thread: no
	frappe here, and the payload and the answers come back raw."""

	def push(row):
		delta = desired if force else snapshot.snapshot_delta(desired, target.remote_hashes())
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
	"""What a tick would push right now, without pushing it. A bracketed count is how many buckets
	of that section differ."""
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


def stamp_synced(unit, outcome):
	"""Record that an ingress holds the desired state — on an in-sync skip, where no row is written
	and this is the only trace, and on a successful push. Never on failure: the timestamp going
	stale is what says a box has been failing."""
	target = unit.targets[0] if unit.targets else None
	if outcome is False or not target or target.server_type != "Ingress Server":
		return False
	frappe.db.set_value(
		target.server_type, target.name, "last_synced_at", frappe.utils.now_datetime(), update_modified=False
	)
	return True
