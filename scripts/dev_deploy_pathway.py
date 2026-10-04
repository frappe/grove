import argparse
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import frappe

BENCH = Path(__file__).resolve().parents[3]


def build(source, architecture, label):
	"""The release build off a working tree, versioned dev-<sha>[-<label>]-<UTC minute>."""
	sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=source, text=True).strip()
	version = "-".join(filter(None, ["dev", sha, label, datetime.now(UTC).strftime("%Y%m%d%H%M")]))
	binary = BENCH / "builds" / f"pathway-{version}"
	subprocess.run(
		["go", "build", "-trimpath", "-ldflags", f"-s -w -X main.version={version}", "-o", binary, "./cmd/pathway"],
		cwd=source,
		env={**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": architecture},
		check=True,
	)
	return binary


def connect(site):
	# The play runs inside this process, and Ansible exits on a non-blocking stdout.
	for descriptor in (0, 1, 2):
		os.set_blocking(descriptor, True)
	# frappe's logger writes relative to the working directory.
	os.chdir(BENCH / "sites")
	frappe.init(site=site, sites_path=".")
	frappe.connect()
	frappe.set_user("Administrator")


def main():
	"""Build pathway off a working tree and ship it to one Gateway Server in place of the pinned
	release. Run with the bench's python:

	env/bin/python apps/grove/scripts/dev_deploy_pathway.py gw1-ap-south-1 ~/pathway --label cut
	"""
	parser = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawTextHelpFormatter)
	parser.add_argument("gateway", help="Gateway Server name")
	parser.add_argument("source", type=Path, help="pathway working tree")
	parser.add_argument("--label", default="", help="what this build is; goes in its version")
	parser.add_argument("--site", default="grove.localhost")
	arguments = parser.parse_args()
	source = arguments.source.resolve()

	connect(arguments.site)
	gateway = frappe.get_doc("Gateway Server", arguments.gateway)
	architecture = frappe.db.get_value("Machine", gateway.machine, "cpu_architecture")
	if not architecture:
		raise SystemExit(f"Machine {gateway.machine} has no CPU Architecture to build for")

	binary = build(source, architecture, arguments.label)
	play, rc = gateway._deploy_agent(agent_binary=str(binary))
	frappe.db.commit()
	print(f"{binary.name} on {gateway.name}: Ansible Play {play}, rc {rc}")
	raise SystemExit(rc)


if __name__ == "__main__":
	main()
