# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""The desired state a box is pushed, and the hash gate that decides which sections travel.

`groups` and `routes` travel whole. `keys` scale with customer count, so they split into 256
buckets (`bucket_of`) hashed independently — one key minted re-pushes one bucket, not the
population. Absence prunes: a deleted group or revoked key stops being named and the agent
removes it."""

import hashlib
import json

import frappe

from grove.access import group_rows, limit_rows, model_rows
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


def effective_keys():
	"""Every LIVE API Key projected for the gateway, with the whole policy the gates read: its
	groups and own allow/deny, its geography, its rate limits and its cap as `budget` — the box
	subtracts its own spend from it and refuses at zero. What is the team's rides on each key:
	`prepaid` (not Free; the wire says `prepaid`, not `free`, so a field absent reads as no gate),
	`log_payloads`, and `limited` — Grove's verdict that the team's balance is gone, which refuses
	every key whatever its own cap says.

	Revoked keys are not projected: absent from their bucket, the push prunes them off every box.
	The row stays in Grove as the record of a credential that existed."""
	deltas = model_rows("Grove API Key")
	memberships = group_rows()
	limits = limit_rows()
	teams = {
		t.name: t for t in frappe.get_all("Central Team", fields=["name", "free", "log_payloads", "credit_exhausted"])
	}
	keys = frappe.get_all(
		"Grove API Key",
		filters={"status": "active"},
		fields=["name", "key_hash", "team", "status", "geography", "cap"],
	)
	records = []
	for k in sorted(keys, key=lambda k: k.key_hash or ""):
		team = teams.get(k.team)
		if not k.key_hash or team is None:
			continue
		records.append({
			"key_hash": k.key_hash,
			"prefix": k.name,  # doc name (random hash) = usage attribution id
			"team": k.team,  # the tenant boundary: cache salt, payload log
			"status": k.status or "active",
			# One comma list: the gateway unions the grants per entry. Sorted, so the same
			# membership always hashes the same.
			"group": ",".join(memberships.get(k.name, [])),
			"allow": ",".join(deltas.get(k.name, {}).get("allow", [])),
			"deny": ",".join(deltas.get(k.name, {}).get("deny", [])),
			"limited": bool(team.credit_exhausted),
			# Opt-in to prompt/output logging. Customer content: absent or falsy stays off.
			"log_payloads": bool(team.log_payloads),
			# Every gateway gets every key; one outside its geography answers 403.
			"geography": k.geography or "",
			"prepaid": not team.free,
			"budget": 0 if team.free else nano(k.cap or 0),
			# Rate limits, `metric:window:value` each. Blank when none: the box merges fields, so
			# leaving it out would keep a removed limit in force.
			"limits": ",".join(limits.get(k.name, [])),
		})
	return records


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
	return {"buckets": {
		label: {"records": rows, "hash": _hash({"records": rows})}
		for label, rows in buckets.items()
	}}


def gateway_snapshot(geography, shared=None):
	"""The same for every gateway in `geography`, so a run builds it once per geography: only the
	routes differ between them. A run hands every call the same `shared` dict, so the key records
	are built once."""
	shared = {} if shared is None else shared
	if "keys" not in shared:
		shared["keys"] = effective_keys()
	return {
		"groups": flat_section({"records": effective_groups()}),
		"keys": bucketed_section(shared["keys"], "key_hash"),
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


def snapshot_delta(snapshot, remote):
	"""The sections whose hash the box does not already hold. A bucket the box hashes but the
	snapshot no longer has is sent explicitly EMPTY, so the agent prunes its members instead of
	holding them forever."""
	delta = {}
	for section, content in snapshot.items():
		if "buckets" in content:
			changed = {
				label: bucket
				for label, bucket in content["buckets"].items()
				if remote.get(f"{section}:{label}") != bucket["hash"]
			}
			held = {k.split(":", 1)[1] for k in remote if k.startswith(f"{section}:")}
			for label in held - set(content["buckets"]):
				changed[label] = {"records": []}
			if changed:
				delta[section] = {"buckets": changed}
		elif remote.get(section) != content["hash"]:
			delta[section] = content
	return delta


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
