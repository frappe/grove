# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Read every vendor key's counters off every gateway store into Provider Key Stats, every 6 hours.

The gateway counts what each key answered in `pk:<key id>` and clears it when read. A pull asks
each store — through its first writer that answers — for every key Grove holds and lands one row
per key the store dialled since the last pull. Nothing is acknowledged: a pull that does not land
is lost, which stats can afford."""

from datetime import datetime
from zoneinfo import ZoneInfo

import frappe

from grove.pathway.run import SyncRun, gateway_units, in_turn

COUNTS = ("requests", "ok", "rate_limited", "unauthorized", "unpaid", "forbidden", "rejected", "failed")
LAST = ("last_used", "last_rate_limited")


class KeyStats(SyncRun):
	"""Every gateway store read once, every key asked of each."""

	sync_type = "Key Stats"
	pause_field = "pause_usage_pull"

	def __init__(self, gateways=None, trigger="Scheduled", wait=0):
		super().__init__(trigger, wait)
		self.gateways = gateways
		self.keys = {}

	def units(self):
		self.keys = held_keys()
		if not self.keys:
			return []
		return gateway_units(self.gateways)

	def work(self, unit):
		return in_turn(unit, lambda target: fetch_key_stats(target, list(self.keys)))

	def settle(self, unit, result):
		"""Landed the moment it arrives."""
		outcome, rows = super().settle(unit, result)
		for row in rows:
			stats = row.pop("stats", None) or {}
			if not row.get("success"):
				continue
			landed = record_key_stats(stats, self.keys, unit.store)
			row["had_data"], row["detail"] = int(landed > 0), f"keys:{landed}"
		return outcome, rows


def pull_all(gateways=None, trigger="Scheduled", wait=0):
	"""Scheduled: one Provider Key Stats row per key per store. Skips if a pull is in flight."""
	return KeyStats(gateways, trigger, wait).run()


def fetch_key_stats(target, ids):
	"""GET /provider-keys off one gateway: {key id: its counts}."""

	def work(row):
		row["stats"] = target.get("provider-keys?ids=" + ",".join(ids))

	return target.dial(work, stats={})


def held_keys():
	"""{key id: (its Model Provider, its title)} for every vendor key Grove holds."""
	rows = frappe.get_all(
		"Model Provider Key", fields=["name", "parent", "title"], parent_doctype="Model Provider"
	)
	return {row.name: (row.parent, row.title) for row in rows}


def record_key_stats(stats, keys, gateway_store):
	"""One row per key the store has dialled — zero requests means it never did. → how many."""
	landed = 0
	for key_id, counts in stats.items():
		if key_id not in keys or not int(counts.get("requests") or 0):
			continue
		model_provider, title = keys[key_id]
		frappe.get_doc(
			{
				"doctype": "Provider Key Stats",
				"model_provider": model_provider,
				"key_title": title,
				"key_id": key_id,
				"gateway_store": gateway_store,
				"stats": frappe.as_json(readable(counts)),
			}
		).insert(ignore_permissions=True)
		landed += 1
	return landed


def readable(counts):
	"""The store's hash as the row keeps it, indented: whole counts, and each `last_*` second as
	site time."""
	stats = {field: int(counts.get(field) or 0) for field in COUNTS}
	for field in LAST:
		at = from_unix(int(counts.get(field) or 0))
		stats[field] = str(at) if at else None
	return stats


def from_unix(seconds):
	"""A gateway's unix second as the site's naive datetime; 0 is never."""
	if not seconds:
		return None
	return datetime.fromtimestamp(seconds, ZoneInfo(frappe.utils.get_system_timezone())).replace(tzinfo=None)
