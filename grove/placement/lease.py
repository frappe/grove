# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""A short-lived note in Redis saying "someone is taking this card right now". Advisory only:
`GPU.held_by` is what OWNS a card, and a lease is safe to lose — on a flushed Redis (`allkeys-lru`,
no persistence) placement is correct, just slower. What it buys is visibility BEFORE commit: a
claim is invisible to other transactions until it commits and InnoDB locks its index entry
meanwhile, so a rival BLOCKS — and under REPEATABLE READ still cannot see the claim afterwards,
because its snapshot predates it. A lease is written before the row and read outside any
transaction, so a rival sees it immediately and skips the card."""

import frappe

LEASE_TTL = 60


def _key(gpu):
	return f"grove:gpu_lease:{gpu}"


def leased(gpus):
	"""`get`, never `exists`: RedisWrapper leaves `set`/`get`/`delete` raw but OVERRIDES `exists` to
	run the name through `make_key`, so the lookup would use a different key, always miss, and
	silently report every card free."""
	return {gpu for gpu in gpus if frappe.cache.get(_key(gpu)) is not None}


def take(gpus, holder):
	"""True if every one was free. All or nothing: a partial lease would leave a card marked busy
	that nobody goes on to claim."""
	taken = []
	for gpu in gpus:
		if frappe.cache.set(_key(gpu), holder, nx=True, ex=LEASE_TTL):
			taken.append(gpu)
			continue
		release(taken)
		return False
	return True


def release(gpus):
	"""Hand cards back before the TTL — only when a placement FAILED. A successful one leaves its
	lease to expire: a rival whose snapshot predates the claim's commit still cannot see it, and
	the lease is what keeps it off the card until then."""
	for gpu in gpus:
		frappe.cache.delete(_key(gpu))
