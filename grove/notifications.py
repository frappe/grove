"""What counts as "open" per doctype. Frappe reads this for the badge on every connections panel
and for the desk's unread counts; the total stays in the link's count."""


def get_notification_config():
	return {
		"for_doctype": {
			"Machine": {"status": "Active"},
			"Model Replica": {"status": "Active"},
			"Pod Activity": {"outcome": "Failure"},
		}
	}
