# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt

# import frappe
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.inference_server.inference_server import InferenceServer


# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]



class IntegrationTestInferenceServer(IntegrationTestCase):
	"""
	Integration tests for InferenceServer.
	Use this class for testing interactions between multiple components.
	"""

	pass


class TestATerminatedServerTakesItsReplicasWithIt(unittest.TestCase):
	"""The box is gone, so the teardown play cannot run: a replica left Active would keep its
	route on every gateway and point callers at nothing."""

	def terminate(self, replicas):
		asked, written, synced = {}, [], []

		def get_all(doctype, filters=None, **kwargs):
			asked[doctype] = filters
			return [frappe._dict(row) for row in replicas]

		db = SimpleNamespace(set_value=lambda *args: written.append(args))
		with (
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(frappe, "db", db),
			patch("grove.grove.doctype.model.model.sync_published", side_effect=synced.append),
		):
			InferenceServer.terminate_replicas(SimpleNamespace(name="inf-1"))
		return asked, written, synced

	def test_every_live_replica_is_retired_and_gives_its_port_back(self):
		asked, written, synced = self.terminate([{"name": "r1", "model": "m1"}])
		# Only Terminated is already gone; Inactive and Broken still hold a port.
		self.assertEqual(asked["Model Replica"], {"inference_server": "inf-1", "status": ("!=", "Terminated")})
		self.assertEqual(written, [("Model Replica", "r1", {"status": "Terminated", "engine_port": 0})])
		# A model nothing serves any more comes off the published list.
		self.assertEqual(synced, ["m1"])

	def test_a_box_with_no_replicas_writes_nothing(self):
		_, written, synced = self.terminate([])
		self.assertEqual((written, synced), ([], []))
