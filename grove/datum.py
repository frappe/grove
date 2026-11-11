# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""datum's write token for one box, minted by Central. Short-lived (Central says how long, no
revocation) and never stored: minted inside the job that writes it to the box, which keeps only when it
runs out, and minted again by the hourly tick with a day left. The roster's own token is the one
kept one, on its Region, until it runs out. Also the roster: every minute, each
Machine's status as one sample under its Region's own resource_id, so a reader knows which quiet boxes
are expected to be quiet (Atlas's shape: `atlas_metal_server_info`)."""

from datetime import UTC, datetime

import frappe
import requests
from frappe.utils import add_to_date, now_datetime

TOKEN_ROUTE = "/api/method/central.api.grove.datum_token"
TOKEN_HEADER = "X-Grove-Token"
CREDENTIAL_FIELDS = ("central_url", "central_token")
VECTOR_HOSTS = ("Gateway Server", "Gateway Store", "Ingress Server")
RENEW_AHEAD_DAYS = 1
ROSTER_METRIC = "grove_machine_info"


def mint_token(region: str, resource_id: str) -> tuple[str, datetime]:
	"""Ask Central for the JWT `resource_id` presents to `region`'s datum, and the moment it runs
	out — `region` is Central's name for it (Region.central_region), not Grove's. Thrown on anything
	but 200, naming Central's answer: a box with no token is a box that ships nothing."""
	settings = frappe.get_single("Grove Settings")
	token = settings.get_password("central_token", raise_exception=False) or ""
	response = requests.get(
		f"{(settings.central_url or '').rstrip('/')}{TOKEN_ROUTE}",
		params={"region": region, "resource_id": resource_id},
		headers={TOKEN_HEADER: token},
		timeout=15,
	)
	if response.status_code != 200:
		frappe.throw(
			f"Central refused a datum token for {resource_id} in {region}: "
			f"{response.status_code} {response.text[:200]}"
		)
	answer = response.json()["message"]
	return answer["token"], add_to_date(now_datetime(), seconds=answer["expires_in"])


def credential_problems() -> list[str]:
	"""Each blank Central credential in Grove Settings, named."""
	settings = frappe.get_single("Grove Settings")
	values = {
		field: settings.get_password(field, raise_exception=False) if field == "central_token" else settings.get(field)
		for field in CREDENTIAL_FIELDS
	}
	return [f"Set {settings.meta.get_label(field)} in Grove Settings." for field, value in values.items() if not value]


def refresh_tokens():
	"""Tick: re-run the install play on every Active box with Vector whose token runs out within a
	day, or whose expiry is unknown. One job per box, so one unreachable box blocks nothing."""
	due = add_to_date(now_datetime(), days=RENEW_AHEAD_DAYS)
	for doctype in VECTOR_HOSTS:
		names = frappe.get_all(
			doctype,
			filters={"status": "Active", "is_vector_setup": 1},
			or_filters=[["datum_token_expires_at", "is", "not set"], ["datum_token_expires_at", "<=", due]],
			pluck="name",
		)
		for name in names:
			frappe.enqueue_doc(doctype, name, "provision_vector", queue="long", timeout=1200)


def ingest_url(metrics_url: str) -> str:
	"""datum's JSON route, beside the remote-write one the Region names."""
	return metrics_url.removesuffix("/remote")


def vector_machines() -> set[str]:
	"""The Machines a Vector box pushes under, as grove:<machine>. A plain inference box's series sit
	under its Monitoring Agent, so it is never expected to push as itself."""
	return {
		machine
		for doctype in VECTOR_HOSTS
		for machine in frappe.get_all(doctype, filters={"is_vector_setup": 1}, pluck="machine")
	}


def push_roster():
	"""Tick, every minute: each Region with a datum gets its Machines. One Region's refusal is
	logged and the next still gets its roster."""
	for name in frappe.get_all("Region", filters={"metrics_url": ("is", "set")}, pluck="name"):
		try:
			post_roster(frappe.get_doc("Region", name))
		except Exception:
			frappe.log_error(f"datum roster failed: {name}")


def post_roster(region):
	"""Every Machine of `region` as `grove_machine_info{machine, status, vector} 1` under
	`grove:region-<region>`. Reader rule: a Machine whose latest row reads Active with vector 1 must have
	rows of its own (grove:<machine>) within a few minutes, or it is dead; any other latest row
	means its silence is expected. Thrown on refusal, naming datum's answer; a 401 also drops the
	kept token, so the next minute asks Central for a new one."""
	machines = frappe.get_all("Machine", filters={"region": region.name}, fields=["name", "status"])
	if not machines:
		return
	pushing = vector_machines()
	ts = datetime.now(UTC).isoformat()
	samples = [
		{
			"metric": ROSTER_METRIC,
			"value": 1,
			"ts": ts,
			"labels": {"machine": m.name, "status": m.status, "vector": str(int(m.name in pushing))},
		}
		for m in machines
	]
	token = region.get_datum_token()
	response = requests.post(
		ingest_url(region.metrics_url),
		json={"samples": samples},
		headers={"Authorization": f"Bearer {token}"},
		timeout=15,
	)
	if response.status_code == 401:
		region.db_set("datum_token_expires_at", None)
	if response.status_code != 200:
		frappe.throw(f"datum refused {region.name}'s roster: {response.status_code} {response.text[:200]}")
