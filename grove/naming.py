# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.naming import getseries

from grove.utils import slugify


class GeneratedName:
	"""Mixin: name a doc `get_generated_name()` unless the caller chose one.

	Frappe blanks `doc.name` before `autoname()`, which is what stops a client inventing its own —
	so a name the caller DID mean is caught in `before_naming` and put back. Without that,
	`get_doc({..., "name": "x"}).insert()` silently gets a generated one."""

	def before_naming(self):
		self._chosen_name = self.name

	def autoname(self):
		self.name = self._chosen_name or self.get_generated_name()


def next_machine_name(prefix, region, zone="", counter=None):
	"""The next `<prefix><n>-<region>[.<zone>]`. `counter` is injectable because the real one is a
	row no test rollback undoes. The series never reuses a number, even when the doc that took it
	is deleted: a retired name may still have a DNS record or a log history behind it."""
	counter = counter or getseries
	region = slugify(region)
	number = counter(f"{prefix}-{region}-" if region else f"{prefix}-", 1)
	name = f"{prefix}{number}-{region}" if region else f"{prefix}{number}"
	return f"{name}.{zone}" if zone else name


def short_name(name):
	"""`gw1-ap-south-1.<zone>` → `gw1-ap-south-1`: the one label a box is known by inside its zone."""
	return (name or "").partition(".")[0]


# One counter behind deployment names and replica numbers, floored by the rename patch above the
# legacy `MD-{#####}` names: two doctypes are two tables, so a clash never fails an insert.
MD_SERIES = "MD-"


def abbreviate_server(server):
	"""`inf3-ap-south-1` → `inf3`. The replica name carries the region already."""
	return slugify(server).split("-")[0]


def next_replica_name(model, server, region, counter=None):
	"""`<model id>-<region>-<server>-<n>`, e.g. `qwen3-8b-ap-south-1-inf3-00007`. The region goes in
	as the Region doc names it — every provider codes its regions differently, and a shortening
	rule fitted to one of them mangles the rest. The number is what keeps the name unique — two
	replicas of one model on one box are a legitimate thing to have — and comes from `tabSeries`:
	two inserts racing cannot land on it twice."""
	counter = counter or getseries
	parts = [slugify((model or "").split("/")[-1]), slugify(region), abbreviate_server(server)]
	return "-".join([part for part in parts if part] + [counter(MD_SERIES, 5)])


def next_deployment_name(counter=None):
	"""`MD-{#####}`, e.g. `MD-00014`. Numbered out of `tabSeries` rather than by
	`append_number_if_name_exists`: that helper reads the taken names and then picks the next,
	which two inserts racing can both do before either writes. Neither the model nor the shape is
	in the name: `gpus_per_replica` is editable and a name is not, so `4xh100` would go stale."""
	return f"{MD_SERIES}{(counter or getseries)(MD_SERIES, 5)}"
