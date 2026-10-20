# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""The desired state a box is pushed, and the hash gate that decides which sections travel.

`groups` and `routes` travel whole. `keys` scale with customer count, so they split into 256
buckets (`bucket_of`) hashed independently — one key minted re-pushes one bucket, not the
population. Absence prunes: a deleted group or revoked key stops being named and the agent
removes it.

A tick rebuilds only the buckets a save moved since the boxes' stamps (`dirty_buckets`), finding
their keys by hashing every live key's hash (`bucket_names`): nothing stores the bucket yet."""

import hashlib
import json

import frappe

from grove.access import effective_limits, group_rows, limit_entries, model_rows
from grove.billing.pricing import nano
from grove.pathway import routes


def effective_groups():
	"""Every Model Group projected for the gateway: what it grants. One record per group
	however many keys point at it — the reason the group is not flattened onto each key."""
	granted = model_rows("Model Group")
	return [
		{
			"name": name,
			"models": ",".join(granted.get(name, {}).get("models", [])),
		}
		for name in sorted(frappe.get_all("Model Group", pluck="name"))
	]


def effective_keys(names=None):
	"""Every LIVE API Key projected for the gateway, with the whole policy the gates read: its
	groups and own allow/deny, its geography, its rate limits (its team's tier's, else the defaults,
	with its own rows replacing the tier's on the metrics they name) and `budget` — its monthly
	cap; the box counts its spend per month and refuses it once this month's reaches that. What is the team's rides on each key:
	`log_payloads`, and `limited` — the team is suspended, which refuses every key whatever its
	own cap says.

	`names` narrows it to those keys, with their teams and rows: what a tick rebuilds. Revoked
	keys are not projected: absent from their bucket, the push prunes them off every box. The row
	stays in Grove as the record of a credential that existed."""
	if names is not None and not names:
		return []
	filters = {"status": "active"}
	if names is not None:
		filters["name"] = ("in", sorted(names))
	keys = frappe.get_all(
		"Grove API Key", filters=filters, fields=["name", "key_hash", "team", "status", "geography", "cap"]
	)
	if names is not None and not keys:
		return []
	team_filter = None if names is None else {"name": ("in", sorted({k.team for k in keys if k.team}))}
	teams = {
		t.name: t
		for t in frappe.get_all(
			"Central Team", filters=team_filter, fields=["name", "log_payloads", "suspended", "tier"]
		)
	}
	deltas = model_rows("Grove API Key", names)
	memberships = group_rows(names)
	limits = effective_limits(
		keys, limit_entries("Grove API Key", names), {name: t.tier for name, t in teams.items()}
	)
	records = []
	for k in sorted(keys, key=lambda k: k.key_hash or ""):
		team = teams.get(k.team)
		if not k.key_hash or team is None:
			continue
		records.append(
			{
				"key_hash": k.key_hash,
				"prefix": k.name,
				"team": k.team,
				"status": k.status or "active",
				"group": ",".join(memberships.get(k.name, [])),
				"allow": ",".join(deltas.get(k.name, {}).get("allow", [])),
				"deny": ",".join(deltas.get(k.name, {}).get("deny", [])),
				"limited": bool(team.suspended),
				"log_payloads": bool(team.log_payloads),
				"geography": k.geography or "",
				"budget": nano(k.cap or 0),
				# Blank when none: the box merges fields, so leaving it out would keep a removed limit in force.
				"limits": ",".join(
					f"{r['metric']}:{r['window']}:{r['value']}" for r in limits.get(k.name, [])
				),
			}
		)
	return records


def dirty_buckets(since):
	"""The key buckets a save after `since` moved: a saved key's own — revoked or not, a revoke
	being what empties its slot — and a saved team's every live key's, its fields riding on each."""
	hashes = frappe.get_all("Grove API Key", filters={"modified": (">", since)}, pluck="key_hash")
	teams = frappe.get_all("Central Team", filters={"modified": (">", since)}, pluck="name")
	if teams:
		hashes += frappe.get_all(
			"Grove API Key", filters={"team": ("in", teams), "status": "active"}, pluck="key_hash"
		)
	return {bucket_of(key_hash) for key_hash in hashes if key_hash}


