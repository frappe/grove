# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""The desired state a box is pushed, and the hash gate that decides which sections travel.
`groups` and `routes` travel whole. `keys` scale with customer count, so they split into 256
buckets (`bucket_of`) hashed independently — one key minted re-pushes one bucket, not the
population. Absence prunes: a deleted group or revoked key stops being named and the agent
removes it. A tick rebuilds only the buckets a save moved since the boxes' stamps
(`dirty_buckets`)."""

import hashlib
import json

import frappe

from grove.access import (
	group_geographies,
	group_rows,
	limit_scopes,
	limits_in_force,
	limits_reaching,
	model_rows,
	reaching,
	team_grants,
)
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
	"""Every LIVE API Key projected for the gateway, with the policy the gates read off the key.
	`budget` is its cap, `budget_window` what the box counts it over. The team's verdicts, settings
	and rate limits ride its own record (`effective_teams`). Revoked keys are not projected: absent
	from their bucket, the push prunes them off every box."""
	if names is not None and not names:
		return []
	filters = {"status": "active"}
	if names is not None:
		filters["name"] = ("in", sorted(names))
	keys = frappe.get_all(
		"Grove API Key", filters=filters, fields=["name", "key_hash", "team", "geography", "cap"]
	)
	if not keys:
		return []
	team_names = key_names = team_filter = None
	if names is not None:
		team_names = sorted({k.team for k in keys if k.team})
		key_names = [k.name for k in keys]
		team_filter = {"name": ("in", team_names)}
	rows = frappe.get_all("Grove Team", filters=team_filter, fields=["name", "key_cap_window"])
	teams = {t.name: t for t in rows}
	grants = team_grants(team_names)
	memberships = group_rows(team_names)
	own_groups = group_rows(key_names, "Grove API Key")
	where = group_geographies()
	records = []
	for k in sorted(keys, key=lambda k: k.key_hash or ""):
		team = teams.get(k.team)
		if not k.key_hash or team is None:
			continue
		own = grants.get(k.team, {})
		records.append(
			{
				"key_hash": k.key_hash,
				"prefix": k.name,
				"team": k.team,
				"status": "active",
				"group": get_key_groups(k, memberships, own_groups, where),
				"allow": ",".join(reaching(own.get("allow", []), k.geography)),
				"deny": ",".join(reaching(own.get("deny", []), k.geography)),
				"geography": k.geography or "",
				"budget": nano(k.cap or 0),
				"budget_window": "total" if team.key_cap_window == "Total" else "month",
			}
		)
	return records


def get_key_groups(key, memberships, own_groups, where):
	"""The groups of the key's team and its own that sit in its geography, comma-joined."""
	groups = memberships.get(key.team, []) + own_groups.get(key.name, [])
	return ",".join(sorted({g for g in groups if where.get(g) == key.geography}))


def effective_teams(names=None, geography=""):
	"""Every Grove Team projected for a gateway of `geography`, read beside each of its keys. Its
	rate limit rows go out as `limits` (per model, for the team) or as `key_limits` (per key, every
	model together), by its tier's scope."""
	if names is not None and not names:
		return []
	teams = frappe.get_all(
		"Grove Team",
		filters=None if names is None else {"name": ("in", sorted(names))},
		fields=["name", "suspended", "log_payloads", "cap_message", "tier"],
	)
	tiers = {team.name: team.tier for team in teams}
	limits, scopes = limits_in_force(tiers), limit_scopes(tiers)
	records = []
	for team in sorted(teams, key=lambda team: team.name):
		rows = wire_limits(limits_reaching(limits[team.name], geography))
		is_per_key = scopes[team.name] == "Key"
		records.append(
			{
				"team": team.name,
				"limited": bool(team.suspended),
				"log_payloads": bool(team.log_payloads),
				# Blank when none: the box merges fields, so leaving one out would keep what was removed in force.
				"cap_message": team.cap_message or "",
				"limits": "" if is_per_key else rows,
				"key_limits": rows if is_per_key else "",
			}
		)
	return records


def wire_limits(rows):
	"""The comma list the gateway reads: `metric:window:value`, the model key last when the row
	names one, so a key holding a colon still splits on the box."""
	return ",".join(
		f"{r['metric']}:{r['window']}:{r['value']}" + (f":{r['model_key']}" if r["model_key"] else "")
		for r in rows
	)


def dirty_buckets(since):
	"""The key buckets a save after `since` moved: a saved key's own — revoked or not, a revoke
	being what empties its slot — and a saved team's every live key's, its grants riding on each."""
	hashes = frappe.get_all("Grove API Key", filters={"modified": (">", since)}, pluck="key_hash")
	teams = frappe.get_all("Grove Team", filters={"modified": (">", since)}, pluck="name")
	if teams:
		hashes += frappe.get_all(
			"Grove API Key", filters={"team": ("in", teams), "status": "active"}, pluck="key_hash"
		)
	return {bucket_of(key_hash) for key_hash in hashes if key_hash}


def dirty_team_buckets(since):
	"""The team buckets a save after `since` moved. None, every bucket, after a Tier or Grove
	Settings save: a tier's rows ride on every team under it, and the settings name the default."""
	settings = frappe.db.get_single_value("Grove Settings", "modified")
	if settings and frappe.utils.get_datetime(settings) > since:
		return None
	# ponytail: a changed tier row re-pushes every team under it; a tier record on the gateway once teams run to tens of thousands.
	if frappe.db.exists("Tier", {"modified": (">", since)}):
		return None
	teams = frappe.get_all("Grove Team", filters={"modified": (">", since)}, pluck="name")
	return {bucket_of(name) for name in teams}


def bucket_names(labels):
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


def team_names(labels):
	"""The teams of `labels`' buckets, picked by hashing every team's name."""
	if not labels:
		return []
	return [name for name in frappe.get_all("Grove Team", pluck="name") if bucket_of(name) in labels]


def teams_section(labels=None, geography=""):
	"""The `teams` section for gateways of `geography`: every team when `labels` is None, else the
	teams of those buckets only."""
	names = None if labels is None else team_names(labels)
	return bucketed_section(effective_teams(names, geography), "team")


def holds_buckets(remote):
	"""Whether a box's hashes name a bucket of each bucketed section; one naming none of a section
	was never given it."""
	return all(any(label.startswith(f"{section}:") for label in remote) for section in ("teams", "keys"))


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


def gateway_snapshot(geography, teams, keys):
	"""A geography's own routes and `teams` (a limit row holds where its model does), over the one
	`keys` section a run built: one snapshot per geography, shared by its gateways."""
	return {
		"groups": flat_section({"records": effective_groups()}),
		"teams": teams,
		"keys": keys,
		"routes": flat_section({"table": routes.gateway_routes(geography)}),
	}


def ingress_snapshot(ingress):
	"""Its replica table and nothing else — that plane has no keys or groups section."""
	return {"routes": flat_section({"table": routes.replicas_for_ingress(ingress)})}


def snapshot_delta(snapshot, remote, labels=None):
	"""The sections whose hash the box does not already hold. `labels` are the buckets a tick
	rebuilt of each bucketed section ({section: labels}), when it did not rebuild them all."""
	delta = {}
	for section, content in snapshot.items():
		if "buckets" in content:
			rebuilt = None if labels is None else labels[section]
			changed = bucket_delta(section, content["buckets"], remote, rebuilt)
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
	for section in ("groups", "teams", "keys", "routes"):
		if section not in delta:
			continue
		label = section_label(section, delta[section])
		if section in counts:
			label = f"{label}:{counts[section]}"
		parts.append(label)
	return " ".join(parts)
