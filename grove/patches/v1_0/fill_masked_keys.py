"""Grove API Key's `masked` is new: every key from before it gets the prefix and four characters
of its secret, read once here so no listing decrypts again. A key with no secret stored stays
blank."""

import frappe
from frappe.utils.password import get_decrypted_password

from grove.grove.doctype.grove_api_key.grove_api_key import KEY_PREFIX


def execute():
	for name in frappe.get_all("Grove API Key", filters={"masked": ("is", "not set")}, pluck="name"):
		secret = get_decrypted_password("Grove API Key", name, "api_secret", raise_exception=False)
		if secret:
			frappe.db.set_value(
				"Grove API Key", name, "masked", f"{secret[: len(KEY_PREFIX) + 4]}…", update_modified=False
			)
