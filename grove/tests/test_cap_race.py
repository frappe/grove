# Copyright (c) 2026, Frappe and contributors
# See license.txt


import threading
import time
import unittest.mock

import frappe
from frappe.tests import IntegrationTestCase

from grove.api import keys, teams
from grove.billing.pricing import committed
from grove.tests.test_api_permissions import tier

TEAM, TIER = "race-two-raises", "race-two-raises-tier"


class TestTwoRaisesOnOneTeam(IntegrationTestCase):
	def test_two_raises_at_once_pass_one_at_a_time(self):
		self.delete_fixtures()
		self.addCleanup(self.delete_fixtures)
		teams.provision_team(TEAM)
		teams.update_team(TEAM, tier=tier(TIER, spend_limit=5))
		first, second = (keys.provision_key(TEAM, title=title)["name"] for title in ("first", "second"))
		frappe.db.commit()  # the raising connections must see them

		both_ready = threading.Barrier(2, timeout=30)
		verdicts = {}
		site, sites_path = frappe.local.site, frappe.local.sites_path

		def raise_on_own_connection(key):
			frappe.init(site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				both_ready.wait()
				verdicts[key] = keys.update_key(TEAM, key, cap=3)["cap"]
				frappe.db.commit()
			except (frappe.ValidationError, frappe.QueryDeadlockError) as outcome:
				frappe.db.rollback()
				verdicts[key] = str(outcome)
			finally:
				frappe.destroy()

		def boxes_count_nothing(team):
			time.sleep(0.5)  # the first holds the team row here while the second arrives at it
			return {}

		raises = [threading.Thread(target=raise_on_own_connection, args=(key,)) for key in (first, second)]
		with unittest.mock.patch("grove.pathway.usage.peek_team", boxes_count_nothing):
			for raising in raises:
				raising.start()
			for raising in raises:
				raising.join(timeout=60)

		frappe.db.commit()  # a snapshot from after both ended
		allowed = [key for key, verdict in verdicts.items() if verdict == 3]
		refused = [verdict for verdict in verdicts.values() if verdict != 3]
		self.assertEqual((len(allowed), len(refused)), (1, 1), verdicts)
		self.assertIn("add up to 6 of a spend limit of 5", refused[0])
		self.assertEqual(committed(TEAM, lock=False), 3.0)

	def delete_fixtures(self):
		frappe.db.rollback()
		for key in frappe.get_all("Grove API Key", {"team": TEAM}, pluck="name"):
			frappe.delete_doc("Grove API Key", key, force=True, ignore_permissions=True)
		# The one delete of a team anywhere: this test commits for real, so it tidies for real.
		frappe.delete_doc("Grove Team", TEAM, force=True, ignore_permissions=True, ignore_on_trash=True)
		frappe.delete_doc("Tier", TIER, force=True, ignore_permissions=True)
		frappe.db.commit()
