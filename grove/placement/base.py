# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Which box a new replica goes on. A policy is one sort key over a Candidate: lower wins, and a
later field only speaks where every earlier one ties.

Nothing here imports frappe, so every test in this package is pure: no site, no mocking.

A policy only ORDERS boxes that are already viable. What makes a box viable is decided by the caller
and is deliberately not pluggable: a policy that could skip the architecture check would produce a
container Docker pulls happily and fails at exec, deep inside a play, with nothing naming why."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
	"""One box, already measured against ONE deployment's shape — all a policy needs to read."""

	inference_server: str
	region: str = ""
	# GPU docnames, not CUDA indices: a MIG slice has no index, and a scan may renumber them.
	fitting_gpus: tuple = ()
	surplus: int = 0
	has_local_weights: bool = False
	active_replicas: int = 0
	replicas_in_region: int = 0
	rejection: str = ""

	@property
	def is_viable(self):
		return not self.rejection


# Fields, in tiebreak order: a box holding the weights (no download), the region this deployment is
# thinnest in, the least surplus (pack: partly-used boxes fill first) or the most (spread: the
# emptiest box changes every time it is picked), the box running the least.
# An unknown policy is a KeyError, not `balanced`: a silent default places replicas nobody asked for.
POLICIES = {
	"balanced": lambda c: (not c.has_local_weights, c.replicas_in_region, c.surplus, c.active_replicas),
	"pack": lambda c: (not c.has_local_weights, c.surplus, c.active_replicas),
	"spread": lambda c: (c.replicas_in_region, -c.surplus, not c.has_local_weights, c.active_replicas),
}


def fitting_gpus(free_gpus, gpu_type="", min_vram_gb=0):
	"""Which of a box's free cards meet a deployment's hard filters, as GPU docnames. `free_gpus`
	is rows carrying `name`, `gpu_type` and `vram_gb`.

	Returned in the order they were GIVEN: `cards_on` orders by CUDA index, which is what "take the
	first N" should mean, and sorting docnames would order by a hash instead.

	`gpu_type` is matched for EQUALITY, which only works because both sides now resolve to one
	`GPU Type` — it was a substring test back when nvidia-smi said `Tesla T4` and AWS said `T4`."""
	wanted = (gpu_type or "").strip()
	matches = []
	for gpu in free_gpus:
		if wanted and (gpu.get("gpu_type") or "") != wanted:
			continue
		if min_vram_gb and (gpu.get("vram_gb") or 0) < min_vram_gb:
			continue
		matches.append(gpu["name"])
	return tuple(matches)
