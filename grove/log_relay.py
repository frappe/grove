# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Relay a live log stream onto a document's form over realtime, for as long as someone is
watching. A follower passes an iterator of lines, with `None` for a tick so a quiet stream still
reaches the liveness check on time. The form pings keep_streaming, which re-sets one redis key with
a short TTL, and the relay stops as soon as it is gone: the job holds a background worker for its
whole life and a site has only a few, so a stream nobody is watching has to end on its own."""

import time

import frappe

ALIVE_KEY = "log_stream_alive:{}:{}"
ALIVE_TTL = 45
FLUSH_INTERVAL = 0.25
EVENT = "grove_log"


def alive_key(doctype, docname):
	return ALIVE_KEY.format(frappe.scrub(doctype), docname)


def keep_alive(doctype, docname):
	frappe.cache.set_value(alive_key(doctype, docname), 1, expires_in_sec=ALIVE_TTL)


def end(doctype, docname):
	frappe.cache.delete_value(alive_key(doctype, docname))


def is_alive(doctype, docname):
	"""use_local_cache=False: frappe.cache memoises reads in frappe.local, so a long-running job
	would re-read its own first answer forever and never see the key expire."""
	return bool(frappe.cache.get_value(alive_key(doctype, docname), use_local_cache=False))


@frappe.whitelist()
def keep_streaming(doctype: str, docname: str):
	"""Permission-checked: this is reachable for any doctype."""
	frappe.has_permission(doctype, doc=docname, throw=True)
	keep_alive(doctype, docname)


def relay(events, doctype, docname):
	"""Abandoning the iterator here is what shuts the source down."""
	lines, flushed_at = [], 0.0

	def publish(payload):
		frappe.publish_realtime(EVENT, doctype=doctype, docname=docname, message=payload)

	for line in events:
		if line is not None:
			lines.append(line)
		if time.monotonic() - flushed_at < FLUSH_INTERVAL:
			continue
		if lines:
			publish({"lines": lines})
		lines, flushed_at = [], time.monotonic()
		if not is_alive(doctype, docname):
			break
	publish({"lines": lines, "done": True})
	end(doctype, docname)
