import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, now_datetime

# Log Settings owns the number once an operator edits it there; this seeds it.
RETENTION_DAYS = 90


class StuckUsage(Document):
	"""Usage on one store that Grove keeps failing to record. For a team, the gateway holds the
	usage and re-sends it every pull; this row says it is stuck, since when, and why — one open row
	per (team, store), updated each failing pull and resolved by the pull that lands it. For a dead
	line (`dead_line` set) the gateway has already handed it over and dropped it: this row holds it
	until a person resolves it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_keys: DF.SmallText | None
		attempts: DF.Int
		dead_line: DF.Data | None
		first_failed: DF.Datetime | None
		gateway_store: DF.Link | None
		last_error: DF.Code | None
		last_failed: DF.Datetime | None
		last_payload: DF.JSON | None
		resolved: DF.Check
		resolved_on: DF.Datetime | None
		team: DF.Link | None
	# end: auto-generated types

	@frappe.whitelist()
	def pull_now(self):
		"""Button: pull this team's keys from every store now instead of at the next hourly pull."""
		frappe.only_for("System Manager")
		frappe.enqueue("grove.pathway.usage.pull_all", queue="short", team=self.team, trigger="Manual", wait=60)
		frappe.msgprint(f"Usage pull queued for {self.team}.", alert=True)

	@frappe.whitelist()
	def mark_resolved(self):
		"""Button: a dead line someone has dealt with. A user's row resolves itself when it lands."""
		frappe.only_for("System Manager")
		if not self.dead_line:
			frappe.throw("A team's stuck usage resolves when a pull lands it — use Pull Now.")
		self.db_set({"resolved": 1, "resolved_on": now_datetime()})

	@staticmethod
	def clear_old_logs(days=RETENTION_DAYS):
		"""Log Settings: rows resolved more than `days` ago. An open row stays whatever its age."""
		cutoff = add_days(now_datetime(), -cint(days))
		frappe.db.delete("Stuck Usage", {"resolved": 1, "resolved_on": ("<", cutoff)})


def record_stuck(team, gateway_store, shares, dead_line=None):
	"""`shares`: {drain id: {key: hash}} that failed, or the raw dead line. Called inside the except
	that failed: the traceback is the reason. `ignore_permissions`, because this write must not be
	the second failure."""
	now = now_datetime()
	keys = [] if dead_line else sorted({key for hashes in shares.values() for key in hashes})
	facts = {
		"api_keys": ", ".join(keys), "last_failed": now,
		"last_error": frappe.get_traceback(), "last_payload": frappe.as_json(shares),
	}
	key = {"team": team, "gateway_store": gateway_store, "dead_line": dead_line}
	name = frappe.db.get_value("Stuck Usage", {**{k: v or ("is", "not set") for k, v in key.items()}, "resolved": 0})
	if name:
		attempts = frappe.db.get_value("Stuck Usage", name, "attempts") or 0
		frappe.db.set_value("Stuck Usage", name, {**facts, "attempts": attempts + 1})
		return name
	return frappe.get_doc({
		"doctype": "Stuck Usage", **key, "first_failed": now, "attempts": 1, **facts,
	}).insert(ignore_permissions=True).name


def resolve_stuck(team, gateway_store):
	"""The team's usage on `gateway_store` landed: close the open row, if there is one."""
	name = frappe.db.get_value(
		"Stuck Usage", {"team": team, "gateway_store": gateway_store, "resolved": 0, "dead_line": ("is", "not set")}
	)
	if name:
		frappe.db.set_value("Stuck Usage", name, {"resolved": 1, "resolved_on": now_datetime()})
	return name
