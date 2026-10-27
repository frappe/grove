# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""datum's write token for one box, minted by Central. Short-lived (7 days, no revocation) and never
stored: minted inside the job that writes it to the box, and re-minted by the daily refresh."""

import frappe
import requests

TOKEN_ROUTE = "/api/method/central.api.grove.datum_token"
CREDENTIAL_FIELDS = ("central_url", "central_api_key", "central_api_secret")
VECTOR_HOSTS = ("Gateway Server", "Gateway Store", "Ingress Server")


def mint_token(region: str, resource_id: str) -> str:
	"""Ask Central for the JWT `resource_id` presents to `region`'s datum — `region` is Central's
	name for it (Region.central_region), not Grove's. Thrown on anything but 200, naming Central's
	answer: a box with no token is a box that ships nothing."""
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
	return response.json()["message"]["token"]


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
	"""Tick: re-run the install play on every Active box that has Vector, so no token is older than a
	day against datum's 7-day TTL. One job per box, so one unreachable box blocks nothing."""
	for doctype in VECTOR_HOSTS:
		for name in frappe.get_all(doctype, filters={"status": "Active", "is_vector_setup": 1}, pluck="name"):
			frappe.enqueue_doc(doctype, name, "provision_vector", queue="long", timeout=1200)
