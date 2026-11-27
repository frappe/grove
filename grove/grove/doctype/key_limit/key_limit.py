# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class KeyLimit(Document):
	"""One cap a key runs under, every model together: `value` of `metric` per `window`. The window
	resets on the UTC clock."""

	pass
