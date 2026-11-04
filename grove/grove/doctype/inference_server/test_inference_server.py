# Copyright (c) 2026, developers@frappe.io and Contributors
# See license.txt

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.inference_server.inference_server import InferenceServer

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestInferenceServer(IntegrationTestCase):
	"""
	Integration tests for InferenceServer.
	Use this class for testing interactions between multiple components.
	"""

	pass


class TestATerminatedServerTakesItsReplicasWithIt(unittest.TestCase):
	"""The box is gone, so the teardown play cannot run: a replica left Active would keep its
	route on every gateway and point callers at nothing, and its cards would read taken forever."""

	def terminate(self, replicas):
		asked, written, synced, freed = {}, [], [], []

		def get_all(doctype, filters=None, **kwargs):
			asked[doctype] = filters
			return [frappe._dict(row) for row in replicas]

		db = SimpleNamespace(
			set_value=lambda *args: written.append(args),
			sql=lambda query, values: freed.append(values["names"]),
		)
		with (
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(frappe, "db", db),
			patch("grove.grove.doctype.model.model.sync_published", side_effect=synced.append),
		):
			InferenceServer.terminate_replicas(SimpleNamespace(name="inf-1"))
		return asked, written, synced, freed

	def test_every_live_replica_is_retired_with_its_port_and_cards_given_back(self):
		asked, written, synced, freed = self.terminate(
			[{"name": "r1", "model": "m1"}, {"name": "r2", "model": "m1"}]
		)
		# Only Terminated is already gone; Inactive and Broken still hold a port.
		self.assertEqual(
			asked["Model Replica"], {"inference_server": "inf-1", "status": ("!=", "Terminated")}
		)
		self.assertEqual(
			written,
			[("Model Replica", {"name": ("in", ["r1", "r2"])}, {"status": "Terminated", "engine_port": 0})],
		)
		self.assertEqual(freed, [["r1", "r2"]])
		self.assertEqual(synced, ["m1"], "published once per model, not per replica")

	def test_a_box_with_no_replicas_writes_nothing(self):
		_, written, synced, freed = self.terminate([])
		self.assertEqual((written, synced, freed), ([], [], []))
