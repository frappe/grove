"""The connections badge: what Frappe counts as open per doctype."""

import unittest

from grove import hooks, notifications


class TestTheConnectionsBadge(unittest.TestCase):
	def test_frappe_is_pointed_at_grove_config(self):
		self.assertEqual(hooks.notification_config, "grove.notifications.get_notification_config")

	def test_a_machine_is_open_while_active(self):
		config = notifications.get_notification_config()
		self.assertEqual(config["for_doctype"]["Machine"], {"status": "Active"})

	def test_a_replica_is_open_while_serving(self):
		config = notifications.get_notification_config()
		self.assertEqual(config["for_doctype"]["Model Replica"], {"status": "Active"})

	def test_a_pod_activity_is_open_while_failed(self):
		config = notifications.get_notification_config()
		self.assertEqual(config["for_doctype"]["Pod Activity"], {"outcome": "Failure"})
