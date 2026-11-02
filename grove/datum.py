# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""datum's write token for one box, minted by Central. Short-lived (Central says how long, no
revocation) and never stored: minted inside the job that writes it to the box, which keeps only when it
runs out, and minted again by the hourly tick with a day left."""

from datetime import datetime

import frappe
import requests
from frappe.utils import add_to_date, now_datetime

TOKEN_ROUTE = "/api/method/central.api.grove.datum_token"
CREDENTIAL_FIELDS = ("central_url", "central_api_key", "central_api_secret")
VECTOR_HOSTS = ("Gateway Server", "Gateway Store", "Ingress Server")

RENEW_AHEAD_DAYS = 1

def mint_token(region: str, resource_id: str) -> tuple[str, datetime]:
	"""Ask Central for the JWT `resource_id` presents to `region`'s datum, and the moment it runs
	out — `region` is Central's name for it (Region.central_region), not Grove's. Thrown on anything
	but 200, naming Central's answer: a box with no token is a box that ships nothing."""
	settings = frappe.get_single("Grove Settings")
	secret = settings.get_password("central_api_secret", raise_exception=False) or ""
	response = requests.get(
		f"{(settings.central_url or '').rstrip('/')}{TOKEN_ROUTE}",
		params={"region": region, "resource_id": resource_id},
		headers={"Authorization": f"token {settings.central_api_key}:{secret}"},
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
		field: settings.get_password(field, raise_exception=False)
		if field == "central_api_secret"
		else settings.get(field)
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
