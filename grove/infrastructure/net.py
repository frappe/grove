"""Which address one Grove box reaches another on.

A Network is one VPC, one subnet, one availability zone, so two boxes that share one have a private
route between them and nothing else does. That one rule decides which address a scrape targets and
which addresses a security group opens, so it lives here rather than inside either of them.
"""

from urllib.parse import urlparse


def reachable_ip(target, viewer_network):
	"""`target` is a plain dict carrying `network`, `private_ip` and `ip` — the shape the monitoring
	queries already select, with the public address aliased to `ip`. Public is the answer for a pod
	(no Machine and so no Network at all), for a colo or bare-metal box with no private address, and
	for a viewer that is itself in no Network."""
	if viewer_network and target.get("network") == viewer_network and target.get("private_ip"):
		return target["private_ip"]
	return target.get("ip")


def private_url(engine_url, private_ip):
	"""Deliberately not reachable_ip, which falls back to public: an ingress is inside the VPC by
	construction, so the private address is the only one it has, and a replica without one is left
	out of its table rather than dialled over the internet. Only the host moves: the scheme comes
	from the URL the control plane already built, and a port, if the URL carries one, is the engine's
	and stays."""
	if not (engine_url and private_ip):
		return ""
	parsed = urlparse(engine_url)
	if not parsed.hostname:
		return ""
	host = f"{private_ip}:{parsed.port}" if parsed.port else private_ip
	return parsed._replace(netloc=host).geturl()