def needs_full(since):
	"""Whether a save after `since` reached keys it does not name: a Tier's rows ride on every key
	under it, and Grove Settings names the default tier."""
	settings = frappe.db.get_single_value("Grove Settings", "modified")
	if settings and frappe.utils.get_datetime(settings) > since:
		return True
	return bool(frappe.db.exists("Tier", {"modified": (">", since)}))


def bucket_names(labels):
	"""The live keys of `labels`' buckets, picked by hashing every live key's hash."""
	if not labels:
		return []
	# ponytail: a two-column scan of every live key; a `bucket` column set at mint makes it one indexed query.
	rows = frappe.get_all("Grove API Key", filters={"status": "active"}, fields=["name", "key_hash"])
	return [row.name for row in rows if row.key_hash and bucket_of(row.key_hash) in labels]


def keys_section(labels=None):
	"""The `keys` section: every live key when `labels` is None, else the keys of those buckets
	only. A bucket left with no live key is absent; the delta prunes it."""
	names = None if labels is None else bucket_names(labels)
	return bucketed_section(effective_keys(names), "key_hash")


def bucket_of(record_id):
	"""Which of the 256 state buckets a record belongs to. The agent prunes by the same rule, so
	the two sides must never disagree."""
	return hashlib.sha256(str(record_id).encode()).hexdigest()[:2]


def _hash(content):
	"""The agent stores this verbatim and never recomputes it, so only THIS function has to be
	deterministic — hence the sorts in the builders above."""
	return hashlib.sha256(
		json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


def flat_section(content):
	return {**content, "hash": _hash(content)}


def bucketed_section(records, id_field):
	buckets = {}
	for record in records:
		buckets.setdefault(bucket_of(record[id_field]), []).append(record)
	return {
		"buckets": {
			label: {"records": rows, "hash": _hash({"records": rows})} for label, rows in buckets.items()
		}
	}


def gateway_snapshot(geography, keys):
	"""The same for every gateway in `geography` but the routes, so a run builds it once per
	geography over the one `keys` section it built."""
	return {
		"groups": flat_section({"records": effective_groups()}),
		"keys": keys,
		"routes": flat_section({"table": routes.gateway_routes(geography)}),
	}


def gateway_geography(gateway):
	"""Blank for a gateway with none, which is then given no routes at all."""
	return frappe.db.get_value("Gateway Server", gateway, "geography") or ""


def gateway_store(gateway):
	"""The Gateway Store a gateway counts on; blank before its first Setup."""
	return frappe.db.get_value("Gateway Server", gateway, "gateway_store")


def ingress_snapshot(ingress):
	"""Its replica table and nothing else — that plane has no keys or groups section."""
	return {"routes": flat_section({"table": routes.replicas_for_ingress(ingress)})}


def snapshot_delta(snapshot, remote, labels=None):
	"""The sections whose hash the box does not already hold. `labels` are the buckets a tick
	rebuilt, when it did not rebuild them all."""
	delta = {}
	for section, content in snapshot.items():
		if "buckets" in content:
			changed = bucket_delta(section, content["buckets"], remote, labels)
			if changed:
				delta[section] = {"buckets": changed}
		elif remote.get(section) != content["hash"]:
			delta[section] = content
	return delta


def bucket_delta(section, buckets, remote, labels):
	"""The buckets whose hash the box lacks, and — explicitly EMPTY — those it still hashes but
	the section no longer has, so the agent prunes their members instead of holding them forever.
	`labels` keeps the prune to the buckets a tick rebuilt: the rest it never looked at."""
	changed = {
		label: bucket
		for label, bucket in buckets.items()
		if remote.get(f"{section}:{label}") != bucket["hash"]
	}
	held = {k.split(":", 1)[1] for k in remote if k.startswith(f"{section}:")}
	if labels is not None:
		held &= set(labels)
	for label in held - set(buckets):
		changed[label] = {"records": []}
	return changed


def section_label(section, content):
	"""`keys[3]` for a bucketed section with three buckets in play, the bare name otherwise."""
	buckets = content.get("buckets")
	return f"{section}[{len(buckets)}]" if buckets is not None else section


def describe(delta, response):
	"""Which sections went (bucket counts in brackets) and how many records the agent wrote."""
	counts = (response or {}).get("counts") or {}
	parts = []
	for section in ("groups", "keys", "routes"):
		if section not in delta:
			continue
		label = section_label(section, delta[section])
		if section in counts:
			label = f"{label}:{counts[section]}"
		parts.append(label)
	return " ".join(parts)
