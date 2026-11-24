# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import functools
import hmac

import frappe
from frappe.utils.password import set_encrypted_password

CONTROL_ROLE = "Grove Control"


def for_control_clients(endpoint):
	"""An endpoint of the control clients (Central, Press) alone: refused before it runs for a
	caller without the role. What it reads and writes is checked again by the doc permissions of
	that role, so neither gate stands in for the other."""

	@functools.wraps(endpoint)
	def wrapper(*args, **kwargs):
		frappe.only_for(CONTROL_ROLE)
		return endpoint(*args, **kwargs)

	return wrapper


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_control_client(email: str):
	# the secret should not be stored in the Request Log.
	token = frappe.form_dict.pop("token", None)
	expected = frappe.conf.get("control_secret")

	if not (expected and token) or not hmac.compare_digest(str(token), str(expected)):
		frappe.throw("Invalid Operation", frappe.AuthenticationError)

	control_user = _create_control_user(email)

	# Mint directly: generate_keys() would reject a Guest caller on permission. The caller is Guest
	# behind the control secret, so no permitted save path exists for it; the secret is the gate.
	api_secret = frappe.generate_hash(length=15)
	control_user.api_key = control_user.api_key or frappe.generate_hash(length=15)
	control_user.api_secret = api_secret
	control_user.save(ignore_permissions=True)

	return {"api_key": control_user.api_key, "api_secret": api_secret, "user": control_user.name}


@frappe.whitelist()
@for_control_clients
def create_control_client_key():
	control_user = frappe.get_doc("User", frappe.session.user)
	api_secret = frappe.generate_hash(length=15)
	control_user.api_key = control_user.api_key or frappe.generate_hash(length=15)
	control_user.api_secret = api_secret
	# A user mints its own secret; generate_keys() and a permitted save both need System Manager.
	control_user.save(ignore_permissions=True)

	return {"api_key": control_user.api_key, "api_secret": api_secret, "user": control_user.name}


@frappe.whitelist(methods=["POST"])
@for_control_clients
def set_report_secret(secret: str):
	"""Central hands Grove the `ai` Service's report secret, which signs every usage report."""
	# the secret should not be stored in the Request Log.
	frappe.form_dict.pop("secret", None)
	if not secret:
		frappe.throw("A report secret is required.")
	# Grove Control holds no write on Grove Settings; the control role is the gate.
	set_encrypted_password("Grove Settings", "Grove Settings", secret, "central_report_secret")
	frappe.db.set_single_value("Grove Settings", "central_report_secret", "*" * len(secret))


def _create_control_user(email):
	if frappe.db.exists("User", {"email": email}):
		frappe.throw("Invalid Operation")

	doc = frappe.new_doc("User")
	doc.email = email
	doc.first_name = "Control Client"
	doc.user_type = "Website User"
	doc.send_welcome_email = 0
	doc.enabled = 1
	doc.append("roles", {"role": CONTROL_ROLE})
	doc.insert(ignore_permissions=True)
	return doc
