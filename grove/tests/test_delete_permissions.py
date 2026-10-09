# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Identity, money and the catalogue are never deleted through permissions: no role holds
`delete` on them. Read off the doctype JSON, so no site is needed and a re-granted delete fails
here before it reaches one. Administrator and `ignore_permissions` bypass DocPerm; only an
`on_trash` guard (Grove Credit has one) stops those."""

import json
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
PROTECTED = (
	"Central Team", "Grove API Key", "Grove Credit", "Usage Record", "Credit Discrepancy",
	"Model Pricing", "Model", "Model Provider", "Stuck Usage",
)


def permissions(doctype):
	folder = doctype.lower().replace(" ", "_")
	# Whichever module holds it.
	(path,) = APP.glob(f"*/doctype/{folder}/{folder}.json")
	return json.load(open(path))["permissions"]


class TestNobodyMayDelete(unittest.TestCase):
	def test_no_role_holds_delete_on_a_protected_doctype(self):
		for doctype in PROTECTED:
			with self.subTest(doctype):
				granted = [perm["role"] for perm in permissions(doctype) if perm.get("delete")]
				self.assertEqual(granted, [], f"{doctype} grants delete to {granted}")


if __name__ == "__main__":
	unittest.main()
